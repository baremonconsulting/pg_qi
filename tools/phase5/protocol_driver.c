#define _POSIX_C_SOURCE 200809L

#include <libpq-fe.h>
#include <openssl/sha.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/wait.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif

typedef struct {
    uint64_t rows, bytes, spool_bytes, messages;
    uint64_t max_rows, max_bytes, max_spool, max_messages;
    size_t max_columns, max_field;
    int cancel_attempted, cancel_succeeded, cancel_timed_out;
    uint64_t request_started_ns, request_finished_ns, request_duration_ns;
    int request_interval_state;
} Limits;

static int64_t now_ms(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return -1;
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static uint64_t now_ns(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return 0;
    return (uint64_t)t.tv_sec * 1000000000ULL + (uint64_t)t.tv_nsec;
}

static const char *interval_state(const Limits *l) {
    switch (l->request_interval_state) {
        case 1: return "COMPLETE";
        case 2: return "ERROR";
        case 3: return "CENSORED_TIMEOUT";
        default: return "NOT_RUN";
    }
}

static void interval_start(Limits *l) {
    l->request_started_ns = now_ns();
}

static void interval_finish(Limits *l, int state) {
    uint64_t finished = now_ns();
    if (!l->request_started_ns || !finished || finished < l->request_started_ns) {
        l->request_started_ns = 0;
        l->request_finished_ns = 0;
        l->request_duration_ns = 0;
        l->request_interval_state = 0;
        return;
    }
    l->request_finished_ns = finished;
    l->request_duration_ns = finished - l->request_started_ns;
    l->request_interval_state = state;
}

static void terminal(const char *state, const char *reason, const Limits *l,
                     const char *sqlstate) {
    printf("{\"terminal_state\":\"%s\",\"reason\":\"%s\","
           "\"sqlstate\":\"%s\",\"rows\":%llu,\"bytes\":%llu,"
           "\"spool_bytes\":%llu,\"messages\":%llu,"
           "\"cancel_attempted\":%s,\"cancel_succeeded\":%s,"
           "\"cancel_timed_out\":%s,"
           "\"request_started_monotonic_ns\":%llu,"
           "\"request_finished_monotonic_ns\":%llu,"
           "\"request_duration_ns\":%llu,\"request_interval_state\":\"%s\","
           "\"request_interval_censored\":%s}\n",
           state, reason, sqlstate ? sqlstate : "",
           (unsigned long long)l->rows, (unsigned long long)l->bytes,
           (unsigned long long)l->spool_bytes,
           (unsigned long long)l->messages,
           l->cancel_attempted ? "true" : "false",
           l->cancel_succeeded ? "true" : "false",
           l->cancel_timed_out ? "true" : "false",
           (unsigned long long)l->request_started_ns,
           (unsigned long long)l->request_finished_ns,
           (unsigned long long)l->request_duration_ns,
           interval_state(l), l->request_interval_state == 3 ? "true" : "false");
    fflush(stdout);
}

static int parse_u64(const char *s, uint64_t maximum, uint64_t *out) {
    char *end = NULL;
    unsigned long long v;
    const unsigned char *p = (const unsigned char *)s;
    if (!*p) return 0;
    while (*p) {
        if (*p < '0' || *p > '9') return 0;
        p++;
    }
    errno = 0; v = strtoull(s, &end, 10);
    if (errno || *end || v == 0 || v > maximum) return 0;
    *out = (uint64_t)v; return 1;
}

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb"); long n; char *p;
    if (!f || fseek(f, 0, SEEK_END) || (n = ftell(f)) < 0 ||
        fseek(f, 0, SEEK_SET)) { if (f) fclose(f); return NULL; }
    p = malloc((size_t)n + 1);
    if (!p || fread(p, 1, (size_t)n, f) != (size_t)n) {
        free(p); fclose(f); return NULL;
    }
    if (memchr(p, '\0', (size_t)n) != NULL) {
        free(p); fclose(f); return NULL;
    }
    p[n] = 0; fclose(f); return p;
}

