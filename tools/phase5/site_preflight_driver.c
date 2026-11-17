#define _GNU_SOURCE

#include <libpq-fe.h>
#include <openssl/evp.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/random.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    const char *id;
    const char *sql;
    int max_rows;
    int max_columns;
} Check;

static const Check checks[] = {
    {"routing_mode", "SHOW pg_qi.routing_mode", 1, 1},
    {"duckdb_force", "SHOW duckdb.force_execution", 1, 1},
    {"server_version_num", "SELECT pg_catalog.current_setting('server_version_num')", 1, 1},
    {"database_identity", "SELECT pg_catalog.current_database(), (SELECT oid::text FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database())", 1, 2},
    {"role_identity", "SELECT SESSION_USER::text, CURRENT_USER::text", 1, 2},
    {"postmaster_start", "SELECT pg_catalog.to_char(pg_catalog.pg_postmaster_start_time() AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')", 1, 1},
    {"cluster_identity", "SELECT system_identifier::text FROM pg_catalog.pg_control_system()", 1, 1},
    {"backend_identity", "SELECT pid::text, pg_catalog.to_char(backend_start AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') FROM pg_catalog.pg_stat_activity WHERE pid = pg_catalog.pg_backend_pid()", 1, 2},
    {"extensions", "SELECT extname, extversion FROM pg_catalog.pg_extension WHERE extname IN ('pg_duckdb','pg_qi') ORDER BY extname", 2, 2},
    {"preload", "SHOW shared_preload_libraries", 1, 1},
    {"pg_qi_enabled", "SHOW pg_qi.enabled", 1, 1}
};

static int64_t now_ms(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value)) return -1;
    return (int64_t)value.tv_sec * 1000 + value.tv_nsec / 1000000;
}

static int parse_positive(const char *text, int maximum, int *result) {
    char *end = NULL; long value;
    if (!text || !*text) return 0;
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p)
        if (*p < '0' || *p > '9') return 0;
    errno = 0; value = strtol(text, &end, 10);
    if (errno || *end || value < 1 || value > maximum) return 0;
    *result = (int)value; return 1;
}

static int safe_scalar(const char *value, int path) {
    size_t length;
    if (!value || !(length = strlen(value)) || length > 1024) return 0;
    if (path && value[0] != '/') return 0;
    return !strpbrk(value, ",\r\n=") && !strstr(value, "://");
}

static int open_verified_file(const char *path, const char *expected) {
    unsigned char buffer[16384], digest[EVP_MAX_MD_SIZE];
    char encoded[65]; unsigned int digest_length = 0; ssize_t count;
    off_t remaining;
    int descriptor = -1, sealed = -1, valid = 0; struct stat metadata;
    EVP_MD_CTX *context = NULL;
    if (!expected || strlen(expected) != 64) return -1;
    for (size_t i = 0; i < 64; ++i)
        if (!((expected[i] >= '0' && expected[i] <= '9') ||
              (expected[i] >= 'a' && expected[i] <= 'f'))) return -1;
    descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (descriptor < 0 || fstat(descriptor, &metadata) || !S_ISREG(metadata.st_mode) ||
        metadata.st_size < 1 || metadata.st_size > 16 * 1024 * 1024) goto done;
    context = EVP_MD_CTX_new();
    if (!context || EVP_DigestInit_ex(context, EVP_sha256(), NULL) != 1) goto done;
    sealed = memfd_create("pgqi-approved-ca", MFD_CLOEXEC | MFD_ALLOW_SEALING);
    if (sealed < 0) goto done;
    remaining = metadata.st_size;
    while (remaining > 0 &&
           (count = read(descriptor, buffer,
                         remaining < (off_t)sizeof(buffer) ? (size_t)remaining : sizeof(buffer))) > 0) {
        ssize_t written = 0;
        if (EVP_DigestUpdate(context, buffer, (size_t)count) != 1) goto done;
        while (written < count) {
            ssize_t step = write(sealed, buffer + written, (size_t)(count - written));
            if (step <= 0) goto done;
            written += step;
        }
        remaining -= count;
    }
    if (remaining != 0 || read(descriptor, buffer, 1) != 0 ||
        EVP_DigestFinal_ex(context, digest, &digest_length) != 1 ||
        digest_length != 32) goto done;
    for (size_t i = 0; i < digest_length; ++i) snprintf(encoded + i * 2, 3, "%02x", digest[i]);
    encoded[64] = 0; valid = strcmp(encoded, expected) == 0;
    if (!valid || lseek(sealed, 0, SEEK_SET) != 0 ||
        fcntl(sealed, F_ADD_SEALS,
              F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL) != 0)
        valid = 0;
done:
    if (context) EVP_MD_CTX_free(context);
    if (descriptor >= 0) close(descriptor);
    if (!valid && sealed >= 0) close(sealed);
    memset(buffer, 0, sizeof(buffer)); memset(digest, 0, sizeof(digest));
    return valid ? sealed : -1;
}

