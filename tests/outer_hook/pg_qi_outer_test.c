/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

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

void _PG_init(void);
void _PG_fini(void);

PG_FUNCTION_INFO_V1(pg_qi_outer_test_become_outer);
PG_FUNCTION_INFO_V1(pg_qi_outer_test_restore_order);

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

    if (reentry_active)
    {
        force_value = GetConfigOption("duckdb.force_execution", false, false);
        if (!parse_bool(force_value, &force_enabled))
            ereport(ERROR,
                    (errmsg("invalid duckdb.force_execution value")));
        inner_force_seen = force_enabled;
        return call_planner(preload_previous_planner_hook, parse, query_string,
                            cursor_options, bound_params,
                            QI_CURRENT_EXPLAIN_STATE);
    }

    if (dynamic_outer)
    {
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
        return result;
    }

    if (!reenter_once)
        return call_planner(next_hook, parse, query_string, cursor_options,
                            bound_params, QI_CURRENT_EXPLAIN_STATE);

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

    preload_previous_planner_hook = planner_hook;
    planner_hook = pg_qi_outer_test_planner;
}

void
_PG_fini(void)
{
    if (planner_hook == pg_qi_outer_test_planner)
        planner_hook = dynamic_outer
            ? dynamic_previous_planner_hook
            : preload_previous_planner_hook;
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
