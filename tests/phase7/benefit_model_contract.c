/* Deterministic contract and boundary harness for PQ7-11. */

#include "pg_qi/qi_benefit_model.h"

#include <limits.h>
#include <stdio.h>
#include <string.h>

static uint64_t random_state = UINT64_C(0x6a09e667f3bcc909);

static uint64_t
next_random(void)
{
    random_state ^= random_state << 7;
    random_state ^= random_state >> 9;
    return random_state;
}

static QiBenefitModelInput
base_input(void)
{
    QiBenefitModelInput input = {0};

    input.postgres_cost_floor = 10000;
    input.relpages = 100;
    input.block_kb = 8;
    input.scan_rows_ceil = 1000;
    input.scan_width = 8;
    input.reltuples_ceil = 1000;
    input.output_rows_ceil = 1;
    input.output_width = 8;
    input.fixed_overhead_us = 1000;
    input.postgres_ns_per_cost = 1000;
    input.scan_ns_per_kb = 100;
    input.conversion_ns_per_row = 10;
    input.output_ns_per_byte = 1;
    input.p95_dispersion_us = 500;
    input.threads = 4;
    input.concurrency = 2;
    input.margin_ppm = 100000;
    input.stats_confident = true;
    input.limits_verified = true;
    return input;
}

int
main(void)
{
    QiBenefitModelInput input = base_input();
    QiBenefitResult result = qi_benefit_model_evaluate(&input);
    uint64_t two_threads;
    unsigned int iteration;

    if (result.state != QI_BENEFIT_PASS ||
        result.reason != QI_BENEFIT_FAVOURABLE)
        return 1;
    two_threads = result.duckdb_ns_high;
    input.concurrency = 4;
    result = qi_benefit_model_evaluate(&input);
    if (result.state != QI_BENEFIT_PASS ||
        result.duckdb_ns_high != two_threads)
        return 2;
    input = base_input();
    input.postgres_cost_floor = 100;
    result = qi_benefit_model_evaluate(&input);
    if (result.state != QI_BENEFIT_UNKNOWN ||
        result.reason != QI_BENEFIT_UNFAVOURABLE)
        return 3;
    input = base_input();
    input.stats_confident = false;
    result = qi_benefit_model_evaluate(&input);
    if (result.state != QI_BENEFIT_UNKNOWN ||
        result.reason != QI_BENEFIT_STATS_UNCONFIDENT)
        return 4;
    input = base_input();
    input.limits_verified = false;
    result = qi_benefit_model_evaluate(&input);
    if (result.state != QI_BENEFIT_UNKNOWN ||
        result.reason != QI_BENEFIT_LIMITS_UNVERIFIED)
        return 5;
    input = base_input();
    input.relpages = UINT64_MAX;
    result = qi_benefit_model_evaluate(&input);
    if (result.state != QI_BENEFIT_UNKNOWN ||
        result.reason != QI_BENEFIT_ARITHMETIC_OVERFLOW)
        return 6;
    input = base_input();
    input.threads = 0;
    result = qi_benefit_model_evaluate(&input);
    if (result.state != QI_BENEFIT_UNKNOWN ||
        result.reason != QI_BENEFIT_INPUT_INVALID)
        return 7;
    input = base_input();
    input.postgres_cost_floor = 2500;
    input.margin_ppm = 500000;
    result = qi_benefit_model_evaluate(&input);
    if (result.state != QI_BENEFIT_UNKNOWN ||
        result.reason != QI_BENEFIT_WITHIN_MARGIN)
        return 8;
    input = base_input();
    input.margin_ppm = 1000001;
    result = qi_benefit_model_evaluate(&input);
    if (result.state != QI_BENEFIT_UNKNOWN ||
        result.reason != QI_BENEFIT_INPUT_INVALID)
        return 9;
    input = base_input();
    input.margin_ppm = 1000000;
    result = qi_benefit_model_evaluate(&input);
    if (result.state == QI_BENEFIT_PASS)
        return 10;
    input = base_input();
    input.concurrency = 8;
    result = qi_benefit_model_evaluate(&input);
    if (result.duckdb_ns_high <= two_threads)
        return 11;
    for (iteration = 0; iteration < 100000U; iteration++)
    {
        QiBenefitModelInput random_input = base_input();
        QiBenefitResult first;
        QiBenefitResult second;

        random_input.postgres_cost_floor = next_random();
        random_input.relpages = next_random();
        random_input.scan_rows_ceil = next_random();
        random_input.scan_width = next_random();
        random_input.reltuples_ceil = next_random();
        random_input.output_rows_ceil = next_random();
        random_input.output_width = next_random();
        random_input.fixed_overhead_us = next_random();
        random_input.postgres_ns_per_cost = next_random();
        random_input.scan_ns_per_kb = next_random();
        random_input.conversion_ns_per_row = next_random();
        random_input.output_ns_per_byte = next_random();
        random_input.p95_dispersion_us = next_random();
        random_input.threads = (uint32_t) next_random();
        random_input.concurrency = (uint32_t) next_random();
        random_input.margin_ppm = (uint32_t) next_random();
        random_input.stats_confident = (next_random() & 1U) != 0;
        random_input.limits_verified = (next_random() & 1U) != 0;
        first = qi_benefit_model_evaluate(&random_input);
        second = qi_benefit_model_evaluate(&random_input);
        if (memcmp(&first, &second, sizeof(first)) != 0)
            return 12;
        if ((!random_input.stats_confident ||
             !random_input.limits_verified) &&
            first.state == QI_BENEFIT_PASS)
            return 13;
        if (first.state == QI_BENEFIT_VETO)
            return 14;
    }
    puts("PQ-40 benefit model contract PASS");
    return 0;
}
