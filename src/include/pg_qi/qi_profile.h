/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_PROFILE_H
#define PG_QI_QI_PROFILE_H

#include "postgres.h"

#include "fmgr.h"
#include "pg_qi/qi_profile_parser.h"
#include "datatype/timestamp.h"

typedef enum QiProfileReason
{
    QI_PROFILE_NOT_OBSERVED = 0,
    QI_PROFILE_MISSING,
    QI_PROFILE_OVERSIZE,
    QI_PROFILE_MALFORMED,
    QI_PROFILE_UNSUPPORTED_FORMAT,
    QI_PROFILE_CHECKSUM_MISMATCH,
    QI_PROFILE_INVALID_INTERVAL,
    QI_PROFILE_INVALID_LIMIT,
    QI_PROFILE_WRONG_POSTGRES_BUILD,
    QI_PROFILE_WRONG_PG_QI_BUILD,
    QI_PROFILE_WRONG_PROVIDER_BUILD,
    QI_PROFILE_WRONG_SITE_BUILD,
    QI_PROFILE_WRONG_VERSIONS,
    QI_PROFILE_NOT_YET_VALID,
    QI_PROFILE_EXPIRED,
    QI_PROFILE_PRESENT
} QiProfileReason;

typedef struct QiProfileEvidence
{
    QiProfileReason reason;
    bool profile_present;
    bool limits_verified;
    bool envelope_hypothetical;
    uint64 generation;
    uint64 install_sequence;
    uint64 expires_at_unix;
    uint32 fingerprint;
} QiProfileEvidence;

extern void qi_profile_init_gucs(void);
extern QiProfileEvidence qi_profile_not_observed(void);
extern QiProfileEvidence qi_profile_observe(TimestampTz now);
extern const char *qi_profile_reason_name(QiProfileReason reason);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_profile_contract(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_test_profile_parse(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_PROFILE_H */
