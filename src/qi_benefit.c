/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <math.h>

#include "fmgr.h"
#include "optimizer/cost.h"
#include "optimizer/optimizer.h"
#include "pg_qi/qi_benefit.h"

#define QI_BENEFIT_MAX_EXACT_DOUBLE UINT64CONST(9007199254740992)

static QiBenefitResult
qi_benefit_result(QiBenefitState state, QiBenefitReason reason)
{
    QiBenefitResult result;

    MemSet(&result, 0, sizeof(result));
    result.state = state;
    result.reason = reason;
    return result;
}

static bool
qi_floor_double(double value, uint64_t *out)
{
    if (!isfinite(value) || value < 0.0 ||
        value >= (double) QI_BENEFIT_MAX_EXACT_DOUBLE)
        return false;
    *out = (uint64) floor(value);
    return true;
}

static bool
qi_ceil_double(double value, uint64_t *out)
{
    if (!isfinite(value) || value < 0.0 ||
        value >= (double) QI_BENEFIT_MAX_EXACT_DOUBLE)
        return false;
    *out = (uint64) ceil(value);
    return true;
}

static bool
qi_stats_proven_confident(QiStatsConfidenceState state)
{
    switch (state)
    {
        case QI_STATS_CONFIDENCE_UNKNOWN:
            return false;
    }
    return false;
}

static bool
qi_cost_settings_supported(void)
{
    return seq_page_cost == DEFAULT_SEQ_PAGE_COST &&
        cpu_tuple_cost == DEFAULT_CPU_TUPLE_COST &&
        cpu_operator_cost == DEFAULT_CPU_OPERATOR_COST;
}

QiBenefitResult
qi_benefit_observe(const QiNativePlanFeatures *features,
                   const QiStatsConfidence *stats,
                   const QiProfileEvidence *profile_evidence,
                   const QiProfileModel *profile_model)
{
    QiBenefitModelInput input;

    if (features == NULL || stats == NULL || profile_evidence == NULL ||
        profile_model == NULL)
        return qi_benefit_result(QI_BENEFIT_UNKNOWN,
                                 QI_BENEFIT_INPUT_INVALID);
    if (features->state != QI_PLAN_FEATURE_BOUNDED)
        return qi_benefit_result(QI_BENEFIT_NOT_EVALUATED,
                                 QI_BENEFIT_NOT_OBSERVED);
    if (features->reason != QI_PLAN_FEATURE_OK ||
        features->spine != QI_PLAN_SPINE_PLAIN_AGG_SEQ_SCAN ||
        (features->known_fields & (QI_PLAN_KNOWN_AGG_ESTIMATE |
          QI_PLAN_KNOWN_SCAN_ESTIMATE | QI_PLAN_KNOWN_RELPAGES |
          QI_PLAN_KNOWN_RELTUPLES)) !=
         (QI_PLAN_KNOWN_AGG_ESTIMATE | QI_PLAN_KNOWN_SCAN_ESTIMATE |
          QI_PLAN_KNOWN_RELPAGES | QI_PLAN_KNOWN_RELTUPLES))
        return qi_benefit_result(QI_BENEFIT_UNKNOWN,
                                 QI_BENEFIT_PLAN_UNKNOWN);
    if (!qi_cost_settings_supported())
        return qi_benefit_result(QI_BENEFIT_UNKNOWN,
                                 QI_BENEFIT_INPUT_INVALID);
    if (!profile_evidence->profile_present || !profile_model->valid ||
        profile_model->install_sequence !=
            profile_evidence->install_sequence ||
        profile_model->fingerprint != profile_evidence->fingerprint)
        return qi_benefit_result(QI_BENEFIT_UNKNOWN,
                                 QI_BENEFIT_PROFILE_UNKNOWN);
    MemSet(&input, 0, sizeof(input));
    if (!qi_floor_double(features->aggregate.total_cost,
                         &input.postgres_cost_floor) ||
        !qi_ceil_double(features->scan.rows, &input.scan_rows_ceil) ||
        !qi_ceil_double(features->reltuples, &input.reltuples_ceil) ||
        !qi_ceil_double(features->aggregate.rows,
                        &input.output_rows_ceil) ||
        features->relpages < 0 || features->scan.width < 0 ||
        features->aggregate.width < 0)
        return qi_benefit_result(QI_BENEFIT_UNKNOWN,
                                 QI_BENEFIT_INPUT_INVALID);
    input.relpages = (uint64) features->relpages;
    input.block_kb = BLCKSZ / 1024U;
    input.scan_width = (uint64) features->scan.width;
    input.output_width = (uint64) features->aggregate.width;
    input.fixed_overhead_us = profile_model->fixed_overhead_us;
    input.postgres_ns_per_cost = profile_model->postgres_ns_per_cost;
    input.scan_ns_per_kb = profile_model->scan_ns_per_kb;
    input.conversion_ns_per_row = profile_model->conversion_ns_per_row;
    input.output_ns_per_byte = profile_model->output_ns_per_byte;
    input.p95_dispersion_us = profile_model->p95_dispersion_us;
    input.threads = profile_model->threads;
    input.concurrency = profile_model->concurrency;
    input.margin_ppm = profile_model->margin_ppm;
    input.stats_confident = qi_stats_proven_confident(stats->state);
    input.limits_verified = profile_evidence->limits_verified;
    return qi_benefit_model_evaluate(&input);
}

