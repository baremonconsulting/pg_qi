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
#include "pg_qi/planner_compat.h"
#include "pg_qi/qi_delegation.h"
#include "pg_qi/qi_detector.h"
#if PG_QI_TESTING
#include "pg_qi/qi_diag.h"
#include "pg_qi/qi_reenter.h"
#endif
#include "portability/instr_time.h"
#include "storage/ipc.h"
#include "utils/builtins.h"
#include "utils/guc.h"

PG_MODULE_MAGIC;

#define PG_QI_VERSION "0.0.1"

static bool pg_qi_enabled = true;
static bool pg_qi_preloaded = false;
#if PG_QI_TESTING
static bool pg_qi_last_plan_has_duckdb_scan = false;
static bool pg_qi_previous_hook_present = false;
static bool pg_qi_final_report_registered = false;
static int pg_qi_benchmark_mode = QI_BENCH_NORMAL;
static bool pg_qi_benchmark_collect = true;
#endif
static qi_planner_fn_type previous_planner_hook = NULL;

#if PG_QI_TESTING
static const struct config_enum_entry pg_qi_benchmark_options[] = {
    {"normal", QI_BENCH_NORMAL, false},
    {"bypass", QI_BENCH_BYPASS, false},
    {"frame_only", QI_BENCH_FRAME_ONLY, false},
    {"shadow_save", QI_BENCH_SHADOW_SAVE, false},
    {"delegate", QI_BENCH_DELEGATE, false},
    {NULL, 0, false}
};
#endif

static inline PlannedStmt *
pg_qi_call_downstream(Query *parse, const char *query_string,
                      int cursor_options, ParamListInfo bound_params
#if QI_HAVE_PLANNER_ES
                      , ExplainState *es
#endif
                      )
{
    if (previous_planner_hook != NULL)
        return QI_CALL_PLANNER(previous_planner_hook, parse, query_string,
                               cursor_options, bound_params,
                               QI_CURRENT_EXPLAIN_STATE);
    return QI_CALL_PLANNER(standard_planner, parse, query_string,
                           cursor_options, bound_params,
                           QI_CURRENT_EXPLAIN_STATE);
}

void _PG_init(void);
void _PG_fini(void);

PG_FUNCTION_INFO_V1(pg_qi_version);
PG_FUNCTION_INFO_V1(pg_qi_is_preloaded);
#if PG_QI_TESTING
PG_FUNCTION_INFO_V1(qi_test_force_meta);
PG_FUNCTION_INFO_V1(qi_test_force_save_roundtrip);
PG_FUNCTION_INFO_V1(qi_test_detector_contract);
PG_FUNCTION_INFO_V1(qi_test_frame_contract);
PG_FUNCTION_INFO_V1(qi_test_frame_state);
PG_FUNCTION_INFO_V1(qi_spike_arm_next);
PG_FUNCTION_INFO_V1(qi_spike_disarm);
PG_FUNCTION_INFO_V1(qi_spike_reset_token);
PG_FUNCTION_INFO_V1(qi_test_token_state);
PG_FUNCTION_INFO_V1(qi_spike_reset_state);
PG_FUNCTION_INFO_V1(qi_spike_state);
PG_FUNCTION_INFO_V1(qi_spike_observer);
PG_FUNCTION_INFO_V1(qi_test_active_control_guard);
#endif

