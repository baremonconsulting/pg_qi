/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "common/cryptohash.h"
#include "fmgr.h"
#include "access/xact.h"
#include "pg_qi/qi_lifecycle.h"

#define QI_LIFECYCLE_MAX_PENDING 8
#define QI_LIFECYCLE_MAX_PLAN_NODES 8192
#define QI_LIFECYCLE_DIGEST_BYTES 32

typedef struct QiLifecycleFingerprint
{
    pg_cryptohash_ctx *hash;
    uint32 nodes;
    bool valid;
} QiLifecycleFingerprint;

typedef struct QiLifecyclePlanToken
{
    NodeTag type;
    Cost startup_cost;
    Cost total_cost;
    Cardinality plan_rows;
    int plan_width;
    int target_count;
    int qual_count;
    int init_count;
    bool parallel_aware;
    bool parallel_safe;
    bool async_capable;
} QiLifecyclePlanToken;

typedef struct QiLifecycleStatementToken
{
    CmdType command_type;
    uint64 query_id;
    int jit_flags;
    int subplan_count;
    int relation_count;
    bool has_returning;
    bool has_modifying_cte;
    bool parallel_mode_needed;
} QiLifecycleStatementToken;

typedef struct QiLifecyclePending
{
    const PlannedStmt *planned_stmt;
    uint8 fingerprint[QI_LIFECYCLE_DIGEST_BYTES];
    QueryDesc *query_desc;
    QiV2SnapshotCandidate candidate;
    bool armed;
    bool bound;
    bool fingerprint_valid;
} QiLifecyclePending;

static QiLifecyclePending qi_lifecycle_pending[QI_LIFECYCLE_MAX_PENDING];
static bool qi_lifecycle_callbacks_registered = false;

static void
qi_lifecycle_hash(QiLifecycleFingerprint *state, const void *data, Size size)
{
    if (state->valid &&
        pg_cryptohash_update(state->hash, (const uint8 *) data, size) != 0)
        state->valid = false;
}

static void
qi_lifecycle_hash_plan(QiLifecycleFingerprint *state, const Plan *plan)
{
    QiLifecyclePlanToken token;
    ListCell *cell;

    if (!state->valid)
        return;
    if (plan == NULL)
    {
        NodeTag tag = T_Invalid;

        qi_lifecycle_hash(state, &tag, sizeof(tag));
        return;
    }
    if (++state->nodes > QI_LIFECYCLE_MAX_PLAN_NODES)
    {
        state->valid = false;
        return;
    }

    MemSet(&token, 0, sizeof(token));
    token.type = plan->type;
    token.startup_cost = plan->startup_cost;
    token.total_cost = plan->total_cost;
    token.plan_rows = plan->plan_rows;
    token.plan_width = plan->plan_width;
    token.target_count = list_length(plan->targetlist);
    token.qual_count = list_length(plan->qual);
    token.init_count = list_length(plan->initPlan);
    token.parallel_aware = plan->parallel_aware;
    token.parallel_safe = plan->parallel_safe;
    token.async_capable = plan->async_capable;
    qi_lifecycle_hash(state, &token, sizeof(token));
    qi_lifecycle_hash_plan(state, plan->lefttree);
    qi_lifecycle_hash_plan(state, plan->righttree);

    switch (nodeTag(plan))
    {
        case T_Append:
            foreach(cell, ((const Append *) plan)->appendplans)
                qi_lifecycle_hash_plan(state, (Plan *) lfirst(cell));
            break;
        case T_MergeAppend:
            foreach(cell, ((const MergeAppend *) plan)->mergeplans)
                qi_lifecycle_hash_plan(state, (Plan *) lfirst(cell));
            break;
        case T_BitmapAnd:
            foreach(cell, ((const BitmapAnd *) plan)->bitmapplans)
                qi_lifecycle_hash_plan(state, (Plan *) lfirst(cell));
            break;
        case T_BitmapOr:
            foreach(cell, ((const BitmapOr *) plan)->bitmapplans)
                qi_lifecycle_hash_plan(state, (Plan *) lfirst(cell));
            break;
        case T_CustomScan:
            foreach(cell, ((const CustomScan *) plan)->custom_plans)
                qi_lifecycle_hash_plan(state, (Plan *) lfirst(cell));
            break;
        case T_SubqueryScan:
            qi_lifecycle_hash_plan(state,
                ((const SubqueryScan *) plan)->subplan);
            break;
        default:
            break;
    }
}

