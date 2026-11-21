#!/usr/bin/env bash

set -euo pipefail
umask 077
major=${1:?PostgreSQL major required}
output=$(realpath -m "${2:?new private output directory required}")
pgconfig=${PG_CONFIG:-/usr/lib/postgresql/$major/bin/pg_config}
pgbin=$("$pgconfig" --bindir)
libdir=$("$pgconfig" --pkglibdir)
revision=${PG_QI_BUILD_ID:?supply the exact pg_qi source identity}
postgres_build=${PG_QI_POSTGRES_BUILD_ID:?supply the exact PostgreSQL build identity}
[[ $revision =~ ^[0-9a-f]{40}$ && $postgres_build =~ ^[0-9a-f]{64}$ ]]
if [[ -z ${PG_QI_SOURCE_ARCHIVE+x} ]]; then
    test "$(git rev-parse --show-toplevel)" = "$(pwd -P)"
    test "$(git rev-parse HEAD)" = "$revision"
    test -z "$(git status --porcelain --untracked-files=all -- Makefile pg_qi.control src sql \
        tools/phase5 tools/phase7 tests/phase7 .github/scripts ':(exclude)tools/phase7/differential-driver')"
    make -C tools/phase7 PG_CONFIG="$pgconfig" clean >/dev/null
else
    capsule=${PG_QI_SOURCE_ARCHIVE:?empty source capsule path}
    test "$(sha1sum "$capsule" | cut -d' ' -f1)" = "$revision"
    python3 -I - "$capsule" <<'PY'
import hashlib
import sys
import tarfile
from pathlib import Path


def require(condition, path):
    if not condition:
        raise SystemExit(f"source capsule mismatch: {path}")


def digest(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(65536), b''):
        result.update(block)
    return result.digest()

paths = set()
with tarfile.open(sys.argv[1], 'r:') as archive:
    for member in archive:
        relative = Path(member.name)
        require(member.isfile() and not relative.is_absolute() and '..' not in relative.parts and
                relative not in paths, member.name)
        require(relative.is_file() and not any(p.is_symlink() for p in (relative, *relative.parents)), member.name)
        with archive.extractfile(member) as expected, relative.open('rb') as actual:
            require(digest(actual) == digest(expected), member.name)
        paths.add(relative)
for base in map(Path, ('src', 'sql', 'tools/phase5', 'tools/phase7', 'tests/phase7', '.github/scripts')):
    for item in base.rglob('*'):
        if item.is_symlink() or not item.is_dir():
            require(item in paths, item)
for item in map(Path, ('Makefile', 'pg_qi.control')):
    require(item in paths, item)
PY
fi
case "$major" in
    16) version=16.16 ;;
    17) version=17.12 ;;
    18) version=18.7 ;;
    19) version=19.0 ;;
    *) exit 2 ;;
esac
test "$("$pgbin/postgres" --version)" = "postgres (PostgreSQL) $version"
test ! -e "$output"
mkdir -m 0700 "$output"
out=$(mktemp -d "$output/count-matrix-pg${major}.XXXXXXXX")
mkdir -m 0700 "$out/libs"
printf 'evidence=%s\n' "$out"

make PG_CONFIG="$pgconfig" PG_QI_TESTING=0 clean >"$out/production-clean.log" 2>&1
make PG_CONFIG="$pgconfig" PG_QI_TESTING=0 PG_QI_BUILD_ID="$revision" \
    PG_QI_POSTGRES_BUILD_ID="$postgres_build" -j2 >"$out/production-build.log" 2>&1
make -C tests/phase7/witness PG_CONFIG="$pgconfig" -j2 >"$out/witness-build.log" 2>&1
make -C tools/phase7 PG_CONFIG="$pgconfig" >"$out/driver-build.log" 2>&1
cp pg_qi.so tests/phase7/witness/pg_qi_execution_witness.so "$out/libs/"
${CC:-cc} -std=c11 -Wall -Wextra -Werror -pedantic -g -fno-omit-frame-pointer \
    -fsanitize=address,undefined -I src/include src/qi_count_cert_parser.c \
    tests/phase7/count_cert_parser_contract.c -o "$out/parser-contract"
ASAN_OPTIONS=abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1 "$out/parser-contract" >"$out/parser-contract.log"
python3 -m unittest tests.phase7.test_count_certificate >"$out/issuer-contract.log" 2>&1

