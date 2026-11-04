#!/usr/bin/env bash

set -euo pipefail

sha=${PG17_ASSERT_SHA:-083ac033419f690758508e08c1736089384bbee8}
prefix=${PG_ASSERT_ROOT:-/opt/postgresql/17-assert}
source_dir=${RUNNER_TEMP:?RUNNER_TEMP is required}/postgresql-17-assert-source
build_dir=${RUNNER_TEMP}/postgresql-17-assert-build

git clone --filter=blob:none https://github.com/postgres/postgres.git \
    "$source_dir"
git -C "$source_dir" checkout --detach "$sha"
test "$(git -C "$source_dir" rev-parse HEAD)" = "$sha"
mkdir -p "$build_dir"
(
    cd "$build_dir"
    "$source_dir/configure" --prefix="$prefix" --enable-cassert \
        --with-openssl --with-lz4
    make -j2
    sudo make install
)
"$prefix/bin/pg_config" --configure | grep -q -- --enable-cassert
