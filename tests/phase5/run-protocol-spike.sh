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
  "log_statement = 'all'" \
  "log_line_prefix = '%a|'" \
  "max_connections = 20" >>"$data/postgresql.conf"
"$pg_bindir/pg_ctl" -D "$data" -l "$out/postgres.log" start >/dev/null
conninfo="host=$socket port=$port dbname=postgres user=$(id -un) connect_timeout=2"
"$pg_bindir/psql" "$conninfo" -X -v ON_ERROR_STOP=1 \
  -c 'CREATE EXTENSION pg_duckdb' \
  -c 'CREATE TABLE p5_literal(id integer PRIMARY KEY, value integer NOT NULL)' \
  -c 'INSERT INTO p5_literal SELECT g, 1 FROM generate_series(1,1000) AS g' \
  -c 'CREATE TABLE p5_revalidation(id integer PRIMARY KEY, value integer NOT NULL)' \
  -c 'INSERT INTO p5_revalidation VALUES (1, 1)' \
  -c 'ANALYZE p5_literal' >/dev/null

audited_cases="$lab/audited-cases"
audited_out="$out/audited-session"
mkdir -m 700 "$audited_cases" "$audited_out"
cp "$repo/tests/phase5/sql/literal.sql" "$audited_cases/case_a.sql"
cp "$repo/tests/phase5/sql/null-empty.sql" "$audited_cases/case_b.sql"
printf 'SELECT FROM;\n' >"$audited_cases/planning_error.sql"
printf 'SELECT 1 / 0;\n' >"$audited_cases/planning_error_non42.sql"
printf 'SELECT 1 / value FROM p5_literal WHERE id = 1;\n' >"$audited_cases/execution_error.sql"
printf "SELECT ('pgqi_absent_' || id::text)::regclass FROM p5_literal WHERE id = 1;\n" >"$audited_cases/execution_error_42.sql"
printf 'SELECT value FROM p5_revalidation WHERE id = 1;\n' >"$audited_cases/revalidation_error.sql"
printf "SELECT query_to_xml('SELECT 1', false, false, '');\n" >"$audited_cases/nested_planning.sql"
cp "$repo/tests/phase5/sql/runtime-error.sql" "$audited_cases/partial_error.sql"
chmod 600 "$audited_cases/case_a.sql" "$audited_cases/case_b.sql"
chmod 600 "$audited_cases/planning_error.sql" "$audited_cases/execution_error.sql" "$audited_cases/partial_error.sql"
chmod 600 "$audited_cases/planning_error_non42.sql" "$audited_cases/execution_error_42.sql"
chmod 600 "$audited_cases/revalidation_error.sql"
chmod 600 "$audited_cases/nested_planning.sql"
PYTHONPATH="$repo" python3 - "$repo" "$conninfo application_name=pgqi_p5_audited" "$audited_cases" "$audited_out" "$pg_bindir/psql" "$conninfo" <<'PY'
import hashlib, json, pathlib, subprocess, sys
from tools.phase5.pilot_runtime import AuditedSession, SequenceJournal
repo, conninfo, cases, output, psql, fixture_conninfo = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4]), sys.argv[5], sys.argv[6]
digests = {name: hashlib.sha256((cases / f"{name}.sql").read_bytes()).hexdigest()
           for name in ("case_a", "case_b", "planning_error", "planning_error_non42",
                        "execution_error", "execution_error_42", "partial_error")}
digests["revalidation_error"] = hashlib.sha256(
    (cases / "revalidation_error.sql").read_bytes()).hexdigest()
digests["nested_planning"] = hashlib.sha256(
    (cases / "nested_planning.sql").read_bytes()).hexdigest()
journal = SequenceJournal(output)
args = ["--conninfo", conninfo, "--case-root", str(cases), "--output-root", str(output),
        "--session-mode", "lab_allowlist", "--deadline-ms", "8000", "--max-requests", "24",
        "--max-rows", "2000", "--max-bytes", "1048576", "--max-columns", "4",
        "--max-field-bytes", "1024", "--max-spool-bytes", "2097152", "--max-messages", "2010"]
session = AuditedSession(repo / "tools/phase5/audited-session-driver", journal, args,
                         digests, hashlib.sha256((repo / "tools/phase5/audited-session-driver").read_bytes()).hexdigest())
