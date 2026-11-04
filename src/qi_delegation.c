/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "postgres.h"

#include "access/htup_details.h"
#include "access/parallel.h"
#include "catalog/pg_authid_d.h"
#include "funcapi.h"
#include "miscadmin.h"
#include "pg_qi/qi_delegation.h"
#include "pg_qi/qi_detector.h"
#if PG_QI_TESTING
#include "pg_qi/qi_reenter.h"
#endif
#include "utils/builtins.h"
#include "utils/guc.h"

#define QI_FORCE_GUC "duckdb.force_execution"

typedef struct QiBackendState
{
    QiDelegationFrame   frames[QI_MAX_DELEGATION_FRAMES];
    int                 depth;
    bool                cleanup_broken;
    uint64              next_frame_id;
    uint64              armed_generation;
    uint64              consumed_generation;
    QiDelegationCounters counters;
} QiBackendState;

static QiBackendState qi_backend_state;
#if PG_QI_TESTING
static int qi_diag_armed_generation = 0;
static int qi_diag_consumed_generation = 0;
static bool qi_diag_token_armed = false;
static int qi_diag_last_force_origin = QI_FORCE_OFF;
static int qi_diag_last_effective_origin = QI_FORCE_OFF;
static bool qi_diag_last_delegation_requested = false;
static bool qi_diag_last_guc_changed = false;
#endif
static bool qi_force_preflight_passed = false;
static int qi_force_flags = 0;

#if PG_QI_TESTING
void
qi_backend_final_report(int code, Datum arg)
{
    /*
     * Machine-readable backend-local evidence for the WP10 concurrency
     * parser.  The callback is deliberately read-only and cannot disturb
     * cleanup accounting while the backend is exiting.
     */
    ereport(LOG,
            (errmsg("PG_QI_FINAL app=%s pid=%d pushed=%d popped=%d unwound=%d depth=%d cleanup_broken=%s pg_plans=%d duckdb_plans=%d planner_entries=" UINT64_FORMAT " planner_us=" UINT64_FORMAT,
                    application_name != NULL && application_name[0] != '\0'
                        ? application_name : "-",
                    MyProcPid,
                    qi_backend_state.counters.pushed,
                    qi_backend_state.counters.popped,
                    qi_backend_state.counters.unwound,
                    qi_backend_state.depth,
                    qi_backend_state.cleanup_broken ? "true" : "false",
                    qi_backend_state.counters.pg_plans,
                    qi_backend_state.counters.duckdb_plans,
                    qi_backend_state.counters.planner_entries,
                    qi_backend_state.counters.planner_elapsed_us)));
}

static const struct config_enum_entry qi_force_origin_options[] = {
    {"off", QI_FORCE_OFF, false},
    {"external", QI_FORCE_EXTERNAL, false},
    {"qi_stack", QI_FORCE_QI_STACK, false},
    {NULL, 0, false}
};
#endif

#if PG_VERSION_NUM >= 170000
static config_handle *qi_force_handle = NULL;
#endif

/*
 * The accounting invariant is:
 *
 *   depth = pushed - popped - unwound
 *
 * A failed capacity check changes neither side.  Cleanup operations never
 * allocate and never throw: an ordering violation only poisons the sentinel,
 * so cleanup cannot hide the ERROR which caused an unwind.
 */
static bool
qi_state_try_push(QiBackendState *state, int guc_nest_level,
                  QiForceOrigin ambient_origin, bool delegation_requested,
                  bool reentry, uint64 generation,
                  QiDelegationFrame **result)
{
    QiDelegationFrame *frame;
    QiForceOrigin inherited_origin;

    if (state->depth < 0 || state->depth >= QI_MAX_DELEGATION_FRAMES)
        return false;

    inherited_origin = state->depth > 0
        ? state->frames[state->depth - 1].effective_origin
        : ambient_origin;

    frame = &state->frames[state->depth];
    MemSet(frame, 0, sizeof(*frame));
    frame->guc_nest_level = guc_nest_level;
    frame->force_origin = inherited_origin;
    frame->delegation_requested = delegation_requested;
    frame->reentry = reentry;
    frame->generation = generation;
    frame->frame_id = ++state->next_frame_id;

    if (reentry && inherited_origin == QI_FORCE_QI_STACK)
        frame->effective_origin = QI_FORCE_OFF;
    else if (inherited_origin == QI_FORCE_EXTERNAL)
        frame->effective_origin = QI_FORCE_EXTERNAL;
    else if (delegation_requested || inherited_origin == QI_FORCE_QI_STACK)
        frame->effective_origin = QI_FORCE_QI_STACK;
    else
        frame->effective_origin = QI_FORCE_OFF;

    frame->guc_changed =
        (inherited_origin == QI_FORCE_OFF) !=
        (frame->effective_origin == QI_FORCE_OFF);
    if (frame->guc_changed)
        state->counters.force_transitions++;
    if (reentry && inherited_origin == QI_FORCE_QI_STACK)
        state->counters.nested_masks++;

    state->depth++;
    state->counters.pushed++;
#if PG_QI_TESTING
    if (state == &qi_backend_state)
    {
        qi_diag_last_force_origin = frame->force_origin;
        qi_diag_last_effective_origin = frame->effective_origin;
        qi_diag_last_delegation_requested = frame->delegation_requested;
        qi_diag_last_guc_changed = frame->guc_changed;
    }
#endif
    *result = frame;
    return true;
}

