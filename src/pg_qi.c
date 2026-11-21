/*
 * Copyright 2026 Baremon s.r.o.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "fmgr.h"
#include "miscadmin.h"
#include "pg_qi/planner_compat.h"
#include "pg_qi/qi_classify.h"
#include "pg_qi/qi_benefit.h"
#include "pg_qi/qi_context.h"
#include "pg_qi/qi_decision.h"
#include "pg_qi/qi_decision_v2.h"
#include "pg_qi/qi_delegation.h"
#include "pg_qi/qi_detector.h"
#include "pg_qi/qi_eligibility.h"
#include "pg_qi/qi_lifecycle.h"
#include "pg_qi/qi_plan_features.h"
#include "pg_qi/qi_profile.h"
#include "pg_qi/qi_shadow_p0.h"
#include "pg_qi/qi_semantic.h"
#include "pg_qi/qi_snapshot.h"
#include "pg_qi/qi_stats_confidence.h"
#if PG_QI_TESTING
#include "pg_qi/qi_diag.h"
#include "pg_qi/qi_reenter.h"
#endif
#include "portability/instr_time.h"
#include "storage/ipc.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/timestamp.h"

PG_MODULE_MAGIC;

#define PG_QI_VERSION "0.0.1"

static bool pg_qi_enabled = true;
static bool pg_qi_preloaded = false;
static int pg_qi_routing_mode = QI_ROUTING_OFF;
#if PG_QI_TESTING
static bool pg_qi_last_plan_has_duckdb_scan = false;
static bool pg_qi_previous_hook_present = false;
static bool pg_qi_final_report_registered = false;
static int pg_qi_benchmark_mode = QI_BENCH_NORMAL;
static bool pg_qi_benchmark_collect = true;
#endif
static qi_planner_fn_type previous_planner_hook = NULL;
#if !PG_QI_TESTING
static int pg_qi_shadow_planner_depth = 0;
#endif

static const struct config_enum_entry pg_qi_routing_mode_options[] = {
    {"off", QI_ROUTING_OFF, false},
    {"allowlist", QI_ROUTING_ALLOWLIST, false},
    {"shadow_p0", QI_ROUTING_SHADOW_P0, false},
    {NULL, 0, false}
};

#if PG_QI_TESTING
static const struct config_enum_entry pg_qi_benchmark_options[] = {
    {"normal", QI_BENCH_NORMAL, false},
    {"bypass", QI_BENCH_BYPASS, false},
    {"frame_only", QI_BENCH_FRAME_ONLY, false},
    {"shadow_save", QI_BENCH_SHADOW_SAVE, false},
    {"delegate", QI_BENCH_DELEGATE, false},
    {NULL, 0, false}
};
#endif

static inline PlannedStmt *
pg_qi_call_downstream(Query *parse, const char *query_string,
                      int cursor_options, ParamListInfo bound_params
#if QI_HAVE_PLANNER_ES
                      , ExplainState *es
#endif
                      )
{
    if (previous_planner_hook != NULL)
        return QI_CALL_PLANNER(previous_planner_hook, parse, query_string,
                               cursor_options, bound_params,
                               QI_CURRENT_EXPLAIN_STATE);
    return QI_CALL_PLANNER(standard_planner, parse, query_string,
                           cursor_options, bound_params,
                           QI_CURRENT_EXPLAIN_STATE);
}

void _PG_init(void);
void _PG_fini(void);

PG_FUNCTION_INFO_V1(pg_qi_version);
PG_FUNCTION_INFO_V1(pg_qi_is_preloaded);
#if PG_QI_TESTING
PG_FUNCTION_INFO_V1(qi_test_force_meta);
PG_FUNCTION_INFO_V1(qi_test_force_save_roundtrip);
PG_FUNCTION_INFO_V1(qi_test_detector_contract);
PG_FUNCTION_INFO_V1(qi_test_frame_contract);
PG_FUNCTION_INFO_V1(qi_test_frame_state);
PG_FUNCTION_INFO_V1(qi_spike_arm_next);
PG_FUNCTION_INFO_V1(qi_spike_disarm);
PG_FUNCTION_INFO_V1(qi_spike_reset_token);
PG_FUNCTION_INFO_V1(qi_test_token_state);
PG_FUNCTION_INFO_V1(qi_spike_reset_state);
PG_FUNCTION_INFO_V1(qi_spike_state);
PG_FUNCTION_INFO_V1(qi_spike_observer);
PG_FUNCTION_INFO_V1(qi_test_active_control_guard);
PG_FUNCTION_INFO_V1(qi_test_classifier_contract);
PG_FUNCTION_INFO_V1(qi_test_decision_v2_contract);
PG_FUNCTION_INFO_V1(qi_test_plan_features_contract);
PG_FUNCTION_INFO_V1(qi_test_stats_confidence_contract);
PG_FUNCTION_INFO_V1(qi_test_profile_contract);
PG_FUNCTION_INFO_V1(qi_test_profile_parse);
PG_FUNCTION_INFO_V1(qi_test_benefit_contract);
PG_FUNCTION_INFO_V1(qi_test_lifecycle_contract);
PG_FUNCTION_INFO_V1(qi_test_shadow_p0_observer_contract);
PG_FUNCTION_INFO_V1(qi_test_semantic_contract);
PG_FUNCTION_INFO_V1(qi_test_semantic_probe);
PG_FUNCTION_INFO_V1(qi_test_v2_snapshot_contract);
#endif

QI_PLANNER_PROTO(pg_qi_planner)
{
#if PG_QI_TESTING
    PlannedStmt *volatile planned_stmt = NULL;
    QiDelegationFrame *volatile frame = NULL;
    volatile uint64 frame_id = 0;
    int save_nestlevel;
    bool delegation_requested;
    uint64 generation = 0;
#if PG_QI_TESTING
    instr_time started_at;
    instr_time finished_at;
    QiClassifyResult classification = qi_classify_query(parse);
    INSTR_TIME_SET_CURRENT(started_at);

    if (!pg_qi_final_report_registered && MyBackendType == B_BACKEND)
    {
        before_shmem_exit(qi_backend_final_report, (Datum) 0);
        pg_qi_final_report_registered = true;
    }

    if (!pg_qi_benchmark_collect)
        return pg_qi_call_downstream(parse, query_string, cursor_options,
                                     bound_params
#if QI_HAVE_PLANNER_ES
                                     , es
#endif
                                     );
#endif

    if (!pg_qi_enabled)
    {
        planned_stmt = pg_qi_call_downstream(parse, query_string,
                                              cursor_options, bound_params
#if QI_HAVE_PLANNER_ES
                                              , es
#endif
                                              );
        goto done;
    }

#if PG_QI_TESTING
    if (pg_qi_benchmark_mode == QI_BENCH_BYPASS)
    {
        planned_stmt = pg_qi_call_downstream(parse, query_string,
                                              cursor_options, bound_params
#if QI_HAVE_PLANNER_ES
                                              , es
#endif
                                              );
        goto done;
    }
#endif

    /* Token consumption must precede every operation that can throw. */
    delegation_requested = false;