static FILE *open_exclusive(const char *path) {
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    FILE *f;
    if (fd < 0) return NULL;
    f = fdopen(fd, "wb");
    if (!f) { close(fd); return NULL; }
    return f;
}

static int wait_socket(PGconn *c, short events, int64_t deadline) {
    struct pollfd pfd; int64_t left; int rc;
    pfd.fd = PQsocket(c); pfd.events = events; pfd.revents = 0;
    if (pfd.fd < 0 || (left = deadline - now_ms()) <= 0) return 0;
    rc = poll(&pfd, 1, left > 1000 ? 1000 : (int)left);
    return rc > 0 ? 1 : rc == 0 ? -1 : (errno == EINTR ? -1 : 0);
}

static PGconn *connect_bounded(const char *conninfo, int64_t deadline) {
    PGconn *c = PQconnectStart(conninfo);
    if (!c || PQstatus(c) == CONNECTION_BAD || PQsetnonblocking(c, 1) != 0) {
        if (c) PQfinish(c);
        return NULL;
    }
    for (;;) {
        PostgresPollingStatusType s = PQconnectPoll(c); int w;
        if (s == PGRES_POLLING_OK) return c;
        if (s == PGRES_POLLING_FAILED) { PQfinish(c); return NULL; }
        w = wait_socket(c, s == PGRES_POLLING_WRITING ? POLLOUT : POLLIN,
                        deadline);
        if (w == 0 || now_ms() >= deadline) { PQfinish(c); return NULL; }
    }
}

static int flush_bounded(PGconn *c, int64_t deadline) {
    for (;;) {
        int rc = PQflush(c);
        if (rc == 0) return 1;
        if (rc < 0 || wait_socket(c, POLLOUT, deadline) == 0 ||
            now_ms() >= deadline) return 0;
    }
}

static int write_u32(FILE *f, uint32_t v) {
    unsigned char b[4] = {(unsigned char)(v >> 24), (unsigned char)(v >> 16),
                          (unsigned char)(v >> 8), (unsigned char)v};
    return fwrite(b, 1, 4, f) == 4;
}

static int write_u64(FILE *f, uint64_t v) {
    unsigned char b[8] = {(unsigned char)(v >> 56), (unsigned char)(v >> 48),
                          (unsigned char)(v >> 40), (unsigned char)(v >> 32),
                          (unsigned char)(v >> 24), (unsigned char)(v >> 16),
                          (unsigned char)(v >> 8), (unsigned char)v};
    return fwrite(b, 1, sizeof(b), f) == sizeof(b);
}

static unsigned char *result_metadata(PGresult *r, size_t maximum_columns,
                                      int spool_version, size_t *length,
                                      const char **reason) {
    static const unsigned char magic_v1[] = "PGQSP001";
    static const unsigned char magic_v2[] = "PGQSP002";
    const unsigned char *magic = spool_version == 2 ? magic_v2 : magic_v1;
    size_t magic_length = sizeof(magic_v1) - 1;
    int fields = PQnfields(r), column;
    size_t total = magic_length + 4;
    unsigned char *payload, *cursor;
    if (fields < 0 || (size_t)fields > maximum_columns) {
        *reason = "max_columns"; return NULL;
    }
    for (column = 0; column < fields; column++) {
        const char *name = PQfname(r, column);
        size_t name_length;
        if (!name || !(name_length = strlen(name)) || name_length > 1024 ||
                name_length > UINT32_MAX ||
                total > SIZE_MAX - (spool_version == 2 ? 16 : 8) - name_length) {
            *reason = "result_metadata"; return NULL;
        }
        total += (spool_version == 2 ? 16 : 8) + name_length;
    }
    payload = malloc(total);
    if (!payload) { *reason = "result_metadata"; return NULL; }
    cursor = payload;
    memcpy(cursor, magic, magic_length); cursor += magic_length;
#define PUT_U32(P, V) do { uint32_t put_value = (uint32_t)(V); \
    *(P)++ = (unsigned char)(put_value >> 24); \
    *(P)++ = (unsigned char)(put_value >> 16); \
    *(P)++ = (unsigned char)(put_value >> 8); \
    *(P)++ = (unsigned char)put_value; } while (0)
    PUT_U32(cursor, fields);
    for (column = 0; column < fields; column++) {
        const char *name = PQfname(r, column);
        size_t name_length = strlen(name);
        PUT_U32(cursor, name_length);
        memcpy(cursor, name, name_length); cursor += name_length;
        PUT_U32(cursor, PQftype(r, column));
        if (spool_version == 2) {
            PUT_U32(cursor, (uint32_t)(int32_t)PQfmod(r, column));
            PUT_U32(cursor, (uint32_t)PQfformat(r, column));
        }
    }
