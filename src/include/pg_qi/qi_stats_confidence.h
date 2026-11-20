/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_STATS_CONFIDENCE_H
#define PG_QI_QI_STATS_CONFIDENCE_H

#include "postgres.h"

#include "pg_qi/qi_plan_features.h"

typedef enum QiStatsConfidenceState
{
    QI_STATS_CONFIDENCE_UNKNOWN = 0
} QiStatsConfidenceState;

typedef enum QiStatsConfidenceReason
{
    QI_STATS_NOT_OBSERVED = 0,
    QI_STATS_PLAN_FEATURES_UNKNOWN,
    QI_STATS_CATALOG_ESTIMATE_UNKNOWN,
    QI_STATS_COUNTERS_DISABLED,
    QI_STATS_RECOVERY_IN_PROGRESS,
    QI_STATS_OWN_XID,
    QI_STATS_ENTRY_MISSING,
    QI_STATS_ENTRY_BUSY,
    QI_STATS_NO_RECORDED_ANALYZE,
    QI_STATS_MODIFICATIONS_REPORTED,
    QI_STATS_FRESHNESS_UNPROVEN,
    QI_STATS_FUTURE_ANALYZE,
    QI_STATS_INVALID_COUNTER
} QiStatsConfidenceReason;

typedef enum QiStatsAnalyzeSource
{
    QI_STATS_ANALYZE_NONE = 0,
    QI_STATS_ANALYZE_MANUAL,
    QI_STATS_ANALYZE_AUTO
} QiStatsAnalyzeSource;

#define QI_STATS_KNOWN_RELATION       (UINT64CONST(1) << 0)
#define QI_STATS_KNOWN_PLAN_ESTIMATE  (UINT64CONST(1) << 1)
#define QI_STATS_KNOWN_CATALOG_SIZE   (UINT64CONST(1) << 2)
#define QI_STATS_KNOWN_MODIFICATIONS  (UINT64CONST(1) << 3)
#define QI_STATS_KNOWN_ANALYZE_SOURCE (UINT64CONST(1) << 4)
#define QI_STATS_KNOWN_ANALYZE_AGE    (UINT64CONST(1) << 5)

typedef struct QiStatsConfidence
{
    QiStatsConfidenceState state;
    QiStatsConfidenceReason reason;
    QiStatsAnalyzeSource analyze_source;
    uint64 known_fields;
    Oid relation_oid;
    int32 relpages;
    double reltuples;
    double scan_rows;
    int32 scan_width;
    int64 modifications_since_analyze;
    int64 analyze_age_ms;
} QiStatsConfidence;

extern QiStatsConfidence qi_stats_confidence_unknown(
    QiStatsConfidenceReason reason);
extern QiStatsConfidence qi_stats_confidence_observe(
    const QiNativePlanFeatures *features);
extern const char *qi_stats_confidence_state_name(
    QiStatsConfidenceState state);
extern const char *qi_stats_confidence_reason_name(
    QiStatsConfidenceReason reason);
extern const char *qi_stats_analyze_source_name(QiStatsAnalyzeSource source);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_stats_confidence_contract(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_STATS_CONFIDENCE_H */
