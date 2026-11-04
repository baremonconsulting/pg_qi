/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef PG_QI_QI_DELEGATION_H
#define PG_QI_QI_DELEGATION_H

#include "postgres.h"
#include "fmgr.h"

#define QI_MAX_DELEGATION_FRAMES 64

typedef enum QiForceOrigin
{
    QI_FORCE_OFF = 0,
    QI_FORCE_EXTERNAL,
    QI_FORCE_QI_STACK
} QiForceOrigin;

typedef struct QiDelegationFrame
{
    int                 guc_nest_level;
    QiForceOrigin       force_origin;
    QiForceOrigin       effective_origin;
    bool                delegation_requested;
    bool                guc_changed;
    bool                reentry;
    uint64              generation;
    uint64              frame_id;
} QiDelegationFrame;

typedef struct QiDelegationCounters
{
    int                 pushed;
    int                 popped;
    int                 unwound;
    int                 force_transitions;
    int                 nested_masks;
    int                 nested_pg_plans;
    int                 nested_duckdb_plans;
    int                 pg_plans;
    int                 duckdb_plans;
    uint64              planner_entries;
    uint64              planner_elapsed_us;
} QiDelegationCounters;

#if PG_QI_TESTING
typedef enum QiBenchmarkMode
{
    QI_BENCH_NORMAL = 0,
    QI_BENCH_BYPASS,
    QI_BENCH_FRAME_ONLY,
    QI_BENCH_SHADOW_SAVE,
    QI_BENCH_DELEGATE
} QiBenchmarkMode;

extern void qi_init_frame_diagnostics(void);
extern void qi_backend_final_report(int code, Datum arg);
#endif
extern QiDelegationFrame *qi_frame_push(int guc_nest_level,
                                        QiForceOrigin ambient_origin,
                                        bool delegation_requested,
                                        bool reentry,
                                        uint64 generation);
extern bool qi_frame_pop(uint64 expected_frame_id);
extern bool qi_frame_unwind_on_error(uint64 expected_frame_id);
extern QiForceOrigin qi_current_force_origin(void);
extern bool qi_token_consume_top_level(uint64 *generation);
extern void qi_record_nested_plan(bool has_duckdb_scan);
extern void qi_record_completed_plan(bool has_duckdb_scan);
extern void qi_record_planner_timing(uint64 elapsed_us);
extern void qi_preflight_force_execution(void);
extern void qi_set_force_execution(bool enabled);
#if PG_QI_TESTING
extern PGDLLEXPORT Datum qi_test_force_meta(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_test_force_save_roundtrip(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_test_frame_contract(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_test_frame_state(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_spike_arm_next(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_spike_disarm(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_spike_reset_token(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_test_token_state(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_spike_reset_state(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_spike_state(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_spike_observer(PG_FUNCTION_ARGS);
extern PGDLLEXPORT Datum qi_test_active_control_guard(PG_FUNCTION_ARGS);
#endif

#endif /* PG_QI_QI_DELEGATION_H */