"$pgbin/initdb" -D "$out/data" --no-locale --encoding=UTF8 --no-sync >"$out/initdb.log" 2>&1
socket=$(mktemp -d /tmp/pgqi-cert-sock.XXXXXXXX)
cleanup() {
    "$pgbin/pg_ctl" -D "$out/data" -m fast -w stop >/dev/null 2>&1 || true
    rmdir -- "$socket" 2>/dev/null || true
}
trap cleanup EXIT
site=$(openssl rand -hex 32)
port=$((55600 + major))
"$pgbin/pg_ctl" -D "$out/data" -l "$out/postgresql.log" -o \
    "-p $port -k $socket -c listen_addresses= -c shared_preload_libraries=pg_duckdb,$out/libs/pg_qi_execution_witness,$out/libs/pg_qi -c max_parallel_workers=0 -c duckdb.max_workers_per_postgres_scan=0 -c autovacuum=off -c pg_qi.site_build_id=$site -c pg_qi.count_certificate_attestation=on" -w start
pid=$(sed -n '1p' "$out/data/postmaster.pid")
conninfo="host=$socket port=$port dbname=postgres user=$(id -un)"
psql=("$pgbin/psql" -XqAt -v ON_ERROR_STOP=1 -d "$conninfo")
acquired_at=$(date +%s)

identity() {
    printf 'postgres_major=%s\npostgres_version=%s\npg_qi_revision=%s\npostgres_build_id=%s\n' \
        "$major" "$version" "$revision" "$postgres_build"
    "${psql[@]}" -c 'SELECT system_identifier FROM pg_catalog.pg_control_system()' \
        -c 'SHOW pg_qi.site_build_id' -c 'SHOW max_parallel_workers' \
        -c 'SHOW duckdb.max_workers_per_postgres_scan'
    printf 'acquired_at_unix=%s\n' "$acquired_at"
    "${psql[@]}" -c "SELECT 'postmaster_start_us=' || (EXTRACT(EPOCH FROM pg_catalog.pg_postmaster_start_time()) * 1000000)::bigint::text"
    for key in 'postgres' 'pg_qi[.]so' 'pg_duckdb[.]so' 'libduckdb[.]so'; do
        printf 'runtime_%s\t' "$key"
        python3 .github/scripts/ci-mapped-file.py "$pid" "/$key"
    done
}
identity >"$out/identity-before.txt"
test "$(sed -n '6p' "$out/identity-before.txt")" = "$site"
for component in "$pgbin/postgres" "$out/libs/pg_qi.so" "$libdir/pg_duckdb.so" "$libdir/libduckdb.so"; do
    digest=$(sha256sum "$component" | cut -d' ' -f1)
    grep -Fq "$(printf '\t%s\t%s' "$component" "$digest")" "$out/identity-before.txt"
done
if "$PWD/tools/phase7/differential-driver" --mode exporter --conninfo "$conninfo" \
        --site-binding wrong-site --deadline-ms 5000 </dev/null >"$out/wrong-site.log" 2>&1; then
    echo 'driver accepted mismatched site identity' >&2
    exit 1
fi
grep -Fxq 'isolated-lab site identity does not match' "$out/wrong-site.log"
printf 'driver-site-mismatch\tPASS\n' >"$out/runtime-results.tsv"
python3 tools/phase7/count_matrix.py --isolated-lab --psql "$pgbin/psql" \
    --driver "$PWD/tools/phase7/differential-driver" --conninfo "$conninfo" --output "$out" >"$out/acquisition.log"
identity >"$out/identity-after.txt"
cmp -s "$out/identity-before.txt" "$out/identity-after.txt"

certtool=(python3 tools/phase7/count_certificate.py "$out" --major "$major" --revision "$revision")
"${certtool[@]}" >"$out/evidence.json"
python3 - "$out/evidence.json" <<'PY' >"$out/evidence-index.json"
import json
import sys
with open(sys.argv[1], encoding='utf-8') as stream:
    evidence = json.load(stream)
receipt = evidence['receipt']
identity = receipt['identity']
print(json.dumps({'evidence_digest': evidence['digest'], 'scope': receipt['scope'],
                  'postgres_version': identity['postgres_version'], 'pg_qi_revision': identity['pg_qi_revision'],
                  'binary_sha256': identity['binary_sha256'], 'identity_sha256': identity['identity_sha256'],
                  'files': receipt['files']}, sort_keys=True))
PY
proof=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["digest"])' "$out/evidence.json")
now=$(date +%s)
if "${certtool[@]}" --expected-digest "$(printf '%064d' 0)" \
        --issued-at "$now" --expires-at "$((now + 1800))" >"$out/wrong-evidence.log" 2>&1; then
    echo 'changed evidence was accepted' >&2
    exit 1
fi
"${certtool[@]}" --expected-digest "$proof" --issued-at "$now" --expires-at "$((now + 1800))" \
    >"$out/prepared-lab-certificate.json"
