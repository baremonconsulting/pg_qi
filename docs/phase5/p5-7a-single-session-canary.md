# P5-7A single-session internal canary

This gate validates one operator-executed internal canary. It does not execute
SQL, grant `CANARY_ACCEPTED`, authorize production use, or authorize a customer
pilot. The operator remains responsible for the live session and the independent
human decision.

## Required execution order

1. Freeze `canary-plan.json` under a fresh, current `OPERATION_GO`. The plan must
   bind the measurement plan, level-one authorization, exact positive SQL bytes,
   expected fresh planning tuple, one direct session and no pool.
2. In that one dedicated session, prove ambient force-off first, execute the one
   approved positive literal, capture its fresh same-session planning snapshot,
   compare its complete spool with the approved oracle, and execute the
   unsupported-query and parameterized-query negative controls. Do not retry,
   reconnect, fall back, or replace the SQL after the run starts.
3. Preserve the truthful result in `canary-run.json`, including failures and
   incomplete runs. A verified plan is not evidence of successful execution.
4. Retire the owned backend, verify its absence from an authorized observer,
   then open a different fresh connection and prove mode `off`, force execution
   `off`, and PostgreSQL execution. Seal that evidence in
   `canary-retirement.json`.
5. Run the offline validator from outside the evidence directory:

       python3 tools/phase5/canary_runtime.py \
         --bundle /absolute/private/bundle \
         --ledger /absolute/write-once/canary-ledger

   The ledger must be the operator-approved, owned `0700` evidence ledger whose
   absolute path, device/inode identity and `ledger-id.json` artifact are frozen
   in `canary-plan.json`. Before validation, the command exclusively
   consumes the `(scope, canary_id, OPERATION_GO)` identity there. Moving or
   copying the bundle therefore cannot authorize a retry. The command also
   exclusively creates `canary-outcome.json` in the bundle on both
   PASS and refusal. An existing outcome blocks a second attempt under the same
   bundle and identity; never delete or replace it. A successful result is only
   `ELIGIBLE_FOR_HUMAN_GRANT`. The human reviewer
   must independently inspect the private evidence before granting
   `CANARY_ACCEPTED`.

## Refusal and stop rules

Any expired or mismatched GO, second session, pool, stale or missing snapshot,
unexpected engine, failed oracle, missing or reordered control, changed artifact,
partial execution, unverified backend exit, or non-default reconnect refuses the
gate. Preserve the failed evidence and stop. Never turn a failed run into a new
attempt under the same canary identity or GO.

The canary does not authorize customer access, production routing, publication,
persistent settings, a broader SQL subset, retry, fallback, or a performance or
value claim.
