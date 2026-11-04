# WP2 — preflight pg_duckdb GUC

Datum: 2026-10-14

## Cíl

Ověřit při preloadu, že `duckdb.force_execution` je skutečný, resetovatelný GUC
registrovaný pg_duckdb, a připravit verzově správný zápis přes
`GUC_ACTION_SAVE`.

## Implementace

- `src/include/pg_qi/qi_delegation.h` publikuje malé interní rozhraní.
- `src/qi_delegation.c` implementuje preflight a save setter.
- PostgreSQL 17/18 používá `get_config_handle()` jako důkaz existence a
  `set_config_with_handle()` pro zápis.
- PostgreSQL 16 používá `GetConfigOptionFlags(name, false)`, kontrolu
  `GUC_CUSTOM_PLACEHOLDER` a name-based `set_config_option()`.
- Všechny verze odmítají `GUC_NO_RESET`.
- Preflight běží pouze při `shared_preload_libraries`; běžné `CREATE EXTENSION`
  znovu neinstaluje hook.
- `qi.test_force_meta()` vrací preflight metadata pro S0.
- `qi.test_force_save_roundtrip()` přímo ověřuje změnu a obnovu GUC v novém
  GUC nest levelu pro oba směry.

## Výsledky

| Verze | Preflight | Handle | Placeholder | GUC_NO_RESET | SAVE roundtrip | Regrese |
|---|---:|---:|---:|---:|---:|---:|
| PostgreSQL 16.15 | PASS | N/A | false | false | PASS | 1/1 PASS |
| PostgreSQL 17.11 | PASS | přítomen | false | false | PASS | 1/1 PASS |
| PostgreSQL 18.6 | PASS | přítomen | false | false | PASS | 1/1 PASS |

Pozorované flags byly ve všech verzích `64`; flags jsou pouze diagnostika,
nikoli důkaz existence.

## Negativní testy

- Bez pg_duckdb server ve všech verzích správně odmítl start.
- Při pořadí `pg_qi,pg_duckdb` server ve všech verzích správně odmítl start.
- PG17/18 vrací vlastní chybu s hintem k pořadí preloadu.
- PG16 vrací core chybu `unrecognized configuration parameter`, protože
  požadovaný `GetConfigOptionFlags(name, false)` vyhodí ERROR ještě před
  následnou kontrolou placeholder flagu. Toto je očekávaný verzový rozdíl,
  nikoli tiché přijetí chybné konfigurace.

## Reprodukovatelnost

Pin pg_duckdb: `ee38d3b540ecea1d93683ba99bdcec5632a21eaf`.

Jeho DuckDB submodule je release `v1.5.4` na `08e34c447b`. Kvůli 3,8 GB RAM
hosta byla použita oficiální binární knihovna DuckDB v1.5.4; archiv má
SHA-256 `838d98a85e697bab9935010c88a8c67d3312ccedcab4cb4a0ba01da65113bb70`.
Integrační vrstva pg_duckdb byla sestavena z pinovaného zdroje samostatně pro
PG16/17/18.

## Nálezy při implementaci

1. SQL entry pointy v odděleném C modulu vyžadují explicitní `PGDLLEXPORT`,
   protože PGXS používá skrytou viditelnost symbolů.
2. `set_config_*` může vrátit `-1`, pokud hodnota nebyla aplikována; setter
   proto považuje každý výsledek `<= 0` za chybu.
3. Při přepínání PGXS verzí je povinné pořadí `clean → build → install`, jinak
   lze omylem zkopírovat knihovnu sestavenou proti jiné major verzi.

## Verdikt

**WP2: PASS.** Pozitivní i negativní preload kontrakt a obě varianty
`GUC_ACTION_SAVE` byly prokázány na PostgreSQL 16, 17 a 18.
