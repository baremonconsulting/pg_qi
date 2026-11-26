# PQ7-9 statistics confidence

PQ7-9 records a bounded set of statistics confidence fields alongside the
native PostgreSQL plan. It uses relation statistics that PostgreSQL already
maintains and leaves selectivity estimation to the native planner.

The native plan estimates captured by PQ7-8 remain authoritative. The
confidence observer adds only ordinary evidence that PostgreSQL already
maintains for the relation:

- catalogue `relpages` and `reltuples`;
- native scan rows and width;
- `n_mod_since_analyze`;
- whether the cumulative statistics system most recently recorded a manual or
  automatic `ANALYZE`;
- the non-negative age of that recorded event, sampled after planning.

The `ANALYZE` source and age are not provenance for the catalogue estimates,
`pg_statistic`, or the statistics consumed by this particular plan. Catalogue
statistics can change without those cumulative timestamps, and an `ANALYZE`
can commit between planning and observation. A later gate must not combine
these fields as if they identified the origin of the planner statistics.

Confidence remains `unknown` because PostgreSQL flushes statistics counters
asynchronously. Another backend can commit changes before the shared counter
reflects them. When catalogue estimates are known, an `ANALYZE` timestamp
exists at or before the observation and the counter is zero, the reason is
`freshness_unproven`. A non-zero counter yields `modifications_reported`.
Partial `ANALYZE` and asynchronous flush can put the timestamp and counter in
different intervals. A missing timestamp yields `no_recorded_analyze` and may
reflect a statistics-counter reset. Missing, invalid, contended,
disabled-counter, recovery and own-XID states also fail closed with explicit
reasons.

The observer reads the shared relation-statistics entry directly and uses a
non-waiting shared lock for the data copy. Locating the entry still uses
PostgreSQL's normal shared statistics hash and retains a backend-local entry
reference; it is not a general lock-free operation. It does not populate or
freeze PostgreSQL's per-transaction statistics snapshot. Because shared
counters can lag, neither a zero counter nor a recent `ANALYZE` event may be
promoted to `current` by later gates without an additional freshness
mechanism.

The private `SHOW pg_qi.stats_confidence` record is available under the same
diagnostic-role and session-identity rules as the other V2 records. Its
`cid`, `g` and `e` fields identify the same atomic planning event as
`pg_qi.decision_v2` and `pg_qi.plan_features`. It is diagnostic evidence only
and does not affect routing in this step.

### Diagnostic record

The semicolon-delimited record is versioned with `v=1`. A complete record
contains `cid`, `g` and `e`, followed by `state`, `reason`, `known`,
`relation`, `source`, `catalog_relpages`, `catalog_reltuples`, `scan_rows`,
`scan_width`, `n_mod_since_analyze` and `analyze_age_ms`. The `known` mask
uses bits 1, 2, 4, 8, 16 and 32 for relation, native plan estimate, catalogue
size, modification counter, recorded `ANALYZE` source and age,
respectively. `source=none` with bit 16 set means that neither manual nor
automatic `ANALYZE` was recorded.

The reason values are `not_observed`, `plan_features_unknown`,
`catalog_estimate_unknown`, `counters_disabled`, `recovery_in_progress`,
`own_xid`, `stats_entry_missing`, `stats_entry_busy`,
`no_recorded_analyze`, `modifications_reported`, `freshness_unproven`,
`future_analyze` and `invalid_counter`. Non-complete record states are
`denied`, `withheld`, `none`, `unavailable`, `exhausted` and `invalid`.
Unavailable numeric fields are rendered as `unknown`.

A rolled-back `ANALYZE` can still update the shared timestamp and counter even
though catalogue statistics roll back. The observer therefore keeps its
confidence at `unknown`.

Extended statistics are validated by PostgreSQL-behaviour fixtures that run
in CI. The fixtures show that PostgreSQL's final estimates respond in the expected direction to
multicolumn `ndistinct`, functional dependencies, joint MCV statistics and
expression statistics. Raw MCV values and sampled catalogue contents are not
published in telemetry. Tests compare relative estimate quality rather than
requiring sampled values to be identical across PostgreSQL majors.

`tests/phase7/verify_stats_confidence.sh` covers a relation before and after
`ANALYZE`, changes in distribution at the same row count, changes in the
current transaction, disabled counters and a second `ANALYZE`. It checks
that capability and semantic decisions stay consistent as statistics change.