static int safe_socket_directory(const char *path, uid_t owner) {
    int descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY);
    struct stat metadata; int valid = 0;
    if (descriptor >= 0 && !fstat(descriptor, &metadata) && S_ISDIR(metadata.st_mode) &&
        metadata.st_uid == owner &&
        !(metadata.st_mode & (S_IWGRP | S_IWOTH))) valid = 1;
    if (descriptor >= 0) close(descriptor);
    return valid;
}

static void discard_notice(void *argument, const char *message) {
    (void)argument; (void)message;
}

static int environment_is_clean(void) {
    extern char **environ;
    static const char *unsafe[] = {"OPENSSL_CONF=", "OPENSSL_MODULES=", "LD_PRELOAD=",
        "LD_LIBRARY_PATH=", "SSL_CERT_FILE=", "SSL_CERT_DIR="};
    for (char **entry = environ; entry && *entry; ++entry) {
        if (!strncmp(*entry, "PG", 2)) return 0;
        for (size_t i = 0; i < sizeof(unsafe) / sizeof(unsafe[0]); ++i)
            if (!strncmp(*entry, unsafe[i], strlen(unsafe[i]))) return 0;
    }
    return 1;
}

static int wait_socket(PGconn *connection, short events, int64_t deadline) {
    struct pollfd item = {PQsocket(connection), events, 0};
    int64_t left = deadline - now_ms(); int result;
    if (item.fd < 0 || left <= 0) return 0;
    result = poll(&item, 1, left > 1000 ? 1000 : (int)left);
    if (result < 0 && errno == EINTR) return -1;
    return result > 0 ? 1 : result == 0 ? -1 : 0;
}

static PGconn *connect_bounded(const char *const *keywords, const char *const *values,
                               int64_t deadline) {
    PGconn *connection = PQconnectStartParams(keywords, values, 0);
    if (connection) PQsetNoticeProcessor(connection, discard_notice, NULL);
    if (!connection || PQstatus(connection) == CONNECTION_BAD ||
        PQsetnonblocking(connection, 1) != 0) {
        if (connection) PQfinish(connection);
        return NULL;
    }
    for (;;) {
        PostgresPollingStatusType status = PQconnectPoll(connection); int ready;
        if (status == PGRES_POLLING_OK) return connection;
        if (status == PGRES_POLLING_FAILED) { PQfinish(connection); return NULL; }
        ready = wait_socket(connection, status == PGRES_POLLING_WRITING ? POLLOUT : POLLIN,
                            deadline);
        if (ready == 0 || now_ms() >= deadline) { PQfinish(connection); return NULL; }
    }
}

static PGresult *query_bounded(PGconn *connection, const char *sql, int64_t deadline) {
    PGresult *result, *extra;
    if (!PQsendQueryParams(connection, sql, 0, NULL, NULL, NULL, NULL, 0)) return NULL;
    while (PQflush(connection) == 1) {
        if (wait_socket(connection, POLLOUT, deadline) == 0 || now_ms() >= deadline) return NULL;
    }
    while (PQisBusy(connection)) {
        int ready = wait_socket(connection, POLLIN, deadline);
        if (ready == 0 || now_ms() >= deadline) return NULL;
        if (ready > 0 && !PQconsumeInput(connection)) return NULL;
    }
    result = PQgetResult(connection);
    if (!result) return NULL;
    extra = PQgetResult(connection);
    if (extra) { PQclear(extra); PQclear(result); return NULL; }
    return result;
}

static void json_string(const char *value) {
    const unsigned char *p = (const unsigned char *)value;
    putchar('"');
    while (*p) {
        if (*p == '"' || *p == '\\') { putchar('\\'); putchar(*p); }
        else if (*p >= 0x20 && *p < 0x7f) putchar(*p);
        else printf("\\u%04x", (unsigned int)*p);
        ++p;
    }
    putchar('"');
}

static void failure(const char *reason) {
    printf("{\"reason\":\"%s\",\"result\":\"FAIL\"}\n", reason);
}

