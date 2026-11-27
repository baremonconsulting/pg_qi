#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
artifacts=$(realpath -m "${2:?Evidence directory is required}")
pg_config=${PG_CONFIG:?PG_CONFIG is required}
pgbin=$("$pg_config" --bindir)
libdir=$("$pg_config" --pkglibdir)
repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo_dir"
mkdir -p "$artifacts"

with_server() (
    local run_dir=$1 preload=$2 suite=$3
    local data=$run_dir/data
    export PGHOST="$run_dir/socket" PGUSER="$(id -un)"
    mkdir -p "$PGHOST"
    "$pgbin/initdb" -D "$data" --no-locale --encoding=UTF8 --no-sync >"$run_dir/initdb.log"
    {
        printf "unix_socket_directories = '%s'\n" "$PGHOST"
        printf "listen_addresses = ''\n"
        printf "shared_preload_libraries = '%s'\n" "$preload"
        printf "pg_qi.site_build_id = '%s'\n" "${PG_QI_SITE_BUILD_ID:?PG_QI_SITE_BUILD_ID is required}"
    } >>"$data/postgresql.conf"
    stop_server() {
        if [ -f "$data/postmaster.pid" ]; then
            "$pgbin/pg_ctl" -D "$data" -m fast -w stop
        fi
    }
    trap stop_server EXIT
    "$pgbin/pg_ctl" -D "$data" -l "$run_dir/server.log" -w start
    "$suite"
)

production_checks() {
    make PG_CONFIG="$pg_config" PG_QI_TESTING=0 installcheck
    bash tests/outer_hook/verify.sh "$major"
    bash tests/outer_hook/verify_runtime.sh "$major" "$artifacts/outer-hook" preloaded
    bash tests/phase7/verify_snapshot.sh "$major"
    bash tests/phase7/verify_plan_features.sh "$major"
    bash tests/phase7/verify_spill_evidence.sh "$major"
    bash tests/phase7/verify_stats_confidence.sh "$major"
    bash tests/phase7/verify_lifecycle.sh "$major"
    bash tests/phase7/verify_profile.sh "$major"
    bash tests/phase7/verify_profile_parser.sh
    bash tests/phase7/verify_benefit_model.sh
    python3 -m unittest tests/phase7/test_decision_snapshot.py tests/phase7/test_pq42_ci.py
    bash tests/phase7/verify_noninterference.sh "$major" "$artifacts/noninterference"
    bash tests/explain_guard/verify.sh "$major"
    bash tests/shadow_evidence/verify.sh "$major"
}

frozen_corpus() {
    "$pgbin/psql" -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE EXTENSION pg_qi_spike_test;
SQL
    export PG_BIN="$pgbin" WP14_ARTIFACT_DIR="$artifacts/differential"
    mkdir -p "$WP14_ARTIFACT_DIR"
    bash scripts/wp14/run-differential.sh
    bash scripts/wp14/run-known-issues.sh
}

make -C tests/outer_hook PG_CONFIG="$pg_config" clean
make -C tests/outer_hook PG_CONFIG="$pg_config" -j2
make -C tests/outer_hook PG_CONFIG="$pg_config" install
trap 'rm -f "$libdir/pg_qi_outer_test.so"' EXIT
with_server "$artifacts/production" pg_duckdb,pg_qi_outer_test,pg_qi production_checks
rm -f "$libdir/pg_qi_outer_test.so"
trap - EXIT

bash tests/phase7/run-differential-integration.sh "$major" "$artifacts/pq31"
bash tests/phase7/verify_count_certificate.sh "$major" "$artifacts/count-certificate"
bash tests/phase4c/refusal-tests.sh
(
    phase4c_root="${RUNNER_TEMP:?RUNNER_TEMP is required}/pgqi4c-pg$major"
    collect_phase4c_evidence() {
        if [ -d "$phase4c_root" ]; then
            while IFS= read -r -d '' file; do
                install -D "$file" "$artifacts/phase4c/${file#"$phase4c_root"/}"
            done < <(find "$phase4c_root" -name data -prune -o -type f \
                \( -name '*.log' -o -name '*.txt' -o -name '*.tsv' -o -name '*.json' \
                   -o -name STATUS -o -name '*.sha256' \) -print0)
        fi
    }
    trap collect_phase4c_evidence EXIT
    export PG_QI_REJECT_NOT_RUN=1
    bash scripts/phase4c/run-production.sh "$major" "$phase4c_root"
    bash scripts/phase4c/run-rollback-drill.sh "$major" "$phase4c_root"
)
bash .github/scripts/ci-production-boundary.sh "$major" "$artifacts" production-boundary-final.txt

PG_QI_TESTING=1 bash .github/scripts/ci-build-install-pg-qi.sh "$major" "$artifacts/test-build"
WP9_MAJORS="$major" WP9_SKIP_BUILD_INSTALL=1 WP9_ARTIFACT_DIR="$artifacts/wp9" \
    bash scripts/spike1/wp9-matrix.sh
with_server "$artifacts/test" pg_duckdb,pg_qi frozen_corpus
