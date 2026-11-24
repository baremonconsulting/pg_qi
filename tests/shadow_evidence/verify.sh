#!/usr/bin/env bash
# Execution-time planning by the DuckDB heap reader must preserve the
# client statement's published shadow evidence.
set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=${PGBIN:-$pg_install_root/$major/bin}
psql=("$pgbin/psql" -XqAt -v ON_ERROR_STOP=1 -d "${PGDATABASE:-postgres}")
query='SELECT grp, count(DISTINCT buyer) FROM public.qi_shadow_evidence_t GROUP BY grp ORDER BY grp'

"${psql[@]}" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_duckdb;
CREATE EXTENSION IF NOT EXISTS pg_qi;
BEGIN;
CREATE TABLE public.qi_shadow_evidence_t(grp int, buyer int);
INSERT INTO public.qi_shadow_evidence_t VALUES (1, 1), (1, 1), (1, 2), (2, 3);
CREATE FUNCTION public.qi_shadow_evidence_nested() RETURNS bigint LANGUAGE plpgsql AS $$
BEGIN
    RETURN (SELECT count(*) FROM public.qi_shadow_evidence_t);
END
$$;
COMMIT;
SQL
trap '"${psql[@]}" -c "DROP FUNCTION public.qi_shadow_evidence_nested(); DROP TABLE public.qi_shadow_evidence_t"' EXIT

result=$("${psql[@]}" \
    -c 'SET pg_qi.routing_mode = shadow_p0' \
    -c 'SET duckdb.force_execution = true' \
    -c "$query" \
    -c 'SHOW pg_qi.shadow_observed_engine' \
    -c 'SHOW pg_qi.shadow_verification' \
    -c 'SHOW pg_qi.decision_v2' \
    -c 'SET duckdb.force_execution = false' \
    -c "$query" \
    -c 'SHOW pg_qi.shadow_observed_engine' \
    -c 'SHOW pg_qi.shadow_verification')

mapfile -t rows <<< "$result"
if [[ ${#rows[@]} -ne 9 ]]; then
    printf 'FAIL shadow evidence: unexpected output\n%s\n' "$result" >&2
    exit 1
fi

printf 'forced shadow evidence: %s / %s\n' "${rows[2]}" "${rows[3]}"
printf 'forced decision_v2: %s\n' "${rows[4]}"
printf 'native shadow evidence: %s / %s\n' "${rows[7]}" "${rows[8]}"

if [[ ${rows[0]} != '1|2' || ${rows[1]} != '2|1' ||
      ${rows[5]} != "${rows[0]}" || ${rows[6]} != "${rows[1]}" ]]; then
    printf 'FAIL shadow evidence: unexpected aggregate results\n%s\n' "$result" >&2
    exit 1
fi
if [[ ";${rows[4]};" != *';force=external;'* ||
      ";${rows[4]};" != *';engine=duckdb;'* ||
      ";${rows[4]};" != *';verification=verified;'* ]]; then
    echo 'FAIL shadow evidence: decision_v2 did not verify the externally forced DuckDB scan' >&2
    exit 1
fi
if [[ ${rows[7]} != unknown || ${rows[8]} != unverified ]]; then
    echo 'FAIL shadow evidence: native query did not publish its own observation' >&2
    exit 1
fi
if [[ ${rows[2]} != duckdb || ${rows[3]} != verified ]]; then
    echo 'FAIL shadow evidence: client DuckDB observation was overwritten' >&2
    exit 1
fi

# Some provider builds bypass planner hooks for their heap reader. A PL/pgSQL
# call exercises execution-time planning on every supported provider build.
nested=$("${psql[@]}" \
    -c 'SET duckdb.force_execution = false' \
    -c 'SELECT public.qi_shadow_evidence_nested()' \
    -c 'SHOW pg_qi.shadow_context')
if [[ "$nested" != $'4\nproven_client' ]]; then
    printf 'FAIL shadow evidence: nested SQL changed the client context\n%s\n' "$nested" >&2
    exit 1
fi

echo 'shadow evidence: ok'
