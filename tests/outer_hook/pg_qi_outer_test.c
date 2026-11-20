/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <limits.h>

#include "executor/executor.h"
#include "fmgr.h"
#include "nodes/nodes.h"
#include "nodes/plannodes.h"
#include "optimizer/planner.h"
#include "pg_qi/planner_compat.h"
#include "utils/builtins.h"
#include "utils/guc.h"

PG_MODULE_MAGIC;

static qi_planner_fn_type preload_previous_planner_hook = NULL;
static qi_planner_fn_type dynamic_previous_planner_hook = NULL;
static bool reenter_once = false;
static bool reentry_active = false;
static bool dynamic_outer = false;
static bool inner_force_seen = true;
static bool error_after_owned_force = false;
static bool error_unconditionally = false;
static int planner_calls = 0;
static bool force_at_entry = true;
static bool pointer_identity = false;
static bool argument_identity = false;
static Query *expected_parse = NULL;
static const char *expected_query_string = NULL;
static int expected_cursor_options = 0;
static ParamListInfo expected_bound_params = NULL;
#if QI_HAVE_PLANNER_ES
static ExplainState *expected_es = NULL;
#endif
static PlannedStmt *predecessor_result = NULL;
static ExecutorStart_hook_type previous_executor_start_hook = NULL;

void _PG_init(void);
void _PG_fini(void);

PG_FUNCTION_INFO_V1(pg_qi_outer_test_become_outer);
PG_FUNCTION_INFO_V1(pg_qi_outer_test_restore_order);
PG_FUNCTION_INFO_V1(pg_qi_outer_test_reset_metrics);

static void
pg_qi_outer_test_executor_start(QueryDesc *query_desc, int eflags)
{
    if (predecessor_result != NULL)
        pointer_identity = query_desc->plannedstmt == predecessor_result;

    if (previous_executor_start_hook != NULL)
        previous_executor_start_hook(query_desc, eflags);
    else
        standard_ExecutorStart(query_desc, eflags);
}

static PlannedStmt *
call_planner(qi_planner_fn_type function, Query *parse,
             const char *query_string, int cursor_options,
             ParamListInfo bound_params,
#if QI_HAVE_PLANNER_ES
             ExplainState *es
#else
             void *es_ignored
#endif
             )
{
#if !QI_HAVE_PLANNER_ES
    (void) es_ignored;
#endif
    if (function != NULL)
        return QI_CALL_PLANNER(function, parse, query_string, cursor_options,
                               bound_params, QI_CURRENT_EXPLAIN_STATE);
    return QI_CALL_PLANNER(standard_planner, parse, query_string,
                           cursor_options, bound_params,
                           QI_CURRENT_EXPLAIN_STATE);
}