static bool
qi_state_remove_top(QiBackendState *state, uint64 expected_frame_id,
                    bool unwind)
{
    QiDelegationFrame *top;

    if (state->depth <= 0)
    {
        state->cleanup_broken = true;
        return false;
    }

    top = &state->frames[state->depth - 1];
    if (top->frame_id != expected_frame_id)
    {
        state->cleanup_broken = true;
        return false;
    }

    MemSet(top, 0, sizeof(*top));
    state->depth--;
    if (unwind)
        state->counters.unwound++;
    else
        state->counters.popped++;
    return true;
}

#if PG_QI_TESTING
void
qi_init_frame_diagnostics(void)
{
    DefineCustomIntVariable("pg_qi.spike_stack_depth",
                            "Current pg_qi delegation-frame depth.",
                            NULL,
                            &qi_backend_state.depth,
                            0, 0, QI_MAX_DELEGATION_FRAMES,
                            PGC_INTERNAL, 0, NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi.spike_cleanup_broken",
                             "Reports a delegation-frame cleanup invariant violation.",
                             NULL,
                             &qi_backend_state.cleanup_broken,
                             false,
                             PGC_INTERNAL, 0, NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_frames_pushed", NULL, NULL,
                            &qi_backend_state.counters.pushed,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_frames_popped", NULL, NULL,
                            &qi_backend_state.counters.popped,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_frames_unwound", NULL, NULL,
                            &qi_backend_state.counters.unwound,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_force_transitions", NULL, NULL,
                            &qi_backend_state.counters.force_transitions,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_nested_masks", NULL, NULL,
                            &qi_backend_state.counters.nested_masks,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_nested_pg_plans", NULL, NULL,
                            &qi_backend_state.counters.nested_pg_plans,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_nested_duckdb_plans", NULL, NULL,
                            &qi_backend_state.counters.nested_duckdb_plans,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_pg_plans", NULL, NULL,
                            &qi_backend_state.counters.pg_plans,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_duckdb_plans", NULL, NULL,
                            &qi_backend_state.counters.duckdb_plans,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_armed_generation", NULL, NULL,
                            &qi_diag_armed_generation,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomIntVariable("pg_qi.spike_consumed_generation", NULL, NULL,
                            &qi_diag_consumed_generation,
                            0, 0, INT_MAX, PGC_INTERNAL, 0,
                            NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi.spike_token_armed", NULL, NULL,
                             &qi_diag_token_armed,
                             false, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.spike_last_force_origin", NULL, NULL,
                             &qi_diag_last_force_origin, QI_FORCE_OFF,
                             qi_force_origin_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomEnumVariable("pg_qi.spike_last_effective_origin", NULL, NULL,
                             &qi_diag_last_effective_origin, QI_FORCE_OFF,
                             qi_force_origin_options, PGC_INTERNAL, 0,
                             NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi.spike_last_delegation_requested", NULL, NULL,
                             &qi_diag_last_delegation_requested, false,
                             PGC_INTERNAL, 0, NULL, NULL, NULL);
    DefineCustomBoolVariable("pg_qi.spike_last_guc_changed", NULL, NULL,
                             &qi_diag_last_guc_changed, false,
                             PGC_INTERNAL, 0, NULL, NULL, NULL);
}
#endif

QiDelegationFrame *
qi_frame_push(int guc_nest_level, QiForceOrigin ambient_origin,
              bool delegation_requested, bool reentry, uint64 generation)
{
    QiDelegationFrame *frame = NULL;

    if (!qi_state_try_push(&qi_backend_state, guc_nest_level, ambient_origin,
                           delegation_requested, reentry, generation, &frame))
        ereport(ERROR,
                (errmsg("pg_qi delegation frame capacity exceeded"),
                 errdetail("The backend-local limit is %d frames.",
                           QI_MAX_DELEGATION_FRAMES)));
    return frame;
}

bool
qi_frame_pop(uint64 expected_frame_id)
{
    return qi_state_remove_top(&qi_backend_state, expected_frame_id, false);
}

bool
qi_frame_unwind_on_error(uint64 expected_frame_id)
{
    return qi_state_remove_top(&qi_backend_state, expected_frame_id, true);
}

QiForceOrigin
qi_current_force_origin(void)
{
    const char *value;
    bool enabled;

    value = GetConfigOption(QI_FORCE_GUC, false, false);
    if (!parse_bool(value, &enabled))
        ereport(ERROR,
                (errmsg("invalid boolean value for %s: %s",
                        QI_FORCE_GUC, value)));
    return enabled ? QI_FORCE_EXTERNAL : QI_FORCE_OFF;
}

bool
qi_token_consume_top_level(uint64 *generation)
{
#if PG_QI_TESTING
    if (qi_backend_state.depth != 0 || qi_reenter_is_active() ||
        qi_backend_state.armed_generation ==
        qi_backend_state.consumed_generation)
        return false;

    /* Backend-local assignment is intentionally non-transactional. */
    qi_backend_state.consumed_generation =
        qi_backend_state.armed_generation;
    qi_diag_consumed_generation =
        (int) qi_backend_state.consumed_generation;
    qi_diag_token_armed = false;
    *generation = qi_backend_state.consumed_generation;
    return true;
#else
    *generation = 0;
    return false;
#endif
}

#if PG_QI_TESTING
static void
qi_token_require_idle(const char *operation)
{
    if (qi_backend_state.depth != 0 || qi_reenter_is_active())
        ereport(ERROR,
                (errmsg("cannot %s pg_qi token while planner state is active",
                        operation)));
}

static bool
qi_token_controls_allowed(void)
{
    return qi_backend_state.depth == 0 && !qi_reenter_is_active();
}
#endif

void
qi_record_nested_plan(bool has_duckdb_scan)
{
    if (has_duckdb_scan)
        qi_backend_state.counters.nested_duckdb_plans++;
    else
        qi_backend_state.counters.nested_pg_plans++;
}

void
qi_record_completed_plan(bool has_duckdb_scan)
{
    if (has_duckdb_scan)
        qi_backend_state.counters.duckdb_plans++;
    else
        qi_backend_state.counters.pg_plans++;
}

void
qi_record_planner_timing(uint64 elapsed_us)
{
    qi_backend_state.counters.planner_entries++;
    qi_backend_state.counters.planner_elapsed_us += elapsed_us;
}

void
qi_preflight_force_execution(void)
{
#if PG_VERSION_NUM >= 170000
    qi_force_handle = get_config_handle(QI_FORCE_GUC);
    if (qi_force_handle == NULL)
        ereport(ERROR,
                (errmsg("%s is unavailable or still a placeholder",
                        QI_FORCE_GUC),
                 errhint("Load pg_duckdb before pg_qi in shared_preload_libraries.")));

    /* Flags are diagnostics, never the proof that the GUC exists. */
    qi_force_flags = GetConfigOptionFlags(QI_FORCE_GUC, false);
#else
    /* missing_ok=false deliberately raises ERROR when pg_duckdb is absent. */
    qi_force_flags = GetConfigOptionFlags(QI_FORCE_GUC, false);
    if ((qi_force_flags & GUC_CUSTOM_PLACEHOLDER) != 0)
        ereport(ERROR,
                (errmsg("%s is still a placeholder", QI_FORCE_GUC),
                 errhint("Load pg_duckdb before pg_qi in shared_preload_libraries.")));
#endif

    if ((qi_force_flags & GUC_NO_RESET) != 0)
        ereport(ERROR,
                (errmsg("%s has GUC_NO_RESET; GUC_ACTION_SAVE is unusable",
                        QI_FORCE_GUC)));

    qi_force_preflight_passed = true;
}

void
qi_set_force_execution(bool enabled)
{
    const char *value = enabled ? "on" : "off";
    int result;

    if (!qi_force_preflight_passed)
        ereport(ERROR,
                (errmsg("pg_qi force-execution preflight has not passed")));

#if PG_VERSION_NUM >= 170000
    result = set_config_with_handle(QI_FORCE_GUC, qi_force_handle, value,
                                    PGC_USERSET, PGC_S_SESSION, GetUserId(),
                                    GUC_ACTION_SAVE, true, ERROR, false);
#else
    result = set_config_option(QI_FORCE_GUC, value,
                               PGC_USERSET, PGC_S_SESSION,
                               GUC_ACTION_SAVE, true, ERROR, false);
#endif

    if (result <= 0)
        ereport(ERROR,
                (errmsg("could not set %s to %s", QI_FORCE_GUC, value)));
}

#if PG_QI_TESTING
Datum
qi_test_force_meta(PG_FUNCTION_ARGS)
{
    TupleDesc tuple_desc;
    Datum values[6];
    bool nulls[6] = {false, false, false, false, false, false};

    if (get_call_result_type(fcinfo, NULL, &tuple_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR,
                (errmsg("return type must be a row type")));

    values[0] = BoolGetDatum(qi_force_preflight_passed);
#if PG_VERSION_NUM >= 170000
    values[1] = BoolGetDatum(qi_force_handle != NULL);
#else
    nulls[1] = true;
    values[1] = BoolGetDatum(false);
#endif
    values[2] = Int32GetDatum(qi_force_flags);
    values[3] = BoolGetDatum((qi_force_flags & GUC_CUSTOM_PLACEHOLDER) != 0);
    values[4] = BoolGetDatum((qi_force_flags & GUC_NO_RESET) != 0);
    values[5] = CStringGetTextDatum(QI_DUCKDB_SCAN_PROVIDER);

    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(tuple_desc, values, nulls)));
}

Datum
qi_test_force_save_roundtrip(PG_FUNCTION_ARGS)
{
    bool requested = PG_GETARG_BOOL(0);
    const char *requested_value = requested ? "on" : "off";
    char *before;
    const char *during;
    const char *after;
    int nest_level;
    bool passed;

    before = pstrdup(GetConfigOption(QI_FORCE_GUC, false, false));
    nest_level = NewGUCNestLevel();
    qi_set_force_execution(requested);
    during = GetConfigOption(QI_FORCE_GUC, false, false);
    passed = strcmp(during, requested_value) == 0;
    AtEOXact_GUC(true, nest_level);
    after = GetConfigOption(QI_FORCE_GUC, false, false);
    passed = passed && strcmp(after, before) == 0;

    PG_RETURN_BOOL(passed);
}

Datum
qi_test_frame_contract(PG_FUNCTION_ARGS)
{
    TupleDesc tuple_desc;
    Datum values[7];
    bool nulls[7] = {false, false, false, false, false, false, false};
    QiBackendState normal = {0};
    QiBackendState overflow = {0};
    QiBackendState corrupt = {0};
    QiDelegationFrame *parent;
    QiDelegationFrame *child;
    QiDelegationFrame *ignored;
    uint64 parent_id;
    uint64 child_id;
    int i;
    bool lifo;
    bool overflow_rejected;
    bool corruption_detected;
    bool unwind_clean;
    bool parent_provenance;
    bool accounting;

    if (get_call_result_type(fcinfo, NULL, &tuple_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR, (errmsg("return type must be a row type")));

    qi_state_try_push(&normal, 1, QI_FORCE_EXTERNAL, false, false, 10,
                      &parent);
    parent_id = parent->frame_id;
    qi_state_try_push(&normal, 2, QI_FORCE_OFF, true, true, 11, &child);
    child_id = child->frame_id;
    parent_provenance = child->force_origin == QI_FORCE_EXTERNAL &&
        child->effective_origin == QI_FORCE_EXTERNAL;
    lifo = qi_state_remove_top(&normal, child_id, false) &&
        qi_state_remove_top(&normal, parent_id, false) && normal.depth == 0;

    for (i = 0; i < QI_MAX_DELEGATION_FRAMES; i++)
        qi_state_try_push(&overflow, i, QI_FORCE_OFF, false, false, i,
                          &ignored);
    overflow_rejected =
        !qi_state_try_push(&overflow, 999, QI_FORCE_OFF, false, false, 999,
                           &ignored) &&
        overflow.depth == QI_MAX_DELEGATION_FRAMES &&
        overflow.counters.pushed == QI_MAX_DELEGATION_FRAMES;

    qi_state_try_push(&corrupt, 1, QI_FORCE_OFF, false, false, 1, &parent);
    corruption_detected =
        !qi_state_remove_top(&corrupt, parent->frame_id + 1, false) &&
        corrupt.cleanup_broken && corrupt.depth == 1;

    MemSet(&normal, 0, sizeof(normal));
    qi_state_try_push(&normal, 1, QI_FORCE_OFF, true, false, 20, &parent);
    unwind_clean = qi_state_remove_top(&normal, parent->frame_id, true) &&
        normal.depth == 0 && !normal.cleanup_broken;
    accounting = normal.depth ==
        (int) (normal.counters.pushed - normal.counters.popped -
               normal.counters.unwound);

    values[0] = BoolGetDatum(lifo);
    values[1] = BoolGetDatum(overflow_rejected);
    values[2] = BoolGetDatum(corruption_detected);
    values[3] = BoolGetDatum(unwind_clean);
    values[4] = BoolGetDatum(parent_provenance);
    values[5] = BoolGetDatum(accounting);
    values[6] = BoolGetDatum(qi_backend_state.depth == 0 &&
                             !qi_backend_state.cleanup_broken);
    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(tuple_desc, values, nulls)));
}

