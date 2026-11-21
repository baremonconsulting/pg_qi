/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_SNAPSHOT_H
#define PG_QI_QI_SNAPSHOT_H

#include "postgres.h"

#include "nodes/nodes.h"
#include "pg_qi/qi_benefit.h"
#include "pg_qi/qi_decision_v2.h"
#include "pg_qi/qi_plan_features.h"
#include "pg_qi/qi_profile.h"
#include "pg_qi/qi_semantic.h"
#include "pg_qi/qi_shadow_p0.h"
#include "pg_qi/qi_stats_confidence.h"

#define QI_SNAPSHOT_ENVELOPE_VERSION 1
#define QI_SNAPSHOT_DIAGNOSTIC_ROLE "pgqi_diagnostic"

typedef enum QiRoutingMode
{
    QI_ROUTING_OFF = 0,
    QI_ROUTING_ALLOWLIST,
    QI_ROUTING_SHADOW_P0
} QiRoutingMode;

typedef enum QiPlanLifecycle
{
    QI_PLAN_LIFECYCLE_UNPROVED = 0,
    QI_PLAN_LIFECYCLE_FRESH_EXECUTION
} QiPlanLifecycle;

typedef struct QiV2SnapshotToken
{
    uint64 generation;
    Oid effective_user;
    Oid session_user;
    bool valid;
} QiV2SnapshotToken;

typedef struct QiV2SnapshotCandidate
{
    QiV2SnapshotToken token;
    QiRoutingMode mode_at_decision;
    bool enabled_at_decision;
    QiShadowContext context;
    QiV2Decision decision;
    QiVerification verification;
    QiShadowObservation observation;
    QiShadowP0FactsState p0_state;
    QiShadowP0FactsReason p0_reason;
    QiSemanticFacts semantic;
    QiNativePlanFeatures plan_features;
    QiStatsConfidence stats_confidence;
    QiProfileEvidence profile_evidence;
    QiBenefitResult benefit;
    QiPlanLifecycle lifecycle;
    bool valid;
} QiV2SnapshotCandidate;

extern void qi_snapshot_init_guc(void);
extern QiV2SnapshotToken qi_snapshot_begin(void);
extern bool qi_snapshot_prepare(QiV2SnapshotCandidate *candidate,
                                QiV2SnapshotToken token,
                                QiRoutingMode mode_at_decision,
                                bool enabled_at_decision,
                                QiShadowContext context,
                                QiPolicyIntent policy_intent,
                                QiForceOrigin force_origin,
                                QiShadowEngine observed_engine,
                                QiVerification verification,
                                QiShadowObservation observation,
                                QiSemanticFacts semantic,
                                QiShadowP0FactsState p0_state,
                                QiShadowP0FactsReason p0_reason,
                                QiNativePlanFeatures plan_features,
                                QiStatsConfidence stats_confidence,
                                QiProfileEvidence profile_evidence,
                                QiBenefitResult benefit);
extern bool qi_snapshot_commit(const QiV2SnapshotCandidate *candidate);
extern void qi_snapshot_handle_utility(Node *utility_stmt);

#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_v2_snapshot_contract(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_SNAPSHOT_H */