#undef PUT_U32
    *length = total;
    return payload;
}

static int write_v2_trailer(FILE *spool, const unsigned char *metadata,
                            size_t metadata_length, const Limits *l,
                            unsigned int completions, int server_error,
                            const char sqlstate[6], const char **reason) {
    static const uint32_t marker = UINT32_MAX - 1U;
    unsigned char digest[SHA256_DIGEST_LENGTH];
    unsigned char state[8] = {0};
    const uint64_t trailer_bytes = 4U + 4U + sizeof(state) + 8U + 4U +
                                   SHA256_DIGEST_LENGTH;
    if (!metadata || (server_error && (!sqlstate || strlen(sqlstate) != 5))) {
        *reason = "v2_terminal_evidence"; return 0;
    }
    if (trailer_bytes > l->max_spool ||
            l->spool_bytes > l->max_spool - trailer_bytes) {
        *reason = "max_spool_bytes"; return 0;
    }
    if (!SHA256(metadata, metadata_length, digest)) {
        *reason = "descriptor_digest"; return 0;
    }
    if (server_error) memcpy(state, sqlstate, 5);
    if (!write_u32(spool, marker) ||
        !write_u32(spool, server_error ? 1U : 0U) ||
        fwrite(state, 1, sizeof(state), spool) != sizeof(state) ||
        !write_u64(spool, l->rows) ||
        !write_u32(spool, completions) ||
        fwrite(digest, 1, sizeof(digest), spool) != sizeof(digest)) {
        *reason = "spool_write"; return 0;
    }
    return 1;
}

static int spool_row(FILE *spool, PGresult *r, Limits *l, const char **reason) {
    int j, nf = PQnfields(r); uint64_t add = 4, row_payload = 0;
    if ((size_t)nf > l->max_columns) { *reason = "max_columns"; return 0; }
    for (j = 0; j < nf; j++) {
        size_t n = PQgetisnull(r, 0, j) ? 0 : (size_t)PQgetlength(r, 0, j);
        if (n > l->max_field) { *reason = "max_field_bytes"; return 0; }
        if ((uint64_t)n > UINT64_MAX - row_payload) { *reason = "max_result_bytes"; return 0; }
        row_payload += (uint64_t)n;
        if ((uint64_t)n > UINT64_MAX - 4 - add) { *reason = "max_spool_bytes"; return 0; }
        add += 4 + (uint64_t)n;
    }
    if (row_payload > l->max_bytes || l->bytes > l->max_bytes - row_payload) {
        *reason = "max_result_bytes"; return 0;
    }
    if (l->rows >= l->max_rows) { *reason = "max_rows"; return 0; }
    if (add > l->max_spool || l->spool_bytes > l->max_spool - add) {
        *reason = "max_spool_bytes"; return 0;
    }
    if (!write_u32(spool, (uint32_t)nf)) { *reason = "spool_write"; return 0; }
    for (j = 0; j < nf; j++) {
        if (PQgetisnull(r, 0, j)) {
            if (!write_u32(spool, UINT32_MAX)) { *reason = "spool_write"; return 0; }
        } else {
            uint32_t n = (uint32_t)PQgetlength(r, 0, j);
            if (!write_u32(spool, n) || fwrite(PQgetvalue(r, 0, j), 1, n, spool) != n) {
                *reason = "spool_write"; return 0;
            }
        }
    }
    l->bytes += row_payload;
    l->rows++; l->spool_bytes += add; return 1;
}

