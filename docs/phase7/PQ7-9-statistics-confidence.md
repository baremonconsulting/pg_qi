# PQ7-9 statistics confidence

PQ7-9 records bounded statistics-confidence evidence alongside the native
PostgreSQL plan. It does not run a second optimizer and does not calculate
selectivity from catalog samples.

The native plan estimates captured by PQ7-8 remain authoritative. The
confidence observer adds only ordinary evidence that PostgreSQL already
maintains for the relation:

- catalog `relpages` and `reltuples`;
- native scan rows and width;
- `n_mod_since_analyze`;
- whether the cumulative statistics system most recently recorded a manual or
  automatic `ANALYZE`;
- the non-negative age of that recorded event, sampled after planning.

The `ANALYZE` source and age are not provenance for the catalog estimates,
`pg_statistic`, or the statistics consumed by this particular plan. Catalog
statistics can change without those cumulative timestamps, and an `ANALYZE`
can commit between planning and observation. A later gate must not combine
these fields as if they identified the origin of the planner statistics.

Confidence remains `unknown` in this first implementation. PostgreSQL's
statistics counters are asynchronous, so a reported zero cannot prove that
another backend has not committed changes which are still waiting to be
flushed. When the relation has known catalog estimates, an analyze event is
recorded, its timestamp is not in the future, and the shared counter reports
zero changes, the reason is `freshness_unproven`: this is useful evidence but
not a freshness certificate. A nonzero counter yields
`modifications_reported`; it is not attributed to an interval because partial
ANALYZE and asynchronous flush can decouple the counter from the latest
reported timestamp. Missing analyze timestamps yield `no_recorded_analyze`,
which also covers an explicit statistics-counter reset and does not claim that
ANALYZE never ran. Missing, invalid, contended, disabled-counter, recovery and
own-XID states also fail closed with explicit reasons.

The observer reads the shared relation-statistics entry directly and uses a
non-waiting shared lock for the data copy. Locating the entry still uses
PostgreSQL's normal shared statistics hash and retains a backend-local entry
reference; it is not a general lock-free operation. It does not populate or
freeze PostgreSQL's per-transaction statistics snapshot. Because shared
counters can lag, neither a zero counter nor a recent analyze age may be
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
uses bits 1, 2, 4, 8, 16 and 32 for relation, native plan estimate, catalog
size, modification counter, recorded analyze source and recorded analyze age,
respectively. `source=none` with bit 16 set means that neither manual nor
automatic `ANALYZE` was recorded.

The reason values are `not_observed`, `plan_features_unknown`,
`catalog_estimate_unknown`, `counters_disabled`, `recovery_in_progress`,
`own_xid`, `stats_entry_missing`, `stats_entry_busy`,
`no_recorded_analyze`, `modifications_reported`, `freshness_unproven`,
`future_analyze` and `invalid_counter`. Non-complete record states are
`denied`, `withheld`, `none`, `unavailable`, `exhausted` and `invalid`.
Unavailable numeric fields are rendered as `unknown`.

The cumulative analyze fields are not transactional provenance. For example,
a rolled-back `ANALYZE` can still update the shared timestamp and counter even
though catalog statistics roll back. This is another reason that the observer
never promotes its result beyond `unknown`.

Extended statistics are validated by PostgreSQL-behaviour fixtures that run
in CI. The fixtures show that PostgreSQL's final estimates respond in the expected direction to
multicolumn `ndistinct`, functional dependencies, joint MCV statistics and
expression statistics. Raw MCV values and sampled catalog contents are not
published in telemetry. Tests compare relative estimate quality rather than
requiring sampled values to be identical across PostgreSQL majors.

`tests/phase7/verify_stats_confidence.sh` covers the unanalyzed, analyzed,
same-row-count distribution drift, own-transaction change, disabled-counter
and reanalyze lifecycle. It also verifies that capability and semantic
decisions do not change when statistics change.