#if PG_QI_TESTING
    delegation_requested = pg_qi_benchmark_mode == QI_BENCH_DELEGATE;
    if (!delegation_requested)
        delegation_requested = qi_token_consume_top_level(&generation);
    qi_failpoint_hit(QI_FAILPOINT_BEFORE_PUSH);
#endif
    save_nestlevel = NewGUCNestLevel();
    frame = qi_frame_push(save_nestlevel, qi_current_force_origin(),
                          delegation_requested,
#if PG_QI_TESTING
                          qi_reenter_is_active(),
#else
                          false,
#endif
                          generation);
    frame_id = frame->frame_id;

    PG_TRY();
    {
#if PG_QI_TESTING
        qi_failpoint_hit(QI_FAILPOINT_INSIDE_TRY_BEFORE_POLICY);
        if (pg_qi_benchmark_mode == QI_BENCH_SHADOW_SAVE)
            qi_set_force_execution(false);
        else if (pg_qi_benchmark_mode != QI_BENCH_FRAME_ONLY &&
                 frame->guc_changed)
            qi_set_force_execution(frame->effective_origin != QI_FORCE_OFF);
        qi_failpoint_hit(QI_FAILPOINT_INSIDE_TRY_AFTER_POLICY);

        if (qi_reenter_is_active())
            qi_failpoint_hit(QI_FAILPOINT_INNER_PLANNER);
        if (qi_reenter_inner_error_enabled())
            ereport(ERROR,
                    (errmsg("pg_qi synthetic inner planner error")));

        qi_maybe_reenter(parse, query_string, cursor_options, bound_params,
                         QI_CURRENT_EXPLAIN_STATE, delegation_requested);
        qi_failpoint_hit(QI_FAILPOINT_INSIDE_TRY_BEFORE_DOWNSTREAM);
#else
        if (frame->guc_changed)
            qi_set_force_execution(frame->effective_origin != QI_FORCE_OFF);
#endif

        planned_stmt = pg_qi_call_downstream(parse, query_string,
                                              cursor_options, bound_params
#if QI_HAVE_PLANNER_ES
                                              , es
#endif
                                              );

#if PG_QI_TESTING
        pg_qi_last_plan_has_duckdb_scan =
            qi_plan_has_duckdb_scan((PlannedStmt *) planned_stmt);
        qi_record_completed_plan(pg_qi_last_plan_has_duckdb_scan);
#endif
    }
    PG_CATCH();
    {
        (void) qi_frame_unwind_on_error(frame_id);
        PG_RE_THROW();
    }
    PG_END_TRY();

    AtEOXact_GUC(true, save_nestlevel);
    (void) qi_frame_pop(frame_id);