QI_PLANNER_PROTO(pg_qi_outer_test_planner)
{
    PlannedStmt *nested;
    PlannedStmt *result;
    Query *parse_copy;
    const char *force_value;
    bool force_enabled;
    qi_planner_fn_type next_hook = dynamic_outer
        ? dynamic_previous_planner_hook
        : preload_previous_planner_hook;

    planner_calls++;
    force_value = GetConfigOption("duckdb.force_execution", false, false);
    if (!parse_bool(force_value, &force_at_entry))
        ereport(ERROR,
                (errmsg("invalid duckdb.force_execution value")));

    if (reentry_active)
    {
        inner_force_seen = force_at_entry;
        if (dynamic_outer)
            argument_identity = parse == expected_parse &&
                query_string == expected_query_string &&
                cursor_options == expected_cursor_options &&
                bound_params == expected_bound_params
#if QI_HAVE_PLANNER_ES
                && es == expected_es
#endif
                ;
        predecessor_result = call_planner(preload_previous_planner_hook,
                                          parse, query_string,
                                          cursor_options, bound_params,
                                          QI_CURRENT_EXPLAIN_STATE);
        return predecessor_result;
    }

    if (dynamic_outer)
    {
        expected_parse = parse;
        expected_query_string = query_string;
        expected_cursor_options = cursor_options;
        expected_bound_params = bound_params;
#if QI_HAVE_PLANNER_ES
        expected_es = es;
#endif
        predecessor_result = NULL;
        reentry_active = true;
        PG_TRY();
        {
            result = call_planner(dynamic_previous_planner_hook, parse,
                                  query_string, cursor_options, bound_params,
                                  QI_CURRENT_EXPLAIN_STATE);
        }
        PG_FINALLY();
        {
            reentry_active = false;
        }
        PG_END_TRY();
        pointer_identity = result == predecessor_result;
        return result;
    }

    if (!reenter_once)
    {
        if (error_unconditionally)
            ereport(ERROR,
                    (errcode(ERRCODE_T_R_SERIALIZATION_FAILURE),
                     errmsg("pg_qi outer fixture downstream error")));
        if (error_after_owned_force)
        {
            force_value = GetConfigOption("duckdb.force_execution", false, false);
            if (!parse_bool(force_value, &force_enabled))
                ereport(ERROR,
                        (errmsg("invalid duckdb.force_execution value")));
            if (force_enabled)
                ereport(ERROR,
                        (errcode(ERRCODE_INTERNAL_ERROR),
                         errmsg("pg_qi outer fixture error after owned force")));
        }
        predecessor_result = call_planner(next_hook, parse, query_string,
                                          cursor_options, bound_params,
                                          QI_CURRENT_EXPLAIN_STATE);
        return predecessor_result;
    }

    reentry_active = true;
    PG_TRY();
    {
        parse_copy = copyObject(parse);
        nested = QI_CALL_PLANNER(planner_hook, parse_copy, query_string,
                                 cursor_options, bound_params,
                                 QI_CURRENT_EXPLAIN_STATE);
        (void) nested;
        result = call_planner(next_hook, parse, query_string, cursor_options,
                              bound_params, QI_CURRENT_EXPLAIN_STATE);
    }
    PG_FINALLY();
    {
        reentry_active = false;
    }
    PG_END_TRY();
    return result;
}

void
_PG_init(void)
{
    DefineCustomBoolVariable("pg_qi_outer_test.reenter_once", NULL, NULL,
                             &reenter_once, false, PGC_SUSET, 0,
                             NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi_outer_test.inner_force_seen", NULL, NULL,
                             &inner_force_seen, true, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi_outer_test.error_after_owned_force",
                             NULL, NULL, &error_after_owned_force, false,
                             PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi_outer_test.error_unconditionally",
                             NULL, NULL, &error_unconditionally, false,
                             PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi_outer_test.planner_calls", NULL, NULL,
                            &planner_calls, 0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi_outer_test.force_at_entry", NULL, NULL,
                             &force_at_entry, true, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi_outer_test.pointer_identity", NULL, NULL,
                             &pointer_identity, false, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi_outer_test.argument_identity", NULL, NULL,
                             &argument_identity, false, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);

    preload_previous_planner_hook = planner_hook;
    planner_hook = pg_qi_outer_test_planner;
    previous_executor_start_hook = ExecutorStart_hook;
    ExecutorStart_hook = pg_qi_outer_test_executor_start;
}

void
_PG_fini(void)
{
    if (planner_hook == pg_qi_outer_test_planner)
        planner_hook = dynamic_outer
            ? dynamic_previous_planner_hook
            : preload_previous_planner_hook;
    if (ExecutorStart_hook == pg_qi_outer_test_executor_start)
        ExecutorStart_hook = previous_executor_start_hook;
}

Datum
pg_qi_outer_test_become_outer(PG_FUNCTION_ARGS)
{
    if (!dynamic_outer)
    {
        dynamic_previous_planner_hook = planner_hook;
        planner_hook = pg_qi_outer_test_planner;
        dynamic_outer = true;
    }
    PG_RETURN_VOID();
}

Datum
pg_qi_outer_test_restore_order(PG_FUNCTION_ARGS)
{
    if (dynamic_outer && planner_hook == pg_qi_outer_test_planner)
    {
        planner_hook = dynamic_previous_planner_hook;
        dynamic_previous_planner_hook = NULL;
        dynamic_outer = false;
    }
    PG_RETURN_VOID();
}

Datum
pg_qi_outer_test_reset_metrics(PG_FUNCTION_ARGS)
{
    planner_calls = 0;
    force_at_entry = true;
    pointer_identity = false;
    argument_identity = false;
    predecessor_result = NULL;
    PG_RETURN_VOID();
}