static bool
qi_lifecycle_fingerprint(const PlannedStmt *planned_stmt,
                         uint8 digest[QI_LIFECYCLE_DIGEST_BYTES])
{
    QiLifecycleFingerprint state;
    QiLifecycleStatementToken token;
    ListCell *cell;

    if (planned_stmt == NULL)
        return false;
    MemSet(&state, 0, sizeof(state));
    state.hash = pg_cryptohash_create(PG_SHA256);
    if (state.hash == NULL || pg_cryptohash_init(state.hash) != 0)
    {
        if (state.hash != NULL)
            pg_cryptohash_free(state.hash);
        return false;
    }
    state.valid = true;
    MemSet(&token, 0, sizeof(token));
    token.command_type = planned_stmt->commandType;
    token.query_id = planned_stmt->queryId;
    token.jit_flags = planned_stmt->jitFlags;
    token.subplan_count = list_length(planned_stmt->subplans);
    token.relation_count = list_length(planned_stmt->relationOids);
    token.has_returning = planned_stmt->hasReturning;
    token.has_modifying_cte = planned_stmt->hasModifyingCTE;
    token.parallel_mode_needed = planned_stmt->parallelModeNeeded;
    qi_lifecycle_hash(&state, &token, sizeof(token));
    qi_lifecycle_hash_plan(&state, planned_stmt->planTree);
    foreach(cell, planned_stmt->subplans)
        qi_lifecycle_hash_plan(&state, (Plan *) lfirst(cell));
    if (state.valid &&
        pg_cryptohash_final(state.hash, digest,
                            QI_LIFECYCLE_DIGEST_BYTES) != 0)
        state.valid = false;
    pg_cryptohash_free(state.hash);
    return state.valid;
}

static void
qi_lifecycle_xact_callback(XactEvent event, void *arg)
{
    (void) arg;
    if (event == XACT_EVENT_COMMIT || event == XACT_EVENT_ABORT ||
        event == XACT_EVENT_PREPARE)
        qi_lifecycle_reset();
}

static void
qi_lifecycle_subxact_callback(SubXactEvent event, SubTransactionId my_subid,
                              SubTransactionId parent_subid, void *arg)
{
    (void) my_subid;
    (void) parent_subid;
    (void) arg;
    if (event == SUBXACT_EVENT_ABORT_SUB)
        qi_lifecycle_reset();
}

void
qi_lifecycle_init(void)
{
    if (!qi_lifecycle_callbacks_registered)
    {
        RegisterXactCallback(qi_lifecycle_xact_callback, NULL);
        RegisterSubXactCallback(qi_lifecycle_subxact_callback, NULL);
        qi_lifecycle_callbacks_registered = true;
    }
}

void
qi_lifecycle_fini(void)
{
    if (qi_lifecycle_callbacks_registered)
    {
        UnregisterSubXactCallback(qi_lifecycle_subxact_callback, NULL);
        UnregisterXactCallback(qi_lifecycle_xact_callback, NULL);
        qi_lifecycle_callbacks_registered = false;
    }
    qi_lifecycle_reset();
}

static void
qi_lifecycle_clear(QiLifecyclePending *pending)
{
    MemSet(pending, 0, sizeof(*pending));
}

