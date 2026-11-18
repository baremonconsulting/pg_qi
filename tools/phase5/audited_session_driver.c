#define main protocol_spike_main
#include "protocol_driver.c"
#undef main

#include <openssl/sha.h>
#include <sys/stat.h>

#define FRAME_MAX (1024U * 1024U)
#define MAX_PREPARED 64

typedef struct { char name[64]; unsigned long long generation; int active; } PlanSlot;

static int plan_index(PlanSlot slots[MAX_PREPARED], const char *name) {
    int i;
    for (i = 0; i < MAX_PREPARED; i++)
        if (slots[i].active && !strcmp(slots[i].name, name)) return i;
    return -1;
}

static int exact_read(void *buffer, size_t length) {
    unsigned char *p = buffer;
    while (length) {
        size_t n = fread(p, 1, length, stdin);
        if (!n) return 0;
        p += n; length -= n;
    }
    return 1;
}

static int frame_reply(unsigned int opcode, unsigned long long sequence,
                       const char *state, const char *planning,
                       const char *execution, const char *snapshot,
                       const char *reason) {
    char json[512]; unsigned char header[6]; int n;
    n = snprintf(json, sizeof(json),
        "{\"execution_state\":\"%s\",\"planning_state\":\"%s\","
        "\"reason\":\"%s\",\"sequence\":%llu,\"snapshot_state\":\"%s\","
        "\"state\":\"%s\"}", execution, planning, reason, sequence, snapshot, state);
    if (n < 0 || (size_t)n >= sizeof(json)) return 0;
    header[0] = (unsigned char)(((unsigned int)n + 2) >> 24);
    header[1] = (unsigned char)(((unsigned int)n + 2) >> 16);
    header[2] = (unsigned char)(((unsigned int)n + 2) >> 8);
    header[3] = (unsigned char)((unsigned int)n + 2);
    header[4] = 1; header[5] = (unsigned char)opcode;
    return fwrite(header, 1, sizeof(header), stdout) == sizeof(header) &&
           fwrite(json, 1, (size_t)n, stdout) == (size_t)n && !fflush(stdout);
}

static int parse_request(const char *json, char case_id[64], char digest[65],
                         unsigned long long *sequence) {
    int consumed = 0; size_t i;
    if (sscanf(json,
        "{\"case_id\":\"%63[A-Za-z0-9_]\",\"case_sha256\":\"%64[0-9a-f]\",\"sequence\":%llu}%n",
        case_id, digest, sequence, &consumed) != 3 || json[consumed]) return 0;
    if (!case_id[0] || strlen(digest) != 64 || !*sequence) return 0;
    for (i = 0; case_id[i]; i++)
        if (!((case_id[i] >= 'A' && case_id[i] <= 'Z') ||
              (case_id[i] >= 'a' && case_id[i] <= 'z') ||
              (case_id[i] >= '0' && case_id[i] <= '9') || case_id[i] == '_')) return 0;
    return 1;
}

static int parse_prepare(const char *json, char case_id[64], char digest[65],
        unsigned long long *generation, char prepared[64], unsigned long long *sequence) {
    int n = 0;
    return sscanf(json, "{\"case_id\":\"%63[A-Za-z0-9_]\",\"case_sha256\":\"%64[0-9a-f]\",\"generation\":%llu,\"prepared\":\"%63[A-Za-z0-9_]\",\"sequence\":%llu}%n",
        case_id, digest, generation, prepared, sequence, &n) == 5 && !json[n] &&
        strlen(digest) == 64 && *generation && *sequence;
}

static int parse_prepared_op(const char *json, unsigned long long *generation,
        char prepared[64], unsigned long long *sequence) {
    int n = 0;
    return sscanf(json, "{\"generation\":%llu,\"prepared\":\"%63[A-Za-z0-9_]\",\"sequence\":%llu}%n",
        generation, prepared, sequence, &n) == 3 && !json[n] && *generation && *sequence;
}

