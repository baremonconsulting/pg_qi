/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Test-only execution witness for PQ-31. This module is never installed as
 * part of pg_qi and deliberately records no SQL, relation names, or values.
 */

#include "postgres.h"

#include <limits.h>

#include "executor/executor.h"
#include "executor/instrument.h"
#include "fmgr.h"
#include "nodes/execnodes.h"
#include "nodes/extensible.h"
#include "nodes/nodeFuncs.h"
#include "nodes/plannodes.h"
#include "portability/instr_time.h"
#include "tcop/tcopprot.h"
#include "utils/guc.h"
#include "miscadmin.h"

PG_MODULE_MAGIC;

#define WITNESS_RECORD_MAX 8192
#define WITNESS_META_MAX 64
#define WITNESS_GUC_FLAGS (GUC_NOT_IN_SAMPLE | GUC_NO_SHOW_ALL)

typedef struct WitnessWalk
{
    PlanState *root;
    bool root_run_completed;
    bool any_custom;
    bool duckdb_present;
    bool duckdb_executed;
    bool other_present;
    bool other_executed;
    bool attribution_incomplete;
    bool any_node_executed;
    int custom_nodes;
    int completed_custom_nodes;
} WitnessWalk;

static ExecutorStart_hook_type previous_start_hook = NULL;
static ExecutorRun_hook_type previous_run_hook = NULL;
static ExecutorFinish_hook_type previous_finish_hook = NULL;
static ExecutorEnd_hook_type previous_end_hook = NULL;

static QueryDesc *active_query = NULL;
static bool active_published = false;
static bool active_run_completed = false;
static bool witness_armed = false;
static bool armed_consumed = false;
static int witness_sequence = 0;
static char *last_record = NULL;
static char last_record_buffer[WITNESS_RECORD_MAX] =
    "v=1;seq=0;status=none;engine=unknown;token=0000000000000000000000000000000000000000000000000000000000000000;pid=0;db=0;user=0;meta=;columns=0;truncated=f;custom_nodes=0;custom_completed=0;duckdb_present=f";
static char *subject_token = NULL;

static bool
subject_token_check(char **new_value, void **extra, GucSource source)
{
    size_t index;
    (void) extra;
    (void) source;
    if (*new_value == NULL || strlen(*new_value) != 64)
        return false;
    for (index = 0; index < 64; index++)
        if (!(((*new_value)[index] >= '0' && (*new_value)[index] <= '9') ||
              ((*new_value)[index] >= 'a' && (*new_value)[index] <= 'f')))
            return false;
    return true;
}

static const char *
last_record_show(void)
{
    return last_record_buffer;
}

void _PG_init(void);
void _PG_fini(void);

static void
witness_armed_assign(bool new_value, void *extra)
{
    (void) extra;
    if (new_value && !witness_armed)
        armed_consumed = false;
}

#if PG_VERSION_NUM >= 180000
#define WITNESS_RUN_PROTO(name) \
    static void name(QueryDesc *query_desc, ScanDirection direction, uint64 count)
#define WITNESS_RUN_CALL(function) function(query_desc, direction, count)
#else
#define WITNESS_RUN_PROTO(name) \
    static void name(QueryDesc *query_desc, ScanDirection direction, uint64 count, \
                     bool execute_once)
#define WITNESS_RUN_CALL(function) \
    function(query_desc, direction, count, execute_once)
#endif

static bool
node_completed_loop(const PlanState *plan_state, const WitnessWalk *walk)
{
#if PG_VERSION_NUM >= 190000
    const NodeInstrumentation *instrument = plan_state->instrument;
#else
    const Instrumentation *instrument = plan_state->instrument;
#endif

    if (plan_state == walk->root && walk->root_run_completed)
        return true;
    if (instrument == NULL)
        return false;
    if (instrument->running)
        return true;
    return instrument->nloops > 0 || instrument->tuplecount > 0 ||
        instrument->ntuples > 0 || !INSTR_TIME_IS_ZERO(instrument->counter);
}

