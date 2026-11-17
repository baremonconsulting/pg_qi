#!/usr/bin/env bash

set -euo pipefail

pgqi4c_repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

pgqi4c_sanitize_connection_env() {
    unset PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD PGPASSFILE
    unset PGSERVICE PGSERVICEFILE PGOPTIONS PGAPPNAME
}

pgqi4c_require_safe_root() {
    local root=$1 canonical
    if test -z "$root" || test "$root" = / || test -L "$root"; then
        echo "refusing unsafe run root: $root" >&2
        return 1
    fi
    if [[ "$root" =~ [[:space:]\\\'] ]]; then
        echo "refusing path with unsafe configuration/conninfo characters: $root" >&2
        return 1
    fi
    canonical=$(realpath -m -- "$root") || return 1
    test "$canonical" = "$root" || { echo "refusing non-canonical run root: $root" >&2; return 1; }
    case "$canonical" in /tmp/*|"$(realpath -m "${RUNNER_TEMP:-/tmp}")"/*|"$(realpath -m "$pgqi4c_repo/artifacts/phase4c")"/*) ;; *)
        echo "refusing non-owned run root: $root" >&2; return 1;; esac
}

pgqi4c_require_private_parent() {
    local parent=$1 mode
    test -d "$parent" && test ! -L "$parent" || { echo "invalid artifact parent: $parent" >&2; return 1; }
    test "$(stat -c %u "$parent")" = "$(id -u)" || { echo "artifact parent is not owned by current user" >&2; return 1; }
    mode=$(stat -c %a "$parent")
    if (( (8#$mode & 8#022) != 0 )); then echo "artifact parent is group/world writable" >&2; return 1; fi
}

pgqi4c_start() {
    local pgbin=$1 root=$2 preload=${3:-pg_duckdb,pg_qi} socket_dir=${4:-$2/socket}
    pgqi4c_require_safe_root "$root"
    test ! -e "$root" || { echo "run root already exists: $root" >&2; return 1; }
    mkdir -m 700 "$root"
    if test "$socket_dir" = "$root/socket"; then
        mkdir -m 700 "$socket_dir"
    else
        pgqi4c_require_safe_root "$socket_dir"
        mkdir -m 700 "$socket_dir"
    fi
    printf 'root=%s\nowner=%s\n' "$root" "$(id -u)" >"$root/.pgqi4c-owned"
    "$pgbin/initdb" -D "$root/data" --no-locale --encoding=UTF8 --no-sync >"$root/initdb.log" 2>&1
    {
        printf "unix_socket_directories = '%s'\n" "$socket_dir"
        printf "listen_addresses = ''\nshared_preload_libraries = '%s'\n" "$preload"
        printf "max_parallel_workers_per_gather = 0\njit = off\nlog_line_prefix = '%%m [%%p] %%a '\n"
    } >>"$root/data/postgresql.conf"
    if ! "$pgbin/pg_ctl" -D "$root/data" -l "$root/server.log" -w start >"$root/server-control.log" 2>&1; then
        "$pgbin/pg_ctl" -D "$root/data" -m immediate stop >>"$root/server-control.log" 2>&1 || true
        return 1
    fi
}

pgqi4c_stop() {
    local pgbin=$1 root=$2
    pgqi4c_require_safe_root "$root"
    test -f "$root/.pgqi4c-owned" || { echo "refusing unowned cluster" >&2; return 1; }
    test "$(sed -n 's/^root=//p' "$root/.pgqi4c-owned")" = "$root" || { echo "ownership marker mismatch" >&2; return 1; }
    test "$(sed -n 's/^owner=//p' "$root/.pgqi4c-owned")" = "$(id -u)" || { echo "ownership uid mismatch" >&2; return 1; }
    test -f "$root/data/PG_VERSION" || { echo "refusing invalid cluster" >&2; return 1; }
    "$pgbin/pg_ctl" -D "$root/data" -m fast -w stop >>"$root/server-control.log" 2>&1
}

pgqi4c_verify_installed_production() {
    local pgconfig=$1 built=$2 libdir installed
    libdir=$($pgconfig --pkglibdir)
    installed=$libdir/pg_qi.so
    test -f "$installed" || { echo "pg_qi is not installed at $installed" >&2; return 1; }
    test "$(sha256sum "$built" | cut -d' ' -f1)" = "$(sha256sum "$installed" | cut -d' ' -f1)" || {
        echo "installed pg_qi does not match the production build; install it explicitly in the dedicated test PostgreSQL tree" >&2
        return 1
    }
    if strings "$installed" | rg 'pg_qi\.spike_|pg_qi\.test_|qi_spike_|qi_test_|PG_QI_FINAL' >/dev/null; then
        echo "test-only surface found in installed pg_qi" >&2
        return 1
    fi
}

pgqi4c_build_driver() {
    local pgconfig=$1 output=$2
    cc -O2 -Wall -Wextra -Werror -I"$($pgconfig --includedir)" \
       "$pgqi4c_repo/scripts/phase4c/production-driver.c" \
       -L"$($pgconfig --libdir)" -Wl,-rpath,"$($pgconfig --libdir)" -lpq -o "$output"
}
