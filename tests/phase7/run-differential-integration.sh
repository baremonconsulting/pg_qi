#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
output=${2:?private output directory is required}
output=$(realpath -m "$output")
pgconfig=${PG_CONFIG:-/usr/lib/postgresql/$major/bin/pg_config}
client_pg_config=${PG_CLIENT_CONFIG:-$pgconfig}
pgbin=$(dirname "$pgconfig")
port=$((55500 + major))
data=$output/data
socket=$output/socket
log=$output/postgresql.log
run=$output/run
libs=$output/libs

test ! -e "$output"
mkdir -m 0700 "$output"
mkdir -m 0700 "$socket" "$run" "$libs"

make PG_CONFIG="$pgconfig" PG_QI_TESTING=0 clean >/dev/null
make PG_CONFIG="$pgconfig" PG_QI_TESTING=0 -j2 >/dev/null
make -C tests/phase7/witness PG_CONFIG="$pgconfig" clean >/dev/null
make -C tests/phase7/witness PG_CONFIG="$pgconfig" -j2 >/dev/null
make -C tools/phase7 PG_CONFIG="$pgconfig" clean all >/dev/null

cp pg_qi.so "$libs/"
cp tests/phase7/witness/pg_qi_execution_witness.so "$libs/"
chmod 0755 "$libs"/*.so

if strings pg_qi.so | grep -E 'pg_qi_witness|pg_qi_execution_witness' >/dev/null; then
    echo "test-only witness surface leaked into production pg_qi.so" >&2
    exit 1
fi

"$pgbin/initdb" -D "$data" --no-locale --encoding=UTF8 --no-sync >"$output/initdb.log"
"$pgbin/pg_ctl" -D "$data" -l "$log" -o \
    "-p $port -k $socket -c listen_addresses= -c shared_preload_libraries=$($pgconfig --pkglibdir)/pg_duckdb,$libs/pg_qi_execution_witness,$libs/pg_qi -c duckdb.postgres_role=bound_duckdb_access -c max_connections=20" \
    -w start
cleanup() {
    "$pgbin/pg_ctl" -D "$data" -m fast -w stop >/dev/null 2>&1 || true
}
trap cleanup EXIT

"$pgbin/psql" -X -v ON_ERROR_STOP=1 -h "$socket" -p "$port" -d postgres <<'SQL'
CREATE ROLE bound_duckdb_access;
CREATE ROLE bound_reader;
CREATE ROLE pgqi_diagnostic;
GRANT bound_duckdb_access, pgqi_diagnostic TO bound_reader;
CREATE EXTENSION pg_duckdb;
CREATE DOMAIN bound_label AS text CHECK (length(VALUE) < 32);
CREATE TABLE witness_fixture(id integer PRIMARY KEY, label text NOT NULL, payload bytea);
INSERT INTO witness_fixture (id, label)
SELECT g, 'v-' || g FROM generate_series(1, 1000) AS g;
UPDATE witness_fixture SET label = 'v-1' || chr(92) || 'path', payload = decode('6162', 'hex') WHERE id = 1;
UPDATE witness_fixture SET label = 'v-1' || repeat(chr(92), 2) || 'path', payload = decode('613632', 'hex') WHERE id = 2;
GRANT SELECT ON witness_fixture TO bound_reader;
CREATE TABLE distinct_report_fixture(
    id integer PRIMARY KEY, bucket integer NOT NULL, item_key integer, account_key integer
);
INSERT INTO distinct_report_fixture
SELECT g, g % 7, NULLIF(g % 113, 0), NULLIF(g % 43, 0)
FROM generate_series(1, 5000) AS g;
ANALYZE distinct_report_fixture;
CREATE FUNCTION witness_nested_spi() RETURNS integer LANGUAGE plpgsql AS $$
BEGIN
  PERFORM count(*) FROM witness_fixture;
  RETURN 1;
END
$$;
SQL

cc -std=c99 -Wall -Wextra -Werror -I"$("$client_pg_config" --includedir)" \
    tests/phase7/bound_driver.c -L"$("$client_pg_config" --libdir)" \
    -Wl,-rpath,"$("$client_pg_config" --libdir)" -lpq -o "$libs/bound-driver"
"$libs/bound-driver" "host=$socket port=$port dbname=postgres user=$USER"

cat >"$output/subject.sql" <<'SQL'
SELECT id, label, (id > 0) AS positive
FROM witness_fixture
ORDER BY id
LIMIT 5
SQL
cat >"$output/concurrent.sql" <<'SQL'
UPDATE witness_fixture SET label = label || '-late' WHERE id = 1;
DELETE FROM witness_fixture WHERE id = 2;
INSERT INTO witness_fixture VALUES (1001, 'inserted-late');
SQL
sql_sha=$(sha256sum "$output/subject.sql" | cut -d' ' -f1)
python3 - "$output/manifest.json" "$sql_sha" <<'PY'
import json, sys
path, digest = sys.argv[1:]
value = {"schema_version": 1, "document_type": "pq31_differential_run",
         "run_id": "pq31-ci", "subject_id": "fixture-five",
         "sql_sha256": digest, "ordered": True,
         "authorization_reference": "PQ-31-CI",
         "arms": ["native", "forced", "shadow"]}
with open(path, "x", encoding="utf-8") as stream:
    json.dump(value, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
PY
chmod 0600 "$output/subject.sql" "$output/concurrent.sql" "$output/manifest.json"

python3 tools/phase7/run_differential.py \
    --driver tools/phase7/differential-driver \
    --conninfo "host=$socket port=$port dbname=postgres user=$USER" \
    --manifest "$output/manifest.json" \
    --sql-file "$output/subject.sql" \
    --concurrent-sql-file "$output/concurrent.sql" \
    --run-root "$run" --deadline-ms 30000

python3 - "$run/result.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    result = json.load(stream)
assert result["result"] == "PASS", result
assert result["missing_import_control"]["result"] == "PASS", result
PY

python3 tests/phase7/run_live_mutations.py "$output/manifest.json" "$run"

run_case() {
    local name=$1 ordered=$2 expected=$3
    local case_root=$output/case-$name
    local digest rc
    local bindings_args=()
    local bound_args=()
    local subject_pgoptions=${PGOPTIONS:-}
    mkdir -m 0700 "$case_root"
    cat >"$case_root/subject.sql"
    digest=$(sha256sum "$case_root/subject.sql" | cut -d' ' -f1)
    if [[ $# -ge 4 ]]; then
        (umask 077; printf '%s\n' "$4" >"$case_root/bindings.json")
        bindings_args=(--bindings-file "$case_root/bindings.json")
    fi
    if [[ $# -eq 5 ]]; then
        bound_args=(--bound-pairs "$5")
        # Match the prepared Bind fixture's serial PostgreSQL table reader.
        subject_pgoptions+=" -c duckdb.max_workers_per_postgres_scan=0"
    fi
    python3 - "$case_root/manifest.json" "$name" "$digest" "$ordered" <<'PY'
import hashlib
import json, sys
from pathlib import Path
path, name, digest, ordered = sys.argv[1:]
value = {"schema_version": 1, "document_type": "pq31_differential_run",
         "run_id": "pq31-ci-" + name, "subject_id": name,
         "sql_sha256": digest, "ordered": ordered == "true",
         "authorization_reference": "PQ-31-CI",
         "arms": ["native", "forced", "shadow"]}
bindings = Path(path).with_name("bindings.json")
if bindings.is_file():
    value["schema_version"] = 2
    value["bindings_sha256"] = hashlib.sha256(bindings.read_bytes()).hexdigest()
with open(path, "x", encoding="utf-8") as stream:
    json.dump(value, stream, sort_keys=True, separators=(",", ":")); stream.write("\n")
PY
    chmod 0600 "$case_root/subject.sql" "$case_root/manifest.json"
    set +e
    PGOPTIONS="$subject_pgoptions" python3 tools/phase7/run_differential.py \
        --driver tools/phase7/differential-driver \
        --conninfo "host=$socket port=$port dbname=postgres user=$USER" \
        --manifest "$case_root/manifest.json" --sql-file "$case_root/subject.sql" \
        --run-root "$case_root" --deadline-ms 30000 "${bindings_args[@]}" "${bound_args[@]}"
    rc=$?
    set -e
    python3 - "$case_root/result.json" "$expected" "$rc" <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
expected = sys.argv[2]
if expected == "PASS_OR_FORCE_DECLINED":
    assert (result["result"] == "PASS" or
            (result["result"], result["reason"]) == ("INCONCLUSIVE", "force_declined")), result
elif expected == "SAVING_BELOW_FLOOR":
    assert (result["result"], result["reason"]) == ("FAIL", "saving_below_floor"), result
    assert (result["eligible"], result["safe"], result["beneficial"]) == ("pass", "pass", "veto"), result
    assert result["min_saving_ms"] == 5000 and result["min_saving_percent"] == 10, result
    assert [sample["order"] for sample in result["samples"]] == [
        ["native", "bound"], ["bound", "native"], ["native", "bound"]
    ], result
    assert all(sample["native_ns"] > 0 and sample["bound_ns"] > 0
               for sample in result["samples"]), result
    assert "bound_key" not in result, result
elif expected == "INELIGIBLE_BINDING":
    assert (result["result"], result["reason"]) == ("FAIL", "ineligible_binding"), result
    assert (result["eligible"], result["safe"], result["beneficial"]) == ("veto", "unknown", "unknown"), result
    assert result["samples"] == [] and "bound_key" not in result, result
elif expected == "PROVIDER_DECLINED":
    assert (result["result"], result["reason"]) == ("FAIL", "provider_declined"), result
    assert (result["eligible"], result["safe"], result["beneficial"]) == ("veto", "unknown", "unknown"), result
    assert result["samples"] == [] and "bound_key" not in result, result
else:
    assert result["result"] == expected, result
assert int(sys.argv[3]) == (0 if result["result"] in {"PASS", "ERROR_PARITY"} else 1), result
PY
}

run_case empty true PASS <<'SQL'
SELECT id FROM witness_fixture WHERE false;
SQL
run_case single true PASS <<'SQL'
SELECT id, label FROM witness_fixture ORDER BY id LIMIT 1;
SQL
run_case null-duplicate false PASS <<'SQL'
SELECT NULL::text AS nullable FROM witness_fixture LIMIT 2;
SQL
run_case supported-types true PASS <<'SQL'
SELECT id::int2 AS i2, id::int4 AS i4, id::int8 AS i8, label::text AS txt,
       (id > 0)::bool AS flag
FROM witness_fixture ORDER BY id LIMIT 1;
SQL
run_case bound-cte-pipeline true PASS '[{"oid":23,"value":"17"}]' <<'SQL'
WITH "source" AS (
    SELECT "id", "id" % 7 AS "bucket"
    FROM "witness_fixture"
    WHERE "id" >= $1
), "ranked" AS (
    SELECT "bucket", row_number() OVER (PARTITION BY "bucket" ORDER BY "id") AS "position"
    FROM "source"
), "summary" AS (
    SELECT "bucket", count(*) AS "members", max("position") AS "last_position"
    FROM "ranked"
    GROUP BY "bucket"
)
SELECT "bucket", "members", "last_position" FROM "summary" ORDER BY "bucket";
SQL
sed 's/WHERE "id" >= \$1/& AND ($2 IS NULL OR "id" <= $2)/' \
    "$output/case-bound-cte-pipeline/subject.sql" |
    run_case bound-cte-optional-filter true PASS_OR_FORCE_DECLINED '[{"oid":23,"value":"17"},{"oid":23,"value":null}]'
python3 tests/phase7/paired_distinct_report.py |
    run_case paired-distinct-range true SAVING_BELOW_FLOOR '[{"oid":23,"value":"17"},{"oid":23,"value":"2011"}]' 3
python3 tests/phase7/paired_distinct_report.py |
    run_case paired-distinct-null true SAVING_BELOW_FLOOR '[{"oid":23,"value":"17"},{"oid":23,"value":null}]' 3
run_case bound-volatile-filter true INELIGIBLE_BINDING '[{"oid":25,"value":"v-17"}]' 3 <<'SQL'
SELECT count(*) FROM witness_fixture WHERE label = $1 AND random() >= 0;
SQL
run_case bound-catalogue-read true PROVIDER_DECLINED '[{"oid":26,"value":"0"}]' 3 <<'SQL'
SELECT count(*) FROM pg_catalog.pg_class WHERE oid > $1;
SQL
run_case character-typmod true FAIL <<'SQL'
SELECT label::varchar(20) AS vc, label::char(20) AS bp
FROM witness_fixture ORDER BY id LIMIT 1;
SQL
run_case execution-error true INCONCLUSIVE <<'SQL'
SELECT label::integer FROM witness_fixture ORDER BY id LIMIT 1;
SQL

"$pgbin/psql" -X -qAt -h "$socket" -p "$port" -d postgres \
    >"$output/witness-lifecycle.out" 2>"$output/witness-lifecycle.err" <<'SQL'
SET pg_qi_witness.subject_token='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
SET pg_qi_witness.armed=on;
SELECT label::integer FROM witness_fixture ORDER BY id LIMIT 1;
SHOW pg_qi_witness.last_record;
SET pg_qi_witness.armed=off;
SET pg_qi_witness.subject_token='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
SET pg_qi_witness.armed=on;
SELECT 1;
SHOW pg_qi_witness.last_record;
SET pg_qi_witness.armed=off;
SET pg_qi_witness.subject_token='cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc';
SET pg_qi_witness.armed=on;
SELECT witness_nested_spi();
SHOW pg_qi_witness.last_record;
SQL
python3 - "$output/witness-lifecycle.out" <<'PY'
import sys
records = [line for line in open(sys.argv[1], encoding="utf-8") if line.startswith("v=1;")]
assert len(records) == 3, records
values = [dict(field.split("=", 1) for field in record.strip().split(";")) for record in records]
assert [item["status"] for item in values] == ["error", "success", "success"], values
assert [item["token"] for item in values] == ["a" * 64, "b" * 64, "c" * 64], values
assert [int(item["seq"]) for item in values] == list(range(int(values[0]["seq"]),
                                                           int(values[0]["seq"]) + 3)), values
assert values[2]["engine"] == "postgres", values
PY

python3 -m unittest \
    tests.phase7.test_decision_snapshot \
    tests.phase7.test_witness_record \
    tests.phase7.test_differential_oracle

cleanup
trap - EXIT
