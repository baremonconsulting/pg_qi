#!/usr/bin/env bash

set -euo pipefail
export PG_QI_TESTING=1

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
install_root=${PG_INSTALL_ROOT:-/home/adam/.local/pgsql}
artifact_root=${WP4_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp4"}

for major in 16 17 18; do
    make -C "$repo_dir" PG_CONFIG="$install_root/$major/bin/pg_config" clean \
        >/dev/null
    make -C "$repo_dir" PG_CONFIG="$install_root/$major/bin/pg_config" -j2 \
        >"$artifact_root/build-pg$major.log" 2>&1
    make -C "$repo_dir" PG_CONFIG="$install_root/$major/bin/pg_config" install \
        >"$artifact_root/install-pg$major.log" 2>&1
done

WP23_ARTIFACT_DIR="$artifact_root" \
    "$repo_dir/scripts/spike1/wp2-wp3-matrix.sh"

echo "WP4 matrix PASS: frame contract and regression suite on PostgreSQL 16, 17, and 18"