bool
qi_lifecycle_arm(const PlannedStmt *planned_stmt,
                 const QiV2SnapshotCandidate *candidate)
{
    uint8 fingerprint[QI_LIFECYCLE_DIGEST_BYTES];
    int free_slot = -1;
    int i;

    if (planned_stmt == NULL || candidate == NULL || !candidate->valid ||
        planned_stmt->commandType != CMD_SELECT ||
        planned_stmt->hasModifyingCTE)
        return false;
    if (!qi_lifecycle_fingerprint(planned_stmt, fingerprint))
        return false;

    /*
     * A later planning result supersedes every older result that has not
     * reached ExecutorStart.  This deliberately abstains if PostgreSQL later
     * reuses an older cached alternative: a fresh planning observation must
     * never be attributed to that execution.
     */
    for (i = 0; i < QI_LIFECYCLE_MAX_PENDING; i++)
    {
        QiLifecyclePending *pending = &qi_lifecycle_pending[i];

        if (pending->armed && !pending->bound)
            qi_lifecycle_clear(pending);
        if (!pending->armed && free_slot < 0)
            free_slot = i;
    }
    if (free_slot < 0)
        return false;

    qi_lifecycle_pending[free_slot].planned_stmt = planned_stmt;
    memcpy(qi_lifecycle_pending[free_slot].fingerprint, fingerprint,
           QI_LIFECYCLE_DIGEST_BYTES);
    qi_lifecycle_pending[free_slot].candidate = *candidate;
    qi_lifecycle_pending[free_slot].candidate.lifecycle =
        QI_PLAN_LIFECYCLE_FRESH_EXECUTION;
    qi_lifecycle_pending[free_slot].armed = true;
    qi_lifecycle_pending[free_slot].fingerprint_valid = true;
    return true;
}

void
qi_lifecycle_executor_start(QueryDesc *query_desc, bool outermost,
                            bool context_proven)
{
    QiLifecyclePending *match = NULL;
    uint8 fingerprint[QI_LIFECYCLE_DIGEST_BYTES];
    int matches = 0;
    int i;

    if (!outermost || !context_proven || query_desc == NULL ||
        query_desc->plannedstmt == NULL)
        return;
    for (i = 0; i < QI_LIFECYCLE_MAX_PENDING; i++)
    {
        QiLifecyclePending *pending = &qi_lifecycle_pending[i];

        if (pending->armed && !pending->bound)
            break;
    }
    if (i == QI_LIFECYCLE_MAX_PENDING)
        return;

    /*
     * The exact object armed by the immediately preceding planner call is an
     * unambiguous identity proof.  The digest path handles PostgreSQL copies.
     */
    for (i = 0; i < QI_LIFECYCLE_MAX_PENDING; i++)
    {
        QiLifecyclePending *pending = &qi_lifecycle_pending[i];

        if (pending->armed && !pending->bound &&
            pending->planned_stmt == query_desc->plannedstmt)
        {
            match = pending;
            matches++;
        }
    }

    if (matches == 0 &&
        !qi_lifecycle_fingerprint(query_desc->plannedstmt, fingerprint))
    {
        qi_lifecycle_reset();
        return;
    }
    if (matches == 0)
    {
        for (i = 0; i < QI_LIFECYCLE_MAX_PENDING; i++)
        {
            QiLifecyclePending *pending = &qi_lifecycle_pending[i];

            if (pending->armed && !pending->bound &&
                pending->fingerprint_valid &&
                memcmp(pending->fingerprint, fingerprint,
                       QI_LIFECYCLE_DIGEST_BYTES) == 0)
            {
                match = pending;
                matches++;
            }
        }
    }
    if (matches != 1)
    {
        qi_lifecycle_reset();
        return;
    }
    /* The first outer execution retires every other unbound candidate. */
    for (i = 0; i < QI_LIFECYCLE_MAX_PENDING; i++)
    {
        QiLifecyclePending *pending = &qi_lifecycle_pending[i];

        if (pending != match && pending->armed && !pending->bound)
            qi_lifecycle_clear(pending);
    }
    match->query_desc = query_desc;
    match->bound = true;
    MemSet(match->fingerprint, 0, sizeof(match->fingerprint));
    match->fingerprint_valid = false;
}

void
qi_lifecycle_executor_run(QueryDesc *query_desc)
{
    int i;

    if (query_desc == NULL)
        return;
    for (i = 0; i < QI_LIFECYCLE_MAX_PENDING; i++)
    {
        QiLifecyclePending *pending = &qi_lifecycle_pending[i];

        if (pending->armed && pending->bound &&
            pending->query_desc == query_desc)
        {
            (void) qi_snapshot_commit(&pending->candidate);
            qi_lifecycle_clear(pending);
            return;
        }
    }
}

