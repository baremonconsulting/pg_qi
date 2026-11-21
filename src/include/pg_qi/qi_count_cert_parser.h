/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_COUNT_CERT_PARSER_H
#define PG_QI_QI_COUNT_CERT_PARSER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define QI_COUNT_CERT_SCOPE "count_star_serial_rr_ro"
#define QI_COUNT_CERT_MAX_BYTES 1024
#define QI_COUNT_CERT_SHA1_HEX 40
#define QI_COUNT_CERT_SHA256_HEX 64
#define QI_COUNT_CERT_MAX_LIFETIME (UINT64_C(90) * 24 * 60 * 60)

typedef struct QiCountCertificate
{
    uint32_t postgres_version_num;
    uint64_t cluster_identifier;
    uint64_t issued_at_unix;
    uint64_t expires_at_unix;
    char site_build[QI_COUNT_CERT_SHA256_HEX + 1];
    char postgres_build[QI_COUNT_CERT_SHA256_HEX + 1];
    char pg_qi_build[QI_COUNT_CERT_SHA1_HEX + 1];
    char pg_duckdb_source[QI_COUNT_CERT_SHA1_HEX + 1];
    char duckdb_source[QI_COUNT_CERT_SHA1_HEX + 1];
    char postgres_binary[QI_COUNT_CERT_SHA256_HEX + 1];
    char pg_qi_binary[QI_COUNT_CERT_SHA256_HEX + 1];
    char pg_duckdb_binary[QI_COUNT_CERT_SHA256_HEX + 1];
    char duckdb_binary[QI_COUNT_CERT_SHA256_HEX + 1];
    char evidence_digest[QI_COUNT_CERT_SHA256_HEX + 1];
} QiCountCertificate;

extern bool qi_count_cert_parse(const char *text, size_t length,
                                QiCountCertificate *output);

#endif /* PG_QI_QI_COUNT_CERT_PARSER_H */