static int send_subject(PGconn *c, const char *sql, const char *protocol,
                        const char *prepared, int64_t deadline) {
    PGresult *r;
    if (prepared) {
        if (!PQsendPrepare(c, prepared, sql, 0, NULL)) return 0;
        if (!flush_bounded(c, deadline)) return 0;
        while (PQisBusy(c)) {
            if (now_ms() >= deadline || wait_socket(c, POLLIN, deadline) == 0 ||
                !PQconsumeInput(c)) return 0;
        }
        r = PQgetResult(c);
        if (!r || PQresultStatus(r) != PGRES_COMMAND_OK) { if (r) PQclear(r); return 0; }
        PQclear(r); while ((r = PQgetResult(c)) != NULL) PQclear(r);
        return PQsendQueryPrepared(c, prepared, 0, NULL, NULL, NULL, 0);
    }
    if (!strcmp(protocol, "extended"))
        return PQsendQueryParams(c, sql, 0, NULL, NULL, NULL, NULL, 0);
    if (!strcmp(protocol, "simple")) return PQsendQuery(c, sql);
    return 0;
}

static int stream(PGconn *c, FILE *spool, Limits *l, int64_t deadline,
                  int spool_version, const char **reason, char sqlstate[6]) {
    PGresult *r = NULL; unsigned int completions = 0;
    unsigned char *metadata = NULL; size_t metadata_length = 0;
    int success = 0, draining_error = 0, controlled_error = 0;
    if (!PQsetSingleRowMode(c)) { *reason = "single_row_mode"; goto done; }
    for (;;) {
        if (now_ms() >= deadline) { *reason = "query_deadline"; goto done; }
        while (PQisBusy(c)) {
            int w = wait_socket(c, POLLIN, deadline);
            if (w == 0 || now_ms() >= deadline) { *reason = "query_deadline"; goto done; }
            if (w > 0 && !PQconsumeInput(c)) { *reason = "connection_error"; goto done; }
        }
        r = PQgetResult(c);
        if (!r) break;
        if (++l->messages > l->max_messages) { *reason = "max_messages"; goto done; }
        if (draining_error) { PQclear(r); r = NULL; continue; }
        if (PQresultStatus(r) == PGRES_SINGLE_TUPLE ||
            PQresultStatus(r) == PGRES_TUPLES_OK) {
            size_t current_length = 0;
            unsigned char *current = result_metadata(r, l->max_columns, spool_version,
                                                     &current_length, reason);
            if (!current) goto done;
            if (!metadata) {
                if (current_length > l->max_spool ||
                        l->spool_bytes > l->max_spool - current_length) {
                    free(current); *reason = "max_spool_bytes"; goto done;
                }
                if (fwrite(current, 1, current_length, spool) != current_length) {
                    free(current); *reason = "spool_write"; goto done;
                }
                metadata = current; metadata_length = current_length;
                l->spool_bytes += current_length;
            } else {
                int matches = current_length == metadata_length &&
                              !memcmp(current, metadata, current_length);
                free(current);
                if (!matches) { *reason = "result_metadata_changed"; goto done; }
            }
        }
        if (PQresultStatus(r) == PGRES_SINGLE_TUPLE) {
            if (!spool_row(spool, r, l, reason)) goto done;
        } else if (PQresultStatus(r) == PGRES_TUPLES_OK) {
            if (PQntuples(r) != 0) { *reason = "not_single_row"; goto done; }
            if (++completions > 1) { *reason = "multiple_result_sets"; goto done; }
        } else if (PQresultStatus(r) == PGRES_FATAL_ERROR) {
            const char *state = PQresultErrorField(r, PG_DIAG_SQLSTATE);
            if (state) {
                memcpy(sqlstate, state, 5);
                sqlstate[5] = 0;
            }
            *reason = l->rows ? "server_error_after_partial_stream" : "server_error";
            if (spool_version == 2 && !metadata) {
                metadata = result_metadata(r, l->max_columns, spool_version,
                                           &metadata_length, reason);
                if (!metadata || metadata_length > l->max_spool ||
                        l->spool_bytes > l->max_spool - metadata_length ||
                        fwrite(metadata, 1, metadata_length, spool) != metadata_length) {
                    if (metadata && !strcmp(*reason, "server_error"))
                        *reason = "spool_write";
                    goto done;
                }
                l->spool_bytes += metadata_length;
            }
            controlled_error = 1; draining_error = 1;
        } else {
            *reason = "unexpected_result_status"; goto done;
        }
        PQclear(r); r = NULL;
    }
    if (controlled_error) {
        if (spool_version == 2 &&
                write_v2_trailer(spool, metadata, metadata_length, l, completions,
                                 1, sqlstate, reason))
            l->spool_bytes += 60U;
        goto done;
    }
    if (completions != 1 || !metadata) { *reason = "missing_result_completion"; goto done; }
    if (spool_version == 2) {
        if (!write_v2_trailer(spool, metadata, metadata_length, l, completions,
                              0, sqlstate, reason))
            goto done;
        l->spool_bytes += 60U;
    }
    success = 1;
done:
    if (r) PQclear(r);
    free(metadata);
    return success;
}

