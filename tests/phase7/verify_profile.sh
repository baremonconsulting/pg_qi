#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=${PG_BIN:-$pg_install_root/$major/bin}
repo=$(cd "$(dirname "$0")/../.." && pwd)
build_id=${PG_QI_BUILD_ID:?PG_QI_BUILD_ID is required}
postgres_build_id=${PG_QI_POSTGRES_BUILD_ID:?PG_QI_POSTGRES_BUILD_ID is required}
site_id=${PG_QI_SITE_BUILD_ID:?PG_QI_SITE_BUILD_ID is required}
now=$(date +%s)
pg_version_num=$("$pgbin/psql" -X -qAt postgres -c 'SHOW server_version_num')
generator=(python3 "$repo/tools/phase7/profile_generator.py"
    --postgres-version-num "$pg_version_num" \
    --postgres-build "$postgres_build_id" --pg-qi-build "$build_id"
    --site-build "$site_id")
profile_a=$("${generator[@]}" --generation 11 --issued-at "$((now - 60))" \
    --expires-at "$((now + 3600))" --memory-bytes 1073741824 \
    --threads 2 --concurrency 1)
profile_b=$("${generator[@]}" --generation 12 --issued-at "$((now - 60))" \
    --expires-at "$((now + 3600))" --memory-bytes 2147483648 \
    --threads 4 --concurrency 2)
wrong_build=$("${generator[@]}" --generation 13 --issued-at "$((now - 60))" \
    --expires-at "$((now + 3600))" \
    --pg-qi-build cccccccccccccccccccccccccccccccccccccccc)
wrong_postgres=$("${generator[@]}" --generation 16 --issued-at "$((now - 60))" \
    --expires-at "$((now + 3600))" \
    --postgres-build ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff)
wrong_site=$("${generator[@]}" --generation 14 --issued-at "$((now - 60))" \
    --expires-at "$((now + 3600))" \
    --site-build dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd)
expired=$("${generator[@]}" --generation 15 --issued-at "$((now - 120))" \
    --expires-at "$((now - 60))")
invalid=invalid-profile
oversize=$(python3 - <<'PY'
print("x" * 2049)
PY
)
work=$(mktemp -d)
cleanup() {
    status=$?
    if test "$status" -ne 0; then
        find "$work" -maxdepth 1 -type f -print \
            -exec sed -n '1,120p' {} \; >&2
    fi
    rm -rf "$work"
    exit "$status"
}
trap cleanup EXIT

"${generator[@]}" --generation 10 --issued-at "$((now - 60))" \
    --expires-at "$((now + 3600))" --output "$work/profile.conf"
grep -Fqx "pg_qi.model_profile = '$(${generator[@]} --generation 10 \
    --issued-at "$((now - 60))" --expires-at "$((now + 3600))")'" \
    "$work/profile.conf"

"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres \
    -v work="$work" -v a="$profile_a" -v b="$profile_b" \
    -v wrong_build="$wrong_build" -v wrong_postgres="$wrong_postgres" \
    -v wrong_site="$wrong_site" \
    -v expired="$expired" -v invalid="$invalid" -v oversize="$oversize" \
    >"$work/session.out" 2>"$work/session.err" <<'SQL'
CREATE ROLE pgqi_diagnostic NOLOGIN;
CREATE ROLE profile_plain;
CREATE ROLE profile_diagnostic;
GRANT pgqi_diagnostic TO profile_diagnostic;
CREATE TABLE profile_fixture(id integer);
INSERT INTO profile_fixture SELECT generate_series(1, 1000);
ANALYZE profile_fixture;
GRANT SELECT ON profile_fixture TO profile_plain;
GRANT SELECT ON profile_fixture TO profile_diagnostic;
SET max_parallel_workers_per_gather = 0;
SET pg_qi.routing_mode = shadow_p0;

\o /dev/null
SELECT count(*) FROM profile_fixture;
\o :work/missing.out
SHOW pg_qi.profile_evidence;
\o /dev/null

ALTER SYSTEM SET pg_qi.model_profile TO :'a';
SELECT pg_reload_conf();
\o /dev/null
SELECT pg_sleep(0.05);
SELECT count(*) FROM profile_fixture;
\o :work/a.out
SHOW pg_qi.profile_evidence;
\o :work/a_benefit.out
SHOW pg_qi.benefit_evidence;
\o :work/a_decision.out
SHOW pg_qi.decision_v2;
\o /dev/null

ALTER SYSTEM SET pg_qi.model_profile TO :'b';
SELECT pg_reload_conf();
\o /dev/null
SELECT pg_sleep(0.05);
SELECT count(*) FROM profile_fixture;
\o :work/b.out
SHOW pg_qi.profile_evidence;
\o /dev/null