certificate=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["certificate_record"])' "$out/prepared-lab-certificate.json")
approval=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["approval_sha256"])' "$out/prepared-lab-certificate.json")
test "$(printf '%s' "$certificate" | sha256sum | cut -d' ' -f1)" = "$approval"

configure() {
    local value=$1 digest=$2 attempt
    "${psql[@]}" -v "record=$value" -v "seal=$digest" >"$out/last-reload.log" <<'SQL'
ALTER SYSTEM SET pg_qi.count_certificate = :'record';
ALTER SYSTEM SET pg_qi.count_certificate_approved_sha256 = :'seal';
SELECT pg_reload_conf();
SQL
    for ((attempt=0; attempt<30; attempt++)); do
        if test "$("${psql[@]}" -c 'SHOW pg_qi.count_certificate')" = "$value" &&
            test "$("${psql[@]}" -c 'SHOW pg_qi.count_certificate_approved_sha256')" = "$digest"; then
            return 0
        fi
        sleep 0.1
    done
    echo 'certificate reload did not converge' >&2
    return 1
}

probe() {
    local label=$1 expected=$2 query=$3 transaction=$4 before_subject=${5:-}
    printf '%s\n' \
        "$transaction" 'SET LOCAL pg_qi.routing_mode = shadow_p0;' \
        'SET LOCAL max_parallel_workers_per_gather = 0;' \
        "$before_subject" \
        "SET LOCAL pg_qi_witness.subject_token = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';" \
        'SET LOCAL pg_qi_witness.armed = on;' "$query" \
        'SHOW pg_qi.decision_v2;' 'SHOW pg_qi_witness.last_record;' \
        'SHOW duckdb.force_execution;' 'ROLLBACK;' | "${psql[@]}" >"$out/probe-$label.log"
    PYTHONPATH="$PWD" python3 - "$out/probe-$label.log" "$expected" <<'PY'
from pathlib import Path
import sys
from tools.phase7.count_matrix import NROWS
from tools.phase7.decision_snapshot import parse_v2
from tools.phase7.witness_record import parse_witness

lines = Path(sys.argv[1]).read_text().splitlines()
record = parse_v2(next(line for line in lines if line.startswith('v=2;')))
witness = parse_witness(next(line for line in lines if line.startswith('v=1;')))
assert record['s'] == 'complete' and record['policy'] == 'pass_through', record
assert record['barrier'] == 'closed' and record['recommendation'] != 'would_delegate', record
assert record['force'] == 'off' and lines[-1] == 'off', lines
assert witness.engine == 'postgres' and witness.sequence == 1, witness
expected = sys.argv[2]
if expected == 'no-pass':
    assert record['safe'] != 'pass', record
else:
    assert record['safe'] == expected, record
if expected == 'pass':
    assert lines[0] == str(NROWS), lines
PY
    printf '%s\tPASS\n' "$label" >>"$out/runtime-results.tsv"
}

readonly_rr='BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;'
subject='SELECT pg_catalog.count(*) FROM pq35_matrix.mixed;'
probe default-unknown unknown "$subject" "$readonly_rr"
configure "$certificate" ''
probe approval-missing unknown "$subject" "$readonly_rr"
configure "$certificate" "$approval"
probe exact-certificate pass "$subject" "$readonly_rr"
probe read-committed unknown "$subject" 'BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY;'
probe read-write unknown "$subject" 'BEGIN ISOLATION LEVEL REPEATABLE READ READ WRITE;'
probe subtransaction unknown "$subject" "$readonly_rr" 'SAVEPOINT count_cert_probe;'
probe assigned-xid unknown "$subject" "$readonly_rr" 'SELECT pg_catalog.pg_current_xact_id();'
probe gather-setting-drift unknown "$subject" "$readonly_rr" 'SET LOCAL max_parallel_workers_per_gather = 1;'
probe provider-worker-drift unknown "$subject" "$readonly_rr" 'SET LOCAL duckdb.max_workers_per_postgres_scan = 1;'
probe count-value unknown 'SELECT pg_catalog.count(payload) FROM pq35_matrix.mixed;' "$readonly_rr"
probe min-value unknown 'SELECT pg_catalog.min(payload) FROM pq35_matrix.mixed;' "$readonly_rr"
probe max-value unknown 'SELECT pg_catalog.max(payload) FROM pq35_matrix.mixed;' "$readonly_rr"
probe outside-only unknown 'SELECT pg_catalog.count(*) FROM ONLY pq35_matrix.mixed;' "$readonly_rr"
probe outside-filter no-pass 'SELECT pg_catalog.count(*) FROM pq35_matrix.mixed WHERE id > 3;' "$readonly_rr"
"${psql[@]}" <<'SQL'
CREATE ROLE pgqi_diagnostic;
CREATE ROLE pq35_probe;
GRANT pgqi_diagnostic TO pq35_probe;
GRANT USAGE ON SCHEMA pq35_matrix TO pq35_probe;
GRANT SELECT ON pq35_matrix.mixed TO pq35_probe;
GRANT SET ON PARAMETER "pg_qi_witness.subject_token", "pg_qi_witness.armed" TO pq35_probe;
ALTER TABLE pq35_matrix.dead_tuples ENABLE ROW LEVEL SECURITY;
SQL
probe changed-role unknown "$subject" "$readonly_rr" 'SET LOCAL ROLE pq35_probe;'
probe outside-rls no-pass 'SELECT pg_catalog.count(*) FROM pq35_matrix.dead_tuples;' "$readonly_rr"