static char *read_credential(int descriptor, int64_t deadline) {
    char *secret = calloc(4097, 1); ssize_t count;
    struct pollfd item = {descriptor, POLLIN, 0}; int ready;
    struct stat metadata; char link_path[64], target[128]; ssize_t target_length;
    if (!secret) return NULL;
    if (fstat(descriptor, &metadata) || !S_ISFIFO(metadata.st_mode) ||
        snprintf(link_path, sizeof(link_path), "/proc/self/fd/%d", descriptor) >= (int)sizeof(link_path) ||
        (target_length = readlink(link_path, target, sizeof(target) - 1)) <= 6) {
        free(secret); return NULL;
    }
    target[target_length] = 0;
    if (strncmp(target, "pipe:[", 6) || target[target_length - 1] != ']') {
        free(secret); return NULL;
    }
    do {
        int64_t left = deadline - now_ms();
        if (left <= 0) { free(secret); return NULL; }
        ready = poll(&item, 1, left > 1000 ? 1000 : (int)left);
    } while (ready < 0 && errno == EINTR);
    if (ready <= 0 || !(item.revents & (POLLIN | POLLHUP))) { free(secret); return NULL; }
    count = read(descriptor, secret, 4096);
    if (count <= 0 || count == 4096) { memset(secret, 0, 4097); free(secret); return NULL; }
    while (count > 0 && (secret[count - 1] == '\n' || secret[count - 1] == '\r'))
        secret[--count] = 0;
    if (!count || memchr(secret, 0, (size_t)count)) {
        memset(secret, 0, 4097); free(secret); return NULL;
    }
    return secret;
}

static void usage(const char *program) {
    fprintf(stderr, "usage: %s --transport tls|socket --host VALUE [--hostaddr IP] "
            "--port N --dbname NAME --user NAME [--sslrootcert PATH] "
            "[--sslrootcert-sha256 HEX] [--credential-fd N] --deadline-ms N\n", program);
}

