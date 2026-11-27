#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
pg_duckdb_sha=${PG_DUCKDB_SHA:-}
duckdb_sha=${DUCKDB_SHA:-}
source "$repo_dir/deps/pg_duckdb.env"
pg_duckdb_sha=${pg_duckdb_sha:-$PG_DUCKDB_SHA}
duckdb_sha=${duckdb_sha:-$DUCKDB_SHA}
build_mode=${PG_DUCKDB_BUILD_MODE:-source}
duckdb_version=${DUCKDB_VERSION:-v1.5.4}
archive_sha=${DUCKDB_ARCHIVE_SHA256:-838d98a85e697bab9935010c88a8c67d3312ccedcab4cb4a0ba01da65113bb70}
work_dir=${RUNNER_TEMP:?RUNNER_TEMP is required}/pg_duckdb-$major
archive=${RUNNER_TEMP}/libduckdb-${duckdb_version}.zip
extract_dir=${RUNNER_TEMP}/libduckdb-${duckdb_version}
export PG_CONFIG=${PG_CONFIG:-"/usr/lib/postgresql/$major/bin/pg_config"}

git clone https://github.com/duckdb/pg_duckdb.git "$work_dir"
git -C "$work_dir" checkout "$pg_duckdb_sha"
git -C "$work_dir" submodule update --init --recursive
test "$(git -C "$work_dir" rev-parse HEAD)" = "$pg_duckdb_sha"
test "$(git -C "$work_dir/third_party/duckdb" rev-parse HEAD)" = "$duckdb_sha"

case "$build_mode" in
    source)
        make -C "$work_dir" -j2 DUCKDB_GEN=ninja \
            DUCKDB_CC=clang DUCKDB_CXX=clang++
        sudo --preserve-env=PG_CONFIG make -C "$work_dir" install \
            DUCKDB_GEN=ninja DUCKDB_CC=clang DUCKDB_CXX=clang++
        ;;
    prebuilt)
        curl --fail --location \
            "https://github.com/duckdb/duckdb/releases/download/$duckdb_version/libduckdb-linux-amd64.zip" \
            --output "$archive"
        echo "$archive_sha  $archive" | sha256sum --check
        mkdir -p "$extract_dir" "$work_dir/third_party/duckdb/build/release/src"
        unzip -q "$archive" -d "$extract_dir"
        cp "$extract_dir/libduckdb.so" \
            "$work_dir/third_party/duckdb/build/release/src/libduckdb.so"
        make -C "$work_dir" -j2
        sudo --preserve-env=PG_CONFIG make -C "$work_dir" install
        ;;
    *)
        echo "unsupported PG_DUCKDB_BUILD_MODE: $build_mode" >&2
        exit 2
        ;;
esac

installed_pg_duckdb=$($PG_CONFIG --pkglibdir)/pg_duckdb.so
built_pg_duckdb=$work_dir/pg_duckdb.so
built_duckdb=$work_dir/third_party/duckdb/build/release/src/libduckdb.so
installed_duckdb=$($PG_CONFIG --pkglibdir)/libduckdb.so
test -f "$built_pg_duckdb" -a -f "$installed_pg_duckdb"
test -f "$built_duckdb" -a -f "$installed_duckdb"
hash_tool=$(cd "$(dirname "$0")" && pwd)/ci-sha256.sh
built_pg_duckdb_sha=$(bash "$hash_tool" "$built_pg_duckdb")
installed_pg_duckdb_sha=$(bash "$hash_tool" "$installed_pg_duckdb")
built_duckdb_sha=$(bash "$hash_tool" "$built_duckdb")
installed_duckdb_sha=$(bash "$hash_tool" "$installed_duckdb")
test "$built_pg_duckdb_sha" = "$installed_pg_duckdb_sha"
test "$built_duckdb_sha" = "$installed_duckdb_sha"

printf 'PG_DUCKDB_SHA=%s\nDUCKDB_SHA=%s\n' "$pg_duckdb_sha" "$duckdb_sha"
if [ -n "${GITHUB_ENV:-}" ]; then
    printf 'PG_DUCKDB_SHA=%s\nDUCKDB_SHA=%s\n' "$pg_duckdb_sha" "$duckdb_sha" >>"$GITHUB_ENV"
fi