if "${psql[@]}" -c 'SET pg_qi.site_build_id = "session-change"' >"$out/change-site.log" 2>&1; then
    echo 'site identity was mutable at session scope' >&2
    exit 1
fi
grep -Fq 'parameter "pg_qi.site_build_id" cannot be changed without restarting the server' "$out/change-site.log"
printf 'site-postmaster-only\tPASS\n' >>"$out/runtime-results.tsv"

mutated=$(python3 -c 'import re,sys; print(re.sub(r";proof=[0-9a-f]{64}",";proof="+"1"*64,sys.argv[1]))' "$certificate")
configure "$mutated" "$approval"
probe record-approval-mismatch unknown "$subject" "$readonly_rr"
for field in v scope pg cluster site pg_build qi_build provider_src duckdb_src pg_bin qi_bin provider_bin duckdb_bin; do
    mutated=$(python3 - "$certificate" "$field" <<'PY'
import sys
fields = [item.split('=', 1) for item in sys.argv[1].split(';')]
name = sys.argv[2]
for item in fields:
    if item[0] == name:
        value = item[1]
        if name in {'v', 'pg', 'cluster'}:
            item[1] = '2' if value == '1' else '1'
        else:
            item[1] = ('2' if value[0] == '1' else '1') + value[1:]
print(';'.join('='.join(item) for item in fields))
PY
    )
    configure "$mutated" "$(printf '%s' "$mutated" | sha256sum | cut -d' ' -f1)"
    probe "mismatched-$field" unknown "$subject" "$readonly_rr"
done
mutated=$(python3 -c 'import re,sys; print(re.sub(r";issued=[0-9]+;expires=[0-9]+$",";issued=1;expires=2",sys.argv[1]))' "$certificate")
configure "$mutated" "$(printf '%s' "$mutated" | sha256sum | cut -d' ' -f1)"
probe expired unknown "$subject" "$readonly_rr"
mutated=$(python3 -c 'import re,sys; start=int(sys.argv[2])+3600; print(re.sub(r";issued=[0-9]+;expires=[0-9]+$",f";issued={start};expires={start+10}",sys.argv[1]))' "$certificate" "$now")
configure "$mutated" "$(printf '%s' "$mutated" | sha256sum | cut -d' ' -f1)"
probe not-yet-valid unknown "$subject" "$readonly_rr"

configure "$certificate" "$approval"
"${psql[@]}" >"$out/probe-same-backend-revocation.log" <<'SQL'
SET pg_qi.routing_mode = shadow_p0;
SET max_parallel_workers_per_gather = 0;
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_catalog.count(*) FROM pq35_matrix.mixed;
SHOW pg_qi.decision_v2;
ROLLBACK;
ALTER SYSTEM RESET pg_qi.count_certificate;
ALTER SYSTEM RESET pg_qi.count_certificate_approved_sha256;
SELECT pg_reload_conf();
SELECT pg_sleep(1);
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_catalog.count(*) FROM pq35_matrix.mixed;
SHOW pg_qi.decision_v2;
ROLLBACK;
SQL
PYTHONPATH="$PWD" python3 - "$out/probe-same-backend-revocation.log" <<'PY'
from pathlib import Path
import sys
from tools.phase7.count_matrix import NROWS
from tools.phase7.decision_snapshot import parse_v2

lines = Path(sys.argv[1]).read_text().splitlines()
before, after = [parse_v2(line) for line in lines if line.startswith('v=2;')]
assert lines.count(str(NROWS)) == 2, lines
assert before['cid'] == after['cid'] and int(after['e']) > int(before['e']), (before, after)
assert before['safe'] == 'pass' and after['safe'] == 'unknown', (before, after)
assert all(r['barrier'] == 'closed' and r['policy'] == 'pass_through' and
           r['force'] == 'off' for r in (before, after)), (before, after)
PY
printf 'same-backend-revocation\tPASS\n' >>"$out/runtime-results.tsv"

printf 'PG%s: nine differential cases and refusal control; guarded activation and revocation passed\n' "$major"