static int wait_result_ready(PGconn *c, int64_t deadline) {
    while (PQisBusy(c)) {
        int ready = wait_socket(c, POLLIN, deadline);
        if (ready == 0 || now_ms() >= deadline) return 0;
        if (ready > 0 && !PQconsumeInput(c)) return 0;
    }
    return 1;
}

static char *show_scalar(PGconn *c, const char *name, int64_t deadline) {
    char sql[128]; PGresult *r; char *copy = NULL;
    if (snprintf(sql, sizeof(sql), "SHOW %s", name) >= (int)sizeof(sql)) return NULL;
    if (!PQsendQueryParams(c, sql, 0, NULL, NULL, NULL, NULL, 0) ||
        !flush_bounded(c, deadline) || !wait_result_ready(c, deadline)) return NULL;
    r = PQgetResult(c);
    if (!r || PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1 ||
        PQnfields(r) != 1 || PQgetisnull(r, 0, 0)) {
        if (r) PQclear(r);
        return NULL;
    }
    copy = strdup(PQgetvalue(r, 0, 0));
    PQclear(r);
    if (!wait_result_ready(c, deadline)) { free(copy); return NULL; }
    if ((r = PQgetResult(c)) != NULL) { PQclear(r); free(copy); return NULL; }
    return copy;
}

static char *query_scalar(PGconn *c, const char *sql, int64_t deadline) {
    PGresult *r; char *copy = NULL;
    if (!PQsendQueryParams(c, sql, 0, NULL, NULL, NULL, NULL, 0) ||
        !flush_bounded(c, deadline)) return NULL;
    if (!wait_result_ready(c, deadline)) return NULL;
    r = PQgetResult(c);
    if (!r || PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1 ||
        PQnfields(r) != 1 || PQgetisnull(r, 0, 0)) {
        if (r) PQclear(r);
        return NULL;
    }
    copy = strdup(PQgetvalue(r, 0, 0)); PQclear(r);
    if (!wait_result_ready(c, deadline)) { free(copy); return NULL; }
    if ((r = PQgetResult(c)) != NULL) { PQclear(r); free(copy); return NULL; }
    return copy;
}

static int commands_bounded(PGconn *c, const char *sql, int64_t deadline) {
    PGresult *r; int count = 0;
    if (!PQsendQuery(c, sql) || !flush_bounded(c, deadline)) return 0;
    for (;;) {
        if (!wait_result_ready(c, deadline)) return 0;
        r = PQgetResult(c);
        if (!r) break;
        int ok = PQresultStatus(r) == PGRES_COMMAND_OK;
        PQclear(r); count++;
        if (!ok) return 0;
    }
    return count > 0;
}

