/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/htup_details.h"
#include "catalog/pg_authid.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "nodes/parsenodes.h"
#include "pg_qi/qi_snapshot.h"
#include "utils/acl.h"
#include "utils/guc.h"
#include "utils/syscache.h"

#define QI_SNAPSHOT_INCARNATION_BYTES 16
#define QI_SNAPSHOT_RENDER_BYTES 1024
#define QI_PLAN_FEATURE_RENDER_BYTES 1024

typedef struct QiV2SnapshotEvent
{
    QiV2SnapshotCandidate candidate;
    uint64 event_sequence;
} QiV2SnapshotEvent;

typedef struct QiV2SnapshotStore
{
    bool initialized;
    bool available;
    bool exhausted;
    bool has_event;
    uint8 incarnation[QI_SNAPSHOT_INCARNATION_BYTES];
    uint64 generation;
    uint64 event_sequence;
    int active_slot;
    QiV2SnapshotEvent slots[2];
} QiV2SnapshotStore;

static QiV2SnapshotStore qi_snapshot_store;
static char *qi_snapshot_placeholder = NULL;
static char qi_snapshot_render_buffer[QI_SNAPSHOT_RENDER_BYTES];
static char *qi_plan_feature_placeholder = NULL;
static char qi_plan_feature_render_buffer[QI_PLAN_FEATURE_RENDER_BYTES];

static void
qi_snapshot_initialize(void)
{
    if (qi_snapshot_store.initialized)
        return;

    qi_snapshot_store.initialized = true;
    qi_snapshot_store.generation = 1;
    qi_snapshot_store.active_slot = 0;
    qi_snapshot_store.available =
        pg_strong_random(qi_snapshot_store.incarnation,
                         sizeof(qi_snapshot_store.incarnation));
}

static void
qi_snapshot_reset_lifetime(void)
{
    qi_snapshot_initialize();
    qi_snapshot_store.has_event = false;
    MemSet(qi_snapshot_store.slots, 0, sizeof(qi_snapshot_store.slots));

    /* Counter exhaustion is backend-lifetime state and reconnect-only. */
    if (!qi_snapshot_store.exhausted)
        qi_snapshot_store.event_sequence = 0;

    if (qi_snapshot_store.generation == PG_UINT64_MAX)
    {
        qi_snapshot_store.available = false;
        qi_snapshot_store.exhausted = true;
        return;
    }
    qi_snapshot_store.generation++;
}

static bool
qi_snapshot_authorized(void)
{
    Oid role_oid;
    HeapTuple role_tuple;
    bool authorized;

    if (superuser())
        return true;
    role_oid = get_role_oid(QI_SNAPSHOT_DIAGNOSTIC_ROLE, true);
    if (!OidIsValid(role_oid))
        return false;
    role_tuple = SearchSysCache1(AUTHOID, ObjectIdGetDatum(role_oid));
    if (!HeapTupleIsValid(role_tuple))
        return false;
    authorized = !((Form_pg_authid) GETSTRUCT(role_tuple))->rolcanlogin &&
        has_privs_of_role(GetUserId(), role_oid);
    ReleaseSysCache(role_tuple);
    return authorized;
}

static const char *
qi_mode_name(QiRoutingMode mode)
{
    switch (mode)
    {
        case QI_ROUTING_OFF: return "off";
        case QI_ROUTING_ALLOWLIST: return "allowlist";
        case QI_ROUTING_SHADOW_P0: return "shadow_p0";
    }
    return "invalid";
}

static const char *
qi_gate_name(QiGateState state)
{
    switch (state)
    {
        case QI_GATE_NOT_EVALUATED: return "not_evaluated";
        case QI_GATE_PASS: return "pass";
        case QI_GATE_VETO: return "veto";
        case QI_GATE_UNKNOWN: return "unknown";
    }
    return "invalid";
}

static const char *
qi_recommendation_name(QiV2Recommendation recommendation)
{
    switch (recommendation)
    {
        case QI_V2_WOULD_PASS_THROUGH: return "would_pass_through";
        case QI_V2_WOULD_DELEGATE: return "would_delegate";
        case QI_V2_INSUFFICIENT_EVIDENCE: return "insufficient_evidence";
    }
    return "invalid";
}

static const char *
qi_context_name(QiShadowContext context)
{
    switch (context)
    {
        case QI_SHADOW_CONTEXT_UNPROVED_OUTERMOST: return "unproved_outermost";
        case QI_SHADOW_CONTEXT_NESTED: return "nested";
        case QI_SHADOW_CONTEXT_PROVEN_CLIENT: return "proven_client";
    }
    return "invalid";
}