ALTER SYSTEM SET pg_qi.model_profile TO :'wrong_build';
SELECT pg_reload_conf();
\o /dev/null
SELECT pg_sleep(0.05);
SELECT count(*) FROM profile_fixture;
\o :work/wrong_build.out
SHOW pg_qi.profile_evidence;
\o /dev/null

ALTER SYSTEM SET pg_qi.model_profile TO :'wrong_postgres';
SELECT pg_reload_conf();
\o /dev/null
SELECT pg_sleep(0.05);
SELECT count(*) FROM profile_fixture;
\o :work/wrong_postgres.out
SHOW pg_qi.profile_evidence;
\o /dev/null

ALTER SYSTEM SET pg_qi.model_profile TO :'wrong_site';
SELECT pg_reload_conf();
\o /dev/null
SELECT pg_sleep(0.05);
SELECT count(*) FROM profile_fixture;
\o :work/wrong_site.out
SHOW pg_qi.profile_evidence;
\o /dev/null

ALTER SYSTEM SET pg_qi.model_profile TO :'expired';
SELECT pg_reload_conf();
\o /dev/null
SELECT pg_sleep(0.05);
SELECT count(*) FROM profile_fixture;
\o :work/expired.out
SHOW pg_qi.profile_evidence;
\o /dev/null

ALTER SYSTEM SET pg_qi.model_profile TO :'invalid';
SELECT pg_reload_conf();
\o /dev/null
SELECT pg_sleep(0.05);
SELECT count(*) FROM profile_fixture;
\o :work/invalid.out
SHOW pg_qi.profile_evidence;
\o /dev/null

ALTER SYSTEM SET pg_qi.model_profile TO :'oversize';
SELECT pg_reload_conf();
\o /dev/null
SELECT pg_sleep(0.05);
SELECT count(*) FROM profile_fixture;
\o :work/oversize.out
SHOW pg_qi.profile_evidence;
\o /dev/null

SET ROLE profile_plain;
\o :work/denied.out
SHOW pg_qi.profile_evidence;
\o :work/benefit_denied.out
SHOW pg_qi.benefit_evidence;
\o /dev/null
RESET ROLE;

\o /dev/null
SELECT count(*) FROM profile_fixture;
SELECT set_config('role', 'profile_diagnostic', false);
\o :work/withheld.out
SHOW pg_qi.profile_evidence;
\o :work/benefit_withheld.out
SHOW pg_qi.benefit_evidence;
\o /dev/null
RESET ROLE;

ALTER SYSTEM RESET pg_qi.model_profile;
SELECT pg_reload_conf();
\o /dev/null
DROP TABLE profile_fixture;
DROP OWNED BY profile_plain;
DROP OWNED BY profile_diagnostic;
DROP ROLE profile_plain;
DROP ROLE profile_diagnostic;
DROP ROLE pgqi_diagnostic;
SQL

python3 - "$work" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
def record(name):
    value = (root / name).read_text().strip()
    return dict(field.split("=", 1) for field in value.split(";"))

missing = record("missing.out")
a = record("a.out")
a_benefit = record("a_benefit.out")
a_decision = record("a_decision.out")
b = record("b.out")
assert missing["reason"] == "missing" and missing["present"] == "false"
assert a["reason"] == "present" and a["present"] == "true"
assert a_benefit["state"] == "unknown"
assert a_benefit["reason"] == "stats_unconfident"
assert int(a_benefit["postgres_ns_low"]) > 0
assert int(a_benefit["duckdb_ns_high"]) > 0
assert (a_benefit["cid"], a_benefit["g"], a_benefit["e"]) == \
       (a_decision["cid"], a_decision["g"], a_decision["e"])
assert b["reason"] == "present" and b["present"] == "true"
assert (a["generation"], b["generation"]) == ("11", "12")
assert a["fingerprint"] != b["fingerprint"]
for value in (a, b):
    assert value["limits_verified"] == "false"
    assert value["envelope"] == "hypothetical"
assert record("wrong_build.out")["reason"] == "wrong_pg_qi_build"
assert record("wrong_postgres.out")["reason"] == "wrong_postgres_build"
assert record("wrong_site.out")["reason"] == "wrong_site_build"
assert record("expired.out")["reason"] == "expired"
assert record("invalid.out")["reason"] == "malformed"
assert record("oversize.out")["reason"] == "oversize"
assert record("denied.out") == {"v": "1", "s": "denied"}
assert record("withheld.out") == {"v": "1", "s": "withheld"}
assert record("benefit_denied.out") == {"v": "1", "s": "denied"}
assert record("benefit_withheld.out") == {"v": "1", "s": "withheld"}
assert int(a["install_sequence"]) < int(b["install_sequence"])
PY

echo "PQ-34 immutable profile verification PASS (PG$major)"
