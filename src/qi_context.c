/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/parallel.h"
#include "commands/prepare.h"
#include "executor/executor.h"
#include "miscadmin.h"
#include "pg_qi/qi_context.h"
#include "pg_qi/qi_explain_guard.h"
#include "pg_qi/qi_lifecycle.h"
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
static NodeTag qi_utility_node_tag = T_Invalid;
static const char *qi_execute_query_string = NULL;

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
    bool sql_execute = qi_utility_call_depth == 1 &&
        qi_utility_node_tag == T_ExecuteStmt &&
        query_desc != NULL &&
        query_desc->sourceText != NULL &&
        qi_execute_query_string != NULL &&
        strcmp(query_desc->sourceText, qi_execute_query_string) == 0;
    bool context_proven = qi_utility_call_depth == 0 || sql_execute;

    qi_lifecycle_executor_start(query_desc, qi_executor_call_depth == 0,
                                context_proven);
    qi_executor_call_depth++;
    PG_TRY();
    {
        if (previous_executor_start_hook != NULL)
            previous_executor_start_hook(query_desc, eflags);
        else
            standard_ExecutorStart(query_desc, eflags);
        qi_explain_guard_executor_started(query_desc);
    }
    PG_FINALLY();
    {
        qi_executor_call_depth--;
    }
    PG_END_TRY();
}

QI_EXECUTOR_RUN_PROTO(qi_executor_run)
{
    qi_lifecycle_executor_run(query_desc);
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
        qi_lifecycle_executor_end(query_desc);
    }
    PG_END_TRY();
}

static void
qi_process_utility(PlannedStmt *pstmt, const char *query_string,
                   bool read_only_tree, ProcessUtilityContext context,
                   ParamListInfo params, QueryEnvironment *query_env,
                   DestReceiver *dest, QueryCompletion *qc)
{
    NodeTag saved_utility_node_tag = qi_utility_node_tag;
    const char *saved_execute_query_string = qi_execute_query_string;

    qi_utility_node_tag = pstmt->utilityStmt == NULL
        ? T_Invalid : nodeTag(pstmt->utilityStmt);
    qi_execute_query_string = NULL;
    if (context == PROCESS_UTILITY_TOPLEVEL &&
        query_string == debug_query_string &&
        pstmt->utilityStmt != NULL && IsA(pstmt->utilityStmt, ExecuteStmt))
    {
        ExecuteStmt *execute = (ExecuteStmt *) pstmt->utilityStmt;
        PreparedStatement *prepared =
            FetchPreparedStatement(execute->name, false);

        if (prepared != NULL && prepared->plansource != NULL)
            qi_execute_query_string = prepared->plansource->query_string;
    }
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
        qi_utility_node_tag = saved_utility_node_tag;
        qi_execute_query_string = saved_execute_query_string;
    }
    PG_END_TRY();

    qi_snapshot_handle_utility(pstmt->utilityStmt);
}

void
qi_context_init_hooks(void)
{
    qi_lifecycle_init();
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
    qi_lifecycle_fini();
}

bool
qi_context_inside_executor(void)
{
    return qi_executor_call_depth > 0;
}

bool
qi_context_proven_for_routing(bool planner_hook_outermost, int planner_depth,
                              const char *query_string)
{
    bool sql_execute = qi_utility_call_depth == 1 &&
        qi_utility_node_tag == T_ExecuteStmt &&
        query_string == qi_execute_query_string;

    return MyBackendType == B_BACKEND &&
        !IsParallelWorker() &&
        query_string != NULL &&
        (query_string == debug_query_string || sql_execute) &&
        planner_depth == 1 &&
        qi_executor_call_depth == 0 &&
        (qi_utility_call_depth == 0 || sql_execute) &&
        planner_hook_outermost &&
        ExecutorStart_hook == qi_executor_start &&
        ExecutorRun_hook == qi_executor_run &&
        ExecutorFinish_hook == qi_executor_finish &&
        ExecutorEnd_hook == qi_executor_end &&
        ProcessUtility_hook == qi_process_utility;
}

bool
qi_context_policy_request_allowed(void)
{
    return qi_utility_call_depth == 0;
}
