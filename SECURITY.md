# Security Policy

## Supported versions

Until the first stable release, security fixes are provided only for the latest code on the `main` branch and the latest published pre-release, where applicable.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability.

Use GitHub Private Vulnerability Reporting through the repository's **Security** tab. If that channel is unavailable, contact Baremon through <https://www.baremon.eu/contact/> and clearly mark the message as a confidential security report.

Include, where possible:

- PostgreSQL version;
- `pg_qi` version or commit;
- `pg_duckdb` version, when relevant;
- operating system and architecture;
- minimal reproduction steps;
- expected impact;
- whether the issue can crash a backend, corrupt memory, bypass policy or expose data.

Do not include production credentials, customer SQL text or customer data.