QiGateState
qi_benefit_gate_state(const QiBenefitResult *result)
{
    if (result == NULL)
        return QI_GATE_UNKNOWN;
    switch (result->state)
    {
        case QI_BENEFIT_NOT_EVALUATED: return QI_GATE_NOT_EVALUATED;
        case QI_BENEFIT_PASS: return QI_GATE_PASS;
        case QI_BENEFIT_VETO: return QI_GATE_VETO;
        case QI_BENEFIT_UNKNOWN: return QI_GATE_UNKNOWN;
    }
    return QI_GATE_UNKNOWN;
}

#if PG_QI_TESTING
Datum
qi_test_benefit_contract(PG_FUNCTION_ARGS)
{
    QiNativePlanFeatures features;
    QiStatsConfidence stats =
        qi_stats_confidence_unknown(QI_STATS_FRESHNESS_UNPROVEN);
    QiProfileEvidence evidence = qi_profile_not_observed();
    QiProfileModel model;
    QiBenefitResult result;
    bool ok = true;

    MemSet(&features, 0, sizeof(features));
    MemSet(&model, 0, sizeof(model));
    result = qi_benefit_observe(&features, &stats, &evidence, &model);
    ok = ok && result.state == QI_BENEFIT_NOT_EVALUATED;

    features.state = QI_PLAN_FEATURE_BOUNDED;
    features.reason = QI_PLAN_FEATURE_OK;
    features.spine = QI_PLAN_SPINE_PLAIN_AGG_SEQ_SCAN;
    features.known_fields = QI_PLAN_KNOWN_AGG_ESTIMATE |
        QI_PLAN_KNOWN_SCAN_ESTIMATE | QI_PLAN_KNOWN_RELPAGES |
        QI_PLAN_KNOWN_RELTUPLES;
    features.aggregate.total_cost = 10000.0;
    features.aggregate.rows = 1.0;
    features.aggregate.width = 8;
    features.scan.rows = 1000.0;
    features.scan.width = 8;
    features.relpages = 100;
    features.reltuples = 1000.0;
    evidence.profile_present = true;
    evidence.install_sequence = 7;
    evidence.fingerprint = 11;
    model.valid = true;
    model.install_sequence = 7;
    model.fingerprint = 11;
    model.threads = 4;
    model.concurrency = 2;
    model.fixed_overhead_us = 1000;
    model.postgres_ns_per_cost = 1000;
    model.scan_ns_per_kb = 100;
    model.conversion_ns_per_row = 10;
    model.output_ns_per_byte = 1;
    model.p95_dispersion_us = 500;
    model.margin_ppm = 100000;
    result = qi_benefit_observe(&features, &stats, &evidence, &model);
    ok = ok && result.state == QI_BENEFIT_UNKNOWN &&
        result.reason == QI_BENEFIT_STATS_UNCONFIDENT &&
        result.postgres_ns_low > 0 && result.duckdb_ns_high > 0;

    model.fingerprint++;
    result = qi_benefit_observe(&features, &stats, &evidence, &model);
    ok = ok && result.reason == QI_BENEFIT_PROFILE_UNKNOWN;
    model.fingerprint = evidence.fingerprint;
    features.scan.rows = NAN;
    result = qi_benefit_observe(&features, &stats, &evidence, &model);
    ok = ok && result.reason == QI_BENEFIT_INPUT_INVALID;

    result = qi_benefit_result(QI_BENEFIT_PASS, QI_BENEFIT_FAVOURABLE);
    ok = ok && qi_benefit_gate_state(&result) == QI_GATE_PASS;
    result = qi_benefit_result(QI_BENEFIT_VETO, QI_BENEFIT_UNFAVOURABLE);
    ok = ok && qi_benefit_gate_state(&result) == QI_GATE_VETO;
    result = qi_benefit_result(QI_BENEFIT_UNKNOWN,
                               QI_BENEFIT_PLAN_UNKNOWN);
    ok = ok && qi_benefit_gate_state(&result) == QI_GATE_UNKNOWN &&
        strcmp(qi_benefit_reason_name(result.reason), "plan_unknown") == 0;
    PG_RETURN_BOOL(ok);
}
#endif
