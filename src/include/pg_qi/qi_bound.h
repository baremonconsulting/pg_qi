/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_BOUND_H
#define PG_QI_QI_BOUND_H

#include "postgres.h"

#include "nodes/params.h"
#include "nodes/parsenodes.h"
#include "pg_qi/qi_decision_v2.h"

#define QI_BOUND_KEY_LENGTH 64

typedef struct QiBoundAssessment
{
    QiGateState eligible;
    QiGateState semantically_safe;
    QiGateState beneficial;
    char key[QI_BOUND_KEY_LENGTH + 1];
    Query *provider_query;
} QiBoundAssessment;

extern void qi_bound_init_gucs(void);
extern QiBoundAssessment qi_bound_assess(Query *query, const char *source,
                                         ParamListInfo params);

#endif /* PG_QI_QI_BOUND_H */
