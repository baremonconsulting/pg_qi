/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_PROFILE_PARSER_H
#define PG_QI_QI_PROFILE_PARSER_H

#include <stddef.h>
#include <stdint.h>

#define QI_PROFILE_FORMAT_VERSION 1U
#define QI_PROFILE_MAX_BYTES 2048U
#define QI_PROFILE_SHA1_HEX_BYTES 40U
#define QI_PROFILE_SHA256_HEX_BYTES 64U
#define QI_PROFILE_MAX_VALIDITY_SECONDS (90U * 24U * 60U * 60U)

typedef enum QiProfileParseReason
{
    QI_PROFILE_PARSE_OK = 0,
    QI_PROFILE_PARSE_MISSING,
    QI_PROFILE_PARSE_OVERSIZE,
    QI_PROFILE_PARSE_MALFORMED,
    QI_PROFILE_PARSE_UNSUPPORTED_FORMAT,
    QI_PROFILE_PARSE_CHECKSUM_MISMATCH,
    QI_PROFILE_PARSE_INVALID_INTERVAL,
    QI_PROFILE_PARSE_INVALID_LIMIT
} QiProfileParseReason;

typedef struct QiProfileData
{
    uint32_t format_version;
    uint64_t generation;
    uint32_t postgres_version_num;
    char postgres_build[QI_PROFILE_SHA256_HEX_BYTES + 1U];
    char pg_qi_build[QI_PROFILE_SHA1_HEX_BYTES + 1U];
    char pg_duckdb_sha[QI_PROFILE_SHA1_HEX_BYTES + 1U];
    char duckdb_sha[QI_PROFILE_SHA1_HEX_BYTES + 1U];
    char site_build[QI_PROFILE_SHA256_HEX_BYTES + 1U];
    uint32_t contract_version;
    uint32_t rule_version;
    uint32_t model_version;
    uint32_t certificate_version;
    uint64_t issued_at_unix;
    uint64_t expires_at_unix;
    uint64_t memory_bytes;
    uint64_t temp_bytes;
    uint32_t threads;
    uint32_t concurrency;
    uint64_t fixed_overhead_us;
    uint64_t scan_ns_per_kb;
    uint64_t conversion_ns_per_row;
    uint32_t margin_ppm;
    uint32_t checksum;
} QiProfileData;

extern uint32_t qi_profile_crc32c(const unsigned char *data, size_t length);
extern QiProfileParseReason qi_profile_parse(const char *text, size_t length,
                                             QiProfileData *output);
extern const char *qi_profile_parse_reason_name(QiProfileParseReason reason);

#endif /* PG_QI_QI_PROFILE_PARSER_H */
