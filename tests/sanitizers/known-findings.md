# Known sanitiser findings

## PostgreSQL indirect calls under UBSan

PostgreSQL 18.7 at `f5c41f6681c910b3f69951bf6fb97c6d74605d25` aborts
during `initdb` when built with Ubuntu Clang 18.1.3 and the UBSan function
check enabled. The bootstrap reaches `hash_search_with_hash_value` in
`src/backend/utils/hash/dynahash.c` through the time-zone setup. UBSan
reports that the call to `string_compare` uses an incompatible function
pointer type. The locally verified original binary had ELF Build ID
`51f59bf946b4c1712939ba44988b987a6a7c124e`.

To reproduce, build that PostgreSQL revision with the undefined-mode flags
from the CI source builder and omit `-fno-sanitize=function`. Run its
`initdb` against a new disposable data directory. The report occurs during
PostgreSQL bootstrap before either extension is loaded.

The buildfarm reported the same failure. The follow-up found more callback
type mismatches and verified the compiler option used here.
[PostgreSQL buildfarm report](https://www.postgresql.org/message-id/6m45hib2ne5yiuxqopaa7y72e2arnc3kvc2mb6wjqpgtfpdo7y%40fcjinyz7gnt3)
· [Upstream follow-up](https://www.postgresql.org/message-id/x2bbwu5hkqie4s7iyi5k7qiipwi2cpkhe6zquwmalt2ptukywf%40sbtmpbfo6zxg)

The undefined build uses `-fno-sanitize=function` as a temporary exception.
PGXS passes the flag to pg_duckdb and pg_qi in that job. The job treats all
other enabled checks as fatal. Remove the exception once a later PostgreSQL
pin passes bootstrap and the instrumented suite with function checking enabled.