first = session.dispatch("case_a")
second = session.dispatch("case_a")
prepared_a = session.prepare("a", "case_a")
planned_a = session.execute_prepared("a")
session.prepare("b", "case_b")
planned_b = session.execute_prepared("b")
cached_a = session.execute_prepared("a")
if cached_a["reason"] != "cached":
    raise SystemExit(f"expected cached A, got {cached_a}")
subprocess.run([psql, fixture_conninfo, "-X", "-v", "ON_ERROR_STOP=1", "-c",
                "ALTER TABLE p5_literal ADD COLUMN invalidation_probe integer"], check=True,
               stdout=subprocess.DEVNULL)
replanned_a = session.execute_prepared("a")
if replanned_a["reason"] != "prepared_plan_activity_unattributed":
    raise SystemExit(f"expected invalidation replan, got {replanned_a}")
cached_replanned_a = session.execute_prepared("a")
if cached_replanned_a["reason"] != "cached":
    raise SystemExit(f"expected cached replanned A, got {cached_replanned_a}")
session.deallocate("a")
session.prepare("a", "case_a")
reprepared_a = session.execute_prepared("a")
session.prepare("all", "case_b")
session.deallocate("all")
after_all = session.execute_prepared("a")
session.prepare("A", "case_b")
session.execute_prepared("A")
session.deallocate("A")
after_upper = session.execute_prepared("a")
session.prepare("nested", "nested_planning")
session.execute_prepared("nested")
nested_cached_outer = session.execute_prepared("nested")
if nested_cached_outer["reason"] != "prepared_plan_activity_unattributed":
    raise SystemExit(f"nested planning was falsely attributed: {nested_cached_outer}")
session.close(); journal.close()
if any(item["state"] != "COMPLETE" for item in
       (first, second, cached_a, replanned_a, cached_replanned_a, reprepared_a,
        after_all, after_upper)):
    raise SystemExit("audited session did not complete")
rows = [json.loads(line) for line in (output / "request.journal.jsonl").read_text().splitlines()]
if [row["sequence"] for row in rows] != list(range(1, len(rows) + 1)):
    raise SystemExit("journal sequence is not contiguous")
cached_intents = [row for row in rows if row["payload"].get("event") == "execute_prepared_intent" and
                  row["payload"].get("prepared") == "a"]
cached_intent = next((row for row in cached_intents if row["sequence"] == cached_a["sequence"]), None)
if (cached_intent is None or
        cached_intent["payload"].get("planning_sequence") != planned_a["sequence"] or
        cached_intent["payload"].get("planning_sequence") == planned_b["sequence"]):
    raise SystemExit("cached A is not bound to A planning evidence")

def set_error_value(value):
    subprocess.run([psql, fixture_conninfo, "-X", "-v", "ON_ERROR_STOP=1", "-c",
                    f"UPDATE p5_literal SET value = {value} WHERE id = 1"], check=True,
                   stdout=subprocess.DEVNULL)

def failure_session(name, prepared=False, transaction=False, prime_cached=False):
    target = output / f"failure-{name}-{'prepared' if prepared else 'transaction' if transaction else 'direct'}"
    target.mkdir(mode=0o700)
    failure_journal = SequenceJournal(target)
    failure_args = list(args)
    failure_args[failure_args.index(str(output))] = str(target)
    failed = AuditedSession(repo / "tools/phase5/audited-session-driver",
        failure_journal, failure_args, digests,
        hashlib.sha256((repo / "tools/phase5/audited-session-driver").read_bytes()).hexdigest())
    if transaction:
        begin = failed.begin()
        if begin["state"] != "COMPLETE": raise SystemExit(begin)
    if prepared:
        prepare_result = failed.prepare("failing", name)
        if prepare_result["state"] != "COMPLETE": raise SystemExit(prepare_result)
        if prime_cached:
            prime = failed.execute_prepared("failing")
            if prime["state"] != "COMPLETE": raise SystemExit(prime)
            set_error_value(0)
        result = failed.execute_prepared("failing")
    else:
        result = failed.dispatch(name)
    failed.close()
    failure_journal.close()
    return result

planning_error = failure_session("planning_error")
if (planning_error["reason"], planning_error["planning_state"], planning_error["execution_state"], planning_error["snapshot_state"]) != \
        ("planning_error", "FAILED", "NOT_RUN", "COMPLETE"):
    raise SystemExit(f"planning error states are untruthful: {planning_error}")
