# WP3 — autoritativní detektor plánů

Datum: 2026-10-14

## Cíl

Implementovat jediný C visitor nad `PlannedStmt`, který ve tvarech plánů
požadovaných Spike 1 spolehlivě rozpozná přesný provider `DuckDBScan`.

## Implementace

`src/qi_detector.c` obsahuje centralizovanou rekurzi:

- `lefttree` a `righttree` pro každý plánový uzel;
- `Append.appendplans`;
- `MergeAppend.mergeplans`;
- `BitmapAnd.bitmapplans` a `BitmapOr.bitmapplans`;
- `SubqueryScan.subplan`;
- `CustomScan.custom_plans`;
- `PlannedStmt.subplans`.

Shoda je pouze přes přesné
`CustomScanMethods.CustomName == "DuckDBScan"`. `Material` nemá zvláštní
výjimku; jeho potomek je nalezen obecnou rekurzí přes `lefttree`.

Planner po dokončení downstream volání nastavuje read-only diagnostiku
`pg_qi.spike_last_plan_has_duckdb_scan`. JSON EXPLAIN se pro PASS/FAIL
nepoužívá.

## Povinné fixtures

| Fixture | PG16 | PG17 | PG18 |
|---|---:|---:|---:|
| Provider v kořeni | PASS | PASS | PASS |
| Provider pod `Material` | PASS | PASS | PASS |
| Provider v `PlannedStmt.subplans` | PASS | PASS | PASS |
| Provider není přítomen | PASS | PASS | PASS |

Fixtures vytváří skutečné PostgreSQL plan node struktury v C a volají stejný
visitor jako planner hook; nejde o textový nebo JSON test.

## Reálný pg_duckdb plán

Na každé verzi vznikla tabulka se 100 řádky. Po
`SET duckdb.force_execution = on` vrátil dotaz
`SELECT count(*) FROM spike_small` hodnotu 100 a následné
`SHOW pg_qi.spike_last_plan_has_duckdb_scan` hodnotu `on`.

Tím je potvrzeno, že detektor rozpoznal skutečný `DuckDBScan` vytvořený
pinovaným pg_duckdb, nikoli pouze syntetické fixtures.

## Omezení tvrzení

Visitor garantuje pokrytí tvarů vyjmenovaných ve Spike 1. Dokument ani kód
netvrdí automatickou úplnost pro všechny budoucí typy PostgreSQL plánů.

## Nálezy při implementaci

První compile probe odhalil, že `CustomScanMethods` vyžaduje veřejný header
`nodes/extensible.h` a `NodeSetTag` přijímá hodnoty `T_*`. Po opravě prošel
build s `-Werror` na všech třech verzích.

## Verdikt

**WP3: PASS.** Čtyři povinné fixtures, negativní případ, reálný provider a
původní rozšířený regresní test prošly na PostgreSQL 16, 17 a 18.
