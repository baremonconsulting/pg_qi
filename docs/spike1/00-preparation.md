# Spike 1 — přípravná rozhodnutí a otevřené otázky

Datum: 2026-10-14

## Výchozí stav

- Repozitář: `https://github.com/baremonconsulting/pg_qi.git`
- Výchozí větev a commit: `main` / `d3529d4e6a7cd71657bbf8279c533a3c5a81e2df`
- Pracovní větev: `spike/1-delegation`
- Primární vývojová verze: PostgreSQL 17
- Povinná ověřovací matice: PostgreSQL 16, 17 a 18
- Pin pg_duckdb: `ee38d3b540ecea1d93683ba99bdcec5632a21eaf`

## Pořadí autority podkladů

Při rozporu se použije toto pořadí:

1. zdrojový kód pinované verze pg_duckdb a zdrojový kód příslušné větve PostgreSQL;
2. `Rigorous Review of the Spike 1 Delegation Mechanism Implementation`;
3. konsolidovaná specifikace v10 po opravách z rigorózního review;
4. český implementační postup v8 a starší technická review.

## Rozhodnutí nutná před WP0/WP1

### R1 — nepravý typ ExplainState

Rozhodnutí: `typedef void ExplainState;` se nikde nepoužije. PostgreSQL 16–18 ho v
podpisu planner hooku nepotřebují; PostgreSQL master má skutečný veřejný typ.

### R2 — failpointy mimo PG_TRY

Rozhodnutí pro Spike 1: po vytvoření rámce budou všechny operace schopné vyvolat
ERROR, včetně failpointů, uvnitř odpovídajícího `PG_TRY`. Neimplementuje se zatím
složitější vlastnictví rámců podle `SubTransactionId` ani dvojice xact/subxact
callbacků. Tím se odstraňuje blokující chyba specifikace v10 a drží se minimální
rozsah experimentu.

### R3 — volatile disciplína

Rozhodnutí: lokální proměnná změněná v `PG_TRY` a použitá po přechodu do
`PG_CATCH` bude `volatile`. Zejména budoucí `qi_try_local_subxact()` použije
`volatile bool ok`. Backendový statický stav se `volatile` neoznačuje.

### R4 — publikace změn

Rozhodnutí: práce probíhá na lokální větvi. Bez samostatného pokynu se nic
nepushuje na GitHub a nevytváří se pull request.

### R5 — důkaz kompatibility

Rozhodnutí: verze API se neurčuje pokusem ani odhadem. Zdrojový recon stanoví
hranici; CI/compile matrix ji pouze ověřuje proti skutečným hlavičkám PG 16–18.

## Otevřené otázky, které neblokují WP0/WP1

- S4: zvolit deterministickou downstream planner chybu nebo transparentně
  označený testovací shim. Řeší se před implementací S4.
- S6b-2: přesný oracle musí u známého delegovatelného dotazu prokázat delegaci,
  nebo zachytit konkrétní důvod odmítnutí. Řeší se při testovací matici.
- S8: zda produkt přijme rozhodnutí pouze při plánování pro generic plan reuse,
  nebo přejde k variantě B. Rozhoduje závěrečný report podle měření.
- Varianta B: první upstream API má zůstat minimální; řeší se jen pokud varianta
  A selže.
- PostgreSQL 19: compatibility shim se připraví, ale PG19 není součástí povinné
  běhové matice Spike 1.

## Přípravné technické zjištění

Na výchozím serveru nebyl nainstalován `pg_config` ani PostgreSQL development
headers. Před uzavřením WP1 je proto nutné připravit hlavičky PG 16, 17 a 18 a
spustit skutečnou compile matrix. Samotný překladač GCC a GNU Make jsou dostupné.

## Definition of Ready pro WP0

- pinované commity jsou dostupné;
- zdrojové soubory lze nezávisle prohledat;
- R1–R5 jsou uzavřené;
- otevřené otázky mají vlastní pozdější bránu a nejsou vydávány za vyřešené.