static int configure_session(PGconn *c, const char *mode, int64_t deadline) {
    const char *sql;
    const char *preload_names[] = {"shared_preload_libraries",
                                   "session_preload_libraries",
                                   "local_preload_libraries"};
    size_t preload_index;
    if (!strcmp(mode, "native_baseline")) {
        for (preload_index = 0; preload_index < sizeof(preload_names) / sizeof(preload_names[0]);
             preload_index++) {
            char *value = show_scalar(c, preload_names[preload_index], deadline);
            if (!value || *value) { free(value); return 0; }
            free(value);
        }
        {
            char *count = query_scalar(c,
                "SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql'", deadline);
            int ok = count && !strcmp(count, "0"); free(count); return ok;
        }
    } else if (!strcmp(mode, "lab_allowlist"))
        sql = "SET pg_qi.enabled=on; SET pg_qi.routing_mode=allowlist; SET duckdb.force_execution=off";
    else if (!strcmp(mode, "off"))
        sql = "SET pg_qi.enabled=on; SET pg_qi.routing_mode=off; SET duckdb.force_execution=off";
    else if (!strcmp(mode, "disabled"))
        sql = "SET pg_qi.enabled=off; SET pg_qi.routing_mode=off; SET duckdb.force_execution=off";
    else return 0;
    return commands_bounded(c, sql, deadline);
}

static int write_native_baseline_snapshot(const char *path) {
    FILE *out = open_exclusive(path);
    int ok = 1;
    if (!out) return 0;
    if (fputs("name\tvalue\n"
              "provider_identity\tPOSTGRESQL_NATIVE_NO_DELEGATING_PROVIDER\n"
              "provider_absence_verified\ttrue\n", out) == EOF)
        ok = 0;
    if (ok && fflush(out)) ok = 0;
    if (ok && fsync(fileno(out))) ok = 0;
    if (fclose(out)) ok = 0;
    return ok;
}

static int write_snapshot(PGconn *c, const char *path, const char *before,
                          int64_t deadline) {
    static const char *names[] = {
        "pg_qi.shadow_contract_version", "pg_qi.shadow_decision_id",
        "pg_qi.shadow_context", "pg_qi.shadow_policy_intent",
        "pg_qi.shadow_reason", "pg_qi.shadow_force_origin",
        "pg_qi.shadow_classifier_status", "pg_qi.shadow_classifier_reason",
        "pg_qi.shadow_observed_engine", "pg_qi.shadow_verification",
        "pg_qi.shadow_observation", "pg_qi.shadow_request_applied",
        "duckdb.force_execution"
    };
    FILE *out = open_exclusive(path); size_t i;
    if (!out) return 0;
    if (fputs("name\tvalue\n", out) == EOF ||
        fprintf(out, "pg_qi.shadow_decision_id_before\t%s\n", before) < 0) {
        fclose(out); return 0;
    }
    for (i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        char *value = show_scalar(c, names[i], deadline);
        if (!value) { fclose(out); return 0; }
        if (strchr(value, '\t') || strchr(value, '\r') || strchr(value, '\n') ||
            fprintf(out, "%s\t%s\n", names[i], value) < 0) {
            free(value); fclose(out); return 0;
        }
        free(value);
    }
    if (fflush(out) || fsync(fileno(out)) || fclose(out)) return 0;
    return 1;
}

