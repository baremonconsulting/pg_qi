#!/usr/bin/env bash
set -euo pipefail

root=${1:?usage: run-site-preflight-integration.sh OUTPUT_DIRECTORY}
repo=$(cd "$(dirname "$0")/../.." && pwd)
pg_config_bin=${PG_CONFIG:-pg_config}
pgbin=$($pg_config_bin --bindir)
driver="$repo/tools/phase5/site-preflight-driver"
fixture="$repo/tests/phase5/create-site-preflight-fixture.py"
data="$root/data"
socket="$root/socket"
log="$root/postgresql.log"
tls="$root/tls"
poison_home="$root/poison-home"
port=${PGQI_PREFLIGHT_TEST_PORT:-55439}

test ! -e "$root"
mkdir -m 700 -p "$root" "$socket" "$tls" "$poison_home/.postgresql"
printf 'not a certificate\n' >"$poison_home/.postgresql/postgresql.crt"
printf 'not a key\n' >"$poison_home/.postgresql/postgresql.key"
printf 'not a CRL\n' >"$poison_home/.postgresql/root.crl"
printf '[poison]\nhost=untrusted.invalid\n' >"$poison_home/.pg_service.conf"
printf '*:*:*:*:POISON_PASSFILE_CANARY\n' >"$poison_home/.pgpass"
chmod 600 "$poison_home/.postgresql/postgresql.key"
chmod 600 "$poison_home/.pgpass"
"$pgbin/initdb" -D "$data" --no-locale --encoding=UTF8 --auth-local=peer --auth-host=scram-sha-256 --no-sync \
  >"$root/initdb.log"

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
  -addext subjectAltName=DNS:localhost -keyout "$tls/server.key" -out "$tls/server.crt" \
  >"$root/openssl.log" 2>&1
chmod 600 "$tls/server.key"
rootcert_sha=$(sha256sum "$tls/server.crt" | awk '{print $1}')

cat >>"$data/postgresql.conf" <<EOF
unix_socket_directories = '$socket'
listen_addresses = '127.0.0.1'
port = $port
ssl = on
ssl_cert_file = '$tls/server.crt'
ssl_key_file = '$tls/server.key'
log_statement = 'none'
logging_collector = off
shared_preload_libraries = 'pg_duckdb,pg_qi'
EOF

"$pgbin/pg_ctl" -D "$data" -l "$log" -w start

cleanup() { "$pgbin/pg_ctl" -D "$data" -m fast -w stop >/dev/null 2>&1 || true; }
trap cleanup EXIT

"$pgbin/psql" -h "$socket" -p "$port" -d postgres -U "${USER}" -v ON_ERROR_STOP=1 \
  >"$root/setup.log" <<SQL
CREATE ROLE pilot_reader LOGIN PASSWORD 'PGQI_CREDENTIAL_CANARY';
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
SQL
cleanup
printf "log_statement = 'all'\n" >>"$data/postgresql.conf"
"$pgbin/pg_ctl" -D "$data" -l "$log" -w start

sql_scalar() { "$pgbin/psql" -XAt -h "$socket" -p "$port" -d postgres -U "${USER}" -c "$1"; }
server_version=$(sql_scalar "SELECT current_setting('server_version_num')")
database_oid=$(sql_scalar "SELECT oid FROM pg_database WHERE datname=current_database()")
postmaster_start=$(sql_scalar "SELECT to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
pg_qi_version=$(sql_scalar "SELECT extversion FROM pg_extension WHERE extname='pg_qi'")
pg_duckdb_version=$(sql_scalar "SELECT extversion FROM pg_extension WHERE extname='pg_duckdb'")
postgres_path="$pgbin/postgres"
pkglibdir=$($pg_config_bin --pkglibdir)
pg_qi_path="$pkglibdir/pg_qi.so"
pg_duckdb_path="$pkglibdir/pg_duckdb.so"
config_sha=$(sha256sum "$data/postgresql.conf" | awk '{print $1}')
commit=$(git -C "$repo" rev-parse HEAD)
tree_sha=$(git -C "$repo" rev-parse 'HEAD^{tree}' | sha256sum | awk '{print $1}')
cluster_id=$("$pgbin/pg_controldata" "$data" | awk -F: '/Database system identifier/{gsub(/ /,"",$2); print $2}')
boot_id=$(tr -d '\n' </proc/sys/kernel/random/boot_id)

create_dossier() {
  local kind=$1 output=$2 anchors=$3
  local role=${USER}
  if [[ $kind == tls ]]; then role=pilot_reader; fi
  python3 "$fixture" --kind "$kind" --output "$output" --commit "$commit" \
    --tree-sha "$tree_sha" --postgres "$postgres_path" --pg-qi "$pg_qi_path" \
    --pg-duckdb "$pg_duckdb_path" --config-sha "$config_sha" --major 16 \
    --server-version "$server_version" --database-oid "$database_oid" --role "$role" \
    --postmaster-start "$postmaster_start" --pg-qi-version "$pg_qi_version" \
    --pg-duckdb-version "$pg_duckdb_version" --cluster-id "$cluster_id" --boot-id "$boot_id" \
    --socket "$socket" --port "$port" --rootcert "$tls/server.crt" \
    --rootcert-sha "$rootcert_sha" --socket-uid "$(id -u)" >"$anchors"
}

run_qualified() {
  local dossier=$1 anchors=$2 output=$3 credential_fd=${4:-}
  readarray -t values < <(python3 - "$anchors" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding='utf-8'))
print(value['scope_sha256']); print(value['dossier_sha256']); print(value['start_sha256'])
PY
)
  local command=(python3 "$repo/tools/phase5/site_preflight.py" --dossier "$dossier"
    --trusted-scope-sha256 "${values[0]}" --trusted-dossier-sha256 "${values[1]}"
    --trusted-start-sha256 "${values[2]}" --execute-driver "$driver")
  if [[ -n $credential_fd ]]; then command+=(--credential-fd "$credential_fd"); fi
  env -i PATH="$PATH" HOME="$poison_home" USER="${USER:-pgqi}" LANG=C "${command[@]}" >"$output"
}