Datum
qi_test_frame_state(PG_FUNCTION_ARGS)
{
    TupleDesc tuple_desc;
    Datum values[10];
    bool nulls[10] = {false};

    if (get_call_result_type(fcinfo, NULL, &tuple_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR, (errmsg("return type must be a row type")));

    values[0] = Int32GetDatum(qi_backend_state.depth);
    values[1] = BoolGetDatum(qi_backend_state.cleanup_broken);
    values[2] = Int64GetDatum((int64) qi_backend_state.counters.pushed);
    values[3] = Int64GetDatum((int64) qi_backend_state.counters.popped);
    values[4] = Int64GetDatum((int64) qi_backend_state.counters.unwound);
    values[5] = Int64GetDatum((int64) qi_backend_state.counters.force_transitions);
    values[6] = Int64GetDatum((int64) qi_backend_state.counters.nested_masks);
    values[7] = Int64GetDatum((int64) qi_backend_state.counters.nested_pg_plans);
    values[8] = Int64GetDatum((int64) qi_backend_state.counters.nested_duckdb_plans);
    values[9] = BoolGetDatum(qi_backend_state.depth ==
        (int64) (qi_backend_state.counters.pushed -
                 qi_backend_state.counters.popped -
                 qi_backend_state.counters.unwound));
    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(tuple_desc, values, nulls)));
}

