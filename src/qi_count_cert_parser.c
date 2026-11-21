/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "pg_qi/qi_count_cert_parser.h"

#include <string.h>

typedef struct QiCountCertCursor
{
    const char *next;
    size_t remaining;
} QiCountCertCursor;

static bool
qi_count_cert_literal(QiCountCertCursor *cursor, const char *expected)
{
    size_t length = strlen(expected);

    if (cursor->remaining < length ||
        memcmp(cursor->next, expected, length) != 0)
        return false;
    cursor->next += length;
    cursor->remaining -= length;
    return true;
}

static bool
qi_count_cert_unsigned(QiCountCertCursor *cursor, uint64_t *result)
{
    uint64_t value = 0;

    /* All numeric fields are strictly positive and canonical. */
    if (cursor->remaining == 0 || cursor->next[0] < '1' ||
        cursor->next[0] > '9')
        return false;
    while (cursor->remaining != 0 && cursor->next[0] >= '0' &&
           cursor->next[0] <= '9')
    {
        uint64_t digit = (uint64_t) (cursor->next[0] - '0');

        if (value > (UINT64_MAX - digit) / 10)
            return false;
        value = value * 10 + digit;
        cursor->next++;
        cursor->remaining--;
    }
    *result = value;
    return true;
}

static bool
qi_count_cert_hex(QiCountCertCursor *cursor, char *result, size_t width)
{
    bool nonzero = false;
    size_t index;

    if (cursor->remaining < width)
        return false;
    for (index = 0; index < width; index++)
    {
        char c = cursor->next[index];

        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            return false;
        if (c != '0')
            nonzero = true;
    }
    if (!nonzero)
        return false;
    memcpy(result, cursor->next, width);
    result[width] = '\0';
    cursor->next += width;
    cursor->remaining -= width;
    return true;
}

bool
qi_count_cert_parse(const char *text, size_t length,
                    QiCountCertificate *output)
{
    QiCountCertCursor cursor = {.next = text, .remaining = length};
    QiCountCertificate candidate = {0};
    uint64_t version;

    if (output == NULL)
        return false;
    memset(output, 0, sizeof(*output));
    if (text == NULL || length == 0 || length > QI_COUNT_CERT_MAX_BYTES ||
        memchr(text, '\0', length) != NULL)
        return false;

#define LITERAL(value) qi_count_cert_literal(&cursor, value)
#define NUMBER(field) qi_count_cert_unsigned(&cursor, &(field))
#define HEX(field) qi_count_cert_hex(&cursor, candidate.field, sizeof(candidate.field) - 1)
    if (!LITERAL("v=1;scope=" QI_COUNT_CERT_SCOPE ";pg=") ||
        !NUMBER(version) || version > UINT32_MAX ||
        !LITERAL(";cluster=") || !NUMBER(candidate.cluster_identifier) ||
        !LITERAL(";site=") || !HEX(site_build) ||
        !LITERAL(";pg_build=") || !HEX(postgres_build) ||
        !LITERAL(";qi_build=") || !HEX(pg_qi_build) ||
        !LITERAL(";provider_src=") || !HEX(pg_duckdb_source) ||
        !LITERAL(";duckdb_src=") || !HEX(duckdb_source) ||
        !LITERAL(";pg_bin=") || !HEX(postgres_binary) ||
        !LITERAL(";qi_bin=") || !HEX(pg_qi_binary) ||
        !LITERAL(";provider_bin=") || !HEX(pg_duckdb_binary) ||
        !LITERAL(";duckdb_bin=") || !HEX(duckdb_binary) ||
        !LITERAL(";proof=") || !HEX(evidence_digest) ||
        !LITERAL(";issued=") || !NUMBER(candidate.issued_at_unix) ||
        !LITERAL(";expires=") || !NUMBER(candidate.expires_at_unix) ||
        cursor.remaining != 0 ||
        candidate.expires_at_unix <= candidate.issued_at_unix ||
        candidate.expires_at_unix - candidate.issued_at_unix >
            QI_COUNT_CERT_MAX_LIFETIME)
        return false;
#undef HEX
#undef NUMBER
#undef LITERAL

    candidate.postgres_version_num = (uint32_t) version;
    *output = candidate;
    return true;
}
