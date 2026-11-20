/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <math.h>

#include "access/xact.h"
#include "access/xlog.h"
#include "catalog/catalog.h"
#include "catalog/pg_class_d.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "pg_qi/qi_stats_confidence.h"
#include "pgstat.h"
#include "utils/pgstat_internal.h"
#include "utils/timestamp.h"

static bool
qi_stats_read_shared(Oid relation_oid, PgStat_StatTabEntry *stats,
                     bool *lock_busy)
{
    PgStat_EntryRef *entry;
    PgStatShared_Relation *shared;
    Oid database_oid;

    *lock_busy = false;
    database_oid = IsSharedRelation(relation_oid) ? InvalidOid : MyDatabaseId;
    entry = pgstat_get_entry_ref(PGSTAT_KIND_RELATION, database_oid,
                                 relation_oid, false, NULL);
    if (entry == NULL)
        return false;
    if (!pgstat_lock_entry_shared(entry, true))
    {
        *lock_busy = true;
        return false;
    }
    shared = (PgStatShared_Relation *) entry->shared_stats;
    *stats = shared->stats;
    pgstat_unlock_entry(entry);
    return true;
}

QiStatsConfidence
qi_stats_confidence_unknown(QiStatsConfidenceReason reason)
{
    QiStatsConfidence confidence;

    MemSet(&confidence, 0, sizeof(confidence));
    confidence.state = QI_STATS_CONFIDENCE_UNKNOWN;
    confidence.reason = reason;
    confidence.analyze_source = QI_STATS_ANALYZE_NONE;
    confidence.relation_oid = InvalidOid;
    confidence.relpages = -1;
    confidence.reltuples = -1.0;
    confidence.scan_rows = -1.0;
    confidence.scan_width = -1;
    confidence.modifications_since_analyze = -1;
    confidence.analyze_age_ms = -1;
    return confidence;
}

static QiStatsConfidence
qi_stats_confidence_classify(const QiNativePlanFeatures *features,
                             const PgStat_StatTabEntry *stats,
                             bool counters_enabled,
                             bool recovery_in_progress,
                             bool own_xid,
                             bool entry_busy,
                             TimestampTz now)
{
    QiStatsConfidence confidence =
        qi_stats_confidence_unknown(QI_STATS_NOT_OBSERVED);
    TimestampTz analyzed_at;

    if (features == NULL ||
        features->state != QI_PLAN_FEATURE_BOUNDED ||
        features->reason != QI_PLAN_FEATURE_OK ||
        !OidIsValid(features->relation_oid))
    {
        confidence.reason = QI_STATS_PLAN_FEATURES_UNKNOWN;
        return confidence;
    }

    confidence.relation_oid = features->relation_oid;
    confidence.known_fields |= QI_STATS_KNOWN_RELATION;
    if ((features->known_fields & QI_PLAN_KNOWN_SCAN_ESTIMATE) != 0 &&
        isfinite(features->scan.rows) && features->scan.rows >= 0.0 &&
        features->scan.width >= 0)
    {
        confidence.scan_rows = features->scan.rows;
        confidence.scan_width = features->scan.width;
        confidence.known_fields |= QI_STATS_KNOWN_PLAN_ESTIMATE;
    }
    if (!counters_enabled)
    {
        confidence.reason = QI_STATS_COUNTERS_DISABLED;
        return confidence;
    }
    if (recovery_in_progress)
    {
        confidence.reason = QI_STATS_RECOVERY_IN_PROGRESS;
        return confidence;
    }
    if (own_xid)
    {
        confidence.reason = QI_STATS_OWN_XID;
        return confidence;
    }
    if (stats == NULL)
    {
        confidence.reason = entry_busy ? QI_STATS_ENTRY_BUSY :
            QI_STATS_ENTRY_MISSING;
        return confidence;
    }
    if (stats->mod_since_analyze < 0)
    {
        confidence.reason = QI_STATS_INVALID_COUNTER;
        return confidence;
    }
    confidence.modifications_since_analyze = stats->mod_since_analyze;
    confidence.known_fields |= QI_STATS_KNOWN_MODIFICATIONS;

    if ((features->known_fields & QI_PLAN_KNOWN_RELPAGES) == 0 ||
        (features->known_fields & QI_PLAN_KNOWN_RELTUPLES) == 0 ||
        features->relpages < 0 || !isfinite(features->reltuples) ||
        features->reltuples < 0.0)
    {
        confidence.reason = QI_STATS_CATALOG_ESTIMATE_UNKNOWN;
        return confidence;
    }
    confidence.relpages = features->relpages;
    confidence.reltuples = features->reltuples;
    confidence.known_fields |= QI_STATS_KNOWN_CATALOG_SIZE;

    if (stats->last_analyze_time == 0 &&
        stats->last_autoanalyze_time == 0)
    {
        confidence.reason = QI_STATS_NO_RECORDED_ANALYZE;
        return confidence;
    }
    if (stats->last_analyze_time >= stats->last_autoanalyze_time)
    {
        analyzed_at = stats->last_analyze_time;
        confidence.analyze_source = QI_STATS_ANALYZE_MANUAL;
    }
    else
    {
        analyzed_at = stats->last_autoanalyze_time;
        confidence.analyze_source = QI_STATS_ANALYZE_AUTO;
    }
    confidence.known_fields |= QI_STATS_KNOWN_ANALYZE_SOURCE;

    if (analyzed_at > now)
    {
        confidence.reason = QI_STATS_FUTURE_ANALYZE;
        return confidence;
    }
    confidence.analyze_age_ms = (now - analyzed_at) / 1000;
    confidence.known_fields |= QI_STATS_KNOWN_ANALYZE_AGE;

    if (confidence.modifications_since_analyze != 0)
    {
        confidence.reason = QI_STATS_MODIFICATIONS_REPORTED;
        return confidence;
    }

    /*
     * Shared counters are asynchronous.  Zero means only that no changes
     * have reached the shared entry; it cannot prove global freshness.
     */
    confidence.reason = QI_STATS_FRESHNESS_UNPROVEN;
    return confidence;
}

