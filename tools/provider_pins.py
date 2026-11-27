"""Read the target pg_duckdb and DuckDB source pins."""

from pathlib import Path

pin_file = Path(__file__).resolve().parents[1] / "deps" / "pg_duckdb.env"
pins = dict(line.split("=", 1) for line in pin_file.read_text(encoding="ascii").splitlines())
PG_DUCKDB_SHA = pins["PG_DUCKDB_SHA"]
DUCKDB_SHA = pins["DUCKDB_SHA"]