void
qi_lifecycle_executor_end(QueryDesc *query_desc)
{
    int i;

    if (query_desc == NULL)
        return;
    for (i = 0; i < QI_LIFECYCLE_MAX_PENDING; i++)
    {
        QiLifecyclePending *pending = &qi_lifecycle_pending[i];

        if (pending->armed && pending->bound &&
            pending->query_desc == query_desc)
        {
            qi_lifecycle_clear(pending);
            return;
        }
    }
}

void
qi_lifecycle_reset(void)
{
    int i;

    for (i = 0; i < QI_LIFECYCLE_MAX_PENDING; i++)
        qi_lifecycle_clear(&qi_lifecycle_pending[i]);
}

#if PG_QI_TESTING
Datum
qi_test_lifecycle_contract(PG_FUNCTION_ARGS)
{
    QiLifecyclePending saved[QI_LIFECYCLE_MAX_PENDING];
    QiV2SnapshotCandidate candidate;
    PlannedStmt plans[3];
    QueryDesc query_desc;
    bool latest_supersedes;
    bool ambiguous_abstains;
    bool exact_binding;
    bool reset_clears;
    int armed_count;
    int i;

    memcpy(saved, qi_lifecycle_pending, sizeof(saved));
    qi_lifecycle_reset();
    MemSet(&candidate, 0, sizeof(candidate));
    candidate.valid = true;
    candidate.token.valid = true;
    MemSet(plans, 0, sizeof(plans));
    for (i = 0; i < 3; i++)
    {
        plans[i].type = T_PlannedStmt;
        plans[i].commandType = CMD_SELECT;
        plans[i].queryId = (uint64) i + 1;
    }
    latest_supersedes = qi_lifecycle_arm(&plans[0], &candidate) &&
        qi_lifecycle_arm(&plans[1], &candidate);
    armed_count = 0;
    for (i = 0; i < QI_LIFECYCLE_MAX_PENDING; i++)
    {
        if (qi_lifecycle_pending[i].armed)
        {
            armed_count++;
            latest_supersedes = latest_supersedes &&
                qi_lifecycle_pending[i].planned_stmt == &plans[1];
        }
    }
    latest_supersedes = latest_supersedes && armed_count == 1;
    plans[0].queryId = 100;
    plans[1].queryId = 100;
    plans[2].queryId = 100;
    qi_lifecycle_reset();
    (void) qi_lifecycle_arm(&plans[0], &candidate);
    qi_lifecycle_pending[1] = qi_lifecycle_pending[0];
    qi_lifecycle_pending[1].planned_stmt = &plans[1];
    MemSet(&query_desc, 0, sizeof(query_desc));
    query_desc.plannedstmt = &plans[2];
    qi_lifecycle_executor_start(&query_desc, true, true);
    ambiguous_abstains = !qi_lifecycle_pending[0].bound &&
        !qi_lifecycle_pending[1].bound;
    qi_lifecycle_reset();
    plans[0].queryId = 1;
    plans[1].queryId = 2;
    latest_supersedes = latest_supersedes &&
        qi_lifecycle_arm(&plans[0], &candidate);
    MemSet(&query_desc, 0, sizeof(query_desc));
    query_desc.plannedstmt = &plans[1];
    qi_lifecycle_executor_start(&query_desc, true, true);
    exact_binding = !qi_lifecycle_pending[0].armed;
    (void) qi_lifecycle_arm(&plans[0], &candidate);
    query_desc.plannedstmt = &plans[0];
    qi_lifecycle_executor_start(&query_desc, true, true);
    exact_binding = exact_binding && qi_lifecycle_pending[0].bound &&
        qi_lifecycle_pending[0].query_desc == &query_desc;
    qi_lifecycle_executor_end(&query_desc);
    exact_binding = exact_binding && !qi_lifecycle_pending[0].armed;
    (void) qi_lifecycle_arm(&plans[0], &candidate);
    qi_lifecycle_reset();
    reset_clears = !qi_lifecycle_pending[0].armed;
    memcpy(qi_lifecycle_pending, saved, sizeof(saved));
    PG_RETURN_BOOL(latest_supersedes && ambiguous_abstains && exact_binding &&
                   reset_clears);
}
#endif
