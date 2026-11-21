/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "pg_qi/qi_profile_parser.h"

#include <limits.h>
#include <stdbool.h>
#include <string.h>

#define QI_PROFILE_MAX_MEMORY_BYTES (UINT64_C(1) << 50)
#define QI_PROFILE_MAX_TEMP_BYTES (UINT64_C(1) << 50)
#define QI_PROFILE_MAX_THREADS 1024U
#define QI_PROFILE_MAX_CONCURRENCY 65535U
#define QI_PROFILE_MAX_COEFFICIENT UINT64_C(1000000000000)
#define QI_PROFILE_MAX_MARGIN_PPM 1000000U

typedef struct QiProfileCursor
{
    const char *text;
    size_t length;
    size_t position;
} QiProfileCursor;

uint32_t
qi_profile_crc32c(const unsigned char *data, size_t length)
{
    uint32_t crc = UINT32_C(0xffffffff);
    size_t index;
    unsigned int bit;

    for (index = 0; index < length; index++)
    {
        crc ^= data[index];
        for (bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^
                (UINT32_C(0x82f63b78) & (uint32_t) -(int32_t) (crc & 1U));
    }
    return ~crc;
}

static bool
qi_profile_expect(QiProfileCursor *cursor, const char *literal)
{
    size_t literal_length = strlen(literal);

    if (literal_length > cursor->length - cursor->position ||
        memcmp(cursor->text + cursor->position, literal, literal_length) != 0)
        return false;
    cursor->position += literal_length;
    return true;
}

static bool
qi_profile_decimal(QiProfileCursor *cursor, uint64_t maximum,
                   uint64_t *result)
{
    uint64_t value = 0;
    size_t start = cursor->position;

    if (start >= cursor->length || cursor->text[start] < '0' ||
        cursor->text[start] > '9')
        return false;
    if (cursor->text[start] == '0' && start + 1 < cursor->length &&
        cursor->text[start + 1] >= '0' && cursor->text[start + 1] <= '9')
        return false;
    while (cursor->position < cursor->length &&
           cursor->text[cursor->position] >= '0' &&
           cursor->text[cursor->position] <= '9')
    {
        unsigned int digit =
            (unsigned int) (cursor->text[cursor->position] - '0');

        if (value > (maximum - digit) / 10U)
            return false;
        value = value * 10U + digit;
        cursor->position++;
    }
    *result = value;
    return cursor->position > start;
}

static bool
qi_profile_hex(QiProfileCursor *cursor, char *result, size_t digits)
{
    size_t index;

    if (digits > cursor->length - cursor->position)
        return false;
    for (index = 0; index < digits; index++)
    {
        char value = cursor->text[cursor->position + index];

        if (!((value >= '0' && value <= '9') ||
              (value >= 'a' && value <= 'f')))
            return false;
        result[index] = value;
    }
    result[digits] = '\0';
    cursor->position += digits;
    return true;
}

static bool
qi_profile_hex32(QiProfileCursor *cursor, uint32_t *result)
{
    uint32_t value = 0;
    unsigned int index;

    if (8U > cursor->length - cursor->position)
        return false;
    for (index = 0; index < 8U; index++)
    {
        unsigned char byte = (unsigned char) cursor->text[cursor->position++];
        unsigned int digit;

        if (byte >= '0' && byte <= '9')
            digit = byte - '0';
        else if (byte >= 'a' && byte <= 'f')
            digit = byte - 'a' + 10U;
        else
            return false;
        value = (value << 4) | digit;
    }
    *result = value;
    return true;
}

static bool
qi_profile_u32(QiProfileCursor *cursor, uint32_t maximum, uint32_t *result)
{
    uint64_t value;

    if (!qi_profile_decimal(cursor, maximum, &value))
        return false;
    *result = (uint32_t) value;
    return true;
}

QiProfileParseReason
qi_profile_parse(const char *text, size_t length, QiProfileData *output)
{
    QiProfileCursor cursor;
    QiProfileData candidate;
    size_t checksum_body_length;

    if (output == NULL)
        return QI_PROFILE_PARSE_MALFORMED;
    memset(output, 0, sizeof(*output));
    if (text == NULL || length == 0)
        return QI_PROFILE_PARSE_MISSING;
    if (length > QI_PROFILE_MAX_BYTES)
        return QI_PROFILE_PARSE_OVERSIZE;
    if (memchr(text, '\0', length) != NULL)
        return QI_PROFILE_PARSE_MALFORMED;

    memset(&candidate, 0, sizeof(candidate));
    cursor.text = text;
    cursor.length = length;
    cursor.position = 0;

#define EXPECT(value) do { if (!qi_profile_expect(&cursor, (value))) \
        return QI_PROFILE_PARSE_MALFORMED; } while (0)
#define DECIMAL(key, field, maximum) do { EXPECT((key)); \
        if (!qi_profile_decimal(&cursor, (maximum), &(field))) \
            return QI_PROFILE_PARSE_MALFORMED; } while (0)