QiStatsConfidence
qi_stats_confidence_observe(const QiNativePlanFeatures *features)
{
    PgStat_StatTabEntry stats;
    const PgStat_StatTabEntry *stats_pointer = NULL;
    bool lock_busy = false;
    bool own_xid = TransactionIdIsValid(GetTopTransactionIdIfAny());
    bool recovery_in_progress = RecoveryInProgress();

    if (pgstat_track_counts && !recovery_in_progress && !own_xid &&
        features != NULL && features->state == QI_PLAN_FEATURE_BOUNDED &&
        features->reason == QI_PLAN_FEATURE_OK &&
        OidIsValid(features->relation_oid) &&
        qi_stats_read_shared(features->relation_oid, &stats, &lock_busy))
        stats_pointer = &stats;
    return qi_stats_confidence_classify(
        features, stats_pointer, pgstat_track_counts, recovery_in_progress,
        own_xid, lock_busy, GetCurrentTimestamp());
}

const char *
qi_stats_confidence_state_name(QiStatsConfidenceState state)
{
    return state == QI_STATS_CONFIDENCE_UNKNOWN ? "unknown" : "invalid";
}

const char *
qi_stats_confidence_reason_name(QiStatsConfidenceReason reason)
{
    switch (reason)
    {
        case QI_STATS_NOT_OBSERVED: return "not_observed";
        case QI_STATS_PLAN_FEATURES_UNKNOWN: return "plan_features_unknown";
        case QI_STATS_CATALOG_ESTIMATE_UNKNOWN:
            return "catalog_estimate_unknown";
        case QI_STATS_COUNTERS_DISABLED: return "counters_disabled";
        case QI_STATS_RECOVERY_IN_PROGRESS: return "recovery_in_progress";
        case QI_STATS_OWN_XID: return "own_xid";
        case QI_STATS_ENTRY_MISSING: return "stats_entry_missing";
        case QI_STATS_ENTRY_BUSY: return "stats_entry_busy";
        case QI_STATS_NO_RECORDED_ANALYZE: return "no_recorded_analyze";
        case QI_STATS_MODIFICATIONS_REPORTED:
            return "modifications_reported";
        case QI_STATS_FRESHNESS_UNPROVEN: return "freshness_unproven";
        case QI_STATS_FUTURE_ANALYZE: return "future_analyze";
        case QI_STATS_INVALID_COUNTER: return "invalid_counter";
    }
    return "invalid";
}

const char *
qi_stats_analyze_source_name(QiStatsAnalyzeSource source)
{
    switch (source)
    {
        case QI_STATS_ANALYZE_NONE: return "none";
        case QI_STATS_ANALYZE_MANUAL: return "manual";
        case QI_STATS_ANALYZE_AUTO: return "auto";
    }
    return "invalid";
}