Datum
qi_spike_arm_next(PG_FUNCTION_ARGS)
{
    qi_token_require_idle("arm");
    if (qi_backend_state.armed_generation == INT_MAX)
        ereport(ERROR,
                (errmsg("pg_qi token generation exhausted"),
                 errhint("Reset the spike token while the planner is idle.")));

    qi_backend_state.armed_generation++;
    qi_diag_armed_generation = (int) qi_backend_state.armed_generation;
    qi_diag_token_armed = true;
    PG_RETURN_INT64((int64) qi_backend_state.armed_generation);
}

Datum
qi_spike_disarm(PG_FUNCTION_ARGS)
{
    qi_token_require_idle("disarm");
    qi_backend_state.consumed_generation =
        qi_backend_state.armed_generation;
    qi_diag_consumed_generation =
        (int) qi_backend_state.consumed_generation;
    qi_diag_token_armed = false;
    PG_RETURN_INT64((int64) qi_backend_state.consumed_generation);
}

Datum
qi_spike_reset_token(PG_FUNCTION_ARGS)
{
    qi_token_require_idle("reset");
    qi_backend_state.armed_generation = 0;
    qi_backend_state.consumed_generation = 0;
    qi_diag_armed_generation = 0;
    qi_diag_consumed_generation = 0;
    qi_diag_token_armed = false;
    qi_diag_last_force_origin = QI_FORCE_OFF;
    qi_diag_last_effective_origin = QI_FORCE_OFF;
    qi_diag_last_delegation_requested = false;
    qi_diag_last_guc_changed = false;
    PG_RETURN_VOID();
}