static void cancel_owned_query(PGconn *c, Limits *l, uint64_t deadline_ms) {
    PGcancel *cancel; int result_pipe[2] = {-1, -1}; pid_t child; unsigned char result = 0;
    pid_t parent = getpid();
    struct pollfd wait_fd; int wait_result; int64_t deadline;
    l->cancel_attempted = 1;
    cancel = PQgetCancel(c);
    if (!cancel) return;
    if (pipe(result_pipe)) { PQfreeCancel(cancel); return; }
    child = fork();
    if (child == 0) {
        char error[256]; ssize_t written;
#ifdef __linux__
        if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() != parent) _exit(1);
#endif
        alarm((unsigned int)((deadline_ms + 999U) / 1000U));
        close(result_pipe[0]);
#ifdef PGQI5_FAULTPOINTS
        if (getenv("PGQI5_CANCEL_BLACKHOLE") != NULL) {
            for (;;) pause();
        }
#endif
        result = PQcancel(cancel, error, sizeof(error)) ? 1 : 0;
        written = write(result_pipe[1], &result, 1);
        (void)written;
        close(result_pipe[1]); PQfreeCancel(cancel); _exit(0);
    }
    close(result_pipe[1]);
    if (child < 0) { close(result_pipe[0]); PQfreeCancel(cancel); return; }
    wait_fd.fd = result_pipe[0]; wait_fd.events = POLLIN; wait_fd.revents = 0;
    deadline = now_ms() + (int64_t)deadline_ms;
    do {
        int64_t remaining = deadline - now_ms();
        if (remaining <= 0) { wait_result = 0; break; }
        wait_result = poll(&wait_fd, 1, remaining > INT_MAX ? INT_MAX : (int)remaining);
    } while (wait_result < 0 && errno == EINTR);
    if (wait_result > 0 && (wait_fd.revents & (POLLIN | POLLHUP)) &&
            read(result_pipe[0], &result, 1) == 1) {
        l->cancel_succeeded = result == 1;
    } else {
        l->cancel_timed_out = 1;
        (void)kill(child, SIGKILL);
    }
    close(result_pipe[0]);
    while (waitpid(child, NULL, 0) < 0 && errno == EINTR) { }
    PQfreeCancel(cancel);
}

static void usage(const char *p) {
    fprintf(stderr, "usage: %s --conninfo STR --sql-file PATH --spool PATH "
            "--protocol extended|simple --deadline-ms N --cancel-deadline-ms N --max-rows N "
            "--max-bytes N --max-columns N --max-field-bytes N "
            "--max-spool-bytes N --max-messages N --snapshot PATH "
            "--session-mode disabled|off|lab_allowlist|native_baseline "
            "[--prepared NAME] [--spool-version 1|2]\n", p);
}