static bool
witness_plan_walker(PlanState *plan_state, void *context)
{
    WitnessWalk *walk = context;

    if (plan_state == NULL)
        return false;
    walk->any_node_executed |= node_completed_loop(plan_state, walk);
    if (IsA(plan_state->plan, CustomScan))
    {
        const CustomScan *custom_scan = (const CustomScan *) plan_state->plan;
        const char *provider = custom_scan->methods != NULL
            ? custom_scan->methods->CustomName : NULL;
        bool executed = node_completed_loop(plan_state, walk);

        walk->any_custom = true;
        walk->custom_nodes++;
        if (executed)
            walk->completed_custom_nodes++;
        if (provider == NULL)
            walk->attribution_incomplete = true;
        else if (strcmp(provider, "DuckDBScan") == 0)
        {
            walk->duckdb_present = true;
            walk->duckdb_executed |= executed;
        }
        else
        {
            walk->other_present = true;
            walk->other_executed |= executed;
        }
        if (!executed)
            walk->attribution_incomplete = true;
    }
    return planstate_tree_walker(plan_state, witness_plan_walker, context);
}

static const char *
witness_engine(QueryDesc *query_desc, WitnessWalk *walk)
{
    if (query_desc == NULL || query_desc->planstate == NULL)
        return "unknown";
    walk->root = query_desc->planstate;
    walk->root_run_completed = active_run_completed;
    (void) witness_plan_walker(query_desc->planstate, walk);
    if (walk->duckdb_executed && walk->other_executed)
        return "mixed";
    if (walk->attribution_incomplete)
        return "unknown";
    if (walk->duckdb_executed && !walk->other_present)
        return "duckdb";
    if (!walk->any_custom)
        return "postgres";
    return "unknown";
}

static const char *
witness_error_engine(QueryDesc *query_desc, WitnessWalk *walk)
{
    if (query_desc == NULL || query_desc->planstate == NULL)
        return "unknown";
    walk->root = query_desc->planstate;
    walk->root_run_completed = false;
    (void) witness_plan_walker(query_desc->planstate, walk);
    if (walk->duckdb_executed && walk->other_executed)
        return "mixed";
    if (walk->attribution_incomplete || walk->other_present)
        return "unknown";
    if (walk->duckdb_executed)
        return "duckdb";
    if (!walk->any_custom && walk->any_node_executed)
        return "postgres";
    return "unknown";
}

static void
publish_record(QueryDesc *query_desc, const char *status, bool force_unknown)
{
    char buffer[WITNESS_RECORD_MAX];
    WitnessWalk walk = {0};
    const char *engine = force_unknown ? "unknown" :
        ((strcmp(status, "error") == 0 && !active_run_completed) ?
         witness_error_engine(query_desc, &walk) : witness_engine(query_desc, &walk));
    TupleDesc tuple_desc = query_desc != NULL ? query_desc->tupDesc : NULL;
    int columns = tuple_desc != NULL ? tuple_desc->natts : 0;
    int visible = 0;
    int offset;
    int index;
    bool truncated = false;

    if (active_published)
        return;
    if (witness_sequence == INT_MAX)
        witness_sequence = 1;
    else
        witness_sequence++;

    offset = snprintf(buffer, sizeof(buffer),
                      "v=1;seq=%d;status=%s;engine=%s;token=%s;pid=%d;db=%u;user=%u;meta=",
                      witness_sequence, status, engine,
                      subject_token != NULL ? subject_token : "",
                      MyProcPid, MyDatabaseId, GetUserId());
    if (offset < 0 || offset >= (int) sizeof(buffer))
        elog(ERROR, "execution witness record overflow");
    for (index = 0; index < columns; index++)
    {
        Form_pg_attribute attribute = TupleDescAttr(tuple_desc, index);
        int written;

        if (attribute->attisdropped)
            continue;
        if (visible >= WITNESS_META_MAX)
        {
            truncated = true;
            break;
        }
        written = snprintf(buffer + offset, sizeof(buffer) - (size_t) offset,
                           "%s%u:%d:%u", visible == 0 ? "" : ",",
                           attribute->atttypid, attribute->atttypmod,
                           attribute->attcollation);
        if (written < 0 || written >= (int) (sizeof(buffer) - (size_t) offset))
        {
            truncated = true;
            break;
        }
        offset += written;
        visible++;
    }
    if (snprintf(buffer + offset, sizeof(buffer) - (size_t) offset,
                 ";columns=%d;truncated=%c;custom_nodes=%d;custom_completed=%d;duckdb_present=%c",
                 visible, truncated ? 't' : 'f', walk.custom_nodes,
                 walk.completed_custom_nodes, walk.duckdb_present ? 't' : 'f') >=
            (int) (sizeof(buffer) - (size_t) offset))
        elog(ERROR, "execution witness record overflow");

    memcpy(last_record_buffer, buffer, strlen(buffer) + 1);
    active_published = true;
}

