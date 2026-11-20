/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/parallel.h"
#include "executor/executor.h"
#include "miscadmin.h"
#include "pg_qi/qi_context.h"
#include "pg_qi/qi_snapshot.h"
#include "tcop/tcopprot.h"
#include "tcop/utility.h"

static ExecutorStart_hook_type previous_executor_start_hook = NULL;
static ExecutorRun_hook_type previous_executor_run_hook = NULL;
static ExecutorFinish_hook_type previous_executor_finish_hook = NULL;
static ExecutorEnd_hook_type previous_executor_end_hook = NULL;
static ProcessUtility_hook_type previous_process_utility_hook = NULL;
static int qi_executor_call_depth = 0;
static int qi_utility_call_depth = 0;

#if PG_VERSION_NUM >= 180000
#define QI_EXECUTOR_RUN_PROTO(name) \
    static void name(QueryDesc *query_desc, ScanDirection direction, \
                     uint64 count)
#define QI_EXECUTOR_RUN_CALL(function) \
    function(query_desc, direction, count)
#else
#define QI_EXECUTOR_RUN_PROTO(name) \
    static void name(QueryDesc *query_desc, ScanDirection direction, \
                     uint64 count, bool execute_once)
#define QI_EXECUTOR_RUN_CALL(function) \
    function(query_desc, direction, count, execute_once)
#endif

static void
qi_executor_start(QueryDesc *query_desc, int eflags)
{
    qi_executor_call_depth++;
    PG_TRY();
    {
        if (previous_executor_start_hook != NULL)
            previous_executor_start_hook(query_desc, eflags);
        else
            standard_ExecutorStart(query_desc, eflags);
    }
    PG_FINALLY();
    {
        qi_executor_call_depth--;
    }
    PG_END_TRY();
}

QI_EXECUTOR_RUN_PROTO(qi_executor_run)
{
    qi_executor_call_depth++;
    PG_TRY();
    {
        if (previous_executor_run_hook != NULL)
            QI_EXECUTOR_RUN_CALL(previous_executor_run_hook);
        else
            QI_EXECUTOR_RUN_CALL(standard_ExecutorRun);
    }
    PG_FINALLY();
    {
        qi_executor_call_depth--;
    }
    PG_END_TRY();
}

static void
qi_executor_finish(QueryDesc *query_desc)
{
    qi_executor_call_depth++;
    PG_TRY();
    {
        if (previous_executor_finish_hook != NULL)
            previous_executor_finish_hook(query_desc);
        else
            standard_ExecutorFinish(query_desc);
    }
    PG_FINALLY();
    {
        qi_executor_call_depth--;
    }
    PG_END_TRY();
}

static void
qi_executor_end(QueryDesc *query_desc)
{
    qi_executor_call_depth++;
    PG_TRY();
    {
        if (previous_executor_end_hook != NULL)
            previous_executor_end_hook(query_desc);
        else
            standard_ExecutorEnd(query_desc);
    }
    PG_FINALLY();
    {
        qi_executor_call_depth--;
    }
    PG_END_TRY();
}

static void
qi_process_utility(PlannedStmt *pstmt, const char *query_string,
                   bool read_only_tree, ProcessUtilityContext context,
                   ParamListInfo params, QueryEnvironment *query_env,
                   DestReceiver *dest, QueryCompletion *qc)
{
    qi_utility_call_depth++;
    PG_TRY();
    {
        if (previous_process_utility_hook != NULL)
            previous_process_utility_hook(pstmt, query_string, read_only_tree,
                                          context, params, query_env, dest, qc);
        else
            standard_ProcessUtility(pstmt, query_string, read_only_tree,
                                    context, params, query_env, dest, qc);
    }
    PG_FINALLY();
    {
        qi_utility_call_depth--;
    }
    PG_END_TRY();

    qi_snapshot_handle_utility(pstmt->utilityStmt);
}

void
qi_context_init_hooks(void)
{
    previous_executor_start_hook = ExecutorStart_hook;
    previous_executor_run_hook = ExecutorRun_hook;
    previous_executor_finish_hook = ExecutorFinish_hook;
    previous_executor_end_hook = ExecutorEnd_hook;
    previous_process_utility_hook = ProcessUtility_hook;

    ExecutorStart_hook = qi_executor_start;
    ExecutorRun_hook = qi_executor_run;
    ExecutorFinish_hook = qi_executor_finish;
    ExecutorEnd_hook = qi_executor_end;
    ProcessUtility_hook = qi_process_utility;
}

void
qi_context_fini_hooks(void)
{
    if (ExecutorStart_hook == qi_executor_start)
        ExecutorStart_hook = previous_executor_start_hook;
    if (ExecutorRun_hook == qi_executor_run)
        ExecutorRun_hook = previous_executor_run_hook;
    if (ExecutorFinish_hook == qi_executor_finish)
        ExecutorFinish_hook = previous_executor_finish_hook;
    if (ExecutorEnd_hook == qi_executor_end)
        ExecutorEnd_hook = previous_executor_end_hook;
    if (ProcessUtility_hook == qi_process_utility)
        ProcessUtility_hook = previous_process_utility_hook;
}

bool
qi_context_proven_for_routing(bool planner_hook_outermost, int planner_depth,
                              const char *query_string)
{
    return MyBackendType == B_BACKEND &&
        !IsParallelWorker() &&
        query_string != NULL &&
        query_string == debug_query_string &&
        planner_depth == 1 &&
        qi_executor_call_depth == 0 &&
        qi_utility_call_depth == 0 &&
        planner_hook_outermost &&
        ExecutorStart_hook == qi_executor_start &&
        ExecutorRun_hook == qi_executor_run &&
        ExecutorFinish_hook == qi_executor_finish &&
        ExecutorEnd_hook == qi_executor_end &&
        ProcessUtility_hook == qi_process_utility;
}
