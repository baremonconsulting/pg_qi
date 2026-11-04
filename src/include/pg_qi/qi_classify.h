/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_CLASSIFY_H
#define PG_QI_QI_CLASSIFY_H

#include "postgres.h"
#include "fmgr.h"
#include "nodes/parsenodes.h"

typedef enum QiClassifyReason
{
    QI_CLASSIFY_ELIGIBLE = 0,
    QI_CLASSIFY_NOT_SELECT,
    QI_CLASSIFY_UNSUPPORTED_QUERY_SHAPE,
    QI_CLASSIFY_UNSUPPORTED_RELATION,
    QI_CLASSIFY_UNSUPPORTED_JOIN,
    QI_CLASSIFY_UNSUPPORTED_NODE,
    QI_CLASSIFY_UNSUPPORTED_TYPE,
    QI_CLASSIFY_UNSUPPORTED_OPERATOR,
    QI_CLASSIFY_UNSUPPORTED_AGGREGATE,
    QI_CLASSIFY_CATALOG_MISMATCH
} QiClassifyReason;

typedef struct QiClassifyResult
{
    bool eligible;
    QiClassifyReason reason;
} QiClassifyResult;

extern QiClassifyResult qi_classify_query(const Query *query);
extern const char *qi_classify_reason_name(QiClassifyReason reason);

#if PG_QI_TESTING
extern void qi_classify_init_test_gucs(void);
extern void qi_classify_record_test_result(QiClassifyResult result);
extern PGDLLEXPORT Datum qi_test_classifier_contract(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_CLASSIFY_H */
