# PGQI-5 qualification inventory and metrics

This document defines the human-readable TSV contract used by the inexpensive
qualification gate. Later machine contracts may encode the same semantics in a
different format; they must not weaken these denominator or authority rules.

## TSV serialization

`templates/natural-workload-inventory.tsv` is UTF-8 with one header and zero
or more records. It has exactly 21 tab-separated columns in the published
order. Fields must not contain literal tabs, CR or LF. `case_id` is a unique,
stable opaque identifier. Empty fields are invalid; use `UNKNOWN` or
`NOT_APPLICABLE` and put the explanation in `limitations`. Unknown enum values,
duplicate IDs and an incorrect field count are invalid.

Weights are non-negative decimal values in units declared by the charter:

- `frequency_weight` is the observed execution count in the frozen observation
  window;
- `baseline_time_weight` is the observed aggregate baseline elapsed time in
  the same window;
- `UNKNOWN` means it was not measured and must never be treated as zero;
- `NOT_APPLICABLE` is valid only for a non-natural control that is excluded
  from all natural coverage calculations.

## Field domains

- `inventory_origin`: `natural_internal`, `natural_customer`,
  `derived_reproduction` or `synthetic_control`.
- Each `*_source_ref` or `*_ref` is a safe opaque controlled reference, never
  query text, credentials, personal data or an endpoint.
- Each `*_authority` is `AUTHORISED`, `INVENTORY_ONLY`, `PROHIBITED`,
  `UNKNOWN` or `NOT_APPLICABLE`. Authority on one axis never grants authority
  on another.
- `executed_sql_origin` and `dataset_origin` use the same origin values plus
  `NOT_EXECUTED` and `NOT_APPLICABLE`.
- `protocol`: a frozen vocabulary in the charter, minimally distinguishing
  simple, extended and prepared execution; an unclassified value is `UNKNOWN`.
- `parameterisation`, `prepared_pool_profile`, `result_type_family` and
  `shape_family` use versioned descriptive vocabularies frozen in the charter;
  unclassified values are `UNKNOWN`, never a free-form guess.
- `eligibility`: `ELIGIBLE`, `UNSUPPORTED`, `NON_EXECUTABLE`, `OUT_OF_SCOPE`
  or `UNKNOWN`.
- `reason_family`: the frozen classifier reason family; `NOT_APPLICABLE` is
  allowed only for an `OUT_OF_SCOPE` control that the charter excludes from
  classification.
- `derivation_parent`: the parent `case_id`, `NOT_APPLICABLE` for an original
  item, or `UNKNOWN` when lineage is unresolved.

## Frozen population and formulas

The charter revision, observation window, natural-item identity and
deduplication rule, classifier build, inventory digest, metric definitions and
decision thresholds are frozen before evaluation. A later change creates a
new revision; it never rewrites the original target after results are known.

Let `N` be every distinct authorised inventory item whose inventory origin is
natural. Unsupported, non-executable, prohibited and unknown-eligibility items
remain in `N`. Derived reproductions and synthetic controls are not members of
`N`, even when linked to a natural parent.

- Structural coverage = count of `ELIGIBLE` items in `N` / count of `N`.
  When eligibility remains `UNKNOWN`, this is only the confirmed lower bound;
  the upper bound is (`ELIGIBLE` + `UNKNOWN`) / count of `N`. A threshold
  failure is conclusive only when the upper bound also fails it. Otherwise the
  decision is `INCONCLUSIVE`.
- Weight measured proportion = count of natural items with a known weight /
  count of `N`; report the known-weight sum beside it.
- When all weights are known, frequency-weighted coverage is the sum of
  frequency weights of `ELIGIBLE` items in `N` divided by the sum of frequency
  weights of all items in `N`. Baseline-time coverage is analogous. If the
  relevant known-weight sum is zero, weighted coverage is `NOT_APPLICABLE` and
  cannot satisfy a positive evidence threshold; no division is performed.
- When all weights are known but eligibility remains `UNKNOWN`, the weighted
  eligible-only value is a lower bound and the upper bound additionally counts
  the weights of `UNKNOWN` items. A weighted threshold failure is conclusive
  only when that upper bound fails; otherwise the decision is `INCONCLUSIVE`.
- When any required weight is unknown, the corresponding full-denominator
  weighted coverage is `UNKNOWN`; a clearly labelled known-subset diagnostic
  may be reported but cannot satisfy a threshold.
- Missing-evidence proportion is the count of natural items missing any input
  required by the selected metric divided by count of `N`.
- If `N` is empty, every natural coverage is `NOT_APPLICABLE` and relevance is
  `INCONCLUSIVE`, not zero and not a PASS.

Thresholds not frozen before evaluation make the related decision
`INCONCLUSIVE`. If an authorised non-empty natural inventory has zero eligible
items and every natural eligibility classification is resolved, natural
relevance is zero and the value path must end as `QUALIFICATION_STOPPED` before
a runner is built or used for that path. Zero confirmed eligible items while
any classification remains `UNKNOWN` is `INCONCLUSIVE`, not proven zero. The
separate `PQ-2` offline implementation mandate may still be completed, but it
must never be represented as workload relevance.

## Synthetic verification fixture

Use a frozen natural denominator of four items: one `ELIGIBLE`, one
`UNSUPPORTED`, one `NON_EXECUTABLE` and one `UNKNOWN`. Add one derived SQL
reproduction of the eligible item over a synthetic dataset. The reproduction
has three distinct origins and authorities and is excluded from `N`.

Confirmed structural coverage is `1/4 = 25%`, with an upper bound of `2/4 =
50%` because one eligibility is unresolved. If only two natural items have
known weights, weight measured proportion is `2/4 = 50%` and full-denominator
weighted coverage is `UNKNOWN`. A second fixture has the same four natural
items, no `ELIGIBLE` item and no unresolved eligibility; it produces zero
structural relevance and `QUALIFICATION_STOPPED`. A fixture with no confirmed
eligible item but at least one `UNKNOWN` eligibility is `INCONCLUSIVE`. An empty
natural inventory also produces `INCONCLUSIVE`.

The derived reproduction may prove harness behaviour only. It does not improve
natural coverage or prove performance on the original dataset.
