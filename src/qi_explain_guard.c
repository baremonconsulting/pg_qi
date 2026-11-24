/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "nodes/execnodes.h"
#include "nodes/extensible.h"
#include "nodes/nodeFuncs.h"
#include "pg_qi/qi_detector.h"
#include "pg_qi/qi_explain_guard.h"
#include "tcop/pquery.h"
#include "utils/portal.h"

/*
 * pg_duckdb prepares a DuckDB scan as an EXPLAIN of its query only when the
 * active portal runs an EXPLAIN command, but its ExplainCustomScan callback
 * always executes the prepared statement. Any other ExplainPrintPlan caller,
 * most notably auto_explain in its ExecutorEnd hook, thereby runs the whole
 * query a second time. Without an active snapshot that crashes the backend
 * (see also duckdb/pg_duckdb#1042).
 *
 * For scans that were not prepared for EXPLAIN, the scan state gets a copy of
 * the provider's executor methods without that callback. Plans printed for
 * such queries still show the DuckDBScan node, but never run it again.
 */

#define QI_EXPLAIN_GUARD_SLOTS 4

typedef struct QiGuardedMethods
{
    const CustomExecMethods *provider;
    CustomExecMethods guarded;
} QiGuardedMethods;

static QiGuardedMethods qi_guarded_methods[QI_EXPLAIN_GUARD_SLOTS];
static int qi_guarded_methods_used = 0;

static const CustomExecMethods *
qi_guarded_methods_for(const CustomExecMethods *provider)
{
    QiGuardedMethods *slot;
    int i;

    for (i = 0; i < qi_guarded_methods_used; i++)
    {
        if (qi_guarded_methods[i].provider == provider)
            return &qi_guarded_methods[i].guarded;
    }

    if (qi_guarded_methods_used == QI_EXPLAIN_GUARD_SLOTS)
        return NULL;

    slot = &qi_guarded_methods[qi_guarded_methods_used++];
    slot->provider = provider;
    slot->guarded = *provider;
    slot->guarded.ExplainCustomScan = NULL;
    return &slot->guarded;
}

static bool
qi_explain_guard_walker(PlanState *planstate, void *context)
{
    if (planstate == NULL)
        return false;

    if (IsA(planstate, CustomScanState))
    {
        CustomScanState *css = (CustomScanState *) planstate;
        const CustomExecMethods *methods = css->methods;

        if (methods != NULL && methods->ExplainCustomScan != NULL &&
            methods->CustomName != NULL &&
            strcmp(methods->CustomName, QI_DUCKDB_SCAN_PROVIDER) == 0)
        {
            const CustomExecMethods *guarded = qi_guarded_methods_for(methods);

            if (guarded != NULL)
                css->methods = guarded;
        }
    }

    return planstate_tree_walker(planstate, qi_explain_guard_walker, context);
}

void
qi_explain_guard_executor_started(QueryDesc *query_desc)
{
    if (query_desc == NULL || query_desc->planstate == NULL)
        return;

    /* The test pg_duckdb's BeginCustomScan uses to prepare an EXPLAIN. */
    if (ActivePortal != NULL && ActivePortal->commandTag == CMDTAG_EXPLAIN)
        return;

    (void) qi_explain_guard_walker(query_desc->planstate, NULL);
}