Datum
qi_test_token_state(PG_FUNCTION_ARGS)
{
    TupleDesc tuple_desc;
    Datum values[3];
    bool nulls[3] = {false, false, false};

    if (get_call_result_type(fcinfo, NULL, &tuple_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR, (errmsg("return type must be a row type")));

    values[0] = Int64GetDatum((int64) qi_backend_state.armed_generation);
    values[1] = Int64GetDatum((int64) qi_backend_state.consumed_generation);
    values[2] = BoolGetDatum(qi_backend_state.armed_generation !=
                             qi_backend_state.consumed_generation);
    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(tuple_desc, values, nulls)));
}

Datum
qi_spike_reset_state(PG_FUNCTION_ARGS)
{
    qi_token_require_idle("reset state for");
    MemSet(qi_backend_state.frames, 0, sizeof(qi_backend_state.frames));
    MemSet(&qi_backend_state.counters, 0, sizeof(qi_backend_state.counters));
    qi_backend_state.cleanup_broken = false;
    qi_backend_state.next_frame_id = 0;
    qi_backend_state.armed_generation = 0;
    qi_backend_state.consumed_generation = 0;
    qi_diag_armed_generation = 0;
    qi_diag_consumed_generation = 0;
    qi_diag_token_armed = false;
    PG_RETURN_VOID();
}