done:
#if PG_QI_TESTING
    qi_classify_record_test_result(classification);
    INSTR_TIME_SET_CURRENT(finished_at);
    INSTR_TIME_SUBTRACT(finished_at, started_at);
    qi_record_planner_timing((uint64) INSTR_TIME_GET_MICROSEC(finished_at));
#endif
    return (PlannedStmt *) planned_stmt;
#else
    PlannedStmt *volatile planned_stmt = NULL;
    QiDelegationFrame *volatile frame = NULL;
    volatile uint64 frame_id = 0;
    volatile int save_nestlevel = 0;
    QiForceOrigin force_origin = QI_FORCE_OFF;
    QiForceOrigin decision_force_origin = QI_FORCE_OFF;
    QiShadowContext context;
    QiPolicyIntent policy_intent = QI_POLICY_PASS_THROUGH;
    QiShadowClassifierStatus classifier_status =
        QI_SHADOW_CLASSIFIER_NOT_EVALUATED;
    QiEligibilityResult eligibility = {
        .candidate = false,
        .reason = QI_ELIGIBILITY_CANDIDATE,
        .prototype_reason = QI_CLASSIFY_ELIGIBLE
    };
    QiShadowReason reason = QI_SHADOW_NO_COMPLETED_PLAN;
    QiShadowEngine observed_engine = QI_SHADOW_ENGINE_UNKNOWN;
    QiVerification verification = QI_VERIFICATION_UNVERIFIED;
    QiShadowObservation observation =
        QI_SHADOW_OBSERVATION_NO_AUTHORITATIVE_MATCH;
    bool context_proven;
    bool request_applied = false;
    bool shadow_p0_facts_observed = false;
    QiRoutingMode mode_at_decision = (QiRoutingMode) pg_qi_routing_mode;
    bool enabled_at_decision = pg_qi_enabled;
    QiV2SnapshotToken v2_token;
    QiV2SnapshotCandidate v2_candidate;
    bool v2_candidate_ready = false;
    QiShadowP0Facts shadow_p0_facts = {
        .state = QI_SHADOW_P0_FACTS_UNKNOWN,
        .reason = QI_SHADOW_P0_FACTS_NO_QUERY,
        .capability = QI_SHADOW_P0_CAPABILITY_UNKNOWN,
        .relation_oid = InvalidOid,
        .argument_attno = InvalidAttrNumber,
        .rtable_entries = 0,
        .relation_entries = 0,
        .nodes_seen = 0,
        .max_depth_seen = 0,
        .facts_emitted = 0
    };
    QiSemanticFacts semantic_facts = {
        .status = QI_SEMANTIC_NOT_EVALUATED,
        .profile = QI_SEMANTIC_PROFILE_NONE,
        .blockers = 0
    };
    QiNativePlanFeatures plan_features =
        qi_plan_features_unknown(QI_PLAN_FEATURE_NOT_OBSERVED);
    QiStatsConfidence stats_confidence =
        qi_stats_confidence_unknown(QI_STATS_NOT_OBSERVED);
    QiProfileEvidence profile_evidence = qi_profile_not_observed();
    QiProfileModel profile_model = {0};
    QiBenefitResult benefit = {
        .state = QI_BENEFIT_NOT_EVALUATED,
        .reason = QI_BENEFIT_NOT_OBSERVED
    };
    volatile bool frame_active = false;

    pg_qi_shadow_planner_depth++;
    context_proven = qi_context_proven_for_routing(
        planner_hook == pg_qi_planner, pg_qi_shadow_planner_depth,
        query_string);
    if (pg_qi_shadow_planner_depth > 1)
        context = QI_SHADOW_CONTEXT_NESTED;
    else if (context_proven)
        context = QI_SHADOW_CONTEXT_PROVEN_CLIENT;
    else
        context = QI_SHADOW_CONTEXT_UNPROVED_OUTERMOST;
    v2_token = qi_snapshot_begin();

    PG_TRY();
    {
        if (qi_delegation_state_poisoned())
            ereport(ERROR,
                    (errmsg("pg_qi routing state is poisoned"),
                     errhint("Reconnect the session before planning more queries.")));

        force_origin = qi_current_force_origin();
        decision_force_origin = force_origin;

        /* A nested invocation must not inherit a parent-owned request. */
        if (context == QI_SHADOW_CONTEXT_NESTED &&
            force_origin == QI_FORCE_QI_STACK)
        {
            save_nestlevel = NewGUCNestLevel();
            frame = qi_frame_push(save_nestlevel, force_origin, false, true, 0);
            frame_id = frame->frame_id;
            frame_active = true;
            if (frame->guc_changed)
                qi_set_force_execution(false);
        }

        if (!pg_qi_enabled)
            reason = QI_SHADOW_CONTROL_DISABLED;
        else if (context == QI_SHADOW_CONTEXT_NESTED)
            reason = QI_SHADOW_CONTEXT_NESTED_REASON;
        else if (force_origin == QI_FORCE_EXTERNAL)
            reason = QI_SHADOW_FORCE_EXTERNAL;
        else if (!context_proven)
            reason = QI_SHADOW_CONTEXT_UNPROVED_REASON;
        else
        {
            /*
             * Shadow observation is independent of the historical V1
             * allow-list.  This ordering makes limits and catalog misses
             * observable even when V1 correctly rejects the query.
             */
            if (pg_qi_routing_mode == QI_ROUTING_SHADOW_P0)
            {
                shadow_p0_facts = qi_shadow_p0_observe_query(parse);
                semantic_facts = qi_semantic_observe_query(
                    parse, &shadow_p0_facts);
                shadow_p0_facts_observed = true;
            }

            eligibility = qi_eligibility_query(parse);
            classifier_status = eligibility.candidate
                ? QI_SHADOW_CLASSIFIER_CANDIDATE
                : QI_SHADOW_CLASSIFIER_REJECTED;
            if (!eligibility.candidate)
                reason = QI_SHADOW_CLASSIFIER_REJECTED_REASON;
            else
            {
                switch ((QiRoutingMode) pg_qi_routing_mode)
                {
                    case QI_ROUTING_OFF:
                        reason = QI_SHADOW_POLICY_MISSING;
                        break;
                    case QI_ROUTING_ALLOWLIST:
                        if (!qi_context_policy_request_allowed())
                        {
                            reason = QI_SHADOW_DECISION_ONLY;
                            break;
                        }
                        policy_intent = QI_POLICY_REQUEST_DELEGATION;
                        reason = QI_SHADOW_DELEGATION_REQUESTED;
                        decision_force_origin = QI_FORCE_QI_STACK;
                        save_nestlevel = NewGUCNestLevel();
                        frame = qi_frame_push(save_nestlevel, force_origin,
                                              true, false, 0);
                        frame_id = frame->frame_id;
                        frame_active = true;
                        if (frame->guc_changed)
                            qi_set_force_execution(true);
                        request_applied = true;
                        break;
                    case QI_ROUTING_SHADOW_P0:
                        /*
                         * Decision-only skeleton.  PQ7-5 extends these bounded
                         * relation facts into the positive capability walker.
                         */
                        reason = QI_SHADOW_DECISION_ONLY;
                        break;
                    default:
                        /* Fail closed if a future value reaches this switch. */
                        reason = QI_SHADOW_POLICY_MISSING;
                        break;
                }
            }
        }

        planned_stmt = pg_qi_call_downstream(parse, query_string,
                                              cursor_options, bound_params
#if QI_HAVE_PLANNER_ES
                                              , es
#endif
                                              );
        if (qi_plan_has_duckdb_scan((PlannedStmt *) planned_stmt))
        {
            observed_engine = QI_SHADOW_ENGINE_DUCKDB;
            observation =
                QI_SHADOW_OBSERVATION_DUCKDB_SCAN_AT_DOWNSTREAM_BOUNDARY;
            if (context_proven && planner_hook == pg_qi_planner)
                verification = QI_VERIFICATION_VERIFIED;
        }

        if (shadow_p0_facts_observed)
        {
            plan_features = qi_plan_features_observe(
                (PlannedStmt *) planned_stmt, &shadow_p0_facts);
            stats_confidence = qi_stats_confidence_observe(&plan_features);
            profile_evidence = qi_profile_observe_model(
                GetCurrentTimestamp(), &profile_model);
            benefit = qi_benefit_observe(&plan_features, &stats_confidence,
                                         &profile_evidence, &profile_model);
        }

        if (request_applied)
            reason = verification == QI_VERIFICATION_VERIFIED &&
                observed_engine == QI_SHADOW_ENGINE_DUCKDB
                ? QI_SHADOW_DELEGATION_VERIFIED
                : QI_SHADOW_DELEGATION_DECLINED;

        if (context_proven && pg_qi_shadow_planner_depth == 1)
            v2_candidate_ready = qi_snapshot_prepare(
                &v2_candidate, v2_token, mode_at_decision,
                enabled_at_decision, context, policy_intent,
                decision_force_origin, observed_engine, verification,
                observation, semantic_facts,
                shadow_p0_facts_observed ? shadow_p0_facts.state :
                    QI_SHADOW_P0_FACTS_UNKNOWN,
                shadow_p0_facts_observed ? shadow_p0_facts.reason :
                    QI_SHADOW_P0_FACTS_NO_QUERY,
                plan_features, stats_confidence, profile_evidence, benefit);

        if (frame_active)
        {
            AtEOXact_GUC(true, save_nestlevel);
            frame_active = false;
            if (!qi_frame_pop(frame_id))
                ereport(ERROR,
                        (errmsg("pg_qi could not restore routing state"),
                         errhint("Reconnect the session before planning more queries.")));
        }

        /* Nested observations never replace the completed outer snapshot. */
        if (pg_qi_shadow_planner_depth == 1)
        {
            if (shadow_p0_facts_observed)
                qi_shadow_p0_publish_facts(shadow_p0_facts);
            else
                qi_shadow_p0_reset_facts();
            qi_shadow_publish(context, policy_intent, decision_force_origin,
                              classifier_status, eligibility, reason,
                              observed_engine, verification, observation,
                              request_applied);
            if (v2_candidate_ready &&
                qi_context_proven_for_routing(
                    planner_hook == pg_qi_planner,
                    pg_qi_shadow_planner_depth, query_string))
                (void) qi_lifecycle_arm((PlannedStmt *) planned_stmt,
                                        &v2_candidate);
        }
    }
    PG_CATCH();
    {
        if (frame_active)
        {
            AtEOXact_GUC(false, save_nestlevel);
            (void) qi_frame_unwind_on_error(frame_id);
        }
        pg_qi_shadow_planner_depth--;
        PG_RE_THROW();
    }
    PG_END_TRY();

    pg_qi_shadow_planner_depth--;
    return (PlannedStmt *) planned_stmt;
