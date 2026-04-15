#!/usr/bin/env python3
"""Import a D1-exported SQL file into a local SQLite database.

Usage:
    python scripts/import_d1_to_sqlite.py [sql_file] [db_file]

Defaults:
    sql_file = d1-export.sql
    db_file  = ~/.grazie2api/main.db

Steps:
    1. Read the schema.sql to create tables (idempotent)
    2. Read the D1 export SQL and execute all INSERT statements
    3. Print row counts for all tables to verify
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"
DEFAULT_SQL = Path("d1-export.sql")
DEFAULT_DB = Path.home() / ".grazie2api" / "main.db"


def main() -> None:
    sql_file = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SQL
    db_file = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DB

    if not sql_file.exists():
        print(f"[ERROR] SQL file not found: {sql_file}")
        print("Run scripts/export_d1.sh first to export D1 data.")
        sys.exit(1)

    # Ensure parent directory
    db_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"[import] SQL source: {sql_file}")
    print(f"[import] Target DB:  {db_file}")

    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Step 1: Create schema
    print("[import] Creating schema from schema.sql ...")
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    conn.commit()

    # Step 2: Execute D1 export
    print("[import] Importing D1 export data ...")
    export_sql = sql_file.read_text(encoding="utf-8")

    # D1 export may contain PRAGMA / CREATE TABLE statements that conflict
    # with our schema. We filter to only execute INSERT/UPDATE/DELETE and
    # safe pragmas.
    safe_lines: list[str] = []
    skip_patterns = re.compile(
        r"^\s*(CREATE\s+TABLE|CREATE\s+INDEX|CREATE\s+UNIQUE\s+INDEX|"
        r"ALTER\s+TABLE|DROP\s+TABLE|DROP\s+INDEX|PRAGMA)",
        re.IGNORECASE,
    )

    # Split by semicolons but handle multi-line statements
    statements = export_sql.split(";")
    imported_count = 0
    error_count = 0

    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        # Skip DDL and PRAGMA statements (our schema.sql handles those)
        if skip_patterns.match(stmt):
            continue
        # Skip comments
        if stmt.startswith("--"):
            continue
        try:
            conn.execute(stmt + ";")
            imported_count += 1
        except sqlite3.Error as e:
            error_count += 1
            if error_count <= 10:
                preview = stmt[:120].replace("\n", " ")
                print(f"  [WARN] {e} | stmt: {preview}...")

    conn.commit()
    print(f"[import] Executed {imported_count} statements ({error_count} errors/skipped)")

    # Step 3: Verify row counts
    print("\n[import] Table row counts:")
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    total_rows = 0
    for table in tables:
        count_cursor = conn.execute(f"SELECT COUNT(*) FROM [{table}]")
        count = count_cursor.fetchone()[0]
        total_rows += count
        print(f"  {table:30s} {count:>8d} rows")

    print(f"\n[import] Total: {len(tables)} tables, {total_rows} rows")
    print(f"[import] Database file: {db_file} ({db_file.stat().st_size} bytes)")

    conn.close()
    print("[import] Done.")


if __name__ == "__main__":
    main()