static int parse_sequence(const char *json, unsigned long long *sequence) {
    int n = 0;
    return sscanf(json, "{\"sequence\":%llu}%n", sequence, &n) == 1 &&
           !json[n] && *sequence;
}

static int command_result(PGconn *conn, int64_t deadline, const char **reason) {
    PGresult *r;
    if (!flush_bounded(conn, deadline)) { *reason = "send_error"; return 0; }
    while (PQisBusy(conn)) {
        if (wait_socket(conn, POLLIN, deadline) <= 0 || !PQconsumeInput(conn)) {
            *reason = "connection_error"; return 0;
        }
    }
    r = PQgetResult(conn);
    if (!r || PQresultStatus(r) != PGRES_COMMAND_OK) {
        if (r) PQclear(r);
        *reason = "server_error";
        return 0;
    }
    PQclear(r);
    if ((r = PQgetResult(conn)) != NULL) {
        PQclear(r); *reason = "multiple_result_sets"; return 0;
    }
    return 1;
}

static int digest_matches(const unsigned char *data, size_t length,
                          const char *expected) {
    unsigned char sum[SHA256_DIGEST_LENGTH]; char actual[65]; size_t i;
    SHA256(data, length, sum);
    for (i = 0; i < sizeof(sum); i++) snprintf(actual + i * 2, 3, "%02x", sum[i]);
    return !strcmp(actual, expected);
}

static int open_private_directory(const char *path) {
    int fd = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || !S_ISDIR(st.st_mode) ||
        st.st_uid != getuid() || (st.st_mode & 0777) != 0700) {
        if (fd >= 0) close(fd);
        return -1;
    }
    return fd;
}

static char *read_case_at(int root_fd, const char *name) {
    int fd = openat(root_fd, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
    struct stat st; char *data; size_t used = 0;
    if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_nlink != 1 ||
        st.st_uid != getuid() || (st.st_mode & 0022) != 0 ||
        st.st_size <= 0 || st.st_size > 1024 * 1024) {
        if (fd >= 0) close(fd);
        return NULL;
    }
    data = malloc((size_t)st.st_size + 1);
    if (!data) { close(fd); return NULL; }
    while (used < (size_t)st.st_size) {
        ssize_t count = read(fd, data + used, (size_t)st.st_size - used);
        if (count <= 0) { free(data); close(fd); return NULL; }
        used += (size_t)count;
    }
    if (close(fd) || memchr(data, '\0', used)) { free(data); return NULL; }
    data[used] = 0;
    return data;
}