static const char *
qi_policy_name(QiPolicyIntent policy)
{
    return policy == QI_POLICY_REQUEST_DELEGATION
        ? "request_delegation" : "pass_through";
}

static const char *
qi_force_name(QiForceOrigin force)
{
    switch (force)
    {
        case QI_FORCE_OFF: return "off";
        case QI_FORCE_EXTERNAL: return "external";
        case QI_FORCE_QI_STACK: return "qi_stack";
    }
    return "invalid";
}

static const char *
qi_engine_name(QiShadowEngine engine)
{
    return engine == QI_SHADOW_ENGINE_DUCKDB ? "duckdb" : "unknown";
}

static const char *
qi_verification_name(QiVerification verification)
{
    return verification == QI_VERIFICATION_VERIFIED
        ? "verified" : "unverified";
}

static const char *
qi_observation_name(QiShadowObservation observation)
{
    switch (observation)
    {
        case QI_SHADOW_OBSERVATION_NONE: return "none";
        case QI_SHADOW_OBSERVATION_NO_AUTHORITATIVE_MATCH:
            return "no_authoritative_match";
        case QI_SHADOW_OBSERVATION_DUCKDB_SCAN_AT_DOWNSTREAM_BOUNDARY:
            return "duckdb_scan_at_downstream_boundary";
    }
    return "invalid";
}

static const char *
qi_p0_state_name(QiShadowP0FactsState state)
{
    return state == QI_SHADOW_P0_FACTS_BOUNDED ? "bounded" : "unknown";
}

static const char *
qi_p0_reason_name(QiShadowP0FactsReason reason)
{
    switch (reason)
    {
        case QI_SHADOW_P0_FACTS_OK: return "ok";
        case QI_SHADOW_P0_FACTS_NO_QUERY: return "no_query";
        case QI_SHADOW_P0_FACTS_LIMIT: return "limit";
        case QI_SHADOW_P0_FACTS_CATALOG_MISS: return "catalog_miss";
        case QI_SHADOW_P0_FACTS_UNSUPPORTED_SHAPE:
            return "unsupported_shape";
        case QI_SHADOW_P0_FACTS_UNSUPPORTED_NODE:
            return "unsupported_node";
        case QI_SHADOW_P0_FACTS_UNSUPPORTED_RELATION:
            return "unsupported_relation";
        case QI_SHADOW_P0_FACTS_CATALOG_MISMATCH:
            return "catalog_mismatch";
    }
    return "invalid";
}

static void
qi_snapshot_incarnation_hex(char *destination, Size destination_size)
{
    static const char digits[] = "0123456789abcdef";
    Size i;

    if (destination_size < QI_SNAPSHOT_INCARNATION_BYTES * 2 + 1)
        return;
    for (i = 0; i < QI_SNAPSHOT_INCARNATION_BYTES; i++)
    {
        destination[i * 2] = digits[qi_snapshot_store.incarnation[i] >> 4];
        destination[i * 2 + 1] = digits[qi_snapshot_store.incarnation[i] & 15];
    }
    destination[QI_SNAPSHOT_INCARNATION_BYTES * 2] = '\0';
}