clean_run() {
  env -i PATH="$PATH" HOME="$poison_home" USER="${USER:-pgqi}" LANG=C "$@"
}

clean_run "$driver" --transport socket --host "$socket" --port "$port" \
  --socket-uid "$(id -u)" --dbname postgres --user "${USER}" \
  --deadline-ms 10000 >"$root/socket.json"
python3 - "$root/socket.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["result"] == "PASS"
assert value["checks"]["routing_mode"]["rows"] == [["off"]]
assert value["checks"]["duckdb_force"]["rows"] == [["off"]]
assert value["checks"]["extensions"]["rows"] == [["pg_duckdb", value["checks"]["extensions"]["rows"][0][1]], ["pg_qi", "0.0.1"]]
PY
create_dossier socket "$root/socket-dossier.json" "$root/socket-anchors.json"
run_qualified "$root/socket-dossier.json" "$root/socket-anchors.json" "$root/socket-qualified.json"

exec 9<<<'PGQI_CREDENTIAL_CANARY'
clean_run "$driver" --transport tls --host localhost --hostaddr 127.0.0.1 --port "$port" \
  --dbname postgres --user pilot_reader --sslrootcert "$tls/server.crt" \
  --sslrootcert-sha256 "$rootcert_sha" \
  --credential-fd 9 --deadline-ms 10000 >"$root/tls.json"
exec 9<&-
python3 - "$root/tls.json" <<'PY'
import json, sys
assert json.load(open(sys.argv[1], encoding="utf-8"))["result"] == "PASS"
PY
create_dossier tls "$root/tls-dossier.json" "$root/tls-anchors.json"
exec 9<<<'PGQI_CREDENTIAL_CANARY'
run_qualified "$root/tls-dossier.json" "$root/tls-anchors.json" "$root/tls-qualified.json" 9
exec 9<&-

exec 9<<<'PGQI_CREDENTIAL_CANARY'
if clean_run "$driver" --transport tls --host wrong.invalid --hostaddr 127.0.0.1 --port "$port" \
  --dbname postgres --user pilot_reader --sslrootcert "$tls/server.crt" \
  --sslrootcert-sha256 "$rootcert_sha" \
  --credential-fd 9 --deadline-ms 10000 >"$root/tls-wrong-host.json"; then
  echo 'TLS hostname mismatch unexpectedly passed' >&2; exit 1
fi
exec 9<&-
python3 - "$root/tls-wrong-host.json" <<'PY'
import json, sys
assert json.load(open(sys.argv[1], encoding="utf-8")) == {"reason": "connect_failed", "result": "FAIL"}
PY

exec 9<<<'WRONG_CREDENTIAL'
if clean_run "$driver" --transport tls --host localhost --hostaddr 127.0.0.1 --port "$port" \
  --dbname postgres --user pilot_reader --sslrootcert "$tls/server.crt" \
  --sslrootcert-sha256 "$rootcert_sha" --credential-fd 9 --deadline-ms 10000 \
  >"$root/tls-wrong-password.json"; then
  echo 'wrong TLS password unexpectedly passed' >&2; exit 1
fi
exec 9<&-

if clean_run "$driver" --transport tls --host localhost --hostaddr 127.0.0.1 --port "$port" \
  --dbname postgres --user pilot_reader --sslrootcert "$tls/server.crt" \
  --sslrootcert-sha256 "$rootcert_sha" --deadline-ms 10000 \
  >"$root/tls-missing-credential.json"; then
  echo 'missing TLS credential unexpectedly passed' >&2; exit 1
fi

exec 9<<<'PGQI_CREDENTIAL_CANARY'
if clean_run "$driver" --transport tls --host localhost --hostaddr 127.0.0.1 --port "$port" \
  --dbname postgres --user pilot_reader --sslrootcert "$tls/server.crt" \
  --sslrootcert-sha256 "$(printf '0%.0s' {1..64})" --credential-fd 9 --deadline-ms 10000 \
  >"$root/tls-wrong-ca-hash.json"; then
  echo 'wrong CA digest unexpectedly passed' >&2; exit 1
fi
exec 9<&-

statements="$root/statements.log"
grep -E ' (statement:|execute <unnamed>:)' "$log" >"$statements"
if [[ $(grep -c 'execute <unnamed>:' "$statements") -ne 40 ]]; then
  echo 'fixed preflight query count differs from four complete ten-query connections' >&2; exit 1
fi
for query in \
  'SHOW pg_qi.routing_mode' \
  'SHOW duckdb.force_execution' \
  "SELECT pg_catalog.current_setting('server_version_num')" \
  'SELECT SESSION_USER::text, CURRENT_USER::text' \
  'SHOW pg_qi.enabled'; do
  grep -F -- "$query" "$statements" >/dev/null
done
if grep -Eiq '(statement:|execute <unnamed>:)[[:space:]]*(SET|ALTER|CREATE|DROP|INSERT|UPDATE|DELETE|MERGE|COPY|CALL|DO|TRUNCATE|GRANT|REVOKE)' "$statements"; then
  echo 'mutating or activation SQL observed' >&2; exit 1
fi
if grep -R -F 'PGQI_CREDENTIAL_CANARY' "$root" --exclude=server.key >/dev/null; then
  echo 'credential canary leaked into evidence' >&2; exit 1
fi

printf 'PASS\n' >"$root/result.txt"
cleanup
trap - EXIT
