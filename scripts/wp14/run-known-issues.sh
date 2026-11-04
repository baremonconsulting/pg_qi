#!/usr/bin/env bash

set -euo pipefail

pgbin=${PG_BIN:?PG_BIN is required}
pghost=${PGHOST:?PGHOST is required}
artifact_dir=${WP14_ARTIFACT_DIR:?WP14_ARTIFACT_DIR is required}
repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
psql=("$pgbin/psql" -X -q -A -t -F '|' -h "$pghost" -d postgres)

mkdir -p "$artifact_dir/issues"
summary="$artifact_dir/known-issues.tsv"
printf 'issue\tclassifier_eligible\treason\treproduction\n' >"$summary"

while IFS='|' read -r issue sql; do
    case "$issue" in ''|'#'*) continue ;; esac
    case_dir="$artifact_dir/issues/$issue"
    mkdir -p "$case_dir"

    "${psql[@]}" >"$case_dir/baseline.out" 2>"$case_dir/baseline.err" <<SQL
SET duckdb.force_execution = off;
$sql;
SQL
    "${psql[@]}" >"$case_dir/delegated.raw" 2>"$case_dir/delegated.err" <<SQL
SET duckdb.force_execution = off;
SELECT qi.spike_arm_next();
$sql;
SHOW pg_qi.test_classifier_eligible;
SHOW pg_qi.test_classifier_reason;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SQL

    eligible=$(tail -3 "$case_dir/delegated.raw" | head -1)
    case "$eligible" in on) eligible=t ;; off) eligible=f ;; esac
    reason=$(tail -2 "$case_dir/delegated.raw" | head -1)
    test "$eligible" = f
    if cmp -s "$case_dir/baseline.out" \
              <(sed '1d' "$case_dir/delegated.raw" | head -n -3) &&
       cmp -s "$case_dir/baseline.err" "$case_dir/delegated.err"; then
        reproduction=NOT_REPRODUCED
    else
        reproduction=REPRODUCED
    fi
    printf '%s\t%s\t%s\t%s\n' \
        "$issue" "$eligible" "$reason" "$reproduction" >>"$summary"
done <"$repo_dir/tests/wp14/known-issues.tsv"

test "$(awk -F '\t' 'NR > 1 && $2 == "f" {n++} END {print n+0}' "$summary")" -eq 5
echo "WP14 known-issue exclusions PASS"