static const char *
qi_snapshot_show(void)
{
    QiV2SnapshotEvent *event;
    QiV2SnapshotCandidate *candidate;
    QiV2Decision *decision;
    char incarnation[QI_SNAPSHOT_INCARNATION_BYTES * 2 + 1] = "";
    int written;

    qi_snapshot_initialize();
    if (!qi_snapshot_authorized())
        return "v=2;sv=1;s=denied";
    if (qi_snapshot_store.exhausted)
        return "v=2;sv=1;s=exhausted";
    if (!qi_snapshot_store.available)
        return "v=2;sv=1;s=unavailable";

    qi_snapshot_incarnation_hex(incarnation, sizeof(incarnation));
    if (!qi_snapshot_store.has_event)
    {
        snprintf(qi_snapshot_render_buffer, sizeof(qi_snapshot_render_buffer),
                 "v=2;sv=1;s=none;cid=%s;g=" UINT64_FORMAT ";e=0",
                 incarnation, qi_snapshot_store.generation);
        return qi_snapshot_render_buffer;
    }

    event = &qi_snapshot_store.slots[qi_snapshot_store.active_slot];
    candidate = &event->candidate;
    if (candidate->token.effective_user != GetUserId() ||
        candidate->token.session_user != GetSessionUserId())
        return "v=2;sv=1;s=withheld";

    decision = &candidate->decision;
    written = snprintf(qi_snapshot_render_buffer,
        sizeof(qi_snapshot_render_buffer),
        "v=2;sv=1;s=complete;cid=%s;g=" UINT64_FORMAT
        ";e=" UINT64_FORMAT ";mode=%s;enabled=%s;context=%s"
        ";eligible=%s;safe=%s;beneficial=%s;barrier=%s"
        ";recommendation=%s;reason=%d;rule=%d;model=%d;certificate=%d"
        ";policy=%s;force=%s;engine=%s;verification=%s;observation=%s"
        ";p0=%s;p0_reason=%s",
        incarnation, candidate->token.generation, event->event_sequence,
        qi_mode_name(candidate->mode_at_decision),
        candidate->enabled_at_decision ? "on" : "off",
        qi_context_name(candidate->context),
        qi_gate_name(decision->eligible),
        qi_gate_name(decision->semantically_safe),
        qi_gate_name(decision->beneficial),
        decision->activation_barrier == QI_V2_ACTIVATION_OPEN
            ? "open" : "closed",
        qi_recommendation_name(decision->recommendation),
        decision->reason_code, decision->rule_version,
        decision->model_version, decision->certificate_version,
        qi_policy_name(decision->policy_intent),
        qi_force_name(decision->force_origin),
        qi_engine_name(decision->observed_engine),
        qi_verification_name(candidate->verification),
        qi_observation_name(candidate->observation),
        qi_p0_state_name(candidate->p0_state),
        qi_p0_reason_name(candidate->p0_reason));
    if (written < 0 || (Size) written >= sizeof(qi_snapshot_render_buffer))
        return "v=2;sv=1;s=invalid";
    return qi_snapshot_render_buffer;
}

static const char *
qi_plan_features_show(void)
{
    QiV2SnapshotEvent *event;
    QiV2SnapshotCandidate *candidate;
    QiNativePlanFeatures *features;
    char incarnation[QI_SNAPSHOT_INCARNATION_BYTES * 2 + 1] = "";
    char relpages[32];
    char reltuples[64];
    int written;

    qi_snapshot_initialize();
    if (!qi_snapshot_authorized())
        return "v=1;s=denied";
    if (qi_snapshot_store.exhausted)
        return "v=1;s=exhausted";
    if (!qi_snapshot_store.available)
        return "v=1;s=unavailable";
    qi_snapshot_incarnation_hex(incarnation, sizeof(incarnation));
    if (!qi_snapshot_store.has_event)
    {
        snprintf(qi_plan_feature_render_buffer,
                 sizeof(qi_plan_feature_render_buffer),
                 "v=1;s=none;cid=%s;g=" UINT64_FORMAT ";e=0",
                 incarnation, qi_snapshot_store.generation);
        return qi_plan_feature_render_buffer;
    }

    event = &qi_snapshot_store.slots[qi_snapshot_store.active_slot];
    candidate = &event->candidate;
    if (candidate->token.effective_user != GetUserId() ||
        candidate->token.session_user != GetSessionUserId())
        return "v=1;s=withheld";
    features = &candidate->plan_features;
    if (features->state != QI_PLAN_FEATURE_BOUNDED)
    {
        written = snprintf(qi_plan_feature_render_buffer,
            sizeof(qi_plan_feature_render_buffer),
            "v=1;s=complete;cid=%s;g=" UINT64_FORMAT ";e=" UINT64_FORMAT
            ";state=%s;reason=%s;spine=%s;known=" UINT64_FORMAT
            ";nodes=%u;depth=%u",
            incarnation, candidate->token.generation, event->event_sequence,
            qi_plan_feature_state_name(features->state),
            qi_plan_feature_reason_name(features->reason),
            qi_plan_feature_spine_name(features->spine),
            features->known_fields, features->nodes_seen,
            features->max_depth_seen);
        if (written < 0 ||
            (Size) written >= sizeof(qi_plan_feature_render_buffer))
            return "v=1;s=invalid";
        return qi_plan_feature_render_buffer;
    }
    if ((features->known_fields & QI_PLAN_KNOWN_RELPAGES) != 0)
        snprintf(relpages, sizeof(relpages), "%d", features->relpages);
    else
        strlcpy(relpages, "unknown", sizeof(relpages));
    if ((features->known_fields & QI_PLAN_KNOWN_RELTUPLES) != 0)
        snprintf(reltuples, sizeof(reltuples), "%.17g", features->reltuples);
    else
        strlcpy(reltuples, "unknown", sizeof(reltuples));
    written = snprintf(qi_plan_feature_render_buffer,
        sizeof(qi_plan_feature_render_buffer),
        "v=1;s=complete;cid=%s;g=" UINT64_FORMAT ";e=" UINT64_FORMAT
        ";state=%s;reason=%s;spine=%s;known=" UINT64_FORMAT
        ";relation=%u;agg_startup=%.17g;agg_total=%.17g;agg_rows=%.17g"
        ";agg_width=%d;scan_startup=%.17g;scan_total=%.17g"
        ";scan_rows=%.17g;scan_width=%d"
        ";catalog_relpages=%s;catalog_reltuples=%s"
        ";agg_parallel_safe=%s;scan_parallel_safe=%s;parallel_aware=%s"
        ";parallel_mode_needed=%s;workers=%d;has_limit=%s"
        ";nodes=%u;depth=%u",
        incarnation, candidate->token.generation, event->event_sequence,
        qi_plan_feature_state_name(features->state),
        qi_plan_feature_reason_name(features->reason),
        qi_plan_feature_spine_name(features->spine),
        features->known_fields, features->relation_oid,
        features->aggregate.startup_cost, features->aggregate.total_cost,
        features->aggregate.rows, features->aggregate.width,
        features->scan.startup_cost, features->scan.total_cost,
        features->scan.rows, features->scan.width,
        relpages, reltuples,
        features->aggregate_parallel_safe ? "true" : "false",
        features->scan_parallel_safe ? "true" : "false",
        features->parallel_aware ? "true" : "false",
        features->parallel_mode_needed ? "true" : "false",
        features->workers_planned,
        features->has_limit ? "true" : "false",
        features->nodes_seen, features->max_depth_seen);
    if (written < 0 || (Size) written >= sizeof(qi_plan_feature_render_buffer))
        return "v=1;s=invalid";
    return qi_plan_feature_render_buffer;
}

