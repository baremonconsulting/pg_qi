#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
sanitizer=${2:?Build mode must be address, undefined or none}
script_dir=$(cd "$(dirname "$0")" && pwd)
source "$script_dir/ci-postgresql-pin.sh" "$major"
prefix=${PG_SOURCE_PREFIX:-${PG_INSTALL_ROOT:-/opt/pg-$sanitizer}/$major}
source_dir=${RUNNER_TEMP:?RUNNER_TEMP is required}/postgresql-$major-source
build_dir=${RUNNER_TEMP}/postgresql-$major-build

case "$sanitizer" in
    address) sanitizer_flags="-fsanitize=address -fno-omit-frame-pointer" ;;
    undefined)
        sanitizer_flags="-fsanitize=undefined -fno-sanitize=vptr -fno-sanitize-recover=all"
        sanitizer_flags+=" -fno-omit-frame-pointer -fno-sanitize=function"
        ;;
    none) sanitizer_flags= ;;
    *) echo "unsupported build mode: $sanitizer" >&2; exit 2 ;;
esac

configure_args=(--prefix="$prefix" --enable-cassert --with-openssl --with-lz4)
if [ -n "$sanitizer_flags" ]; then
    clang_version=${CLANG_VERSION:-18}
    configure_args+=(--enable-debug "CC=clang-$clang_version" "CXX=clang++-$clang_version"
        "CFLAGS=-O1 $sanitizer_flags" "CXXFLAGS=-O1 $sanitizer_flags" "LDFLAGS=$sanitizer_flags")
fi

git init -q "$source_dir"
git -C "$source_dir" fetch --depth=1 https://github.com/postgres/postgres.git "$source_sha"
git -C "$source_dir" checkout --detach FETCH_HEAD
test "$(git -C "$source_dir" rev-parse HEAD)" = "$source_sha"
mkdir -p "$build_dir"
(
    cd "$build_dir"
    "$source_dir/configure" "${configure_args[@]}"
    make -j2
    make install
)
if [ -n "$sanitizer_flags" ]; then
    for module in file_fdw auto_explain; do
        make -C "$source_dir/contrib/$module" USE_PGXS=1 \
            PG_CONFIG="$prefix/bin/pg_config" -j2
        make -C "$source_dir/contrib/$module" USE_PGXS=1 \
            PG_CONFIG="$prefix/bin/pg_config" install
    done
fi

test "$("$prefix/bin/pg_config" --version)" = "$expected_version"
test "$("$prefix/bin/postgres" --version)" = "postgres (PostgreSQL) ${expected_version#PostgreSQL }"
"$prefix/bin/pg_config" --configure | grep -q -- --enable-cassert
