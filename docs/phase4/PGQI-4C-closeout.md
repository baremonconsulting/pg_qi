# PGQI-4C closeout

Status: automated implementation and evidence gates passed; Human Review not
yet granted.

## Evidence matrix

| Gate | Local | CI PG16 | CI PG17 | CI PG18 | CI PG19 pinned |
|---|---|---|---|---|---|
| Production compile and boundary | PASS (PG17 exploratory) | PASS | PASS | PASS | PASS |
| Production smoke | PASS (PG17; foreign case NOT_RUN because local `file_fdw` is absent) | PASS | PASS | PASS | PASS |
| Security/lifecycle regression | PASS (PG17) | PASS | PASS | PASS | PASS |
| Rollback drill | PASS (PG17) | PASS | PASS | PASS | PASS |
| Manual request benchmark | PASS (short PG17 run) | PASS | PASS | PASS | PASS |

The required CI run completed 24/24 jobs successfully:
<https://github.com/baremonconsulting/pg_qi/actions/runs/44047628228>.
The separate production benchmark completed 4/4 PostgreSQL jobs successfully:
<https://github.com/baremonconsulting/pg_qi/actions/runs/44051490924>.
The earlier superseded CI attempt was cancelled after it exposed and prompted
removal of a non-portable runtime dependency on `rg`.

Five independent final reviews were performed. Their blocking findings were
iteratively corrected, including scenario completeness, full unordered result
validation, cleanup ownership, path/connection safety, installed-artifact
identity, cached-plan rollback, external force provenance, diagnostic
retention, classifier security coverage and portable tooling.

## Known limits retained

- default-off, superuser-only bounded policy;
- cached plans require deallocation or session retirement after disablement;
- no cancellation, retry, watchdog, fleet telemetry or automatic fallback;
- no inference of PostgreSQL execution from a negative detector result;
- PostgreSQL 19 evidence is for the repository's pinned build;
- timing on hosted infrastructure is descriptive, not a universal service
  level or performance certification.

## Human Review gate

The reviewer must confirm the exact revision, all required artifact cells,
open findings, cookbook commands and default-off policy. Merge, deployment and
pilot activation remain separate explicit decisions.
