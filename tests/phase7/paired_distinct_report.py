#!/usr/bin/env python3
"""Generate a reporting query from the fixture's selected measures."""

if __name__ == "__main__":
    measures = (("item_key", "items"), ("account_key", "accounts"))
    aggregates = ",\n           ".join(
        f'count(DISTINCT "{source}") AS "{name}"' for source, name in measures
    )
    columns = ", ".join(f'"{name}"' for _, name in measures)
    print(f"""WITH "filtered" AS (
    SELECT "bucket", "item_key", "account_key"
    FROM "distinct_report_fixture"
    WHERE "id" >= $1 AND ($2 IS NULL OR "id" <= $2)
), "counts" AS (
    SELECT "bucket",
           {aggregates}
    FROM "filtered"
    GROUP BY "bucket"
), "ranked" AS (
    SELECT "bucket", {columns},
           row_number() OVER (ORDER BY "items" DESC, "bucket") AS "position"
    FROM "counts"
)
SELECT "bucket", {columns}, "position"
FROM "ranked"
ORDER BY "position";""")
