/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_BENEFIT_H
#define PG_QI_QI_BENEFIT_H

#include "postgres.h"

#include "pg_qi/qi_benefit_model.h"
#include "pg_qi/qi_decision_v2.h"
#include "pg_qi/qi_plan_features.h"
#include "pg_qi/qi_profile.h"
#include "pg_qi/qi_spill.h"
#include "pg_qi/qi_stats_confidence.h"

extern QiBenefitResult qi_benefit_observe(
    const QiNativePlanFeatures *features,
    const QiStatsConfidence *stats,
    const QiProfileEvidence *profile_evidence,
    const QiProfileModel *profile_model);
extern QiBenefitResult qi_benefit_apply_spill(QiBenefitResult benefit,
                                               QiSpillEvidence spill);
extern QiGateState qi_benefit_gate_state(const QiBenefitResult *result);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_benefit_contract(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_BENEFIT_H */