void
qi_snapshot_init_guc(void)
{
    DefineCustomStringVariable("pg_qi.decision_v2",
                               "Reports the private atomic V2 decision record.",
                               NULL, &qi_snapshot_placeholder, "",
                               PGC_INTERNAL,
                               GUC_NO_SHOW_ALL | GUC_NOT_IN_SAMPLE,
                               NULL, NULL, qi_snapshot_show);
    DefineCustomStringVariable("pg_qi.plan_features",
                               "Reports private bounded native plan features.",
                               NULL, &qi_plan_feature_placeholder, "",
                               PGC_INTERNAL,
                               GUC_NO_SHOW_ALL | GUC_NOT_IN_SAMPLE,
                               NULL, NULL, qi_plan_features_show);
}

QiV2SnapshotToken
qi_snapshot_begin(void)
{
    QiV2SnapshotToken token;

    qi_snapshot_initialize();
    token.generation = qi_snapshot_store.generation;
    token.effective_user = GetUserId();
    token.session_user = GetSessionUserId();
    token.valid = qi_snapshot_store.available && !qi_snapshot_store.exhausted;
    return token;
}

bool
qi_snapshot_prepare(QiV2SnapshotCandidate *candidate,
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
                    QiNativePlanFeatures plan_features)
{
    QiV2Decision decision;

    if (candidate == NULL || !token.valid ||
        mode_at_decision < QI_ROUTING_OFF ||
        mode_at_decision > QI_ROUTING_SHADOW_P0 ||
        context != QI_SHADOW_CONTEXT_PROVEN_CLIENT)
        return false;

    decision = qi_v2_evaluate(QI_GATE_NOT_EVALUATED,
                              qi_semantic_gate_state(&semantic),
                              QI_GATE_NOT_EVALUATED,
                              QI_V2_ACTIVATION_CLOSED,
                              QI_DECISION_V2_RULE_VERSION,
                              QI_DECISION_V2_MODEL_VERSION,
                              QI_DECISION_V2_CERTIFICATE_VERSION);
    decision.policy_intent = policy_intent;
    decision.force_origin = force_origin;
    decision.observed_engine = observed_engine;
    if (!qi_v2_decision_valid(&decision))
        return false;

    MemSet(candidate, 0, sizeof(*candidate));
    candidate->token = token;
    candidate->mode_at_decision = mode_at_decision;
    candidate->enabled_at_decision = enabled_at_decision;
    candidate->context = context;
    candidate->decision = decision;
    candidate->verification = verification;
    candidate->observation = observation;
    candidate->p0_state = p0_state;
    candidate->p0_reason = p0_reason;
    candidate->semantic = semantic;
    candidate->plan_features = plan_features;
    candidate->valid = true;
    return true;
}

