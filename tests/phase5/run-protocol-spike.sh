#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
: "${PG_CONFIG:?set PG_CONFIG to a disposable PostgreSQL 16 toolchain}"
pg_config=$PG_CONFIG
test -x "$pg_config"
test "$($pg_config --version | awk '{print $2}' | cut -d. -f1)" = 16
pg_bindir=$($pg_config --bindir)
out=${1:?pass a new result directory}
mkdir -p "$(dirname "$out")"
mkdir -m 700 "$out"

lab=$(mktemp -d "${TMPDIR:-/tmp}/pgqi-p5-XXXXXX")
socket="$lab/socket"
data="$lab/data"
source_copy="$lab/source"
port=$((55000 + ($$ % 9000)))
cleanup() {
  "$pg_bindir/pg_ctl" -D "$data" -m immediate stop >/dev/null 2>&1 || true
  rm -rf -- "$lab"
}
trap cleanup EXIT INT TERM
mkdir -m 700 "$socket" "$source_copy"

git -C "$repo" archive HEAD | tar -x -C "$source_copy"
make -C "$source_copy" PG_CONFIG="$pg_config" -j2 >/dev/null
make -C "$repo/tools/phase5" PG_CONFIG="$pg_config" clean all >/dev/null

"$pg_bindir/initdb" -D "$data" --no-locale --encoding=UTF8 --auth=trust >/dev/null
printf '%s\n' \
  "shared_preload_libraries = 'pg_duckdb,$source_copy/pg_qi'" \
  "unix_socket_directories = '$socket'" \
  "listen_addresses = ''" \
  "port = $port" \
  "max_connections = 20" >>"$data/postgresql.conf"
"$pg_bindir/pg_ctl" -D "$data" -l "$out/postgres.log" start >/dev/null
conninfo="host=$socket port=$port dbname=postgres user=$(id -un) connect_timeout=2"
"$pg_bindir/psql" "$conninfo" -X -v ON_ERROR_STOP=1 \
  -c 'CREATE EXTENSION pg_duckdb' \
  -c 'CREATE TABLE p5_literal(id integer PRIMARY KEY, value integer NOT NULL)' \
  -c 'INSERT INTO p5_literal SELECT g, 1 FROM generate_series(1,1000) AS g' \
  -c 'ANALYZE p5_literal' >/dev/null

run_case() {
  local name=$1 protocol=$2 sql=$3 expected=$4 session_mode=$5
  shift 5
  set +e
  python3 "$repo/tools/phase5/supervisor.py" \
    --driver "$repo/tools/phase5/protocol-driver" --run-root "$out" \
    --conninfo "$conninfo" --sql-file "$repo/tests/phase5/sql/$sql" \
    --spool "$out/$name.spool" --snapshot "$out/$name.snapshot.tsv" \
    --protocol "$protocol" --session-mode "$session_mode" \
    --deadline-ms 8000 --cancel-grace-ms 1000 \
    --max-address-space-kib 65536 --max-output-bytes 65536 \
    --max-rows 1300000 --max-bytes 125829120 --max-columns 16 \
    --max-field-bytes 1048576 --max-spool-bytes 134217728 \
    --max-messages 1300010 "$@" >"$out/$name.json"
  local rc=$?
  set -e
  python3 - "$out/$name.json" "$expected" "$rc" <<'PY'
import json, pathlib, sys
p, expected, rc = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
d = json.loads(p.read_text())
if d["terminal"].get("terminal_state") != expected:
    raise SystemExit(f"{p.name}: expected {expected}, got {d['terminal']}")
if d["peak_rss_kib"] >= 32768:
    raise SystemExit(f"{p.name}: measured peak RSS threshold exceeded")
if (expected == "COMPLETE") != (rc == 0):
    raise SystemExit(f"{p.name}: unexpected exit {rc}")
PY
}

run_case extended_literal extended literal.sql COMPLETE lab_allowlist
run_case prepared_literal extended literal.sql COMPLETE lab_allowlist \
  --prepared pgqi_p5_literal
run_case simple_literal simple literal.sql COMPLETE lab_allowlist
run_case bounded_large extended large.sql COMPLETE off
run_case partial_error extended runtime-error.sql FAILED off
run_case row_limit extended literal.sql LIMIT_EXCEEDED off --max-rows 10
run_case byte_limit extended literal.sql LIMIT_EXCEEDED off --max-bytes 100
run_case byte_one_over extended byte-one-over.sql LIMIT_EXCEEDED off --max-bytes 10
run_case byte_two_fields extended byte-two-fields.sql LIMIT_EXCEEDED off --max-bytes 10
run_case byte_exact extended byte-exact.sql COMPLETE off --max-bytes 10
run_case null_empty extended null-empty.sql COMPLETE off --max-bytes 1
run_case column_limit extended literal.sql LIMIT_EXCEEDED off --max-columns 1
run_case empty_column_limit extended empty-columns.sql LIMIT_EXCEEDED off --max-columns 1
run_case message_limit extended literal.sql LIMIT_EXCEEDED off --max-messages 10
run_case max_field extended max-field.sql LIMIT_EXCEEDED off --max-field-bytes 1024
run_case spool_limit extended large.sql LIMIT_EXCEEDED off --max-spool-bytes 4096
run_case query_deadline extended slow.sql FAILED off --deadline-ms 250
run_case multiple_simple simple multiple-simple.sql FAILED off

