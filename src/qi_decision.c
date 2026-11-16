/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <limits.h>

#include "pg_qi/qi_decision.h"
#include "utils/guc.h"

static int qi_shadow_contract_version = 1;
static int qi_shadow_decision_id = 0;
static int qi_shadow_policy_intent = 0;
static int qi_shadow_reason = QI_SHADOW_NO_COMPLETED_PLAN;
static int qi_shadow_force_origin = QI_FORCE_OFF;
static int qi_shadow_observed_engine = QI_SHADOW_ENGINE_UNKNOWN;
static int qi_shadow_verification = 0;
static int qi_shadow_context = QI_SHADOW_CONTEXT_UNPROVED_OUTERMOST;
static int qi_shadow_classifier_status = QI_SHADOW_CLASSIFIER_NOT_EVALUATED;
static int qi_shadow_classifier_reason = QI_ELIGIBILITY_CANDIDATE;
static int qi_shadow_observation = QI_SHADOW_OBSERVATION_NONE;
static bool qi_shadow_request_applied = false;

static const struct config_enum_entry qi_policy_intent_options[] = {
    {"pass_through", QI_POLICY_PASS_THROUGH, false},
    {"request_delegation", QI_POLICY_REQUEST_DELEGATION, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_shadow_reason_options[] = {
    {"no_completed_plan", QI_SHADOW_NO_COMPLETED_PLAN, false},
    {"control_disabled", QI_SHADOW_CONTROL_DISABLED, false},
    {"context_nested", QI_SHADOW_CONTEXT_NESTED_REASON, false},
    {"force_external", QI_SHADOW_FORCE_EXTERNAL, false},
    {"classifier_rejected", QI_SHADOW_CLASSIFIER_REJECTED_REASON, false},
    {"policy_missing", QI_SHADOW_POLICY_MISSING, false},
    {"context_unproved", QI_SHADOW_CONTEXT_UNPROVED_REASON, false},
    {"state_poisoned", QI_SHADOW_STATE_POISONED, false},
    {"delegation_requested", QI_SHADOW_DELEGATION_REQUESTED, false},
    {"delegation_declined", QI_SHADOW_DELEGATION_DECLINED, false},
    {"delegation_verified", QI_SHADOW_DELEGATION_VERIFIED, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_force_origin_options[] = {
    {"off", QI_FORCE_OFF, false},
    {"external", QI_FORCE_EXTERNAL, false},
    {"qi_stack", QI_FORCE_QI_STACK, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_engine_options[] = {
    {"unknown", QI_SHADOW_ENGINE_UNKNOWN, false},
    {"duckdb", QI_SHADOW_ENGINE_DUCKDB, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_verification_options[] = {
    {"unverified", QI_VERIFICATION_UNVERIFIED, false},
    {"verified", QI_VERIFICATION_VERIFIED, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_context_options[] = {
    {"unproved_outermost", QI_SHADOW_CONTEXT_UNPROVED_OUTERMOST, false},
    {"nested", QI_SHADOW_CONTEXT_NESTED, false},
    {"proven_client", QI_SHADOW_CONTEXT_PROVEN_CLIENT, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_classifier_status_options[] = {
    {"not_evaluated", QI_SHADOW_CLASSIFIER_NOT_EVALUATED, false},
    {"candidate", QI_SHADOW_CLASSIFIER_CANDIDATE, false},
    {"rejected", QI_SHADOW_CLASSIFIER_REJECTED, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_classifier_reason_options[] = {
    {"candidate", QI_ELIGIBILITY_CANDIDATE, false},
    {"prototype_rejected", QI_ELIGIBILITY_PROTOTYPE_REJECTED, false},
    {"security_context", QI_ELIGIBILITY_SECURITY_CONTEXT, false},
    {"table_sample", QI_ELIGIBILITY_TABLE_SAMPLE, false},
    {"inheritance", QI_ELIGIBILITY_INHERITANCE, false},
    {"unsupported_relation", QI_ELIGIBILITY_UNSUPPORTED_RELATION, false},
    {"unsupported_join", QI_ELIGIBILITY_UNSUPPORTED_JOIN, false},
    {"unsupported_aggregate", QI_ELIGIBILITY_UNSUPPORTED_AGGREGATE, false},
    {"unsupported_grouping", QI_ELIGIBILITY_UNSUPPORTED_GROUPING, false},
    {"unsupported_ordering", QI_ELIGIBILITY_UNSUPPORTED_ORDERING, false},
    {"unsupported_parameter", QI_ELIGIBILITY_UNSUPPORTED_PARAMETER, false},
    {"unsupported_function", QI_ELIGIBILITY_UNSUPPORTED_FUNCTION, false},
    {"unsupported_cast", QI_ELIGIBILITY_UNSUPPORTED_CAST, false},
    {"unsupported_expression", QI_ELIGIBILITY_UNSUPPORTED_EXPRESSION, false},
    {"invalid_var", QI_ELIGIBILITY_INVALID_VAR, false},
    {NULL, 0, false}
};

static const struct config_enum_entry qi_observation_options[] = {
    {"none", QI_SHADOW_OBSERVATION_NONE, false},
    {"no_authoritative_match",
     QI_SHADOW_OBSERVATION_NO_AUTHORITATIVE_MATCH, false},
    {"duckdb_scan_at_downstream_boundary",
     QI_SHADOW_OBSERVATION_DUCKDB_SCAN_AT_DOWNSTREAM_BOUNDARY, false},
    {NULL, 0, false}
};

void
qi_shadow_init_gucs(void)
{
    DefineCustomIntVariable("pg_qi.shadow_contract_version", NULL, NULL,
                            &qi_shadow_contract_version, 1, 1, 1,
                            PGC_INTERNAL, 0, NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.shadow_decision_id", NULL, NULL,
                            &qi_shadow_decision_id, 0, 0, INT_MAX,
                            PGC_INTERNAL, 0, NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_policy_intent", NULL, NULL,
                             &qi_shadow_policy_intent, 0,
                             qi_policy_intent_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_reason", NULL, NULL,
                             &qi_shadow_reason, QI_SHADOW_NO_COMPLETED_PLAN,
                             qi_shadow_reason_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_force_origin", NULL, NULL,
                             &qi_shadow_force_origin, QI_FORCE_OFF,
                             qi_force_origin_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_observed_engine", NULL, NULL,
                             &qi_shadow_observed_engine,
                             QI_SHADOW_ENGINE_UNKNOWN, qi_engine_options,
                             PGC_INTERNAL, 0, NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_verification", NULL, NULL,
                             &qi_shadow_verification, 0,
                             qi_verification_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_context", NULL, NULL,
                             &qi_shadow_context,
                             QI_SHADOW_CONTEXT_UNPROVED_OUTERMOST,
                             qi_context_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_classifier_status", NULL, NULL,
                             &qi_shadow_classifier_status,
                             QI_SHADOW_CLASSIFIER_NOT_EVALUATED,
                             qi_classifier_status_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_classifier_reason", NULL, NULL,
                             &qi_shadow_classifier_reason,
                             QI_ELIGIBILITY_CANDIDATE,
                             qi_classifier_reason_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.shadow_observation", NULL, NULL,
                             &qi_shadow_observation,
                             QI_SHADOW_OBSERVATION_NONE,
                             qi_observation_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi.shadow_request_applied", NULL, NULL,
                             &qi_shadow_request_applied, false,
                             PGC_INTERNAL, 0, NULL, NULL, NULL);
}

void
qi_shadow_publish(QiShadowContext context, QiPolicyIntent policy_intent,
                  QiForceOrigin force_origin,
                  QiShadowClassifierStatus classifier_status,
                  QiEligibilityResult eligibility, QiShadowReason reason,
                  QiShadowEngine observed_engine,
                  QiVerification verification,
                  QiShadowObservation observation, bool request_applied)
{
    if (qi_shadow_decision_id < INT_MAX)
        qi_shadow_decision_id++;
    qi_shadow_policy_intent = policy_intent;
    qi_shadow_reason = reason;
    qi_shadow_force_origin = force_origin;
    qi_shadow_observed_engine = observed_engine;
    qi_shadow_verification = verification;
    qi_shadow_context = context;
    qi_shadow_classifier_status = classifier_status;
    qi_shadow_classifier_reason = eligibility.reason;
    qi_shadow_observation = observation;
    qi_shadow_request_applied = request_applied;
}
