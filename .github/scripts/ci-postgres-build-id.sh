#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_config=${PG_CONFIG:-/usr/lib/postgresql/$major/bin/pg_config}
postgres=$($pg_config --bindir)/postgres
source_dir=${RUNNER_TEMP:-/tmp}/postgresql-$major-source

test -x "$pg_config"
test -x "$postgres"
source_commit=package
if test -d "$source_dir/.git"; then
    source_commit=$(git -C "$source_dir" rev-parse HEAD)
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
