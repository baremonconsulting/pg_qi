# Contributing to pg_qi

Thank you for considering a contribution to `pg_qi`.

## Project stage

The project is pre-alpha. Interfaces and file layout may change. Discuss substantial architectural work in an issue before implementation.

## Development environment

Install PostgreSQL server development packages for a supported major version, then run:

```bash
make
sudo make install
make installcheck
```

The initial support matrix is PostgreSQL 16–18.

## Pull requests

A pull request should:

- solve one clearly described problem;
- preserve the PostgreSQL hook chain;
- include tests for observable behaviour;
- document version-specific behaviour;
- avoid allocations or catalogue access in critical paths unless justified;
- explain error-path and memory-context behaviour where relevant;
- update the changelog for user-visible changes.

## Coding conventions

- Write portable C compatible with the supported PostgreSQL versions.
- Follow PostgreSQL naming and formatting conventions where practical.
- Prefix externally visible symbols with `pg_qi_` or `qi_`.
- Treat compiler warnings as defects.
- Avoid undefined behaviour and implicit ownership rules.
- Add comments for invariants and non-obvious PostgreSQL lifecycle constraints.

## Licence and provenance

All contributions accepted into this repository are licensed under Apache License 2.0.

By submitting a contribution, you confirm that you have the right to submit it under that licence. Do not submit proprietary code, confidential information, customer query text, customer schema names, credentials or data values.

Third-party code must have a compatible licence and clear attribution.

## Security

Do not report vulnerabilities publicly. Follow [SECURITY.md](SECURITY.md).