static void
witness_start(QueryDesc *query_desc, int eflags)
{
    bool owns_subject = witness_armed && !armed_consumed && active_query == NULL &&
        query_desc->sourceText != NULL && debug_query_string != NULL &&
        strcmp(query_desc->sourceText, debug_query_string) == 0;

    if (owns_subject)
    {
        active_query = query_desc;
        armed_consumed = true;
        active_published = false;
        active_run_completed = false;
        query_desc->instrument_options |= INSTRUMENT_ROWS;
    }
    PG_TRY();
    {
        if (previous_start_hook != NULL)
            previous_start_hook(query_desc, eflags);
        else
            standard_ExecutorStart(query_desc, eflags);
    }
    PG_CATCH();
    {
        if (owns_subject)
        {
            publish_record(query_desc, "error", true);
            active_query = NULL;
        }
        PG_RE_THROW();
    }
    PG_END_TRY();
}

WITNESS_RUN_PROTO(witness_run)
{
    PG_TRY();
    {
        if (previous_run_hook != NULL)
            WITNESS_RUN_CALL(previous_run_hook);
        else
            WITNESS_RUN_CALL(standard_ExecutorRun);
        if (query_desc == active_query)
            active_run_completed = true;
    }
    PG_CATCH();
    {
        if (query_desc == active_query)
        {
            publish_record(query_desc, "error", false);
            active_query = NULL;
        }
        PG_RE_THROW();
    }
    PG_END_TRY();
}

static void
witness_finish(QueryDesc *query_desc)
{
    PG_TRY();
    {
        if (previous_finish_hook != NULL)
            previous_finish_hook(query_desc);
        else
            standard_ExecutorFinish(query_desc);
        if (query_desc == active_query)
            publish_record(query_desc, "success", false);
    }
    PG_CATCH();
    {
        if (query_desc == active_query)
        {
            publish_record(query_desc, "error", false);
            active_query = NULL;
        }
        PG_RE_THROW();
    }
    PG_END_TRY();
}

static void
witness_end(QueryDesc *query_desc)
{
    if (query_desc == active_query && !active_published)
        publish_record(query_desc, "success", false);
    if (previous_end_hook != NULL)
        previous_end_hook(query_desc);
    else
        standard_ExecutorEnd(query_desc);
    if (query_desc == active_query)
        active_query = NULL;
}

void
_PG_init(void)
{
    DefineCustomIntVariable("pg_qi_witness.sequence", NULL, NULL,
                            &witness_sequence, 0, 0, INT_MAX, PGC_INTERNAL,
                            WITNESS_GUC_FLAGS, NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi_witness.armed", NULL, NULL,
                             &witness_armed, false, PGC_USERSET,
                             WITNESS_GUC_FLAGS, NULL, witness_armed_assign, NULL);
    DefineCustomStringVariable("pg_qi_witness.subject_token", NULL, NULL,
                               &subject_token,
                               "0000000000000000000000000000000000000000000000000000000000000000",
                               PGC_USERSET, WITNESS_GUC_FLAGS,
                               subject_token_check, NULL, NULL);
    DefineCustomStringVariable("pg_qi_witness.last_record", NULL, NULL,
                               &last_record,
                               "v=1;seq=0;status=none;engine=unknown;token=0000000000000000000000000000000000000000000000000000000000000000;pid=0;db=0;user=0;meta=;columns=0;truncated=f;custom_nodes=0;custom_completed=0;duckdb_present=f",
                               PGC_INTERNAL, WITNESS_GUC_FLAGS,
                               NULL, NULL, last_record_show);

    previous_start_hook = ExecutorStart_hook;
    previous_run_hook = ExecutorRun_hook;
    previous_finish_hook = ExecutorFinish_hook;
    previous_end_hook = ExecutorEnd_hook;
    ExecutorStart_hook = witness_start;
    ExecutorRun_hook = witness_run;
    ExecutorFinish_hook = witness_finish;
    ExecutorEnd_hook = witness_end;
}

void
_PG_fini(void)
{
    if (ExecutorStart_hook == witness_start)
        ExecutorStart_hook = previous_start_hook;
    if (ExecutorRun_hook == witness_run)
        ExecutorRun_hook = previous_run_hook;
    if (ExecutorFinish_hook == witness_finish)
        ExecutorFinish_hook = previous_finish_hook;
    if (ExecutorEnd_hook == witness_end)
        ExecutorEnd_hook = previous_end_hook;
}
