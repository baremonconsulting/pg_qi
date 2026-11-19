# P5-7B bounded internal pilot and closeout

This step executes only the measurement levels frozen in the approved
measurement plan and then validates their private evidence as one bounded
internal pilot. It does not grant `INTERNAL_CLOSEOUT`, customer use,
production routing, publication, or a persistent allow-list.

## Authority boundary

Before the first window, the bundle must bind all of the following:

- the exact frozen measurement plan and current `OPERATION_GO`;
- a passing P5-7A canary validation and a separate human
  `CANARY_ACCEPTED` gate receipt bound to that validation digest;
- one ordered window for every approved concurrency level, starting at level
  one without gaps;
- the exact per-level authorization and a unique private evidence directory;
- the retention owner and, when pending, its due date.

The closeout tool has no database, process-control, retry, escalation, or
sharing interface. Operators execute each level with `measurement_runtime.py`.
The tool only recomputes the sealed evidence graph after execution.

## Window procedure

For every approved window, in order:

1. Confirm the same current operation approval and exact level authorization.
2. Run that single frozen level; never escalate automatically.
3. Stop admission after the window, including failed and partial windows.
4. Retire all owned sessions and cached plans.
5. Establish a fresh default-off/force-off reconnect.
6. Seal the raw stream, report, retirement and terminal state in a distinct
   private bundle. Never reuse a run ID or overwrite an earlier bundle.

The next window may start only after the previous window has finished. Every
window must end within the operation approval interval.

## Closeout decisions

The closeout keeps independent decisions for:

- safety;
- evidence completeness;
- exit and retirement;
- workload relevance;
- measured value;
- actual service work;
- project direction: continue, targeted change, pause, or stop.

Technical PASS requires every frozen level, complete private validation and a
clean verified retirement for every window. Administrative closure of a
failed or partial run is never PASS. A negative or inconclusive value result
is retained as a valid product result and is not rewritten into a technical
failure.

Retention may remain `PENDING` only with the frozen owner and due date.
Retirement itself may never remain unverified.

## Sharing

Private evidence remains private. `shared_derived_paths` must be empty unless
the plan contains a valid `SHARE_RELEASE` receipt for the exact scope. A share
release does not alter the private result and does not imply publication.

## Validation

The bundle root must be an owner-only mode-0700 directory containing:

- `internal-pilot-plan.json`;
- one immutable `internal_pilot_window` contract per approved level;
- the referenced private evidence bundles;
- `internal-pilot-closeout.json`.

Run:

```sh
python3 tools/phase5/internal_pilot_closeout.py \
  --bundle /private/pilot-closeout \
  --output /private/pilot-closeout-validation.json
```

The output is created exclusively and reports only
`ELIGIBLE_FOR_HUMAN_GRANT` or `REFUSED`. A human decision remains necessary
for `INTERNAL_CLOSEOUT`.