#endif
}

void
_PG_init(void)
{
    DefineCustomBoolVariable("pg_qi.enabled",
                             "Enables pg_qi policy evaluation in this backend.",
                             "When enabled, pg_qi evaluates policy; routing still requires an explicit allow-list mode.",
                             &pg_qi_enabled,
                             true,
                             PGC_SUSET,
                             0,
                             NULL,
                             NULL,
                             NULL);

    DefineCustomEnumVariable("pg_qi.routing_mode",
                             "Selects disabled, allow-list, or decision-only shadow policy.",
                             "Only allowlist may request delegation; shadow_p0 is inert.",
                             &pg_qi_routing_mode,
                             QI_ROUTING_OFF,
                             pg_qi_routing_mode_options,
                             PGC_SUSET,
                             0,
                             NULL,
                             NULL,
                             NULL);

    qi_shadow_init_gucs();
    qi_shadow_p0_init_gucs();
    qi_profile_init_gucs();
    qi_snapshot_init_guc();

#if PG_QI_TESTING
    DefineCustomBoolVariable("pg_qi.spike_last_plan_has_duckdb_scan",
                             "Reports whether the last completed plan contained DuckDBScan.",
                             NULL,
                             &pg_qi_last_plan_has_duckdb_scan,
                             false,
                             PGC_INTERNAL,
                             0,
                             NULL,
                             NULL,
                             NULL);
    DefineCustomBoolVariable("pg_qi.spike_previous_planner_hook_present",
                             NULL, NULL,
                             &pg_qi_previous_hook_present,
                             false, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.spike_benchmark_mode",
                             "Selects the Spike 1 benchmark layer.",
                             "Test-only; normal is required outside WP11.",
                             &pg_qi_benchmark_mode, QI_BENCH_NORMAL,
                             pg_qi_benchmark_options, PGC_USERSET, 0,
                             NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi.spike_benchmark_collect",
                             "Collects WP11 planner timing and counters.",
                             "Disable only for benchmark metadata queries.",
                             &pg_qi_benchmark_collect, true,
                             PGC_USERSET, 0, NULL, NULL, NULL);

    qi_init_frame_diagnostics();
    qi_classify_init_test_gucs();
    qi_reenter_init_gucs();
    qi_diag_init_gucs();
#endif

    MarkGUCPrefixReserved("pg_qi");

    /*
     * SQL-callable functions remain available when the library is loaded by
     * CREATE EXTENSION. The planner hook is installed only during server
     * preload, which is the required deployment mode for pg_qi.
     */
    if (!process_shared_preload_libraries_in_progress)
        return;

    qi_preflight_force_execution();
    pg_qi_preloaded = true;
    previous_planner_hook = planner_hook;
#if PG_QI_TESTING
    pg_qi_previous_hook_present = previous_planner_hook != NULL;
#endif
    planner_hook = pg_qi_planner;
    qi_context_init_hooks();
}

void
_PG_fini(void)
{
    qi_context_fini_hooks();
    if (planner_hook == pg_qi_planner)
        planner_hook = previous_planner_hook;
}

Datum
pg_qi_version(PG_FUNCTION_ARGS)
{
    PG_RETURN_TEXT_P(cstring_to_text(PG_QI_VERSION));
}

Datum
pg_qi_is_preloaded(PG_FUNCTION_ARGS)
{
    PG_RETURN_BOOL(pg_qi_preloaded);
}
