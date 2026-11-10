"""Create the next numbered schema migration file.

    python tools/new_migration.py "add calibration table"

Creates app/core/database/schema/NNNN_add_calibration_table.sql.  Write your
CREATE / ALTER statements in it; the application applies it on the next start
(and to any database opened in the Session Workflow).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEMPLATE = """-- Migration {number}: {title}
-- Append-only: once applied to a real database, never edit this file;
-- add another migration instead.  Statements end with ';'.

-- Example:
-- CREATE TABLE calibration (
--     id INTEGER PRIMARY KEY,
--     device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
--     taken_at REAL NOT NULL,
--     value REAL NOT NULL
-- );
-- CREATE INDEX idx_calibration_device ON calibration(device_id, taken_at);
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("title", help="what the migration does")
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = parser.parse_args()
    schema = Path(args.root) / "app" / "core" / "database" / "schema"
    numbers = [int(p.name[:4]) for p in schema.glob("[0-9][0-9][0-9][0-9]_*.sql")]
    number = max(numbers, default=0) + 1
    slug = re.sub(r"[^a-z0-9]+", "_", args.title.lower()).strip("_")[:50] or "migration"
    target = schema / f"{number:04d}_{slug}.sql"
    target.write_text(TEMPLATE.format(number=number, title=args.title), encoding="utf-8")
    print(f"created {target}")
    print("note: a file with only comments is rejected at start-up; add at least one statement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