int main(int argc, char **argv) {
    const char *transport = NULL, *host = NULL, *hostaddr = NULL, *port = NULL;
    const char *dbname = NULL, *user = NULL, *rootcert = NULL, *rootcert_sha256 = NULL;
    int credential_fd = -1, rootcert_fd = -1, deadline_ms = 0, socket_uid = -1, i, exit_code = 1;
    char verified_rootcert[64] = {0};
    char isolated_home[] = "/tmp/pgqi-home-XXXXXX"; int isolated_home_created = 0;
    char *password = NULL; PGconn *connection = NULL;
    unsigned char nonce[16]; char nonce_hex[33];
    PGresult *results[sizeof(checks) / sizeof(checks[0])] = {0};
    const char *keywords[20] = {0}; const char *values[20] = {0}; size_t parameter = 0;

    for (i = 1; i < argc; i += 2) {
        const char *option = argv[i], *value;
        if (i + 1 >= argc) { usage(argv[0]); return 64; }
        value = argv[i + 1];
        if (!strcmp(option, "--transport")) transport = value;
        else if (!strcmp(option, "--host")) host = value;
        else if (!strcmp(option, "--hostaddr")) hostaddr = value;
        else if (!strcmp(option, "--port")) port = value;
        else if (!strcmp(option, "--dbname")) dbname = value;
        else if (!strcmp(option, "--user")) user = value;
        else if (!strcmp(option, "--sslrootcert")) rootcert = value;
        else if (!strcmp(option, "--sslrootcert-sha256")) rootcert_sha256 = value;
        else if (!strcmp(option, "--credential-fd") && parse_positive(value, 1048576, &credential_fd)) {}
        else if (!strcmp(option, "--socket-uid") && parse_positive(value, 2147483647, &socket_uid)) {}
        else if (!strcmp(option, "--deadline-ms") && parse_positive(value, 60000, &deadline_ms)) {}
        else { usage(argv[0]); return 64; }
    }
    if (!environment_is_clean()) { failure("ambient_environment"); return 2; }
    if (getrandom(nonce, sizeof(nonce), 0) != (ssize_t)sizeof(nonce)) {
        failure("entropy_unavailable"); return 2;
    }
    for (size_t n = 0; n < sizeof(nonce); ++n) snprintf(nonce_hex + n * 2, 3, "%02x", nonce[n]);
    nonce_hex[32] = 0; memset(nonce, 0, sizeof(nonce));
    if (!transport || !host || !port || !dbname || !user || !deadline_ms ||
        !safe_scalar(host, !strcmp(transport, "socket")) || !safe_scalar(port, 0) || !safe_scalar(dbname, 0) ||
        !safe_scalar(user, 0) || (strcmp(transport, "tls") && strcmp(transport, "socket"))) {
        failure("invalid_policy"); return 2;
    }
    if (!strcmp(transport, "tls")) {
        if (!hostaddr || !rootcert || !safe_scalar(hostaddr, 0) || !safe_scalar(rootcert, 1) ||
            credential_fd < 0) {
            failure("invalid_tls_policy"); return 2;
        }
        rootcert_fd = open_verified_file(rootcert, rootcert_sha256);
        if (rootcert_fd < 0 || snprintf(verified_rootcert, sizeof(verified_rootcert),
                                       "/proc/self/fd/%d", rootcert_fd) >= (int)sizeof(verified_rootcert)) {
            if (rootcert_fd >= 0) close(rootcert_fd);
            failure("invalid_tls_policy"); return 2;
        }
        password = read_credential(credential_fd, now_ms() + deadline_ms);
        close(credential_fd); credential_fd = -1;
        if (!password) { close(rootcert_fd); failure("credential_channel"); return 2; }
        keywords[parameter] = "host"; values[parameter++] = host;
        keywords[parameter] = "hostaddr"; values[parameter++] = hostaddr;
        keywords[parameter] = "sslmode"; values[parameter++] = "verify-full";
        keywords[parameter] = "sslrootcert"; values[parameter++] = verified_rootcert;
        keywords[parameter] = "sslcert"; values[parameter++] = "";
        keywords[parameter] = "sslkey"; values[parameter++] = "";
        keywords[parameter] = "sslcrl"; values[parameter++] = "";
        keywords[parameter] = "sslcrldir"; values[parameter++] = "";
        keywords[parameter] = "gssencmode"; values[parameter++] = "disable";
        keywords[parameter] = "password"; values[parameter++] = password;
    } else {
        if (hostaddr || rootcert || rootcert_sha256 || credential_fd >= 0 || socket_uid < 0 ||
            !safe_socket_directory(host, (uid_t)socket_uid)) { failure("invalid_socket_policy"); return 2; }
        keywords[parameter] = "host"; values[parameter++] = host;
    }
    keywords[parameter] = "port"; values[parameter++] = port;
    keywords[parameter] = "dbname"; values[parameter++] = dbname;
    keywords[parameter] = "user"; values[parameter++] = user;
    keywords[parameter] = "passfile"; values[parameter++] = "/dev/null";
    keywords[parameter] = "connect_timeout"; values[parameter++] = "10";

    if (!mkdtemp(isolated_home)) {
        failure("runtime_isolation_failed"); goto done;
    }
    isolated_home_created = 1;
    if (chmod(isolated_home, 0700) || setenv("HOME", isolated_home, 1)) {
        failure("runtime_isolation_failed"); goto done;
    }

    connection = connect_bounded(keywords, values, now_ms() + deadline_ms);
    if (password) { memset(password, 0, strlen(password)); free(password); password = NULL; }
    if (!connection) { failure("connect_failed"); goto done; }
    for (size_t check_index = 0; check_index < sizeof(checks) / sizeof(checks[0]); ++check_index) {
        PGresult *result = query_bounded(connection, checks[check_index].sql, now_ms() + deadline_ms);
        if (!result || PQresultStatus(result) != PGRES_TUPLES_OK ||
            PQntuples(result) > checks[check_index].max_rows ||
            PQnfields(result) != checks[check_index].max_columns) {
            if (result) PQclear(result);
            failure("query_failed"); goto done;
        }
        for (int row = 0; row < PQntuples(result); ++row) {
            for (int column = 0; column < PQnfields(result); ++column) {
                const char *field = PQgetisnull(result, row, column) ? "" : PQgetvalue(result, row, column);
                if (strlen(field) > 512 || strpbrk(field, "\r\n\t")) {
                    PQclear(result); failure("unsafe_result"); goto done;
                }
            }
        }
        if (check_index == 0 && (PQntuples(result) != 1 || strcmp(PQgetvalue(result, 0, 0), "off"))) {
            PQclear(result); failure("routing_not_off"); goto done;
        }
        if (check_index == 1 && (PQntuples(result) != 1 || strcmp(PQgetvalue(result, 0, 0), "off"))) {
            PQclear(result); failure("force_not_off"); goto done;
        }
        results[check_index] = result;
    }
    printf("{\"checks\":{");
    for (size_t check_index = 0; check_index < sizeof(checks) / sizeof(checks[0]); ++check_index) {
        PGresult *result = results[check_index];
        if (check_index) putchar(',');
        json_string(checks[check_index].id);
        printf(":{\"rows\":[");
        for (int row = 0; row < PQntuples(result); ++row) {
            if (row) putchar(',');
            putchar('[');
            for (int column = 0; column < PQnfields(result); ++column) {
                const char *field = PQgetisnull(result, row, column) ? "" : PQgetvalue(result, row, column);
                if (column) putchar(',');
                json_string(field);
            }
            putchar(']');
        }
        printf("]}");
    }
    printf("},\"connection_nonce\":\"%s\",\"result\":\"PASS\"}\n", nonce_hex); exit_code = 0;
done:
    for (size_t check_index = 0; check_index < sizeof(results) / sizeof(results[0]); ++check_index)
        if (results[check_index]) PQclear(results[check_index]);
    if (connection) PQfinish(connection);
    if (rootcert_fd >= 0) close(rootcert_fd);
    if (isolated_home_created) rmdir(isolated_home);
    if (password) { memset(password, 0, strlen(password)); free(password); }
    return exit_code;
}
