# PQ7-11 — conservative benefit model

PQ7-11 adds a pure, checked-integer comparison between a conservative native
PostgreSQL lower latency bound and a conservative delegated DuckDB upper
latency bound. It remains decision-only and cannot alter routing or resources.

## Profile v2

Profile format v2 adds calibrated `pg_ns_cost`, `output_ns_byte` and `p95_us`
terms. `threads` is the total profile envelope shared across the declared
`concurrency`. The slowdown factor is conservatively
`max(1, ceil(concurrency / threads))`; the model never assumes perfect thread
scaling. Fixed overhead is not scaled because `fixed_us` is calibrated for the
declared concurrency envelope. Scan, conversion and output coefficients are
calibrated as single-thread costs. All profile values remain build-, site-
and time-bound and are copied atomically with their evidence.

The generator rejects invalid identifiers, bounds and validity windows before
writing a profile. Format v1 is rejected rather than silently interpreted with
new units.

## Bounds

The adapter accepts only the bounded plain aggregate over sequential scan
shape from PQ7-8. Planner doubles must be finite, nonnegative and inside the
exact-integer conversion domain. Delegated quantities round up and the native
cost rounds down. Because PostgreSQL cost units are session-configurable, the
adapter abstains unless `seq_page_cost`, `cpu_tuple_cost` and
`cpu_operator_cost` retain the PostgreSQL defaults used for calibration.

The pure model uses checked uint64 arithmetic:

```
logical_scan_kb = max(relpages * block_kb,
                      ceil(scan_rows * scan_width / 1024))
conversion_rows = max(scan_rows, reltuples) + output_rows
contention_factor = max(1, ceil(concurrency / threads))

T_pg_low = floor(pg_total_cost) * pg_ns_cost
T_duck_high = fixed_us * 1000
              + (scan_work + conversion_work + output_work)
                * contention_factor
```

The absolute margin is strictly greater than both fixed overhead and the p95
dispersion value. The relative margin is compared by checked ppm cross
multiplication. Overflow, invalid input or the undecided margin band yields
UNKNOWN; values are never saturated or clamped.

## Trust gates

A positive result additionally requires confident statistics and verified
resource limits. PQ7-9 and PQ7-10 deliberately do not yet prove those states,
so live PQ7-11 observations abstain even when the pure numerical model would
be favourable. Synthetic contract tests exercise PASS and distinct UNKNOWN
outcomes without weakening production evidence. A numerical VETO would need a
DuckDB lower bound and PostgreSQL upper bound, which PQ7-11 does not claim.

`SHOW pg_qi.benefit_evidence` is private under the existing diagnostic-role
and session-identity checks. It reports the atomic event identity, state,
reason and derived bounds. Authorised readers may infer calibrated terms from
those bounds, so this diagnostic is not a coefficient-secrecy boundary.

## Spill estimates in shadow mode

`SHOW pg_qi.spill_evidence` reports the shape, estimated bytes, budget and
reason for the same private snapshot as `pg_qi.benefit_evidence`. It covers
fixed-width integer and Boolean values in grouped counts, COUNT(DISTINCT),
row DISTINCT and sorts over a simple sequential heap scan. Parallel plans,
additional scans, variable-width keys and unsupported aggregate states yield
an unknown estimate.

For ungrouped aggregates, the check looks for DISTINCT or ORDER BY inside
aggregate calls in the outer query.

The estimate uses the larger of the scan's row estimate and the catalogue's
row estimate. It charges each observed sort or hash stage for those rows.
For a hash stage it allows 64 bytes of overhead per row, plus the keys and
aggregate states. A sort allows 32 bytes in addition to the row payload.
A factor of two leaves room for row growth and duplicated state, with one
MiB of reserve per configured DuckDB thread. For a top-level sort the
estimate also allows one materialised result, sized from the estimated
output rows and projected column widths. These are conservative sizing
assumptions for the supported shapes. The resulting value is a planning
estimate and the catalogue's row count can be stale.

A positive `duckdb.memory_limit` provides the comparison budget, in MiB
on the tested provider. An automatic memory limit or thread count makes the
budget unknown.

The diagnostic reports the original sizing estimate in `unadjusted_bytes`.
It divides that value by 4 for grouped counts, 5.5 for DISTINCT and 2.5 for
sorts, then records the result rounded down to a whole byte in
`estimated_bytes`. These shape-specific thresholds come from calibration.
A calibrated estimate at or above the budget records likely spill. The
benefit model then requires evidence of filesystem access and available
space, so the planner records `spill_resources_unverified` while those
facts are missing. An unsupported estimate records
`spill_estimate_unknown`. A calibrated estimate below the budget leaves
the existing statistics, profile and resource gates in place.

## Non-interference

The benefit state is supplied to the existing V2 decision contract with the
activation barrier closed. No force state, plan pointer, execution engine,
resource setting or V1 behavior is changed.
