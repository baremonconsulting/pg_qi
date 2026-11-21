/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_COUNT_CERTIFICATE_H
#define PG_QI_QI_COUNT_CERTIFICATE_H

#include "postgres.h"
#include "pg_qi/qi_count_cert_parser.h"

typedef struct QiCountCertificateBinding
{
    char approved_sha256[QI_COUNT_CERT_SHA256_HEX + 1];
} QiCountCertificateBinding;

extern void qi_count_certificate_init(void);
extern bool qi_count_certificate_current(void);
extern bool qi_count_certificate_bind(QiCountCertificateBinding *binding);

#endif /* PG_QI_QI_COUNT_CERTIFICATE_H */
