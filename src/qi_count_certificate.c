/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/xlog.h"
#include "common/cryptohash.h"
#include "common/sha2.h"
#include "miscadmin.h"
#include "pg_qi/qi_build_identity.h"
#include "pg_qi/qi_count_cert_parser.h"
#include "pg_qi/qi_count_certificate.h"
#include "utils/guc.h"
#include "utils/timestamp.h"

#ifdef USE_OPENSSL
#include <openssl/evp.h>
#endif

#if defined(__linux__)
#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
#endif

typedef enum QiCountRuntimeFile
{
    QI_COUNT_FILE_POSTGRES = 0,
    QI_COUNT_FILE_PG_QI,
    QI_COUNT_FILE_PG_DUCKDB,
    QI_COUNT_FILE_DUCKDB,
    QI_COUNT_FILE_COUNT
} QiCountRuntimeFile;

typedef struct QiCountRuntimeIdentity
{
    bool observed;
    char digest[QI_COUNT_FILE_COUNT][QI_COUNT_CERT_SHA256_HEX + 1];
} QiCountRuntimeIdentity;

static bool qi_count_cert_attest = false;
static char *qi_count_cert_value = NULL;
static char *qi_count_cert_approved = NULL;
static QiCountRuntimeIdentity qi_count_runtime = {0};
static QiCountCertificate qi_count_installed = {0};
static char qi_count_installed_digest[QI_COUNT_CERT_SHA256_HEX + 1];
static bool qi_count_installed_valid = false;

/* OpenSSL-backed pg_cryptohash requires a PostgreSQL ResourceOwner, which
 * is not present during shared preload or every configuration assignment.
 * Give the short-lived digest its own explicitly managed OpenSSL context.
 * Non-OpenSSL PostgreSQL supplies its untracked software implementation. */
#ifdef USE_OPENSSL
typedef EVP_MD_CTX QiCountHash;
#else
typedef pg_cryptohash_ctx QiCountHash;
#endif

static void
qi_count_hash_free(QiCountHash *hash)
{
#ifdef USE_OPENSSL
    EVP_MD_CTX_free(hash);
#else
    pg_cryptohash_free(hash);
#endif
}

static QiCountHash *
qi_count_hash_new(void)
{
    QiCountHash *hash;

#ifdef USE_OPENSSL
    hash = EVP_MD_CTX_new();
    if (hash != NULL && EVP_DigestInit_ex(hash, EVP_sha256(), NULL) == 1)
        return hash;
#else
    hash = pg_cryptohash_create(PG_SHA256);
    if (hash != NULL && pg_cryptohash_init(hash) == 0)
        return hash;
#endif
    if (hash != NULL)
        qi_count_hash_free(hash);
    return NULL;
}

static bool
qi_count_hash_update(QiCountHash *hash, const uint8 *data, size_t length)
{
#ifdef USE_OPENSSL
    return EVP_DigestUpdate(hash, data, length) == 1;
#else
    return pg_cryptohash_update(hash, data, length) == 0;
#endif
}

static bool
qi_count_hash_finish(QiCountHash *hash, uint8 *bytes)
{
#ifdef USE_OPENSSL
    unsigned int length = 0;

    return EVP_DigestFinal_ex(hash, bytes, &length) == 1 &&
        length == PG_SHA256_DIGEST_LENGTH;
#else
    return pg_cryptohash_final(hash, bytes, PG_SHA256_DIGEST_LENGTH) == 0;
#endif
}

static void
qi_count_digest_hex(const uint8 bytes[PG_SHA256_DIGEST_LENGTH], char *output)
{
    static const char alphabet[] = "0123456789abcdef";
    size_t i;

    for (i = 0; i < PG_SHA256_DIGEST_LENGTH; ++i)
    {
        output[i * 2] = alphabet[bytes[i] >> 4];
        output[i * 2 + 1] = alphabet[bytes[i] & 15];
    }
    output[QI_COUNT_CERT_SHA256_HEX] = '\0';
}

static bool
qi_count_hash_bytes(const char *text, size_t length, char *output)
{
    QiCountHash *hash = qi_count_hash_new();
    uint8 bytes[PG_SHA256_DIGEST_LENGTH];
    bool valid;

    if (hash == NULL)
        return false;
    valid = qi_count_hash_update(hash, (const uint8 *) text, length) &&
        qi_count_hash_finish(hash, bytes);
    qi_count_hash_free(hash);
    if (valid)
        qi_count_digest_hex(bytes, output);
    return valid;
}