#define U32(key, field, maximum) do { EXPECT((key)); \
        if (!qi_profile_u32(&cursor, (maximum), &(field))) \
            return QI_PROFILE_PARSE_MALFORMED; } while (0)

    U32("v=", candidate.format_version, UINT32_MAX);
    if (candidate.format_version != QI_PROFILE_FORMAT_VERSION)
        return QI_PROFILE_PARSE_UNSUPPORTED_FORMAT;
    DECIMAL(";gen=", candidate.generation, UINT64_MAX);
    U32(";pg=", candidate.postgres_version_num, UINT32_MAX);
    EXPECT(";pgbuild=");
    if (!qi_profile_hex(&cursor, candidate.postgres_build,
                        QI_PROFILE_SHA256_HEX_BYTES))
        return QI_PROFILE_PARSE_MALFORMED;
    EXPECT(";qibuild=");
    if (!qi_profile_hex(&cursor, candidate.pg_qi_build,
                        QI_PROFILE_SHA1_HEX_BYTES))
        return QI_PROFILE_PARSE_MALFORMED;
    EXPECT(";pgduckdb=");
    if (!qi_profile_hex(&cursor, candidate.pg_duckdb_sha,
                        QI_PROFILE_SHA1_HEX_BYTES))
        return QI_PROFILE_PARSE_MALFORMED;
    EXPECT(";duckdb=");
    if (!qi_profile_hex(&cursor, candidate.duckdb_sha,
                        QI_PROFILE_SHA1_HEX_BYTES))
        return QI_PROFILE_PARSE_MALFORMED;
    EXPECT(";site=");
    if (!qi_profile_hex(&cursor, candidate.site_build,
                        QI_PROFILE_SHA256_HEX_BYTES))
        return QI_PROFILE_PARSE_MALFORMED;
    U32(";contract=", candidate.contract_version, INT32_MAX);
    U32(";rule=", candidate.rule_version, INT32_MAX);
    U32(";model=", candidate.model_version, INT32_MAX);
    U32(";certificate=", candidate.certificate_version, INT32_MAX);
    DECIMAL(";iat=", candidate.issued_at_unix, INT64_MAX);
    DECIMAL(";exp=", candidate.expires_at_unix, INT64_MAX);
    DECIMAL(";memory=", candidate.memory_bytes,
            QI_PROFILE_MAX_MEMORY_BYTES);
    DECIMAL(";temp=", candidate.temp_bytes, QI_PROFILE_MAX_TEMP_BYTES);
    U32(";threads=", candidate.threads, QI_PROFILE_MAX_THREADS);
    U32(";concurrency=", candidate.concurrency,
        QI_PROFILE_MAX_CONCURRENCY);
    DECIMAL(";fixed_us=", candidate.fixed_overhead_us,
            QI_PROFILE_MAX_COEFFICIENT);
    DECIMAL(";pg_ns_cost=", candidate.postgres_ns_per_cost,
            QI_PROFILE_MAX_COEFFICIENT);
    DECIMAL(";scan_ns_kb=", candidate.scan_ns_per_kb,
            QI_PROFILE_MAX_COEFFICIENT);
    DECIMAL(";conv_ns_row=", candidate.conversion_ns_per_row,
            QI_PROFILE_MAX_COEFFICIENT);
    DECIMAL(";output_ns_byte=", candidate.output_ns_per_byte,
            QI_PROFILE_MAX_COEFFICIENT);
    DECIMAL(";p95_us=", candidate.p95_dispersion_us,
            QI_PROFILE_MAX_COEFFICIENT);
    U32(";margin_ppm=", candidate.margin_ppm,
        QI_PROFILE_MAX_MARGIN_PPM);
    checksum_body_length = cursor.position;
    EXPECT(";crc=");
    if (!qi_profile_hex32(&cursor, &candidate.checksum) ||
        cursor.position != cursor.length)
        return QI_PROFILE_PARSE_MALFORMED;

#undef U32
#undef DECIMAL
#undef EXPECT

    if (candidate.generation == 0 || candidate.memory_bytes == 0 ||
        candidate.threads == 0 || candidate.concurrency == 0 ||
        candidate.postgres_ns_per_cost == 0 || candidate.margin_ppm == 0)
        return QI_PROFILE_PARSE_INVALID_LIMIT;
    if (candidate.expires_at_unix <= candidate.issued_at_unix ||
        candidate.expires_at_unix - candidate.issued_at_unix >
            QI_PROFILE_MAX_VALIDITY_SECONDS)
        return QI_PROFILE_PARSE_INVALID_INTERVAL;
    if (candidate.checksum != qi_profile_crc32c(
            (const unsigned char *) text, checksum_body_length))
        return QI_PROFILE_PARSE_CHECKSUM_MISMATCH;

    *output = candidate;
    return QI_PROFILE_PARSE_OK;
}

const char *
qi_profile_parse_reason_name(QiProfileParseReason reason)
{
    switch (reason)
    {
        case QI_PROFILE_PARSE_OK: return "ok";
        case QI_PROFILE_PARSE_MISSING: return "missing";
        case QI_PROFILE_PARSE_OVERSIZE: return "oversize";
        case QI_PROFILE_PARSE_MALFORMED: return "malformed";
        case QI_PROFILE_PARSE_UNSUPPORTED_FORMAT:
            return "unsupported_format";
        case QI_PROFILE_PARSE_CHECKSUM_MISMATCH:
            return "checksum_mismatch";
        case QI_PROFILE_PARSE_INVALID_INTERVAL:
            return "invalid_interval";
        case QI_PROFILE_PARSE_INVALID_LIMIT: return "invalid_limit";
    }
    return "invalid";
}
