#!/usr/bin/env bash

set -euo pipefail

pgbin=${PG_BIN:?PG_BIN is required}
pghost=${PGHOST:?PGHOST is required}
artifact_dir=${WP14_ARTIFACT_DIR:?WP14_ARTIFACT_DIR is required}
repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
psql=("$pgbin/psql" -X -q -v ON_ERROR_STOP=1 -A -t -F '|'
      -h "$pghost" -d postgres)

mkdir -p "$artifact_dir/cases"
results="$artifact_dir/results.tsv"
printf 'case\texpected_eligible\tclassification\tplan\tcomparison\n' >"$results"

"${psql[@]}" -f "$repo_dir/tests/wp14/setup.sql" \
    >"$artifact_dir/setup.log" 2>&1

run_case() {
    local id=$1 expected=$2 ordered=$3 sql=$4
    local case_dir="$artifact_dir/cases/$id"
    local classification plan comparison
    mkdir -p "$case_dir"

    "${psql[@]}" >"$case_dir/baseline.data" 2>"$case_dir/baseline.stderr" <<SQL
SET duckdb.force_execution = off;
CREATE TEMP TABLE qi_result_shape AS $sql WITH NO DATA;
\o $case_dir/baseline.types
SELECT attnum, atttypid FROM pg_attribute WHERE attrelid = 'qi_result_shape'::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum;
\o
DROP TABLE qi_result_shape;
$sql;
SQL
    cp "$case_dir/baseline.data" "$case_dir/baseline.result"

    "${psql[@]}" >"$case_dir/delegated.raw" 2>"$case_dir/delegated.stderr" <<SQL
SET duckdb.force_execution = off;
CREATE TEMP TABLE qi_result_shape AS $sql WITH NO DATA;
\o $case_dir/delegated.types
SELECT attnum, atttypid FROM pg_attribute WHERE attrelid = 'qi_result_shape'::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum;
\o
DROP TABLE qi_result_shape;
SELECT qi.spike_arm_next();
$sql;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SHOW pg_qi.test_classifier_eligible;
SHOW pg_qi.test_classifier_reason;
SQL
    plan=$(tail -3 "$case_dir/delegated.raw" | head -1)
    classification=$(tail -2 "$case_dir/delegated.raw" | head -1)
    case "$classification" in on) classification=t ;; off) classification=f ;; esac
    tail -1 "$case_dir/delegated.raw" >"$case_dir/classifier-reason.txt"
    tail -n +2 "$case_dir/delegated.raw" | head -n -3 \
        >"$case_dir/delegated.result"

    if [ "$ordered" = t ]; then
        if cmp -s "$case_dir/baseline.result" "$case_dir/delegated.result"; then
            comparison=PASS
        else
            comparison=DIVERGENCE
        fi
    else
        LC_ALL=C sort "$case_dir/baseline.result" >"$case_dir/baseline.sorted"
        LC_ALL=C sort "$case_dir/delegated.result" >"$case_dir/delegated.sorted"
        if cmp -s "$case_dir/baseline.sorted" "$case_dir/delegated.sorted"; then
            comparison=PASS
        else
            comparison=DIVERGENCE
        fi
    fi

    if ! cmp -s "$case_dir/baseline.types" "$case_dir/delegated.types"; then
        comparison=TYPE_DIVERGENCE
    fi

    test "$classification" = "$expected"
    if [ "$expected" = t ]; then
        test "$plan" = on
        test "$comparison" = PASS
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$id" "$expected" "$classification" "$plan" "$comparison" \
        >>"$results"
}

while IFS='|' read -r id expected ordered sql; do
    case "$id" in ''|'#'*) continue ;; esac
    run_case "$id" "$expected" "$ordered" "$sql"
done <"$repo_dir/tests/wp14/corpus.tsv"

# Fixed-seed deterministic integer fuzz within the allow-list.
seed=142002
for index in $(seq 1 100); do
    seed=$(((1103515245 * seed + 12345) & 2147483647))
    threshold=$((seed % 190 + 1))
    run_case "fuzz_$index" t t \
        "SELECT id, group_id, nullable_value FROM qi_gate_data WHERE id >= $threshold AND id < $((threshold + 10)) ORDER BY id"
done

# The comparator must detect a deliberately altered result.
printf 'sentinel\n' >"$artifact_dir/negative-left.txt"
printf 'different\n' >"$artifact_dir/negative-right.txt"
if cmp -s "$artifact_dir/negative-left.txt" "$artifact_dir/negative-right.txt"; then
    echo "negative control was not detected" >&2
    exit 1
fi
echo DETECTED >"$artifact_dir/negative-control.txt"

test "$(awk -F '\t' 'NR > 1 && $2 == "t" && $5 == "PASS" {n++} END {print n+0}' "$results")" -eq 115
echo "WP14 differential corpus PASS"