bool
qi_snapshot_commit(const QiV2SnapshotCandidate *candidate)
{
    int inactive_slot;
    QiV2SnapshotEvent event;

    if (candidate == NULL || !candidate->valid ||
        !qi_snapshot_store.available || qi_snapshot_store.exhausted ||
        candidate->token.generation != qi_snapshot_store.generation ||
        candidate->token.effective_user != GetUserId() ||
        candidate->token.session_user != GetSessionUserId())
        return false;

    if (qi_snapshot_store.event_sequence == PG_UINT64_MAX)
    {
        qi_snapshot_store.exhausted = true;
        return false;
    }

    event.candidate = *candidate;
    event.event_sequence = qi_snapshot_store.event_sequence + 1;
    inactive_slot = qi_snapshot_store.active_slot == 0 ? 1 : 0;
    qi_snapshot_store.slots[inactive_slot] = event;
    qi_snapshot_store.event_sequence = event.event_sequence;
    qi_snapshot_store.active_slot = inactive_slot;
    qi_snapshot_store.has_event = true;
    return true;
}

void
qi_snapshot_handle_utility(Node *utility_stmt)
{
    if (utility_stmt == NULL)
        return;
    if (IsA(utility_stmt, DiscardStmt) &&
        ((DiscardStmt *) utility_stmt)->target == DISCARD_ALL)
        qi_snapshot_reset_lifetime();
    else if (IsA(utility_stmt, VariableSetStmt))
    {
        VariableSetStmt *set = (VariableSetStmt *) utility_stmt;

        if (set->name != NULL &&
            (strcmp(set->name, "role") == 0 ||
             strcmp(set->name, "session_authorization") == 0))
            qi_snapshot_reset_lifetime();
    }
}

#if PG_QI_TESTING
Datum
qi_test_v2_snapshot_contract(PG_FUNCTION_ARGS)
{
    QiV2SnapshotStore saved = qi_snapshot_store;
    QiV2SnapshotToken token;
    QiV2SnapshotCandidate candidate;
    bool first_commit;
    bool sequence_one;
    bool saturation_rejected;
    bool saturation_latched;
    bool reset_changed_generation;
    bool generation_saturation_latched;
    uint64 original_generation;

    MemSet(&qi_snapshot_store, 0, sizeof(qi_snapshot_store));
    token = qi_snapshot_begin();
    original_generation = token.generation;
    first_commit = qi_snapshot_prepare(
        &candidate, token, QI_ROUTING_SHADOW_P0, true,
        QI_SHADOW_CONTEXT_PROVEN_CLIENT, QI_POLICY_PASS_THROUGH,
        QI_FORCE_OFF, QI_SHADOW_ENGINE_UNKNOWN,
        QI_VERIFICATION_UNVERIFIED,
        QI_SHADOW_OBSERVATION_NO_AUTHORITATIVE_MATCH,
        (QiSemanticFacts) {QI_SEMANTIC_UNKNOWN,
                           QI_SEMANTIC_PROFILE_COUNT_STAR,
                           QI_SEMANTIC_BLOCKER_INACTIVE |
                           QI_SEMANTIC_BLOCKER_BUILD_IDENTITY_UNVERIFIED |
                           QI_SEMANTIC_BLOCKER_SNAPSHOT_UNPROVEN},
        QI_SHADOW_P0_FACTS_BOUNDED, QI_SHADOW_P0_FACTS_OK,
        qi_plan_features_unknown(QI_PLAN_FEATURE_NOT_OBSERVED)) &&
        qi_snapshot_commit(&candidate);
    sequence_one = qi_snapshot_store.has_event &&
        qi_snapshot_store.event_sequence == 1;
    qi_snapshot_store.event_sequence = PG_UINT64_MAX;
    saturation_rejected = !qi_snapshot_commit(&candidate);
    saturation_latched = qi_snapshot_store.exhausted &&
        qi_snapshot_store.event_sequence == PG_UINT64_MAX;
    qi_snapshot_reset_lifetime();
    reset_changed_generation = !qi_snapshot_store.has_event &&
        qi_snapshot_store.generation == original_generation + 1 &&
        qi_snapshot_store.exhausted &&
        qi_snapshot_store.event_sequence == PG_UINT64_MAX;
    qi_snapshot_store.exhausted = false;
    qi_snapshot_store.available = true;
    qi_snapshot_store.generation = PG_UINT64_MAX;
    qi_snapshot_reset_lifetime();
    generation_saturation_latched = !qi_snapshot_store.available &&
        qi_snapshot_store.exhausted &&
        qi_snapshot_store.generation == PG_UINT64_MAX;
    qi_snapshot_store = saved;

    PG_RETURN_BOOL(first_commit && sequence_one && saturation_rejected &&
                   saturation_latched && reset_changed_generation &&
                   generation_saturation_latched);
}
#endif
