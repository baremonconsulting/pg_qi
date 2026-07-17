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
#include "optimizer/planner.h"
#include "utils/builtins.h"
#include "utils/guc.h"

PG_MODULE_MAGIC;

#define PG_QI_VERSION "0.0.1"

static bool pg_qi_enabled = true;
static bool pg_qi_preloaded = false;
static planner_hook_type previous_planner_hook = NULL;

void _PG_init(void);
void _PG_fini(void);

PG_FUNCTION_INFO_V1(pg_qi_version);
PG_FUNCTION_INFO_V1(pg_qi_is_preloaded);

static PlannedStmt *
pg_qi_planner(Query *parse, const char *query_string, int cursor_options,
              ParamListInfo bound_params)
{
    /*
     * Bootstrap behaviour is deliberately transparent. Future policy logic
     * will run before this downstream call, but it must always preserve the
     * planner-hook chain.
     */
    if (previous_planner_hook != NULL)
        return previous_planner_hook(parse, query_string, cursor_options, bound_params);

    return standard_planner(parse, query_string, cursor_options, bound_params);
}

void
_PG_init(void)
{
    DefineCustomBoolVariable("pg_qi.enabled",
                             "Enables pg_qi policy evaluation in this backend.",
                             "The bootstrap version does not change planner decisions.",
                             &pg_qi_enabled,
                             true,
                             PGC_SUSET,
                             0,
                             NULL,
                             NULL,
                             NULL);

    /*
     * SQL-callable functions remain available when the library is loaded by
     * CREATE EXTENSION. The planner hook is installed only during server
     * preload, which is the required deployment mode for pg_qi.
     */
    if (!process_shared_preload_libraries_in_progress)
        return;

    pg_qi_preloaded = true;
    previous_planner_hook = planner_hook;
    planner_hook = pg_qi_planner;
}

void
_PG_fini(void)
{
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
