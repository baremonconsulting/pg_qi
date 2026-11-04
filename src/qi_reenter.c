/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/xact.h"
#include "miscadmin.h"
#include "nodes/nodes.h"
#include "optimizer/optimizer.h"
#include "pg_qi/qi_delegation.h"
#include "pg_qi/qi_diag.h"
#include "pg_qi/qi_detector.h"
#include "pg_qi/qi_reenter.h"
#include "utils/guc.h"
#include "utils/memutils.h"
#include "utils/resowner.h"

typedef enum QiReentryMode
{
    QI_REENTRY_OFF = 0,
    QI_REENTRY_PROPAGATE,
    QI_REENTRY_SUBXACT
} QiReentryMode;

typedef struct QiReentryCall
{
    Query              *parse;
    const char         *query_string;
    int                 cursor_options;
    ParamListInfo       bound_params;
    void               *explain_state;
} QiReentryCall;

typedef void (*QiSubxactBody) (void *arg);

static int qi_reentry_mode = QI_REENTRY_OFF;
static bool qi_reentry_inner_error = false;
static bool qi_reentry_active = false;

static const struct config_enum_entry qi_reentry_mode_options[] = {
    {"off", QI_REENTRY_OFF, false},
    {"propagate", QI_REENTRY_PROPAGATE, false},
    {"subxact", QI_REENTRY_SUBXACT, false},
    {NULL, 0, false}
};

void
qi_reenter_init_gucs(void)
{
    DefineCustomEnumVariable("pg_qi.spike_reentry_mode",
                             "Controls the Spike 1 synthetic planner reentry.",
                             NULL,
                             &qi_reentry_mode,
                             QI_REENTRY_OFF,
                             qi_reentry_mode_options,
                             PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi.spike_reentry_inner_error",
                             "Injects an ERROR inside the nested planner frame.",
                             NULL,
                             &qi_reentry_inner_error,
                             false,
                             PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi.spike_reentry_active",
                             "Reports whether the synthetic reentry guard is active.",
                             NULL,
                             &qi_reentry_active,
                             false,
                             PGC_INTERNAL, 0, NULL, NULL, NULL);
}

bool
qi_reenter_is_active(void)
{
    return qi_reentry_active;
}

bool
qi_reenter_inner_error_enabled(void)
{
    return qi_reentry_active && qi_reentry_inner_error;
}

static void
qi_plan_copy_and_record(void *arg)
{
    QiReentryCall *call = (QiReentryCall *) arg;
    Query *copy = copyObject(call->parse);
    PlannedStmt *inner_plan;

#if PG_VERSION_NUM >= 190000
    inner_plan = planner(copy, call->query_string, call->cursor_options,
                         call->bound_params,
                         (ExplainState *) call->explain_state);
#else
    inner_plan = planner(copy, call->query_string, call->cursor_options,
                         call->bound_params);
#endif
    qi_record_nested_plan(qi_plan_has_duckdb_scan(inner_plan));
}

/* The only local-subtransaction error-catching pattern in pg_qi. */
static bool
qi_try_local_subxact(QiSubxactBody body, void *arg)
{
    MemoryContext old_context = CurrentMemoryContext;
    ResourceOwner old_owner = CurrentResourceOwner;
    volatile bool ok = false;

    BeginInternalSubTransaction(NULL);
    PG_TRY();
    {
        qi_failpoint_hit(QI_FAILPOINT_SUBTRANSACTION_BODY);
        body(arg);
        ReleaseCurrentSubTransaction();
        MemoryContextSwitchTo(old_context);
        CurrentResourceOwner = old_owner;
        ok = true;
    }
    PG_CATCH();
    {
        MemoryContextSwitchTo(old_context);
        FlushErrorState();
        RollbackAndReleaseCurrentSubTransaction();
        MemoryContextSwitchTo(old_context);
        CurrentResourceOwner = old_owner;
        ok = false;
    }
    PG_END_TRY();

    return ok;
}

void
qi_maybe_reenter(Query *parse, const char *query_string, int cursor_options,
                 ParamListInfo bound_params, void *explain_state,
                 bool outer_delegation_requested)
{
    QiReentryCall call;

    if (qi_reentry_mode == QI_REENTRY_OFF || qi_reentry_active ||
        !outer_delegation_requested || parse->commandType != CMD_SELECT ||
        parse->utilityStmt != NULL)
        return;

    call.parse = parse;
    call.query_string = query_string;
    call.cursor_options = cursor_options;
    call.bound_params = bound_params;
    call.explain_state = explain_state;

    qi_reentry_active = true;
    PG_TRY();
    {
        if (qi_reentry_mode == QI_REENTRY_SUBXACT)
            (void) qi_try_local_subxact(qi_plan_copy_and_record, &call);
        else
            qi_plan_copy_and_record(&call);
    }
    PG_FINALLY();
    {
        qi_reentry_active = false;
    }
    PG_END_TRY();
}
