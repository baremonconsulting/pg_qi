# PGQI-4C closeout

Status: **COMPLETE — PASS**. Automated implementation and evidence gates,
Human Review, merge and post-merge CI have all completed successfully.

Authoritative merged revision:
`a7592e649bf8b8f345b83d15880c6497cbe65468` (pull request
[#14](https://github.com/baremonconsulting/pg_qi/pull/14), merged 2026-11-17).

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
The post-merge `main` run completed successfully against the authoritative
merge revision:
<https://github.com/baremonconsulting/pg_qi/actions/runs/44059931763>.
The earlier superseded CI attempt was cancelled after it exposed and prompted
removal of a non-portable runtime dependency on `rg`.

Five independent final reviews were performed. Their blocking findings were
iteratively corrected, including scenario completeness, full unordered result
validation, cleanup ownership, path/connection safety, installed-artefact
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

## Human Review and disposition

Human Review was granted on 2026-11-17 after confirmation of the exact
revision, required artefact cells, open findings, cookbook commands and the
default-off policy. Pull request #14 was subsequently merged into `main`, and
the post-merge CI run passed on the resulting merge revision.

PGQI-4C is therefore closed. This closeout does not authorise deployment or
pilot activation; either remains a separate explicit operational decision.
