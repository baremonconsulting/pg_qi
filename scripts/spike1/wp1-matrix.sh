#!/usr/bin/env bash

set -euo pipefail

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
artifact_dir=${WP1_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp1"}
install_root=${PG_INSTALL_ROOT:-/home/adam/.local/pgsql}

mkdir -p "$artifact_dir"

for major in 16 17 18; do
    pg_config="$install_root/$major/bin/pg_config"
    initdb="$install_root/$major/bin/initdb"
    pg_ctl="$install_root/$major/bin/pg_ctl"
    psql="$install_root/$major/bin/psql"
    port=$((55400 + major))
    data_dir="$artifact_dir/pg${major}-data"
    socket_dir="$artifact_dir/pg${major}-socket"
    build_log="$artifact_dir/pg${major}-build.log"
    install_log="$artifact_dir/pg${major}-install.log"
    server_log="$artifact_dir/pg${major}-server.log"
    test_log="$artifact_dir/pg${major}-installcheck.log"
    preload_log="$artifact_dir/pg${major}-preload-smoke.log"

    test -x "$pg_config"
    test -x "$initdb"
    test -x "$pg_ctl"
    test -x "$psql"

    rm -rf "$data_dir" "$socket_dir"
    mkdir -p "$socket_dir"

    "$pg_config" --version | tee "$artifact_dir/pg${major}-version.txt"

    make -C "$repo_dir" clean PG_CONFIG="$pg_config" >/dev/null
    make -C "$repo_dir" PG_CONFIG="$pg_config" >"$build_log" 2>&1
    make -C "$repo_dir" install PG_CONFIG="$pg_config" >"$install_log" 2>&1

    "$initdb" -D "$data_dir" --no-locale --encoding=UTF8 \
        >"$artifact_dir/pg${major}-initdb.log" 2>&1
    {
        echo "shared_preload_libraries = 'pg_qi'"
        echo "unix_socket_directories = '$socket_dir'"
        echo "listen_addresses = ''"
        echo "port = $port"
    } >>"$data_dir/postgresql.conf"

    cleanup() {
        "$pg_ctl" -D "$data_dir" -m fast stop >/dev/null 2>&1 || true
    }
    trap cleanup EXIT

    "$pg_ctl" -D "$data_dir" -l "$server_log" -w start >/dev/null
    "$psql" -X --host="$socket_dir" --port="$port" --username="$(id -un)" \
        --dbname=postgres --set=ON_ERROR_STOP=1 --tuples-only --no-align \
        >"$preload_log" <<'SQL'
CREATE EXTENSION pg_qi;
SELECT qi.is_preloaded();
SHOW pg_qi.enabled;
DROP EXTENSION pg_qi;
SQL
    if [ "$(sed -n '2p' "$preload_log")" != "t" ]; then
        echo "PostgreSQL $major did not report pg_qi as preloaded" >&2
        exit 1
    fi
    make -C "$repo_dir" installcheck PG_CONFIG="$pg_config" \
        PGHOST="$socket_dir" PGPORT="$port" PGUSER="$(id -un)" \
        >"$test_log" 2>&1
    cleanup
    trap - EXIT

    if ! grep -q "All .* tests passed" "$test_log"; then
        echo "PostgreSQL $major regression output did not contain PASS marker" >&2
        exit 1
    fi
done

echo "WP1 matrix PASS: PostgreSQL 16, 17, and 18"
