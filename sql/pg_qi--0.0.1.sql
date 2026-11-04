/*
 * Copyright 2026 Baremon s.r.o.
 * SPDX-License-Identifier: Apache-2.0
 */

CREATE FUNCTION version()
RETURNS text
AS 'MODULE_PATHNAME', 'pg_qi_version'
LANGUAGE C
IMMUTABLE
STRICT
PARALLEL SAFE;

COMMENT ON FUNCTION version() IS
    'Returns the installed pg_qi extension version.';

CREATE FUNCTION is_preloaded()
RETURNS boolean
AS 'MODULE_PATHNAME', 'pg_qi_is_preloaded'
LANGUAGE C
STABLE
STRICT
PARALLEL SAFE;

COMMENT ON FUNCTION is_preloaded() IS
    'Reports whether pg_qi was loaded through shared_preload_libraries in this backend.';
