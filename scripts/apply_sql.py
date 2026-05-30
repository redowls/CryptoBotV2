r"""Apply a .sql file to the configured database, one GO batch at a time.

The ``db/*.sql`` files use ``GO`` batch separators — an SSMS directive, not
T-SQL. pyodbc/SQLAlchemy cannot execute ``GO``, so this loader splits the file
on ``^\s*GO\s*$`` and runs each batch separately (CLAUDE.md "Live setup facts").
The schema files are written to be re-runnable / idempotent, so re-applying is
safe.

Usage:
    .\.venv\Scripts\python.exe -m scripts.apply_sql db/03_schema_phase3.sql
"""
from __future__ import annotations

import re
import sys

from dotenv import load_dotenv

from core.db import connect_with_retry

_GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)


def main(argv: list[str]) -> int:
    load_dotenv()
    if not argv:
        print("usage: python -m scripts.apply_sql <file.sql>", file=sys.stderr)
        return 2

    path = argv[0]
    with open(path, "r", encoding="utf-8") as fh:
        sql = fh.read()

    batches = [b.strip() for b in _GO.split(sql) if b.strip()]
    if not batches:
        print(f"{path}: nothing to run (no SQL batches found).", file=sys.stderr)
        return 1

    # exec_driver_sql passes raw SQL straight to pyodbc — no SQLAlchemy
    # bind-param parsing, so ':r' / ':n' in comments aren't mistaken for binds.
    with connect_with_retry() as conn:
        with conn.begin():
            for batch in batches:
                conn.exec_driver_sql(batch)

    print(f"Applied {len(batches)} batch(es) from {path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
