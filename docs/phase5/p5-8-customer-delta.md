# P5-8 conditional customer non-production delta

This tranche prepares and validates a customer-specific delta dossier. It does
not connect to a customer site, execute SQL, grant `CUSTOMER_OPERATION_GO`,
share evidence or publish results. With no named customer and mandate, the
authoritative result is `NOT_RUN`, not an incomplete internal pass.

## Authority graph

Internal, customer, sharing and publication authority are independent:

1. an exact internal scope, a passing P5-7B validation and a human
   `INTERNAL_CLOSEOUT` receipt bound to that validation;
2. a new customer charter and `CUSTOMER_PILOT_MANDATE` bound to its digest;
3. fresh evidence and receipts for every gate affected by the declared delta;
4. a time-bounded `CUSTOMER_OPERATION_GO` for one revised scope;
5. separate `SHARE_RELEASE` and `PUBLICATION_RELEASE` decisions.

An internal technical PASS or positive value result satisfies none of items
2–5. Existing runtimes continue to accept only internal `OPERATION_GO`.
Customer-origin SQL or data is refused by the internal evidence path.

## Dossier

The dossier freezes a non-identifying `cust-<sha256>` reference, charter revision, one site,
target/build/pool identities, workload and data authority, evidence policy,
recovery, support and incident procedures, five independent role
approvals, a maximum fourteen-day non-production window, the exact changes and
the affected gates. Production scope is fixed to `NONE`.

Role bindings and the charter window are typed customer-scope inputs and must
match the dossier exactly. The customer GO is bound to the dossier and mandate
digests. Each rerun uses gate-specific, customer-scope evidence; site and
recovery gates use their dedicated preflight and recovery contracts.

The validator loads both complete scope contracts, compares every one of their
thirteen input artifact digests and derives the delta and affected-gate table.
The declared delta must match that result exactly, and workload, dataset and
target must all be customer revisions. Workload, dataset, release, target,
limits and any other changed scope input repeat their mapped qualification,
data, release, toolkit, oracle, resource, recovery and operation gates. A
copied internal receipt, stale scope, missing gate or invented unchanged delta
is refused. `CUSTOMER_OPERATION_GO` is additionally bound to a passing
customer-scope preflight and recovery validation.

## Current NOT_RUN closeout

`docs/phase5/current/p5-8-customer-delta-not-run.json` records the current
state: no customer, mandate, sponsor decision or internal closeout grant; no
customer gate ran; no customer environment was touched; no sharing or
publication authority exists. `customer_delta.py` recomputes that result.

```sh
python3 tools/phase5/customer_delta.py \
  --bundle /private/customer-delta \
  --output /private/customer-delta-validation.json
```

Even a complete future dossier returns `NOT_RUN / REFUSED` in this tranche
because the customer execution path is deliberately unavailable. Enabling any
customer run requires a separately reviewed implementation and authority
change. A fully prepared but unexecuted environment must be retired/off;
`NOT_TOUCHED` is reserved for the empty no-customer state.
The prepared closeout is accepted only after the bounded GO has expired and
the prepared environment is recorded as `RETIRED_OFF`.

## Confidentiality and external attestations

Customer SQL, data, identity, logo/quotes and results remain confidential.
The validator contains no interface for exporting them. It can verify declared
site and non-production bindings but cannot independently observe that a real
environment is non-production; that property requires external attestation.

## Closeout content

The closeout records failures, explicit NOT_RUN reasons, limits on
generalisation and the final environment state. A customer window is rejected
in this implementation. Share and publication receipts are also rejected here;
they require their own exact package, recipient/channel and consent workflow.
