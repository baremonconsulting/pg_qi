/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_PLAN_FEATURES_H
#define PG_QI_QI_PLAN_FEATURES_H

#include "postgres.h"

#include "nodes/plannodes.h"
#include "pg_qi/qi_shadow_p0.h"

#define QI_PLAN_FEATURE_MAX_RTES 16

typedef enum QiPlanFeatureState
{
    QI_PLAN_FEATURE_UNKNOWN = 0,
    QI_PLAN_FEATURE_BOUNDED
} QiPlanFeatureState;

typedef enum QiPlanFeatureReason
{
    QI_PLAN_FEATURE_NOT_OBSERVED = 0,
    QI_PLAN_FEATURE_OK,
    QI_PLAN_FEATURE_P0_UNKNOWN,
    QI_PLAN_FEATURE_RTE_LIMIT,
    QI_PLAN_FEATURE_AUXILIARY_PLAN,
    QI_PLAN_FEATURE_UNSUPPORTED_ROOT,
    QI_PLAN_FEATURE_UNSUPPORTED_AGGREGATE,
    QI_PLAN_FEATURE_PARTIAL_AGGREGATE,
    QI_PLAN_FEATURE_FINAL_AGGREGATE,
    QI_PLAN_FEATURE_SORT,
    QI_PLAN_FEATURE_GATHER,
    QI_PLAN_FEATURE_GATHER_MERGE,
    QI_PLAN_FEATURE_LIMIT_NODE,
    QI_PLAN_FEATURE_CUSTOM_SCAN,
    QI_PLAN_FEATURE_ALTERNATE_SCAN,
    QI_PLAN_FEATURE_UNSUPPORTED_SCAN,
    QI_PLAN_FEATURE_RELATION_MISMATCH,
    QI_PLAN_FEATURE_CATALOG_MISS,
    QI_PLAN_FEATURE_INVALID_ESTIMATE,
    QI_PLAN_FEATURE_DISABLED_PATH,
    QI_PLAN_FEATURE_MALFORMED
} QiPlanFeatureReason;

typedef enum QiPlanFeatureSpine
{
    QI_PLAN_SPINE_NONE = 0,
    QI_PLAN_SPINE_PLAIN_AGG_SEQ_SCAN
} QiPlanFeatureSpine;

#define QI_PLAN_KNOWN_RELATION        (UINT64CONST(1) << 0)
#define QI_PLAN_KNOWN_AGG_ESTIMATE    (UINT64CONST(1) << 1)
#define QI_PLAN_KNOWN_SCAN_ESTIMATE   (UINT64CONST(1) << 2)
#define QI_PLAN_KNOWN_RELPAGES        (UINT64CONST(1) << 3)
#define QI_PLAN_KNOWN_RELTUPLES       (UINT64CONST(1) << 4)
#define QI_PLAN_KNOWN_PARALLEL        (UINT64CONST(1) << 5)
#define QI_PLAN_KNOWN_WORKERS         (UINT64CONST(1) << 6)
#define QI_PLAN_KNOWN_LIMIT           (UINT64CONST(1) << 7)

typedef struct QiPlanNodeEstimate
{
    double startup_cost;
    double total_cost;
    double rows;
    int32 width;
} QiPlanNodeEstimate;

typedef struct QiNativePlanFeatures
{
    QiPlanFeatureState state;
    QiPlanFeatureReason reason;
    QiPlanFeatureSpine spine;
    uint64 known_fields;
    Oid relation_oid;
    QiPlanNodeEstimate aggregate;
    QiPlanNodeEstimate scan;
    int32 relpages;
    double reltuples;
    bool aggregate_parallel_safe;
    bool scan_parallel_safe;
    bool parallel_aware;
    bool parallel_mode_needed;
    int32 workers_planned;
    bool has_limit;
    uint16 nodes_seen;
    uint16 max_depth_seen;
} QiNativePlanFeatures;

extern QiNativePlanFeatures qi_plan_features_unknown(QiPlanFeatureReason reason);
extern QiNativePlanFeatures qi_plan_features_observe(
    const PlannedStmt *stmt, const QiShadowP0Facts *p0);
extern const char *qi_plan_feature_state_name(QiPlanFeatureState state);
extern const char *qi_plan_feature_reason_name(QiPlanFeatureReason reason);
extern const char *qi_plan_feature_spine_name(QiPlanFeatureSpine spine);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_plan_features_contract(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_PLAN_FEATURES_H */