#if PG_QI_TESTING
Datum
qi_test_stats_confidence_contract(PG_FUNCTION_ARGS)
{
    QiNativePlanFeatures features;
    PgStat_StatTabEntry stats;
    QiStatsConfidence result;
    TimestampTz now = INT64CONST(2000000);
    bool ok = true;

    MemSet(&features, 0, sizeof(features));
    MemSet(&stats, 0, sizeof(stats));
    features.state = QI_PLAN_FEATURE_BOUNDED;
    features.reason = QI_PLAN_FEATURE_OK;
    features.relation_oid = RelationRelationId;
    features.known_fields = QI_PLAN_KNOWN_RELATION |
        QI_PLAN_KNOWN_SCAN_ESTIMATE | QI_PLAN_KNOWN_RELPAGES |
        QI_PLAN_KNOWN_RELTUPLES;
    features.relpages = 10;
    features.reltuples = 100.0;
    features.scan.rows = 100.0;
    features.scan.width = 4;

    result = qi_stats_confidence_classify(
        NULL, NULL, true, false, false, false, now);
    ok = ok && result.reason == QI_STATS_PLAN_FEATURES_UNKNOWN &&
        result.known_fields == 0 && result.relation_oid == InvalidOid &&
        result.analyze_source == QI_STATS_ANALYZE_NONE;
    features.state = QI_PLAN_FEATURE_UNKNOWN;
    result = qi_stats_confidence_classify(
        &features, NULL, true, false, false, false, now);
    ok = ok && result.reason == QI_STATS_PLAN_FEATURES_UNKNOWN;
    features.state = QI_PLAN_FEATURE_BOUNDED;
    features.known_fields &= ~QI_PLAN_KNOWN_RELTUPLES;
    stats.mod_since_analyze = 7;
    result = qi_stats_confidence_classify(
        &features, &stats, true, false, false, false, now);
    ok = ok && result.reason == QI_STATS_CATALOG_ESTIMATE_UNKNOWN &&
        result.modifications_since_analyze == 7 &&
        (result.known_fields & QI_STATS_KNOWN_MODIFICATIONS) != 0;
    features.known_fields |= QI_PLAN_KNOWN_RELTUPLES;
    stats.mod_since_analyze = 0;
    result = qi_stats_confidence_classify(
        &features, NULL, false, false, false, false, now);
    ok = ok && result.reason == QI_STATS_COUNTERS_DISABLED;
    result = qi_stats_confidence_classify(
        &features, NULL, true, true, false, false, now);
    ok = ok && result.reason == QI_STATS_RECOVERY_IN_PROGRESS;
    result = qi_stats_confidence_classify(
        &features, NULL, true, false, true, false, now);
    ok = ok && result.reason == QI_STATS_OWN_XID;
    result = qi_stats_confidence_classify(
        &features, NULL, true, false, false, false, now);
    ok = ok && result.reason == QI_STATS_ENTRY_MISSING;
    result = qi_stats_confidence_classify(
        &features, NULL, true, false, false, true, now);
    ok = ok && result.reason == QI_STATS_ENTRY_BUSY;
    result = qi_stats_confidence_classify(
        &features, &stats, true, false, false, false, now);
    ok = ok && result.reason == QI_STATS_NO_RECORDED_ANALYZE;
    stats.mod_since_analyze = -1;
    result = qi_stats_confidence_classify(
        &features, &stats, true, false, false, false, now);
    ok = ok && result.reason == QI_STATS_INVALID_COUNTER;
    stats.mod_since_analyze = 1;
    stats.last_analyze_time = INT64CONST(1000000);
    result = qi_stats_confidence_classify(
        &features, &stats, true, false, false, false, now);
    ok = ok && result.reason == QI_STATS_MODIFICATIONS_REPORTED &&
        result.analyze_source == QI_STATS_ANALYZE_MANUAL;
    stats.mod_since_analyze = 0;
    stats.last_analyze_time = 0;
    stats.last_autoanalyze_time = INT64CONST(1000000);
    result = qi_stats_confidence_classify(
        &features, &stats, true, false, false, false, now);
    ok = ok && result.reason == QI_STATS_FRESHNESS_UNPROVEN &&
        result.state == QI_STATS_CONFIDENCE_UNKNOWN &&
        result.analyze_source == QI_STATS_ANALYZE_AUTO &&
        result.analyze_age_ms == 1000 &&
        result.known_fields == (QI_STATS_KNOWN_RELATION |
                                QI_STATS_KNOWN_PLAN_ESTIMATE |
                                QI_STATS_KNOWN_CATALOG_SIZE |
                                QI_STATS_KNOWN_MODIFICATIONS |
                                QI_STATS_KNOWN_ANALYZE_SOURCE |
                                QI_STATS_KNOWN_ANALYZE_AGE);
    stats.last_analyze_time = INT64CONST(1500000);
    stats.last_autoanalyze_time = INT64CONST(1500000);
    result = qi_stats_confidence_classify(
        &features, &stats, true, false, false, false, now);
    ok = ok && result.analyze_source == QI_STATS_ANALYZE_MANUAL;
    stats.last_autoanalyze_time = INT64CONST(1750000);
    result = qi_stats_confidence_classify(
        &features, &stats, true, false, false, false, now);
    ok = ok && result.analyze_source == QI_STATS_ANALYZE_AUTO;
    stats.last_autoanalyze_time = INT64CONST(3000000);
    result = qi_stats_confidence_classify(
        &features, &stats, true, false, false, false, now);
    ok = ok && result.reason == QI_STATS_FUTURE_ANALYZE;

    PG_RETURN_BOOL(ok);
}
#endif
