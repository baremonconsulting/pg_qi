# Spike 1 — zdrojový recon

Datum ověření: 2026-10-14

Tento dokument zachycuje fakta ověřená přímo ve zdrojovém kódu. Nejde o běhové
výsledky Spike 1. Běhové scénáře začnou až v pozdějších pracovních balíčcích.

## Pinované zdroje

### pg_qi

- repozitář: `baremonconsulting/pg_qi`
- výchozí commit: `d3529d4e6a7cd71657bbf8279c533a3c5a81e2df`

### pg_duckdb

- repozitář: `duckdb/pg_duckdb`
- commit: `ee38d3b540ecea1d93683ba99bdcec5632a21eaf`
- commit message: `Support INSERT ... INTO ... SELECT ... for Postgres table (#688)`

### PostgreSQL

Commity stabilních větví použité při reconu:

- `REL_16_STABLE`: `333d566c4f51f1b8b002175032586423c581d3d6`
- `REL_17_STABLE`: `9e8f4ce286bf03306fc0ad29374095782b2a8136`
- `REL_18_STABLE`: `70ffc30e12b1de43d6160682fc44c78b8de00f24`
- `master`: `2246c04e7b99519010cd47be636273a5700b0b0d`

Tyto SHA jsou důkazním snapshotem tohoto reconu. Pozdější compile matrix může
použít aktuální minor release packages, ale odchylku musí zaznamenat.

## G1 — planner API a kompatibilita

### Ověřené tvrzení G1.1

PostgreSQL 16, 17 a 18 mají čtyřargumentové `planner_hook_type` a
`standard_planner`:

```text
Query *parse,
const char *query_string,
int cursorOptions,
ParamListInfo boundParams
```

Zdroj: `src/include/optimizer/planner.h` ve větvích `REL_16_STABLE`,
`REL_17_STABLE` a `REL_18_STABLE`.

### Ověřené tvrzení G1.2

PostgreSQL `master` má pět argumentů a přidává `ExplainState *es`.

Zdroj: `src/include/optimizer/planner.h` na výše uvedeném master SHA.

### Ověřené tvrzení G1.3

Pinovaný pg_duckdb používá stejnou hranici:

```c
#if PG_VERSION_NUM >= 190000
```

Zdroj: `src/pgduckdb_hooks.cpp:257-310`.

### Závěr G1

Compatibility shim pg_qi použije hranici `PG_VERSION_NUM >= 190000`.
Pro PG 16–18 nesmí definovat falešný `ExplainState`. Compile matrix je regresní
kontrola, ne mechanismus pro objevování podpisu.

## G2 — pg_duckdb integrační fakta

### Ověřené tvrzení G2.1 — force GUC

`duckdb.force_execution` je bool GUC registrovaný s kontextem `PGC_USERSET` a
flag `GUC_REPORT`.

Zdroj: `src/pgduckdb_guc.cpp:147-154`.

### Ověřené tvrzení G2.2 — funkce a bezpečnostní GUC

`duckdb.unsafe_allow_execution_inside_functions` je bool GUC s kontextem
`PGC_SUSET`. Pokud `executor_nest_level > 0` a tento GUC je false,
`IsAllowedStatement()` odmítne DuckDB execution.

Zdroje:

- `src/pgduckdb_guc.cpp:153-154`
- `src/pgduckdb_hooks.cpp:237-240`

### Ověřené tvrzení G2.3 — dotaz musí mít FROM

Force cesta vrací true pouze pokud současně platí:

- `duckdb_force_execution` je true;
- statement je povolený;
- `ContainsFromClause(query)` je true.

Zdroj: `src/pgduckdb_hooks.cpp:116-200`.

Důsledek: `SELECT 1` není validní důkaz delegace. Testovací dotazy používají
například `SELECT count(*) FROM qi_bench_t`.

### Ověřené tvrzení G2.4 — force se nečte v executor hooku

Výskyty `duckdb_force_execution` na pinovaném SHA jsou omezené na:

- deklaraci/registraci GUC;
- planner rozhodnutí v `ShouldTryToUseDuckdbExecution()`;
- COPY utility cestu.

Executor hooky určují druh už hotového plánu přes `IsDuckdbPlan()`; force GUC
znovu nečtou.

Zdroje:

- `src/pgduckdb_hooks.cpp:178-200`
- `src/utility/copy.cpp:377`
- `src/pgduckdb_hooks.cpp:313-337`

### Ověřené tvrzení G2.5 — fallback

Force větev volá `DuckdbPlanNode(..., false)`. Pokud nevznikne DuckDB plán,
pokračuje se fallbackem na předchozí planner hook. Naproti tomu dotaz, který
DuckDB vyžaduje, volá `DuckdbPlanNode(..., true)`.

Zdroj: `src/pgduckdb_hooks.cpp:264-297`.

