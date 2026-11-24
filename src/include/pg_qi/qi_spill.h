/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_SPILL_H
#define PG_QI_QI_SPILL_H

#include "postgres.h"

#include "nodes/parsenodes.h"
#include "nodes/plannodes.h"

typedef enum QiSpillState
{
    QI_SPILL_NOT_EVALUATED = 0,
    QI_SPILL_UNKNOWN,
    QI_SPILL_WITHIN_BUDGET,
    QI_SPILL_LIKELY
} QiSpillState;

typedef enum QiSpillReason
{
    QI_SPILL_NOT_OBSERVED = 0,
    QI_SPILL_UNSUPPORTED_PLAN,
    QI_SPILL_UNBOUNDED_WIDTH,
    QI_SPILL_INVALID_ESTIMATE,
    QI_SPILL_BUDGET_UNKNOWN,
    QI_SPILL_ESTIMATE_FITS,
    QI_SPILL_RESOURCES_UNVERIFIED
} QiSpillReason;

typedef enum QiSpillShape
{
    QI_SPILL_NONE = 0,
    QI_SPILL_HASH_AGGREGATE,
    QI_SPILL_DISTINCT,
    QI_SPILL_SORT
} QiSpillShape;

typedef struct QiSpillEvidence
{
    QiSpillState state;
    QiSpillReason reason;
    QiSpillShape shape;
    uint64 estimated_bytes;
    uint64 budget_bytes;
} QiSpillEvidence;

extern bool qi_spill_query_requested(const Query *query);
extern QiSpillEvidence qi_spill_observe(const PlannedStmt *stmt);
extern const char *qi_spill_state_name(QiSpillState state);
extern const char *qi_spill_reason_name(QiSpillReason reason);
extern const char *qi_spill_shape_name(QiSpillShape shape);

#endif /* PG_QI_QI_SPILL_H */
