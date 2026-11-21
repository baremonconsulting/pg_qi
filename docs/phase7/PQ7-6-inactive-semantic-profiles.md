# Inactive semantic profiles

PQ7-6 records four narrowly bounded semantic declarations for future
certification work. The declarations cover PostgreSQL's built-in `count(*)`,
`count(int4)`, `min(int4)`, and `max(int4)` aggregates over one ordinary
relation.

The declarations do not activate anything by themselves. Matching one at
planning produces semantic state `UNKNOWN` and leaves the activation barrier closed. A
match always records these unresolved conditions internally:

- the declaration is inactive;
- the loaded provider build identity has not been verified;
- snapshot equivalence has not been proved.

Catalog, permission, role, row-security, isolation, and assigned-transaction
identity checks can add further blockers. The observation does not execute
SQL, assign a transaction ID, call user code, or request delegation.

The declarations use exact built-in object identities and metadata:

| Profile | Aggregate OID | Input | Result | Aggregate sort operator |
|---|---:|---|---|---:|
| `count(*)` | 2803 | none | `int8` | none |
| `count(int4)` | 2147 | formal `any`, actual `int4` | `int8` | none |
| `min(int4)` | 2132 | `int4` | `int4` | 97 |
| `max(int4)` | 2116 | `int4` | `int4` | 521 |

All declared typmods are `-1` and all declared collations are invalid. Both
COUNT variants return zero on an empty input and report bigint overflow.
`COUNT(*)` counts rows regardless of nulls; `COUNT(expression)` counts only
non-null expression values.
MIN/MAX return a typed null for an empty or all-null input and preserve the
`int4` domain without an arithmetic overflow rule. The MIN/MAX sort operators
are aggregate catalog metadata; they do not admit operator expressions.

`TABLESAMPLE`, including `REPEATABLE`, remains outside these declarations.
Aggregate-derived `numeric` results also remain outside them because exact
type and decimal scale are part of the semantic contract. Neither case is
normalized into an apparently matching result.

Promotion requires a separate reviewed change with runtime build-identity
evidence, snapshot proof, cross-version differential evidence, and an explicit
activation decision. [PQ7-7](PQ7-7-count-semantic-certificate.md) defines the
separate, default-off execution-time qualification for `COUNT(*)` only.