### Ověřené tvrzení G2.6 — hook chaining

Při inicializaci pg_duckdb:

```c
prev_planner_hook = planner_hook ? planner_hook : standard_planner;
planner_hook = DuckdbPlannerHook;
```

Zdroj: `src/pgduckdb_hooks.cpp:506-510`.

Při preload pořadí `shared_preload_libraries='pg_duckdb,pg_qi'` se pg_qi načte
po pg_duckdb a uloží jeho hook jako downstream. Toto musí ještě potvrdit S0 za
běhu; recon sám běhový důkaz nenahrazuje.

### Ověřené tvrzení G2.7 — provider a Material wrapper

Provider name je přesně `DuckDBScan` pro scan i executor methods.

Zdroj: `src/pgduckdb_node.cpp:437-446`.

`IsDuckdbPlan()` výslovně rozbaluje kořenový `Material`, než kontroluje
`CustomScan`. Root-only detector je proto nedostatečný.

Zdroj: `src/pgduckdb_hooks.cpp:313-337`.

### Ověřené tvrzení G2.8 — EXPLAIN property

Přesný název doplňkové EXPLAIN vlastnosti je `DuckDB Execution Plan`.

Zdroje:

- `src/pgduckdb_node.cpp:411-414`
- `test/pycheck/explain_test.py:63-81`

Tato vlastnost je pouze podpůrná. Autoritativním oraclem Spike 1 bude C detector.

### Ověřené tvrzení G2.9 — podporované řady

Pinovaný pg_duckdb má Docker build matrix pro PostgreSQL 14, 15, 16, 17 a 18.

Zdroj: `.github/workflows/docker.yaml:23-24` a `:105-106`.

## GUC API fakta PostgreSQL

### Ověřené tvrzení GUC.1

`GetConfigOptionFlags(name, true)` vrací při chybějícím GUC nulu. Nula tedy
není důkaz existence. S `missing_ok=false` funkce vyvolá ERROR.

Zdroj: `src/backend/utils/misc/guc.c`, komentář a implementace
`GetConfigOptionFlags()` ve větvích PG 16, 17 a 18.

### Ověřené tvrzení GUC.2

`get_config_handle()` a `set_config_with_handle()` jsou ve veřejném `guc.h` v
PG 17 a PG 18, ale nejsou v PG 16.

Zdroj: `src/include/utils/guc.h` v příslušných větvích.

### Ověřené tvrzení GUC.3

`get_config_handle()` vrací handle pouze pro nalezený ne-placeholder GUC;
jinak vrací NULL.

Zdroj: `src/backend/utils/misc/guc.c`, implementace `get_config_handle()` v
PG 17 a PG 18.

### Ověřené tvrzení GUC.4

`GUC_NO_RESET` znamená zákaz RESET/SAVE a `GUC_CUSTOM_PLACEHOLDER` identifikuje
placeholder. Oba flagy jsou veřejné ve všech třech podporovaných řadách.

Zdroj: `src/include/utils/guc.h` v PG 16, 17 a 18.

## Error handling fakta PostgreSQL

### Ověřené tvrzení ERR.1

PostgreSQL výslovně požaduje `volatile` pro lokální proměnnou, která je změněna
v `PG_TRY` a použita v `PG_CATCH`. Dokumentace zároveň varuje, že recovery kód
má zůstat malý a nový ERROR v něm má omezenou bezpečnost.

Zdroj: `src/include/utils/elog.h:317-382` v `REL_17_STABLE`; pravidlo je
součástí podporovaných větví.

### Návrhový důsledek

- cleanup vlastního rámce nebude alokovat ani vyhazovat chybu;
- po push nebude failpoint schopný ERROR umístěn mimo `PG_TRY`;
- budoucí lokální subtransaction helper použije `volatile bool ok`.

## WP0 exit gate

| Kontrola | Stav |
|---|---|
| Pin pg_qi zaznamenán | PASS |
| Pin pg_duckdb zaznamenán a dostupný | PASS |
| PG 16/17/18 planner podpis ověřen | PASS |
| PG19 hranice ověřena proti master a pg_duckdb | PASS |
| GUC názvy, kontexty a API rozdíly ověřeny | PASS |
| Provider name a Material wrapper ověřeny | PASS |
| Force read window ověřeno | PASS |
| EXPLAIN property přesně identifikována | PASS |
| Známé P0 rozpory mají závazné rozhodnutí | PASS |
| Běhový hook order | OPEN — patří do S0, neblokuje WP1 |
| GUC_NO_RESET na reálném běžícím clusteru | OPEN — patří do S0, neblokuje WP1 |

**Verdikt WP0: PASS pro zahájení WP1.**

WP0 uzavírá zdrojový recon. Neprohlašuje delegační mechanismus za validovaný.
