#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
expected_source_commit=${2:-}
pg_config=${PG_CONFIG:-/usr/lib/postgresql/$major/bin/pg_config}
postgres=$($pg_config --bindir)/postgres
source_dir=${RUNNER_TEMP:-/tmp}/postgresql-$major-source

test -x "$pg_config"
test -x "$postgres"
if [ -n "$expected_source_commit" ]; then
    [[ "$expected_source_commit" =~ ^[0-9a-f]{40}$ ]]
fi
source_commit=${expected_source_commit:-package}
if test -d "$source_dir/.git"; then
    source_commit=$(git -C "$source_dir" rev-parse HEAD)
    if [ -n "$expected_source_commit" ]; then
        test "$source_commit" = "$expected_source_commit"
    fi
fi
postgres_sha=$(sha256sum "$postgres")
postgres_sha=${postgres_sha%% *}
configure=$($pg_config --configure)
configure=${configure#"${configure%%[![:space:]]*}"}

{
    printf 'major=%s\n' "$major"
    printf 'version=%s\n' "$($pg_config --version)"
    printf 'source_commit=%s\n' "$source_commit"
    printf 'configure=%s\n' "$configure"
    printf 'postgres_sha256=%s\n' "$postgres_sha"
} | sha256sum | cut -d' ' -f1