planning_error_non42 = failure_session("planning_error_non42")
if (planning_error_non42["reason"], planning_error_non42["planning_state"],
        planning_error_non42["execution_state"]) != ("planning_error", "FAILED", "NOT_RUN"):
    raise SystemExit(f"non-42 planning error states are untruthful: {planning_error_non42}")
prepared_planning_error = failure_session("planning_error_non42", prepared=True)
if (prepared_planning_error["reason"], prepared_planning_error["planning_state"],
        prepared_planning_error["execution_state"]) != ("planning_error", "FAILED", "NOT_RUN"):
    raise SystemExit(f"prepared planning error states are untruthful: {prepared_planning_error}")
execution_error_42 = failure_session("execution_error_42")
if (execution_error_42["reason"], execution_error_42["planning_state"],
        execution_error_42["execution_state"]) != ("fresh_execution_error", "COMPLETE", "FAILED"):
    raise SystemExit(f"class-42 execution error states are untruthful: {execution_error_42}")

target = output / "failure-revalidation-prepared"
target.mkdir(mode=0o700)
revalidation_journal = SequenceJournal(target)
revalidation_args = list(args)
revalidation_args[revalidation_args.index(str(output))] = str(target)
revalidation = AuditedSession(repo / "tools/phase5/audited-session-driver",
    revalidation_journal, revalidation_args, digests,
    hashlib.sha256((repo / "tools/phase5/audited-session-driver").read_bytes()).hexdigest())
if revalidation.prepare("revalidation", "revalidation_error")["state"] != "COMPLETE":
    raise SystemExit("revalidation prepare failed")
if revalidation.execute_prepared("revalidation")["state"] != "COMPLETE":
    raise SystemExit("revalidation prime failed")
subprocess.run([psql, fixture_conninfo, "-X", "-v", "ON_ERROR_STOP=1", "-c",
                "ALTER TABLE p5_revalidation ALTER COLUMN value TYPE bigint"], check=True,
               stdout=subprocess.DEVNULL)
revalidation_error = revalidation.execute_prepared("revalidation")
revalidation.close(); revalidation_journal.close()
if (revalidation_error["reason"], revalidation_error["planning_state"],
        revalidation_error["execution_state"], revalidation_error["snapshot_state"]) != \
        ("prepared_error_unattributed", "UNAVAILABLE", "UNAVAILABLE", "COMPLETE"):
    raise SystemExit(f"failed revalidation states are untruthful: {revalidation_error}")
cached_error = failure_session("execution_error", prepared=True, prime_cached=True)
set_error_value(1)
if (cached_error["reason"], cached_error["planning_state"], cached_error["execution_state"], cached_error["snapshot_state"]) != \
        ("prepared_error_unattributed", "UNAVAILABLE", "UNAVAILABLE", "COMPLETE"):
    raise SystemExit(f"cached execution error states are untruthful: {cached_error}")
partial_error = failure_session("partial_error")
if (partial_error["state"], partial_error["reason"], partial_error["execution_state"], partial_error["snapshot_state"]) != \
        ("FAILED", "partial_stream_error", "FAILED", "COMPLETE"):
    raise SystemExit(f"partial stream error states are untruthful: {partial_error}")
set_error_value(0)
aborted = failure_session("execution_error", transaction=True)
set_error_value(1)
if (aborted["reason"], aborted["planning_state"], aborted["execution_state"],
        aborted["snapshot_state"]) != ("aborted_transaction", "UNAVAILABLE",
                                        "UNAVAILABLE", "INCOMPLETE"):
    raise SystemExit(f"aborted transaction states are untruthful: {aborted}")
PY
sleep 0.1
grep 'pgqi_p5_audited|' "$out/postgres.log" >"$audited_out/sql-dispatch.log"
if grep -Eiq 'ALTER[[:space:]]+SYSTEM|CREATE[[:space:]]+ROLE|DROP[[:space:]]+ROLE|pg_terminate_backend|COPY.*PROGRAM|CREATE[[:space:]]+EXTENSION|DROP[[:space:]]+EXTENSION' "$audited_out/sql-dispatch.log"; then
  echo 'forbidden SQL in audited-session dispatch' >&2
  exit 1
fi

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
