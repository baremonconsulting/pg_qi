#define _POSIX_C_SOURCE 200809L

#include <libpq-fe.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    uint64_t rows, bytes, spool_bytes, messages;
    uint64_t max_rows, max_bytes, max_spool, max_messages;
    size_t max_columns, max_field;
    int cancel_attempted, cancel_succeeded;
} Limits;

static int64_t now_ms(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return -1;
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static void terminal(const char *state, const char *reason, const Limits *l,
                     const char *sqlstate) {
    printf("{\"terminal_state\":\"%s\",\"reason\":\"%s\","
           "\"sqlstate\":\"%s\",\"rows\":%llu,\"bytes\":%llu,"
           "\"spool_bytes\":%llu,\"messages\":%llu,"
           "\"cancel_attempted\":%s,\"cancel_succeeded\":%s}\n",
           state, reason, sqlstate ? sqlstate : "",
           (unsigned long long)l->rows, (unsigned long long)l->bytes,
           (unsigned long long)l->spool_bytes,
           (unsigned long long)l->messages,
           l->cancel_attempted ? "true" : "false",
           l->cancel_succeeded ? "true" : "false");
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

static unsigned char *result_metadata(PGresult *r, size_t maximum_columns,
                                      size_t *length, const char **reason) {
    static const unsigned char magic[] = "PGQSP001";
    int fields = PQnfields(r), column;
    size_t total = sizeof(magic) - 1 + 4;
    unsigned char *payload, *cursor;
    if (fields < 0 || (size_t)fields > maximum_columns) {
        *reason = "max_columns"; return NULL;
    }
    for (column = 0; column < fields; column++) {
        const char *name = PQfname(r, column);
        size_t name_length;
        if (!name || !(name_length = strlen(name)) || name_length > 1024 ||
                name_length > UINT32_MAX || total > SIZE_MAX - 8 - name_length) {
            *reason = "result_metadata"; return NULL;
        }
        total += 8 + name_length;
    }
    payload = malloc(total);
    if (!payload) { *reason = "result_metadata"; return NULL; }
    cursor = payload;
    memcpy(cursor, magic, sizeof(magic) - 1); cursor += sizeof(magic) - 1;
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
    }
#undef PUT_U32
    *length = total;
    return payload;
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
                  const char **reason, char sqlstate[6]) {
    PGresult *r = NULL; unsigned int completions = 0;
    unsigned char *metadata = NULL; size_t metadata_length = 0;
    int success = 0;
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
        if (PQresultStatus(r) == PGRES_SINGLE_TUPLE ||
            PQresultStatus(r) == PGRES_TUPLES_OK) {
            size_t current_length = 0;
            unsigned char *current = result_metadata(r, l->max_columns,
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
            goto done;
        } else {
            *reason = "unexpected_result_status"; goto done;
        }
        PQclear(r); r = NULL;
    }
    if (completions != 1 || !metadata) { *reason = "missing_result_completion"; goto done; }
    success = 1;
done:
    if (r) PQclear(r);
    free(metadata);
    return success;
}

static char *show_scalar(PGconn *c, const char *name, int64_t deadline) {
    char sql[128]; PGresult *r; char *copy = NULL;
    if (snprintf(sql, sizeof(sql), "SHOW %s", name) >= (int)sizeof(sql)) return NULL;
    if (!PQsendQueryParams(c, sql, 0, NULL, NULL, NULL, NULL, 0) ||
        !flush_bounded(c, deadline)) return NULL;
    while (PQisBusy(c)) {
        int ready = wait_socket(c, POLLIN, deadline);
        if (ready == 0 || now_ms() >= deadline) return NULL;
        if (ready > 0 && !PQconsumeInput(c)) return NULL;
    }
    r = PQgetResult(c);
    if (!r || PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1 ||
        PQnfields(r) != 1 || PQgetisnull(r, 0, 0)) {
        if (r) PQclear(r);
        return NULL;
    }
    copy = strdup(PQgetvalue(r, 0, 0));
    PQclear(r);
    if ((r = PQgetResult(c)) != NULL) { PQclear(r); free(copy); return NULL; }
    return copy;
}

static int configure_session(PGconn *c, const char *mode) {
    PGresult *r; const char *sql;
    const char *preload_names[] = {"shared_preload_libraries",
                                   "session_preload_libraries",
                                   "local_preload_libraries"};
    size_t preload_index;
    if (!strcmp(mode, "native_baseline")) {
        if (PQsetnonblocking(c, 0) != 0) return 0;
        for (preload_index = 0; preload_index < sizeof(preload_names) / sizeof(preload_names[0]);
             preload_index++) {
            char check[96];
            if (snprintf(check, sizeof(check), "SHOW %s", preload_names[preload_index]) >=
                    (int)sizeof(check)) return 0;
            r = PQexec(c, check);
            if (!r || PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1 ||
                    PQnfields(r) != 1 || PQgetisnull(r, 0, 0) || *PQgetvalue(r, 0, 0)) {
                if (r) PQclear(r);
                return 0;
            }
            PQclear(r);
        }
        r = PQexec(c, "SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql'");
        if (!r || PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1 ||
                PQnfields(r) != 1 || strcmp(PQgetvalue(r, 0, 0), "0")) {
            if (r) PQclear(r);
            return 0;
        }
        PQclear(r);
        return PQsetnonblocking(c, 1) == 0;
    } else if (!strcmp(mode, "lab_allowlist"))
        sql = "SET pg_qi.enabled=on; SET pg_qi.routing_mode=allowlist; SET duckdb.force_execution=off";
    else if (!strcmp(mode, "off"))
        sql = "SET pg_qi.routing_mode=off; SET duckdb.force_execution=off";
    else return 0;
    if (PQsetnonblocking(c, 0) != 0) return 0;
    r = PQexec(c, sql);
    if (!r || PQresultStatus(r) != PGRES_COMMAND_OK) { if (r) PQclear(r); return 0; }
    PQclear(r);
    return PQsetnonblocking(c, 1) == 0;
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

static void cancel_owned_query(PGconn *c, Limits *l) {
    char error[256]; PGcancel *cancel;
    l->cancel_attempted = 1;
    cancel = PQgetCancel(c);
    if (!cancel) return;
    l->cancel_succeeded = PQcancel(cancel, error, sizeof(error));
    PQfreeCancel(cancel);
}

static void usage(const char *p) {
    fprintf(stderr, "usage: %s --conninfo STR --sql-file PATH --spool PATH "
            "--protocol extended|simple --deadline-ms N --max-rows N "
            "--max-bytes N --max-columns N --max-field-bytes N "
            "--max-spool-bytes N --max-messages N --snapshot PATH "
            "--session-mode off|lab_allowlist|native_baseline [--prepared NAME]\n", p);
}

int main(int argc, char **argv) {
    const char *conninfo = NULL, *sql_path = NULL, *spool_path = NULL;
    const char *protocol = NULL, *prepared = NULL, *snapshot = NULL;
    const char *session_mode = NULL;
    const char *reason = "invalid_arguments";
    char sqlstate[6] = ""; uint64_t deadline_ms = 0, v; int i, ok = 0;
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
        else if (!strcmp(option, "--deadline-ms") &&
                 parse_u64(value, 3600000, &v)) deadline_ms = v;
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
        !session_mode || !deadline_ms ||
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
    if (!configure_session(c, session_mode) ||
        (!(before = !strcmp(session_mode, "native_baseline") ? strdup("0") :
          show_scalar(c, "pg_qi.shadow_decision_id",
                      now_ms() + (int64_t)deadline_ms)))) {
        terminal("FAILED", "session_setup", &l, NULL); goto done;
    }
    query_deadline = now_ms() + (int64_t)deadline_ms;
    if (!send_subject(c, sql, protocol, prepared, query_deadline) ||
        !flush_bounded(c, query_deadline)) {
        terminal("FAILED", "send_error", &l, NULL); goto done;
    }
    if (!stream(c, spool, &l, query_deadline, &reason, sqlstate)) {
        if (!strncmp(reason, "max_", 4) || !strcmp(reason, "query_deadline"))
            cancel_owned_query(c, &l);
        terminal(!strncmp(reason, "max_", 4) ? "LIMIT_EXCEEDED" : "FAILED",
                 reason, &l, sqlstate[0] ? sqlstate : NULL); goto done;
    }
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
