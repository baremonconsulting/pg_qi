/* Test-only SQL surface. Never installed by the default production build. */

CREATE FUNCTION test_force_meta(OUT preflight_passed boolean, OUT handle_present boolean, OUT flags integer, OUT is_placeholder boolean, OUT no_reset boolean, OUT provider_name text)
RETURNS record AS 'MODULE_PATHNAME', 'qi_test_force_meta' LANGUAGE C STABLE STRICT PARALLEL SAFE;
CREATE FUNCTION test_detector_contract(OUT provider_at_root boolean, OUT provider_under_material boolean, OUT provider_in_subplan boolean, OUT provider_absent boolean)
RETURNS record AS 'MODULE_PATHNAME', 'qi_test_detector_contract' LANGUAGE C STABLE STRICT PARALLEL SAFE;
CREATE FUNCTION test_force_save_roundtrip(enabled boolean) RETURNS boolean
AS 'MODULE_PATHNAME', 'qi_test_force_save_roundtrip' LANGUAGE C VOLATILE STRICT PARALLEL UNSAFE;
CREATE FUNCTION test_frame_contract(OUT lifo boolean, OUT overflow_rejected boolean, OUT corruption_detected boolean, OUT unwind_clean boolean, OUT parent_provenance boolean, OUT accounting_valid boolean, OUT backend_clean boolean)
RETURNS record AS 'MODULE_PATHNAME', 'qi_test_frame_contract' LANGUAGE C VOLATILE STRICT PARALLEL UNSAFE;
CREATE FUNCTION test_frame_state(OUT depth integer, OUT cleanup_broken boolean, OUT pushed bigint, OUT popped bigint, OUT unwound bigint, OUT force_transitions bigint, OUT nested_masks bigint, OUT nested_pg_plans bigint, OUT nested_duckdb_plans bigint, OUT accounting_valid boolean)
RETURNS record AS 'MODULE_PATHNAME', 'qi_test_frame_state' LANGUAGE C STABLE STRICT PARALLEL UNSAFE;
CREATE FUNCTION spike_arm_next() RETURNS bigint AS 'MODULE_PATHNAME', 'qi_spike_arm_next' LANGUAGE C VOLATILE STRICT PARALLEL UNSAFE;
CREATE FUNCTION spike_disarm() RETURNS bigint AS 'MODULE_PATHNAME', 'qi_spike_disarm' LANGUAGE C VOLATILE STRICT PARALLEL UNSAFE;
CREATE FUNCTION spike_reset_token() RETURNS void AS 'MODULE_PATHNAME', 'qi_spike_reset_token' LANGUAGE C VOLATILE STRICT PARALLEL UNSAFE;
CREATE FUNCTION test_token_state(OUT armed_generation bigint, OUT consumed_generation bigint, OUT token_armed boolean)
RETURNS record AS 'MODULE_PATHNAME', 'qi_test_token_state' LANGUAGE C VOLATILE STRICT PARALLEL UNSAFE;
CREATE FUNCTION spike_reset_state() RETURNS void AS 'MODULE_PATHNAME', 'qi_spike_reset_state' LANGUAGE C VOLATILE STRICT PARALLEL UNSAFE;
CREATE FUNCTION spike_state(OUT depth integer, OUT cleanup_broken boolean, OUT pushed bigint, OUT popped bigint, OUT unwound bigint, OUT force_transitions bigint, OUT nested_masks bigint, OUT nested_pg_plans bigint, OUT nested_duckdb_plans bigint, OUT armed_generation bigint, OUT consumed_generation bigint, OUT token_armed boolean, OUT reentry_active boolean, OUT accounting_valid boolean)
RETURNS record AS 'MODULE_PATHNAME', 'qi_spike_state' LANGUAGE C VOLATILE STRICT PARALLEL UNSAFE;
COMMENT ON FUNCTION spike_state() IS 'TAP convenience snapshot; pushed/popped compensate for this SELECT own planner frame. Use SHOW diagnostics for primary assertions.';
CREATE FUNCTION spike_observer(OUT pid integer, OUT parallel_worker boolean, OUT depth integer, OUT cleanup_broken boolean, OUT token_armed boolean)
RETURNS record AS 'MODULE_PATHNAME', 'qi_spike_observer' LANGUAGE C VOLATILE STRICT PARALLEL SAFE;
CREATE FUNCTION test_active_control_guard() RETURNS boolean
AS 'MODULE_PATHNAME', 'qi_test_active_control_guard' LANGUAGE C VOLATILE STRICT PARALLEL UNSAFE;
