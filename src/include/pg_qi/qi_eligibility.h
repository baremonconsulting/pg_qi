/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_ELIGIBILITY_H
#define PG_QI_QI_ELIGIBILITY_H

#include "postgres.h"
#include "nodes/parsenodes.h"

typedef enum QiEligibilityReason
{
    QI_ELIGIBILITY_CANDIDATE = 0,
    QI_ELIGIBILITY_PROTOTYPE_REJECTED,
    QI_ELIGIBILITY_SECURITY_CONTEXT,
    QI_ELIGIBILITY_TABLE_SAMPLE,
    QI_ELIGIBILITY_INHERITANCE,
    QI_ELIGIBILITY_UNSUPPORTED_RELATION,
    QI_ELIGIBILITY_UNSUPPORTED_JOIN,
    QI_ELIGIBILITY_UNSUPPORTED_AGGREGATE,
    QI_ELIGIBILITY_UNSUPPORTED_GROUPING,
    QI_ELIGIBILITY_UNSUPPORTED_ORDERING,
    QI_ELIGIBILITY_UNSUPPORTED_PARAMETER,
    QI_ELIGIBILITY_UNSUPPORTED_FUNCTION,
    QI_ELIGIBILITY_UNSUPPORTED_CAST,
    QI_ELIGIBILITY_UNSUPPORTED_EXPRESSION,
    QI_ELIGIBILITY_INVALID_VAR
} QiEligibilityReason;

typedef struct QiEligibilityResult
{
    bool candidate;
    QiEligibilityReason reason;
    int prototype_reason;
} QiEligibilityResult;

extern QiEligibilityResult qi_eligibility_query(const Query *query);
extern const char *qi_eligibility_reason_name(QiEligibilityReason reason);

#endif /* PG_QI_QI_ELIGIBILITY_H */