static void
qi_count_certificate_assign(const char *value, void *extra)
{
    size_t length = value == NULL ? 0 : strlen(value);

    (void) extra;
    qi_count_installed_valid = qi_count_cert_parse(value, length, &qi_count_installed) &&
        qi_count_hash_bytes(value, length, qi_count_installed_digest);
    if (!qi_count_installed_valid)
        qi_count_installed_digest[0] = '\0';
}

#if defined(__linux__)
#define QI_COUNT_MAP_LINE_BYTES 8192
#define QI_COUNT_MAP_MAX_LINES 16384
#define QI_COUNT_MAX_BINARY_BYTES (UINT64_C(512) * 1024 * 1024)

typedef struct QiCountMapping
{
    char path[MAXPGPATH];
    unsigned int device_major;
    unsigned int device_minor;
    unsigned long long inode;
} QiCountMapping;

static const char *const qi_count_names[QI_COUNT_FILE_COUNT] = {
    "postgres", "pg_qi.so", "pg_duckdb.so", "libduckdb.so"
};

static bool
qi_count_maps(QiCountMapping *mappings)
{
    FILE *maps = fopen("/proc/self/maps", "r");
    char line[QI_COUNT_MAP_LINE_BYTES];
    unsigned int lines = 0;
    bool valid = false;

    if (maps == NULL)
        return false;
    while (fgets(line, sizeof(line), maps) != NULL)
    {
        unsigned long long inode;
        unsigned int device_major;
        unsigned int device_minor;
        char *path;
        char *basename;
        char *newline;
        char *deleted;
        int offset = 0;
        int i;

        if (++lines > QI_COUNT_MAP_MAX_LINES ||
            (newline = strchr(line, '\n')) == NULL)
            goto done;
        *newline = '\0';
        if (sscanf(line, "%*s %*s %*s %x:%x %llu %n",
                   &device_major, &device_minor, &inode, &offset) != 3 ||
            offset <= 0)
            goto done;
        path = line + offset;
        if (*path != '/' || inode == 0)
            continue;
        deleted = strstr(path, " (deleted)");
        if (deleted != NULL && strcmp(deleted, " (deleted)") == 0)
            *deleted = '\0';
        else
            deleted = NULL;
        basename = strrchr(path, '/') + 1;
        for (i = 0; i < QI_COUNT_FILE_COUNT; ++i)
            if (strcmp(basename, qi_count_names[i]) == 0)
                break;
        if (i == QI_COUNT_FILE_COUNT)
            continue;
        if (deleted != NULL || strlen(path) >= sizeof(mappings[i].path))
            goto done;
        if (mappings[i].path[0] != '\0')
        {
            if (mappings[i].inode != inode ||
                mappings[i].device_major != device_major ||
                mappings[i].device_minor != device_minor ||
                strcmp(mappings[i].path, path) != 0)
                goto done;
        }
        else
        {
            strlcpy(mappings[i].path, path, sizeof(mappings[i].path));
            mappings[i].device_major = device_major;
            mappings[i].device_minor = device_minor;
            mappings[i].inode = inode;
        }
    }
    valid = !ferror(maps);
done:
    fclose(maps);
    return valid;
}