for mode in custom generic; do
  "$pg_bindir/psql" "$conninfo" -X -At \
    -f "$repo/tests/phase5/sql/parameter-$mode.psql" \
    >"$out/parameter-$mode.txt"
done

sleepers=1
for _ in $(seq 1 40); do
  sleepers=$("$pg_bindir/psql" "$conninfo" -X -At -v ON_ERROR_STOP=1 \
    -c "SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'SELECT pg_sleep(10)%' AND state = 'active'")
  test "$sleepers" = 0 && break
  sleep 0.05
done
test "$sleepers" = 0
printf 'matching_active_sleep\t%s\n' "$sleepers" >"$out/cancel-retirement.tsv"

stall_dir="$lab/stall"
mkdir -m 700 "$stall_dir"
stall_socket="$stall_dir/.s.PGSQL.$port"
python3 "$repo/tests/phase5/fixtures/stalled-postgres-socket.py" "$stall_socket" &
stall_pid=$!
for _ in $(seq 1 40); do test -S "$stall_socket" && break; sleep 0.01; done
test -S "$stall_socket"
set +e
python3 "$repo/tools/phase5/supervisor.py" \
  --driver "$repo/tools/phase5/protocol-driver" --run-root "$out" \
  --conninfo "host=$stall_dir port=$port dbname=postgres user=$(id -un)" \
  --sql-file "$repo/tests/phase5/sql/literal.sql" \
  --spool "$out/connect-failure.spool" --snapshot "$out/connect-failure.snapshot.tsv" \
  --protocol extended --session-mode off --deadline-ms 250 --cancel-grace-ms 250 \
  --max-address-space-kib 65536 --max-output-bytes 65536 \
  --max-rows 10 --max-bytes 1024 --max-columns 2 --max-field-bytes 128 \
  --max-spool-bytes 2048 --max-messages 20 >"$out/connect-failure.json"
connect_rc=$?
kill "$stall_pid" >/dev/null 2>&1 || true
wait "$stall_pid" 2>/dev/null || true

python3 "$repo/tools/phase5/supervisor.py" \
  --driver "$repo/tests/phase5/fixtures/hang-driver.sh" --run-root "$out" \
  --conninfo unused --sql-file "$repo/tests/phase5/sql/literal.sql" \
  --spool "$out/supervisor-hang.spool" --snapshot "$out/supervisor-hang.snapshot.tsv" \
  --protocol extended --session-mode off --deadline-ms 100 --cancel-grace-ms 100 \
  --max-address-space-kib 65536 --max-output-bytes 65536 \
  --max-rows 10 --max-bytes 1024 --max-columns 2 --max-field-bytes 128 \
  --max-spool-bytes 2048 --max-messages 20 >"$out/supervisor-hang.json"
hang_rc=$?
set -e
test "$connect_rc" -ne 0
test "$hang_rc" -ne 0

"$pg_bindir/psql" "$conninfo" -X -At -F $'\t' -v ON_ERROR_STOP=1 \
  -c "SELECT current_setting('server_version'), current_setting('server_version_num'), extname, extversion FROM pg_extension WHERE extname='pg_duckdb'" \
  >"$out/lab-identity.tsv"
sha256sum "$source_copy/pg_qi.so" "$repo/tools/phase5/protocol-driver" \
  >"$out/tested-binaries.sha256"
printf 'source_commit\t%s\nsource_dirty\t%s\npg_config\t%s\n' \
  "$(git -C "$repo" rev-parse HEAD)" \
  "$(test -n "$(git -C "$repo" status --porcelain)" && echo true || echo false)" \
  "$($pg_config --version)" >"$out/build-identity.tsv"

"$pg_bindir/pg_ctl" -D "$data" -m fast -w stop >/dev/null

mutation="$lab/mutated-evidence"
mkdir -m 700 "$mutation"
find "$out" -maxdepth 1 -type f ! -name 'summary.json' ! -name 'SHA256SUMS' \
  -exec cp -l {} "$mutation/" \;
cp "$out/extended_literal.snapshot.tsv" "$mutation/extended_literal.snapshot.tsv.replacement"
sed -i 's/pg_qi.shadow_request_applied\ton/pg_qi.shadow_request_applied\toff/' \
  "$mutation/extended_literal.snapshot.tsv.replacement"
mv -f "$mutation/extended_literal.snapshot.tsv.replacement" \
  "$mutation/extended_literal.snapshot.tsv"
printf 'mutated_snapshot_refused\tPASS\nno_false_summary_or_index\tPASS\n' \
  >"$mutation/mutation-tests.tsv"
set +e
python3 "$repo/tests/phase5/verify_spike.py" "$mutation" >/dev/null 2>&1
mutation_rc=$?
set -e
test "$mutation_rc" -ne 0
test ! -e "$mutation/summary.json"
test ! -e "$mutation/SHA256SUMS"
printf 'mutated_snapshot_refused\tPASS\nno_false_summary_or_index\tPASS\n' \
  >"$out/mutation-tests.tsv"

python3 "$repo/tests/phase5/verify_spike.py" "$out" >"$out/verification.json"
echo "Protocol spike PASS: $out"
