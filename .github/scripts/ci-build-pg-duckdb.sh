#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_duckdb_sha=${PG_DUCKDB_SHA:-ee38d3b540ecea1d93683ba99bdcec5632a21eaf}
duckdb_version=${DUCKDB_VERSION:-v1.5.4}
archive_sha=${DUCKDB_ARCHIVE_SHA256:-838d98a85e697bab9935010c88a8c67d3312ccedcab4cb4a0ba01da65113bb70}
work_dir=${RUNNER_TEMP:?RUNNER_TEMP is required}/pg_duckdb-$major
archive=${RUNNER_TEMP}/libduckdb-${duckdb_version}.zip
extract_dir=${RUNNER_TEMP}/libduckdb-${duckdb_version}
export PG_CONFIG="/usr/lib/postgresql/$major/bin/pg_config"

git clone https://github.com/duckdb/pg_duckdb.git "$work_dir"
git -C "$work_dir" checkout "$pg_duckdb_sha"
git -C "$work_dir" submodule update --init --recursive
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
