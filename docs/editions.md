# Open-source and commercial editions

## Open-source repository

All source code committed to this repository is licensed under Apache License 2.0. Once published under that licence, the granted rights are not withdrawn from existing versions.

The repository is developed as the open-source foundation of `pg_qi`. Its public interfaces must remain independently understandable, testable and safe for the functionality they expose.

## Separate commercial software

Baremon may distribute additional proprietary modules, binaries, services, certified builds or support under separate commercial terms. Such software is not licensed under Apache License 2.0 merely because it interoperates with this project.

Proprietary source code, licensing mechanisms, private policy logic, customer-specific rules and commercial build material must not be committed to this repository.

## Dependency direction

The intended dependency direction is:

```text
commercial module  ─────►  deliberate public pg_qi interface
open-source pg_qi  ──X──►  commercial implementation
```

The open-source build must never require proprietary headers, source files, generated artefacts or credentials.

The exact feature boundary may evolve before the first stable release. Any code already published here remains governed by its published open-source licence.