Datum
qi_spike_state(PG_FUNCTION_ARGS)
{
    TupleDesc tuple_desc;
    Datum values[14];
    bool nulls[14] = {false};
    int compensated_pushed = qi_backend_state.counters.pushed;
    int compensated_popped = qi_backend_state.counters.popped;

    if (get_call_result_type(fcinfo, NULL, &tuple_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR, (errmsg("return type must be a row type")));

    /* Compensate for the planner frame created by this helper's own SELECT. */
    if (compensated_pushed > 0 && compensated_popped > 0)
    {
        compensated_pushed--;
        compensated_popped--;
    }
    values[0] = Int32GetDatum(qi_backend_state.depth);
    values[1] = BoolGetDatum(qi_backend_state.cleanup_broken);
    values[2] = Int64GetDatum((int64) compensated_pushed);
    values[3] = Int64GetDatum((int64) compensated_popped);
    values[4] = Int64GetDatum((int64) qi_backend_state.counters.unwound);
    values[5] = Int64GetDatum((int64) qi_backend_state.counters.force_transitions);
    values[6] = Int64GetDatum((int64) qi_backend_state.counters.nested_masks);
    values[7] = Int64GetDatum((int64) qi_backend_state.counters.nested_pg_plans);
    values[8] = Int64GetDatum((int64) qi_backend_state.counters.nested_duckdb_plans);
    values[9] = Int64GetDatum((int64) qi_backend_state.armed_generation);
    values[10] = Int64GetDatum((int64) qi_backend_state.consumed_generation);
    values[11] = BoolGetDatum(qi_diag_token_armed);
    values[12] = BoolGetDatum(qi_reenter_is_active());
    values[13] = BoolGetDatum(qi_backend_state.depth ==
        qi_backend_state.counters.pushed -
        qi_backend_state.counters.popped -
        qi_backend_state.counters.unwound);
    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(tuple_desc, values, nulls)));
}

Datum
qi_spike_observer(PG_FUNCTION_ARGS)
{
    TupleDesc tuple_desc;
    Datum values[5];
    bool nulls[5] = {false, false, false, false, false};

    if (get_call_result_type(fcinfo, NULL, &tuple_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR, (errmsg("return type must be a row type")));
    values[0] = Int32GetDatum(MyProcPid);
    values[1] = BoolGetDatum(IsParallelWorker());
    values[2] = Int32GetDatum(qi_backend_state.depth);
    values[3] = BoolGetDatum(qi_backend_state.cleanup_broken);
    values[4] = BoolGetDatum(qi_diag_token_armed);
    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(tuple_desc, values, nulls)));
}

Datum
qi_test_active_control_guard(PG_FUNCTION_ARGS)
{
    QiDelegationFrame *frame;
    int nest_level;
    bool rejected;

    nest_level = NewGUCNestLevel();
    frame = qi_frame_push(nest_level, QI_FORCE_OFF, false, false, 0);
    rejected = !qi_token_controls_allowed();
    AtEOXact_GUC(true, nest_level);
    (void) qi_frame_pop(frame->frame_id);
    PG_RETURN_BOOL(rejected && qi_backend_state.depth == 0 &&
                   !qi_backend_state.cleanup_broken);
}
#endif
