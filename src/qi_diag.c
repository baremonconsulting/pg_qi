/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include <limits.h>

#include "pg_qi/qi_diag.h"
#include "utils/guc.h"

static int qi_failpoint = QI_FAILPOINT_OFF;
static int qi_failpoint_nth = 1;
static int qi_failpoint_hits = 0;
static int qi_failpoint_fired = 0;

static const struct config_enum_entry qi_failpoint_options[] = {
    {"off", QI_FAILPOINT_OFF, false},
    {"before_push", QI_FAILPOINT_BEFORE_PUSH, false},
    {"inside_try_before_policy", QI_FAILPOINT_INSIDE_TRY_BEFORE_POLICY, false},
    {"inside_try_after_policy", QI_FAILPOINT_INSIDE_TRY_AFTER_POLICY, false},
    {"inside_try_before_downstream", QI_FAILPOINT_INSIDE_TRY_BEFORE_DOWNSTREAM, false},
    {"inner_planner", QI_FAILPOINT_INNER_PLANNER, false},
    {"subtransaction_body", QI_FAILPOINT_SUBTRANSACTION_BODY, false},
    {NULL, 0, false}
};

static void
qi_assign_failpoint(int newval, void *extra)
{
    qi_failpoint_hits = 0;
}

static void
qi_assign_failpoint_nth(int newval, void *extra)
{
    qi_failpoint_hits = 0;
}

void
qi_diag_init_gucs(void)
{
    DefineCustomEnumVariable("pg_qi.spike_failpoint",
                             "Selects a protected Spike 1 failpoint.",
                             NULL,
                             &qi_failpoint,
                             QI_FAILPOINT_OFF,
                             qi_failpoint_options,
                             PGC_SUSET, 0, NULL, qi_assign_failpoint, NULL);
    DefineCustomIntVariable("pg_qi.spike_failpoint_nth",
                            "Fires the selected failpoint on its Nth hit.",
                            NULL,
                            &qi_failpoint_nth,
                            1, 1, INT_MAX,
                            PGC_SUSET, 0, NULL,
                            qi_assign_failpoint_nth, NULL);
    DefineCustomIntVariable("pg_qi.spike_failpoint_hits",
                            "Current hit count for the selected failpoint.",
                            NULL,
                            &qi_failpoint_hits,
                            0, 0, INT_MAX,
                            PGC_INTERNAL, 0, NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_failpoint_fired",
                            "Number of failpoints fired in this backend.",
                            NULL,
                            &qi_failpoint_fired,
                            0, 0, INT_MAX,
                            PGC_INTERNAL, 0, NULL, NULL, NULL);
}

void
qi_failpoint_hit(QiFailpoint location)
{
    const char *name = "unknown";

    if (qi_failpoint != location)
        return;

    qi_failpoint_hits++;
    if (qi_failpoint_hits != qi_failpoint_nth)
        return;

    switch (location)
    {
        case QI_FAILPOINT_BEFORE_PUSH:
            name = "before_push";
            break;
        case QI_FAILPOINT_INSIDE_TRY_BEFORE_POLICY:
            name = "inside_try_before_policy";
            break;
        case QI_FAILPOINT_INSIDE_TRY_AFTER_POLICY:
            name = "inside_try_after_policy";
            break;
        case QI_FAILPOINT_INSIDE_TRY_BEFORE_DOWNSTREAM:
            name = "inside_try_before_downstream";
            break;
        case QI_FAILPOINT_INNER_PLANNER:
            name = "inner_planner";
            break;
        case QI_FAILPOINT_SUBTRANSACTION_BODY:
            name = "subtransaction_body";
            break;
        case QI_FAILPOINT_OFF:
            break;
    }
    qi_failpoint = QI_FAILPOINT_OFF;
    qi_failpoint_hits = 0;
    qi_failpoint_fired++;
    ereport(ERROR,
            (errmsg("pg_qi failpoint fired: %s", name)));
}
