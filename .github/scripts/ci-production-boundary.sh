#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
artifact_dir=${2:?artefact directory is required}
pg_config=${PG_CONFIG:-/usr/lib/postgresql/$major/bin/pg_config}
built=${PG_QI_BUILT_PATH:-pg_qi.so}
libdir=$($pg_config --pkglibdir)
sharedir=$($pg_config --sharedir)
installed=$libdir/pg_qi.so
report=$artifact_dir/${3:-production-boundary.txt}
strings_tool=${STRINGS:-strings}
nm_tool=${NM:-nm}
grep_tool=${GREP:-grep}

mkdir -p "$artifact_dir"
test -f "$built"
test -f "$installed"
hash_tool=$(cd "$(dirname "$0")" && pwd)/ci-sha256.sh
built_sha=$(bash "$hash_tool" "$built")
installed_sha=$(bash "$hash_tool" "$installed")
test "$built_sha" = "$installed_sha" || {
    echo "installed pg_qi differs from the production build" >&2
    exit 1
}

for binary in "$built" "$installed"; do
    strings_output=$(mktemp)
    nm_output=$(mktemp)
    trap 'rm -f "$strings_output" "$nm_output"' EXIT
    if ! "$strings_tool" "$binary" >"$strings_output"; then
        echo "strings scanner failed for $binary" >&2
        exit 1
    fi
    if "$grep_tool" -E 'pg_qi\.(spike|test)[._]|pg_qi_witness[._]|qi_(spike|test)_|pg_qi_outer_test|pg_qi_execution_witness|PG_QI_FINAL' "$strings_output" >/dev/null; then
        echo "test-only string leaked into $binary" >&2
        exit 1
    else
        grep_status=$?
        if test "$grep_status" -ne 1; then
            echo "string-result scanner failed for $binary" >&2
            exit 1
        fi
    fi
    if ! "$nm_tool" -D --defined-only "$binary" >"$nm_output"; then
        echo "symbol scanner failed for $binary" >&2
        exit 1
    fi
    if "$grep_tool" -E ' (pg_qi_(spike|test|witness)|qi_(spike|test)|pg_qi_outer_test|pg_qi_execution_witness|PG_QI_FINAL)' "$nm_output" >/dev/null; then
        echo "test-only exported symbol leaked into $binary" >&2
        exit 1
    else
        grep_status=$?
        if test "$grep_status" -ne 1; then
            echo "symbol-result scanner failed for $binary" >&2
            exit 1
        fi
    fi
    rm -f "$strings_output" "$nm_output"
    trap - EXIT
done

for pattern in \
    "$libdir"/pg_qi_spike_test* "$libdir"/pg_qi_test* \
    "$libdir"/pg_qi_outer_test* "$libdir"/pg_qi_execution_witness* "$libdir"/pg_qi_witness* \
    "$sharedir/extension"/pg_qi_spike_test* \
    "$sharedir/extension"/pg_qi_test* \
    "$sharedir/extension"/pg_qi_outer_test* "$sharedir/extension"/pg_qi_witness* \
    "$sharedir/extension"/pg_qi_execution_witness*; do
    if test -e "$pattern" || test -L "$pattern"; then
        echo "test-only pg_qi artefact is installed in the production prefix: $pattern" >&2
        exit 1
    fi
done

{
    printf 'status=PASS\n'
    printf 'built=%s\ninstalled=%s\n' "$built" "$installed"
    printf 'built_sha256=%s\ninstalled_sha256=%s\n' "$built_sha" "$installed_sha"
    printf 'exported_test_symbols=0\ninstalled_test_artifacts=0\n'
} >"$report"