static bool
qi_count_hash_mapping(const QiCountMapping *mapping, char *output)
{
    unsigned char buffer[32768];
    uint8 bytes[PG_SHA256_DIGEST_LENGTH];
    QiCountHash *hash = NULL;
    struct stat before;
    struct stat after;
    uint64 seen = 0;
    bool valid = false;
    int fd;

    if (mapping->path[0] == '\0')
        return false;
    fd = open(mapping->path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
    if (fd < 0)
        return false;
    if (fstat(fd, &before) != 0 || !S_ISREG(before.st_mode) ||
        (unsigned long long) before.st_ino != mapping->inode ||
        major(before.st_dev) != mapping->device_major ||
        minor(before.st_dev) != mapping->device_minor ||
        before.st_size <= 0 || (uint64) before.st_size > QI_COUNT_MAX_BINARY_BYTES)
        goto done;
    hash = qi_count_hash_new();
    if (hash == NULL)
        goto done;
    for (;;)
    {
        ssize_t length = read(fd, buffer, sizeof(buffer));

        if (length < 0 && errno == EINTR)
            continue;
        if (length < 0)
            goto done;
        if (length == 0)
            break;
        seen += (uint64) length;
        if (seen > (uint64) before.st_size ||
            !qi_count_hash_update(hash, buffer, (size_t) length))
            goto done;
    }
    if (fstat(fd, &after) != 0 || seen != (uint64) before.st_size ||
        before.st_size != after.st_size ||
        before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
        before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
        before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
        before.st_ctim.tv_nsec != after.st_ctim.tv_nsec ||
        !qi_count_hash_finish(hash, bytes))
        goto done;
    qi_count_digest_hex(bytes, output);
    valid = true;
done:
    if (hash != NULL)
        qi_count_hash_free(hash);
    close(fd);
    return valid;
}

static void
qi_count_attest_postmaster(void)
{
    QiCountRuntimeIdentity candidate = {0};
    QiCountMapping mappings[QI_COUNT_FILE_COUNT] = {0};
    int i;

    if (!qi_count_maps(mappings))
        return;
    for (i = 0; i < QI_COUNT_FILE_COUNT; ++i)
        if (!qi_count_hash_mapping(&mappings[i], candidate.digest[i]))
            return;
    candidate.observed = true;
    qi_count_runtime = candidate;
}
#endif

void
qi_count_certificate_init(void)
{
    if (!process_shared_preload_libraries_in_progress)
        return;

    DefineCustomBoolVariable("pg_qi.count_certificate_attestation",
                             "Attests the preloaded PostgreSQL and provider binaries for a COUNT(*) certificate.",
                             NULL, &qi_count_cert_attest, false, PGC_POSTMASTER,
                             GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL | GUC_NOT_IN_SAMPLE,
                             NULL, NULL, NULL);
    DefineCustomStringVariable("pg_qi.count_certificate",
                               "Installs one scoped COUNT(*) semantic certificate for shadow observations.",
                               NULL, &qi_count_cert_value, "", PGC_SIGHUP,
                               GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL | GUC_NOT_IN_SAMPLE,
                               NULL, qi_count_certificate_assign, NULL);
    DefineCustomStringVariable("pg_qi.count_certificate_approved_sha256",
                               "Independently approves the exact installed certificate record by SHA-256.",
                               NULL, &qi_count_cert_approved, "", PGC_SIGHUP,
                               GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL | GUC_NOT_IN_SAMPLE,
                               NULL, NULL, NULL);
#if defined(__linux__)
    if (!PG_QI_TESTING && qi_count_cert_attest)
        qi_count_attest_postmaster();
#endif
}

static bool
qi_count_certificate_configured(void)
{
    return !PG_QI_TESTING && qi_count_runtime.observed && qi_count_installed_valid &&
        qi_count_cert_approved != NULL && qi_count_cert_approved[0] != '\0';
}

bool
qi_count_certificate_current(void)
{
    const QiCountCertificate *installed = &qi_count_installed;
    const char *site;
    pg_time_t now;

    if (!qi_count_certificate_configured() ||
        strcmp(qi_count_cert_approved, qi_count_installed_digest) != 0 ||
        installed->postgres_version_num != PG_VERSION_NUM ||
        strcmp(installed->postgres_build, PG_QI_POSTGRES_BUILD_ID) != 0 ||
        strcmp(installed->pg_qi_build, PG_QI_BUILD_ID) != 0 ||
        strcmp(installed->pg_duckdb_source, QI_EXPECTED_PG_DUCKDB_SHA) != 0 ||
        strcmp(installed->duckdb_source, QI_EXPECTED_DUCKDB_SHA) != 0 ||
        strcmp(installed->postgres_binary, qi_count_runtime.digest[QI_COUNT_FILE_POSTGRES]) != 0 ||
        strcmp(installed->pg_qi_binary, qi_count_runtime.digest[QI_COUNT_FILE_PG_QI]) != 0 ||
        strcmp(installed->pg_duckdb_binary, qi_count_runtime.digest[QI_COUNT_FILE_PG_DUCKDB]) != 0 ||
        strcmp(installed->duckdb_binary, qi_count_runtime.digest[QI_COUNT_FILE_DUCKDB]) != 0)
        return false;

    site = GetConfigOption("pg_qi.site_build_id", true, false);
    now = timestamptz_to_time_t(GetCurrentTimestamp());
    return site != NULL && strcmp(site, installed->site_build) == 0 &&
        installed->cluster_identifier == GetSystemIdentifier() && now >= 0 &&
        (uint64) now >= installed->issued_at_unix && (uint64) now < installed->expires_at_unix;
}

bool
qi_count_certificate_bind(QiCountCertificateBinding *binding)
{
    if (!qi_count_certificate_current())
        return false;
    memcpy(binding->approved_sha256, qi_count_installed_digest,
           sizeof(binding->approved_sha256));
    return true;
}
