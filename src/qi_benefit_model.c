/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "pg_qi/qi_benefit_model.h"

#include <limits.h>
#include <stddef.h>

static bool
qi_add(uint64_t a, uint64_t b, uint64_t *out)
{
    if (a > UINT64_MAX - b)
        return false;
    *out = a + b;
    return true;
}

static bool
qi_mul(uint64_t a, uint64_t b, uint64_t *out)
{
    if (a != 0 && b > UINT64_MAX / a)
        return false;
    *out = a * b;
    return true;
}

static bool
qi_ceil_div(uint64_t numerator, uint64_t denominator, uint64_t *out)
{
    uint64_t adjusted;

    if (denominator == 0 ||
        !qi_add(numerator, denominator - 1U, &adjusted))
        return false;
    *out = adjusted / denominator;
    return true;
}

static QiBenefitResult
qi_unknown(QiBenefitReason reason)
{
    QiBenefitResult result = {0};

    result.state = QI_BENEFIT_UNKNOWN;
    result.reason = reason;
    return result;
}

static QiBenefitResult
qi_computed_unknown(QiBenefitResult result, QiBenefitReason reason)
{
    result.state = QI_BENEFIT_UNKNOWN;
    result.reason = reason;
    return result;
}

QiBenefitResult
qi_benefit_model_evaluate(const QiBenefitModelInput *input)
{
    QiBenefitResult result = {0};
    uint64_t page_kb;
    uint64_t tuple_bytes;
    uint64_t tuple_kb;
    uint64_t scan_kb;
    uint64_t conversion_rows;
    uint64_t scan_ns;
    uint64_t conversion_ns;
    uint64_t parallel_ns;
    uint64_t fixed_ns;
    uint64_t contention_factor;
    uint64_t output_bytes;
    uint64_t output_ns;
    uint64_t left;
    uint64_t right;
    uint64_t relative_factor;

    if (input == NULL || input->block_kb == 0 || input->threads == 0 ||
        input->concurrency == 0 || input->postgres_ns_per_cost == 0 ||
        input->margin_ppm == 0 || input->margin_ppm > QI_BENEFIT_PPM)
        return qi_unknown(QI_BENEFIT_INPUT_INVALID);
    if (!qi_mul(input->postgres_cost_floor,
                input->postgres_ns_per_cost, &result.postgres_ns_low) ||
        !qi_mul(input->relpages, input->block_kb, &page_kb) ||
        !qi_mul(input->scan_rows_ceil, input->scan_width, &tuple_bytes) ||
        !qi_ceil_div(tuple_bytes, 1024U, &tuple_kb))
        return qi_unknown(QI_BENEFIT_ARITHMETIC_OVERFLOW);
    scan_kb = page_kb > tuple_kb ? page_kb : tuple_kb;
    conversion_rows = input->scan_rows_ceil > input->reltuples_ceil
        ? input->scan_rows_ceil : input->reltuples_ceil;
    if (!qi_add(conversion_rows, input->output_rows_ceil,
                &conversion_rows) ||
        !qi_mul(scan_kb, input->scan_ns_per_kb, &scan_ns) ||
        !qi_mul(conversion_rows, input->conversion_ns_per_row,
                &conversion_ns) ||
        !qi_mul(input->output_rows_ceil, input->output_width,
                &output_bytes) ||
        !qi_mul(output_bytes, input->output_ns_per_byte, &output_ns) ||
        !qi_add(scan_ns, conversion_ns, &parallel_ns) ||
        !qi_add(parallel_ns, output_ns, &parallel_ns))
        return qi_unknown(QI_BENEFIT_ARITHMETIC_OVERFLOW);
    if (!qi_ceil_div(input->concurrency, input->threads,
                     &contention_factor) ||
        !qi_mul(parallel_ns, contention_factor, &output_ns) ||
        !qi_mul(input->fixed_overhead_us, 1000U, &fixed_ns) ||
        !qi_add(fixed_ns, output_ns, &result.duckdb_ns_high))
        return qi_unknown(QI_BENEFIT_ARITHMETIC_OVERFLOW);
    result.absolute_margin_ns = input->fixed_overhead_us >
        input->p95_dispersion_us ? input->fixed_overhead_us :
        input->p95_dispersion_us;
    if (!qi_add(result.absolute_margin_ns, 1U,
                &result.absolute_margin_ns) ||
        !qi_mul(result.absolute_margin_ns, 1000U,
                &result.absolute_margin_ns))
        return qi_unknown(QI_BENEFIT_ARITHMETIC_OVERFLOW);
    if (!input->stats_confident)
        return qi_computed_unknown(result, QI_BENEFIT_STATS_UNCONFIDENT);
    if (!input->limits_verified)
        return qi_computed_unknown(result, QI_BENEFIT_LIMITS_UNVERIFIED);
    if (result.duckdb_ns_high >= result.postgres_ns_low)
        return qi_computed_unknown(result, QI_BENEFIT_UNFAVOURABLE);
    relative_factor = QI_BENEFIT_PPM - input->margin_ppm;
    if (!qi_add(result.duckdb_ns_high, result.absolute_margin_ns, &left) ||
        !qi_mul(left, QI_BENEFIT_PPM, &left) ||
        !qi_mul(result.postgres_ns_low, relative_factor, &right))
        return qi_computed_unknown(result, QI_BENEFIT_ARITHMETIC_OVERFLOW);
    if (left < right)
    {
        result.state = QI_BENEFIT_PASS;
        result.reason = QI_BENEFIT_FAVOURABLE;
    }
    else
    {
        result.state = QI_BENEFIT_UNKNOWN;
        result.reason = QI_BENEFIT_WITHIN_MARGIN;
    }
    return result;
}

const char *
qi_benefit_reason_name(QiBenefitReason reason)
{
    switch (reason)
    {
        case QI_BENEFIT_NOT_OBSERVED: return "not_observed";
        case QI_BENEFIT_PLAN_UNKNOWN: return "plan_unknown";
        case QI_BENEFIT_PROFILE_UNKNOWN: return "profile_unknown";
        case QI_BENEFIT_INPUT_INVALID: return "input_invalid";
        case QI_BENEFIT_ARITHMETIC_OVERFLOW: return "arithmetic_overflow";
        case QI_BENEFIT_STATS_UNCONFIDENT: return "stats_unconfident";
        case QI_BENEFIT_LIMITS_UNVERIFIED: return "limits_unverified";
        case QI_BENEFIT_WITHIN_MARGIN: return "within_margin";
        case QI_BENEFIT_FAVOURABLE: return "favourable";
        case QI_BENEFIT_UNFAVOURABLE: return "unfavourable";
        case QI_BENEFIT_SPILL_ESTIMATE_UNKNOWN: return "spill_estimate_unknown";
        case QI_BENEFIT_SPILL_RESOURCES_UNVERIFIED: return "spill_resources_unverified";
    }
    return "invalid";
}
