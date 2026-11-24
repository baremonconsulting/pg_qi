/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_BENEFIT_MODEL_H
#define PG_QI_QI_BENEFIT_MODEL_H

#include <stdbool.h>
#include <stdint.h>

#define QI_BENEFIT_PPM UINT64_C(1000000)

typedef enum QiBenefitState
{
    QI_BENEFIT_NOT_EVALUATED = 0,
    QI_BENEFIT_UNKNOWN,
    QI_BENEFIT_PASS,
    QI_BENEFIT_VETO
} QiBenefitState;

typedef enum QiBenefitReason
{
    QI_BENEFIT_NOT_OBSERVED = 0,
    QI_BENEFIT_PLAN_UNKNOWN,
    QI_BENEFIT_PROFILE_UNKNOWN,
    QI_BENEFIT_INPUT_INVALID,
    QI_BENEFIT_ARITHMETIC_OVERFLOW,
    QI_BENEFIT_STATS_UNCONFIDENT,
    QI_BENEFIT_LIMITS_UNVERIFIED,
    QI_BENEFIT_WITHIN_MARGIN,
    QI_BENEFIT_FAVOURABLE,
    QI_BENEFIT_UNFAVOURABLE,
    QI_BENEFIT_SPILL_ESTIMATE_UNKNOWN,
    QI_BENEFIT_SPILL_RESOURCES_UNVERIFIED
} QiBenefitReason;

typedef struct QiBenefitModelInput
{
    uint64_t postgres_cost_floor;
    uint64_t relpages;
    uint64_t block_kb;
    uint64_t scan_rows_ceil;
    uint64_t scan_width;
    uint64_t reltuples_ceil;
    uint64_t output_rows_ceil;
    uint64_t output_width;
    uint64_t fixed_overhead_us;
    uint64_t postgres_ns_per_cost;
    uint64_t scan_ns_per_kb;
    uint64_t conversion_ns_per_row;
    uint64_t output_ns_per_byte;
    uint64_t p95_dispersion_us;
    uint32_t threads;
    uint32_t concurrency;
    uint32_t margin_ppm;
    bool stats_confident;
    bool limits_verified;
} QiBenefitModelInput;

typedef struct QiBenefitResult
{
    QiBenefitState state;
    QiBenefitReason reason;
    uint64_t postgres_ns_low;
    uint64_t duckdb_ns_high;
    uint64_t absolute_margin_ns;
} QiBenefitResult;

extern QiBenefitResult qi_benefit_model_evaluate(
    const QiBenefitModelInput *input);
extern const char *qi_benefit_reason_name(QiBenefitReason reason);

#endif /* PG_QI_QI_BENEFIT_MODEL_H */