int main(int argc, char **argv) {
    const char *conninfo = NULL, *case_root = NULL, *output_root = NULL;
    const char *mode = NULL; PGconn *conn = NULL; int i, case_fd = -1, output_fd = -1;
    uint64_t deadline_ms = 0, max_requests = 0, request_count = 0, value;
    Limits base = {0};
    PlanSlot plans[MAX_PREPARED];
    memset(plans, 0, sizeof(plans));
    for (i = 1; i + 1 < argc; i += 2) {
        if (!strcmp(argv[i], "--conninfo")) conninfo = argv[i + 1];
        else if (!strcmp(argv[i], "--case-root")) case_root = argv[i + 1];
        else if (!strcmp(argv[i], "--output-root")) output_root = argv[i + 1];
        else if (!strcmp(argv[i], "--session-mode")) mode = argv[i + 1];
        else if (!strcmp(argv[i], "--deadline-ms") && parse_u64(argv[i+1], 3600000, &value)) deadline_ms=value;
        else if (!strcmp(argv[i], "--max-requests") && parse_u64(argv[i+1], 100000, &value)) max_requests=value;
        else if (!strcmp(argv[i], "--max-rows") && parse_u64(argv[i+1], UINT32_MAX, &value)) base.max_rows=value;
        else if (!strcmp(argv[i], "--max-bytes") && parse_u64(argv[i+1], INT64_MAX, &value)) base.max_bytes=value;
        else if (!strcmp(argv[i], "--max-columns") && parse_u64(argv[i+1], UINT32_MAX, &value)) base.max_columns=(size_t)value;
        else if (!strcmp(argv[i], "--max-field-bytes") && parse_u64(argv[i+1], UINT32_MAX, &value)) base.max_field=(size_t)value;
        else if (!strcmp(argv[i], "--max-spool-bytes") && parse_u64(argv[i+1], INT64_MAX, &value)) base.max_spool=value;
        else if (!strcmp(argv[i], "--max-messages") && parse_u64(argv[i+1], UINT32_MAX, &value)) base.max_messages=value;
        else return 64;
    }
    if (i != argc || !conninfo || !case_root || !output_root || !mode || !deadline_ms || !max_requests ||
        !base.max_rows || !base.max_bytes || !base.max_columns || !base.max_field ||
        !base.max_spool || !base.max_messages) return 64;
    case_fd = open_private_directory(case_root);
    output_fd = open_private_directory(output_root);
    if (case_fd < 0 || output_fd < 0) {
        if (case_fd >= 0) close(case_fd);
        if (output_fd >= 0) close(output_fd);
        return 64;
    }
    conn = connect_bounded(conninfo, now_ms() + (int64_t)deadline_ms);
    if (!conn || !configure_session(conn, mode)) {
        if (conn) PQfinish(conn);
        close(output_fd); close(case_fd);
        return 1;
    }
    {
        PGresult *readonly;
        if (PQsetnonblocking(conn, 0) != 0) {
            PQfinish(conn); close(output_fd); close(case_fd); return 1;
        }
        readonly = PQexec(conn, "SET default_transaction_read_only=on");
        if (!readonly || PQresultStatus(readonly) != PGRES_COMMAND_OK) {
            if (readonly) PQclear(readonly);
            PQfinish(conn); close(output_fd); close(case_fd); return 1;
        }
        PQclear(readonly);
        if (PQsetnonblocking(conn, 1) != 0) {
            PQfinish(conn); close(output_fd); close(case_fd); return 1;
        }
    }
    for (;;) {
        unsigned char header[4], *body = NULL; uint32_t length; unsigned int opcode;
        char case_id[64] = "", digest[65] = "", prepared[64] = "";
        char sql_path[PATH_MAX], spool_path[PATH_MAX], snapshot_path[PATH_MAX];
        unsigned long long sequence = 0, generation = 0; char *sql = NULL, *before = NULL, *after = NULL; FILE *spool = NULL;
        int prepared_slot = -1;
        Limits limits = base; const char *reason = "invalid_frame"; char sqlstate[6] = ""; int ok = 0;
        const char *planning_state = "UNAVAILABLE", *execution_state = "NOT_RUN";
        const char *snapshot_state = "NOT_RUN";
        if (!exact_read(header, 4)) break;
        length = ((uint32_t)header[0] << 24) | ((uint32_t)header[1] << 16) |
                 ((uint32_t)header[2] << 8) | header[3];
        if (length < 2 || length > FRAME_MAX || !(body = malloc((size_t)length + 1)) ||
            !exact_read(body, length)) break;
        body[length] = 0; opcode = body[1];
        if (body[0] != 1 || memchr(body + 2, '\0', length - 2)) { free(body); break; }
        if (opcode == 2 && !strcmp((char *)body + 2, "{}")) { free(body); break; }
        if (opcode == 1) {
            if (!parse_request((char *)body + 2, case_id, digest, &sequence)) { free(body); break; }
        } else if (opcode == 3) {
            if (!parse_prepare((char *)body + 2, case_id, digest, &generation, prepared, &sequence)) { free(body); break; }
        } else if (opcode == 4 || opcode == 5) {
            if (!parse_prepared_op((char *)body + 2, &generation, prepared, &sequence)) { free(body); break; }
        } else if (opcode == 6) {
            if (!parse_sequence((char *)body + 2, &sequence)) { free(body); break; }
        } else { free(body); break; }
        free(body);
        if (++request_count > max_requests) break;
        if (opcode == 6) {
            if (!PQsendQuery(conn, "BEGIN") ||
                    !command_result(conn, now_ms()+(int64_t)deadline_ms, &reason)) { }
            else ok = 1;
            if (!frame_reply(opcode, sequence, ok ? "COMPLETE" : "FAILED",
                    "NOT_RUN", ok ? "COMPLETE" : "FAILED", "NOT_RUN",
                    ok ? "ok" : reason)) ok = 0;
            if (!ok) break;
            continue;
        }
        if (opcode == 3) {
            int slot = -1;
            if (plan_index(plans, prepared) >= 0) reason = "prepared_already_active";
            else {
                for (slot = 0; slot < MAX_PREPARED && plans[slot].active; slot++) { }
                if (slot == MAX_PREPARED) reason = "prepared_registry_full";
            }
            if (snprintf(sql_path,sizeof(sql_path),"%s.sql",case_id) >= (int)sizeof(sql_path)) break;
            if (snprintf(snapshot_path,sizeof(snapshot_path),"/proc/self/fd/%d/%020llu-%s-prepare.snapshot.tsv",
                         output_fd,sequence,prepared) >= (int)sizeof(snapshot_path)) break;
            if (strcmp(reason, "invalid_frame")) { }
            else if (!(sql = read_case_at(case_fd, sql_path)) ||
                     !digest_matches((unsigned char *)sql, strlen(sql), digest)) reason = "case_digest";
            else if (!(before = show_scalar(conn, "pg_qi.shadow_decision_id",
                                             now_ms()+(int64_t)deadline_ms))) reason = "snapshot_before";
            else if (!PQsendPrepare(conn, prepared, sql, 0, NULL) ||
                     !command_result(conn, now_ms()+(int64_t)deadline_ms, &reason)) { }
            else if (!write_snapshot(conn, snapshot_path, before,
                                     now_ms()+(int64_t)deadline_ms)) reason = "snapshot_write";
            else {
                strcpy(plans[slot].name, prepared); plans[slot].generation = generation;
                plans[slot].active = 1; ok = 1;
            }
            if (!frame_reply(opcode, sequence, ok ? "COMPLETE" : "FAILED",
                    ok ? "COMPLETE" : "FAILED", "NOT_RUN",
                    ok ? "COMPLETE" : "INCOMPLETE",
                    reason)) ok = 0;
            free(before); free(sql);
            if (!ok) break;
            continue;
        }
        if (opcode == 5) {
            char command[96];
            int slot = plan_index(plans, prepared);
            if (slot < 0 || plans[slot].generation != generation) reason = "prepared_generation";
            else if (snprintf(command, sizeof(command), "DEALLOCATE %s", prepared) >= (int)sizeof(command) ||
                    !PQsendQuery(conn, command) ||
                    !command_result(conn, now_ms()+(int64_t)deadline_ms, &reason)) { }
            else { plans[slot].active = 0; ok = 1; }
            if (!frame_reply(opcode, sequence, ok ? "COMPLETE" : "FAILED",
                    "NOT_RUN", ok ? "COMPLETE" : "FAILED", "NOT_RUN",
                    reason)) ok = 0;
            if (!ok) break;
            continue;
        }
        if (opcode == 4) {
            prepared_slot = plan_index(plans, prepared);
            if (prepared_slot < 0 || plans[prepared_slot].generation != generation) {
                if (!frame_reply(opcode, sequence, "REFUSED", "NOT_RUN", "NOT_RUN",
                        "NOT_RUN", "prepared_generation")) break;
                continue;
            }
        }
        if (snprintf(sql_path,sizeof(sql_path),"%s.sql",case_id) >= (int)sizeof(sql_path) ||
            snprintf(spool_path,sizeof(spool_path),"/proc/self/fd/%d/%020llu-%s.spool",output_fd,sequence,case_id) >= (int)sizeof(spool_path) ||
            snprintf(snapshot_path,sizeof(snapshot_path),"/proc/self/fd/%d/%020llu-%s.snapshot.tsv",output_fd,sequence,case_id) >= (int)sizeof(snapshot_path)) break;
        if (opcode == 1) sql = read_case_at(case_fd, sql_path);
        if (opcode == 1 && (!sql || !digest_matches((unsigned char *)sql, strlen(sql), digest))) reason = "case_digest";
        else if (!(spool = open_exclusive(spool_path))) reason = "spool_open";
        else if (!(before = show_scalar(conn, "pg_qi.shadow_decision_id", now_ms()+(int64_t)deadline_ms))) reason = "snapshot_before";
        else if (!((opcode == 1 ? PQsendQueryParams(conn, sql, 0, NULL, NULL, NULL, NULL, 0) :
                              PQsendQueryPrepared(conn, prepared, 0, NULL, NULL, NULL, 0))) ||
                 !flush_bounded(conn, now_ms()+(int64_t)deadline_ms)) reason = "send_error";
        else {
            execution_state = "INCOMPLETE";
            if (!stream(conn, spool, &limits, now_ms()+(int64_t)deadline_ms, &reason, sqlstate)) {
                if (!strcmp(reason, "server_error") ||
                        !strcmp(reason, "server_error_after_partial_stream")) {
                    PGresult *tail;
                    execution_state = "FAILED";
                    while ((tail = PQgetResult(conn)) != NULL) PQclear(tail);
                    after = show_scalar(conn, "pg_qi.shadow_decision_id",
                                        now_ms()+(int64_t)deadline_ms);
                    if (after && write_snapshot(conn, snapshot_path, before,
                                                now_ms()+(int64_t)deadline_ms)) {
                        snapshot_state = "COMPLETE";
                        if (opcode == 4) {
                            planning_state = "COMPLETE";
                            if (strcmp(before, after)) {
                                reason = "replanned_execution_error";
                                plans[prepared_slot].generation++;
                            } else reason = "cached_execution_error";
                        } else if (strcmp(before, after)) {
                            planning_state = "COMPLETE";
                            reason = "fresh_execution_error";
                        } else {
                            planning_state = "FAILED";
                            reason = "planning_error";
                        }
                    } else snapshot_state = "INCOMPLETE";
                    if (PQtransactionStatus(conn) == PQTRANS_INERROR)
                        reason = "aborted_transaction";
                }
            } else {
                execution_state = "COMPLETE";
                after = show_scalar(conn, "pg_qi.shadow_decision_id",
                                    now_ms()+(int64_t)deadline_ms);
                if (!after || !write_snapshot(conn, snapshot_path, before, now_ms()+(int64_t)deadline_ms)) {
                    reason = "snapshot_write"; snapshot_state = "INCOMPLETE";
                } else {
                    snapshot_state = "COMPLETE"; planning_state = "COMPLETE";
                    if (opcode == 4) {
                        if (strcmp(before, after)) {
                            reason = "replanned";
                            plans[prepared_slot].generation++;
                        } else reason = "cached";
                    } else reason = "fresh";
                    if (fflush(spool) || fsync(fileno(spool))) reason = "spool_sync";
                    else ok = 1;
                }
            }
        }
        {
            int uncertain = !strcmp(reason, "query_deadline") ||
                            (strlen(reason) == 16 && !memcmp(reason, "connection_error", 16)) ||
                            !strcmp(reason, "server_error_after_partial_stream") ||
                            !strcmp(reason, "snapshot_write") || !strcmp(reason, "spool_sync") ||
                            !strcmp(reason, "send_error");
            const char *terminal = ok ? "COMPLETE" : (uncertain ? "INCOMPLETE" : "FAILED");
            if (fsync(output_fd) || !frame_reply(opcode, sequence, terminal,
                    planning_state, execution_state, snapshot_state,
                    ok ? "ok" : reason)) ok = 0;
        }
        if (spool) fclose(spool);
        free(before);
        free(after);
        free(sql);
        if (!ok) break;
    }
    PQfinish(conn);
    fsync(output_fd);
    close(output_fd);
    close(case_fd);
    return 0;
}
