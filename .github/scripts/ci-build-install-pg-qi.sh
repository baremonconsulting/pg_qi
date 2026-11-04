#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
artifact_dir=${2:-ci-artifacts/build-pg$major}
pg_config="/usr/lib/postgresql/$major/bin/pg_config"
testing=${PG_QI_TESTING:-1}

mkdir -p "$artifact_dir"
make PG_CONFIG="$pg_config" PG_QI_TESTING="$testing" clean >"$artifact_dir/clean.log" 2>&1
make PG_CONFIG="$pg_config" PG_QI_TESTING="$testing" -j2 >"$artifact_dir/build.log" 2>&1
sudo --preserve-env=PG_CONFIG make PG_CONFIG="$pg_config" PG_QI_TESTING="$testing" install \
    >"$artifact_dir/install.log" 2>&1