QI_PLANNER_PROTO(pg_qi_planner)
{
    PlannedStmt *volatile planned_stmt = NULL;
    QiDelegationFrame *volatile frame = NULL;
    volatile uint64 frame_id = 0;
    int save_nestlevel;
    bool delegation_requested;
    uint64 generation = 0;
#if PG_QI_TESTING
    instr_time started_at;
    instr_time finished_at;
    INSTR_TIME_SET_CURRENT(started_at);

    if (!pg_qi_final_report_registered && MyBackendType == B_BACKEND)
    {
        before_shmem_exit(qi_backend_final_report, (Datum) 0);
        pg_qi_final_report_registered = true;
    }

    if (!pg_qi_benchmark_collect)
        return pg_qi_call_downstream(parse, query_string, cursor_options,
                                     bound_params
#if QI_HAVE_PLANNER_ES
                                     , es
#endif
                                     );
#endif

    if (!pg_qi_enabled)
    {
        planned_stmt = pg_qi_call_downstream(parse, query_string,
                                              cursor_options, bound_params
#if QI_HAVE_PLANNER_ES
                                              , es
#endif
                                              );
        goto done;
    }

#if PG_QI_TESTING
    if (pg_qi_benchmark_mode == QI_BENCH_BYPASS)
    {
        planned_stmt = pg_qi_call_downstream(parse, query_string,
                                              cursor_options, bound_params
#if QI_HAVE_PLANNER_ES
                                              , es
#endif
                                              );
        goto done;
    }
#endif

    /* Token consumption must precede every operation that can throw. */
    delegation_requested = false;
#if PG_QI_TESTING
    delegation_requested = pg_qi_benchmark_mode == QI_BENCH_DELEGATE;
    if (!delegation_requested)
        delegation_requested = qi_token_consume_top_level(&generation);
    qi_failpoint_hit(QI_FAILPOINT_BEFORE_PUSH);
#endif
    save_nestlevel = NewGUCNestLevel();
    frame = qi_frame_push(save_nestlevel, qi_current_force_origin(),
                          delegation_requested,
#if PG_QI_TESTING
                          qi_reenter_is_active(),
#else
                          false,
#endif
                          generation);
    frame_id = frame->frame_id;

    PG_TRY();
    {
#if PG_QI_TESTING
        qi_failpoint_hit(QI_FAILPOINT_INSIDE_TRY_BEFORE_POLICY);
        if (pg_qi_benchmark_mode == QI_BENCH_SHADOW_SAVE)
            qi_set_force_execution(false);
        else if (pg_qi_benchmark_mode != QI_BENCH_FRAME_ONLY &&
                 frame->guc_changed)
            qi_set_force_execution(frame->effective_origin != QI_FORCE_OFF);
        qi_failpoint_hit(QI_FAILPOINT_INSIDE_TRY_AFTER_POLICY);

        if (qi_reenter_is_active())
            qi_failpoint_hit(QI_FAILPOINT_INNER_PLANNER);
        if (qi_reenter_inner_error_enabled())
            ereport(ERROR,
                    (errmsg("pg_qi synthetic inner planner error")));

        qi_maybe_reenter(parse, query_string, cursor_options, bound_params,
                         QI_CURRENT_EXPLAIN_STATE, delegation_requested);
        qi_failpoint_hit(QI_FAILPOINT_INSIDE_TRY_BEFORE_DOWNSTREAM);
#else
        if (frame->guc_changed)
            qi_set_force_execution(frame->effective_origin != QI_FORCE_OFF);
#endif

        planned_stmt = pg_qi_call_downstream(parse, query_string,
                                              cursor_options, bound_params
#if QI_HAVE_PLANNER_ES
                                              , es
#endif
                                              );

#if PG_QI_TESTING
        pg_qi_last_plan_has_duckdb_scan =
            qi_plan_has_duckdb_scan((PlannedStmt *) planned_stmt);
        qi_record_completed_plan(pg_qi_last_plan_has_duckdb_scan);
#endif
    }
    PG_CATCH();
    {
        (void) qi_frame_unwind_on_error(frame_id);
        PG_RE_THROW();
    }
    PG_END_TRY();

    AtEOXact_GUC(true, save_nestlevel);
    (void) qi_frame_pop(frame_id);
done:
#if PG_QI_TESTING
    INSTR_TIME_SET_CURRENT(finished_at);
    INSTR_TIME_SUBTRACT(finished_at, started_at);
    qi_record_planner_timing((uint64) INSTR_TIME_GET_MICROSEC(finished_at));
#endif
    return (PlannedStmt *) planned_stmt;
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

#if PG_QI_TESTING
    DefineCustomBoolVariable("pg_qi.spike_last_plan_has_duckdb_scan",
                             "Reports whether the last completed plan contained DuckDBScan.",
                             NULL,
                             &pg_qi_last_plan_has_duckdb_scan,
                             false,
                             PGC_INTERNAL,
                             0,
                             NULL,
                             NULL,
                             NULL);
    DefineCustomBoolVariable("pg_qi.spike_previous_planner_hook_present",
                             NULL, NULL,
                             &pg_qi_previous_hook_present,
                             false, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.spike_benchmark_mode",
                             "Selects the Spike 1 benchmark layer.",
                             "Test-only; normal is required outside WP11.",
                             &pg_qi_benchmark_mode, QI_BENCH_NORMAL,
                             pg_qi_benchmark_options, PGC_USERSET, 0,
                             NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi.spike_benchmark_collect",
                             "Collects WP11 planner timing and counters.",
                             "Disable only for benchmark metadata queries.",
                             &pg_qi_benchmark_collect, true,
                             PGC_USERSET, 0, NULL, NULL, NULL);

    qi_init_frame_diagnostics();
    qi_reenter_init_gucs();
    qi_diag_init_gucs();
#endif

    /*
     * SQL-callable functions remain available when the library is loaded by
     * CREATE EXTENSION. The planner hook is installed only during server
     * preload, which is the required deployment mode for pg_qi.
     */
    if (!process_shared_preload_libraries_in_progress)
        return;

    qi_preflight_force_execution();
    pg_qi_preloaded = true;
    previous_planner_hook = planner_hook;
#if PG_QI_TESTING
    pg_qi_previous_hook_present = previous_planner_hook != NULL;
#endif
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
