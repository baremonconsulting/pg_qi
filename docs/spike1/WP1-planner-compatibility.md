# WP1 — planner compatibility shim

Datum: 2026-10-14

## Cíl

Izolovat rozdíl planner API mezi vydanými PostgreSQL 16–18 a vývojovou řadou
19+ do jediného headeru a ověřit vydanou matici skutečnou kompilací s warningy
jako chybami.

## Implementace

Přidán `src/include/pg_qi/planner_compat.h`, který vlastní:

- `qi_planner_fn_type`;
- `QI_PLANNER_PROTO(fn)`;
- `QI_CALL_PLANNER(fn, ...)` s explicitním callee;
- `QI_CURRENT_EXPLAIN_STATE`;
- jedinou verzi podmínky `PG_VERSION_NUM >= 190000`.

`src/pg_qi.c` již nezná konkrétní počet argumentů planneru. Stejné makro volá
jak uložený downstream hook, tak `standard_planner`.

## Bezpečnostní a review požadavky

- Není definován falešný `ExplainState`.
- PostgreSQL 16–18 neodkazují na lokální proměnnou `es`.
- Callee je parametr makra; shim je použitelný i pro budoucí syntetickou rekurzi.
- Hook chain a `_PG_fini()` zůstávají zachované.
- `-Werror` je zapnutý pro celý extension build nad výchozí sadou warningů
  poskytovanou PGXS. `-Wextra` se záměrně nepřidává globálně: PostgreSQL 16 má
  ve veřejném inline headeru nepoužitý parametr a cizí warning by jinak
  znemožnil čistý extension build.

## Ověřovací matice

| Verze | `pg_config` | Build | `make installcheck` | Výsledek |
|---|---|---|---|---|
| PostgreSQL 16 | 16.15 | PASS (`-Werror`) | 1/1 PASS | PASS |
| PostgreSQL 17 | 17.11 | PASS (`-Werror`) | 1/1 PASS | PASS |
| PostgreSQL 18 | 18.6 | PASS (`-Werror`) | 1/1 PASS | PASS |

Každá verze byla ověřena v novém izolovaném clusteru s
`shared_preload_libraries = 'pg_qi'`. Samostatný smoke test potvrdil
`qi.is_preloaded() = true` a `pg_qi.enabled = on`; potom prošel původní
transparentní regresní test.

## Reprodukovatelnost

Lokální matici spouští `scripts/spike1/wp1-matrix.sh`. Výchozí instalace jsou
v `/home/adam/.local/pgsql/{16,17,18}` a výstupy vznikají v
`artifacts/spike1/wp1`.

PostgreSQL bylo sestaveno z oficiálních release zdrojů. Na tomto hostu nebyly
systémově dostupné `bison`, `flex` a `m4`; balíčky byly proto pouze rozbaleny
do uživatelského adresáře `/home/adam/.local/pg-build-tools`, bez `sudo` a bez
změny systémové instalace.

CI už obsahovalo matici 16/17/18. WP1 do něj přidává preload knihovny před
startem serveru; `Makefile` vynucuje `-Werror`. Cloudový GitHub Actions běh
nebyl spuštěn, protože změny nebyly pushnuty.

## Nález při implementaci

První pokus použil globální `-Wall -Wextra -Werror`. PostgreSQL 16 pak selhal
na nepoužitém parametru ve vlastním veřejném inline headeru
`storage/bufpage.h`. Finální profil proto používá PGXS warning set a přidává
`-Werror`, což drží kód pg_qi přísný a současně neeskaluje nové warningy
cizích serverových headerů.

## Verdikt

**WP1: PASS.** Povinná matice PostgreSQL 16–18 prošla buildem, preload smoke
testem i původním regresním testem. Větev pro PostgreSQL 19 zůstává připravená,
ale podle implementačního plánu není PG19 součástí povinné matice před GA.