int main(int argc, char **argv) {
    const char *conninfo = NULL, *sql_path = NULL, *spool_path = NULL;
    const char *protocol = NULL, *prepared = NULL, *snapshot = NULL;
    const char *session_mode = NULL;
    int spool_version = 1;
    const char *reason = "invalid_arguments";
    char sqlstate[6] = ""; uint64_t deadline_ms = 0, cancel_deadline_ms = 0, v; int i, ok = 0;
    Limits l = {0}; char *sql = NULL, *before = NULL; FILE *spool = NULL;
    PGconn *c = NULL; int64_t query_deadline;
    for (i = 1; i < argc; i++) {
        const char *option = argv[i]; const char *value;
        if (++i >= argc) { usage(argv[0]); return 64; }
        value = argv[i];
        if (!strcmp(option, "--conninfo")) conninfo = value;
        else if (!strcmp(option, "--sql-file")) sql_path = value;
        else if (!strcmp(option, "--spool")) spool_path = value;
        else if (!strcmp(option, "--protocol")) protocol = value;
        else if (!strcmp(option, "--prepared")) prepared = value;
        else if (!strcmp(option, "--snapshot")) snapshot = value;
        else if (!strcmp(option, "--session-mode")) session_mode = value;
        else if (!strcmp(option, "--spool-version") &&
                 (!strcmp(value, "1") || !strcmp(value, "2")))
            spool_version = value[0] - '0';
        else if (!strcmp(option, "--deadline-ms") &&
                 parse_u64(value, 3600000, &v)) deadline_ms = v;
        else if (!strcmp(option, "--cancel-deadline-ms") &&
                 parse_u64(value, 60000, &v)) cancel_deadline_ms = v;
        else if (!strcmp(option, "--max-rows") &&
                 parse_u64(value, UINT32_MAX, &v)) l.max_rows = v;
        else if (!strcmp(option, "--max-bytes") &&
                 parse_u64(value, INT64_MAX, &v)) l.max_bytes = v;
        else if (!strcmp(option, "--max-columns") &&
                 parse_u64(value, UINT32_MAX, &v)) l.max_columns = (size_t)v;
        else if (!strcmp(option, "--max-field-bytes") &&
                 parse_u64(value, UINT32_MAX, &v)) l.max_field = (size_t)v;
        else if (!strcmp(option, "--max-spool-bytes") &&
                 parse_u64(value, INT64_MAX, &v)) l.max_spool = v;
        else if (!strcmp(option, "--max-messages") &&
                 parse_u64(value, UINT32_MAX, &v)) l.max_messages = v;
        else { usage(argv[0]); return 64; }
    }
    if (!conninfo || !sql_path || !spool_path || !snapshot || !protocol ||
        !session_mode || !deadline_ms || !cancel_deadline_ms ||
        !l.max_rows || !l.max_bytes || !l.max_columns || !l.max_field ||
        !l.max_spool || !l.max_messages ||
        (strcmp(protocol, "extended") && strcmp(protocol, "simple"))) {
        usage(argv[0]); return 64;
    }
    if (prepared && strcmp(protocol, "extended")) { terminal("REFUSED", "prepared_requires_extended", &l, NULL); return 65; }
    sql = read_file(sql_path);
    if (!sql || !sql[0]) { terminal("REFUSED", "sql_file", &l, NULL); goto done; }
    spool = open_exclusive(spool_path);
    if (!spool) { terminal("REFUSED", "spool_open", &l, NULL); goto done; }
    c = connect_bounded(conninfo, now_ms() + (int64_t)deadline_ms);
    if (!c) { terminal("FAILED", "connect_deadline_or_error", &l, NULL); goto done; }
    if (!configure_session(c, session_mode, now_ms() + (int64_t)deadline_ms) ||
        (!(before = !strcmp(session_mode, "native_baseline") ? strdup("0") :
          show_scalar(c, "pg_qi.shadow_decision_id",
                      now_ms() + (int64_t)deadline_ms)))) {
        terminal("FAILED", "session_setup", &l, NULL); goto done;
    }
    query_deadline = now_ms() + (int64_t)deadline_ms;
    interval_start(&l);
    if (!send_subject(c, sql, protocol, prepared, query_deadline) ||
        !flush_bounded(c, query_deadline)) {
        const char *send_reason = now_ms() >= query_deadline ? "query_deadline" : "send_error";
        interval_finish(&l, !strcmp(send_reason, "query_deadline") ? 3 : 2);
        if (!strcmp(send_reason, "query_deadline")) cancel_owned_query(c, &l, cancel_deadline_ms);
        terminal("FAILED", send_reason, &l, NULL); goto done;
    }
    if (!stream(c, spool, &l, query_deadline, spool_version, &reason, sqlstate)) {
        interval_finish(&l, !strcmp(reason, "query_deadline") ? 3 : 2);
        if (!strncmp(reason, "max_", 4) || !strcmp(reason, "query_deadline"))
            cancel_owned_query(c, &l, cancel_deadline_ms);
        terminal(!strncmp(reason, "max_", 4) ? "LIMIT_EXCEEDED" : "FAILED",
                 reason, &l, sqlstate[0] ? sqlstate : NULL); goto done;
    }
    interval_finish(&l, 1);
    if (!(strcmp(session_mode, "native_baseline") ?
          write_snapshot(c, snapshot, before, now_ms() + (int64_t)deadline_ms) :
          write_native_baseline_snapshot(snapshot))) {
        terminal("FAILED", "snapshot_write", &l, NULL); goto done;
    }
    if (fflush(spool) || fsync(fileno(spool))) {
        terminal("FAILED", "spool_sync", &l, NULL); goto done;
    }
    terminal("COMPLETE", "ok", &l, NULL); ok = 1;
done:
    if (c) PQfinish(c);
    if (spool) fclose(spool);
    free(before);
    free(sql);
    return ok ? 0 : 1;
}
