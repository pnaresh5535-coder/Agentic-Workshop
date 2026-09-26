"""Load seed/tickets.csv and seed/customers.csv into app.db.

Usage: uv run python load_seed.py

Every run validates both CSVs first, then drops, recreates and fills the
`tickets` and `customers` tables in one transaction, so the load is
repeatable and a failed load leaves the previous app.db untouched.
"""

import csv
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SEED_DIR = REPO_ROOT / "seed"
DB_PATH = REPO_ROOT / "app.db"

TICKET_COLUMNS = ("ticket_id", "customer_id", "created_at", "text")
CUSTOMER_COLUMNS = ("customer_id", "name", "plan", "open_tickets")

SCHEMA = (
    "CREATE TABLE tickets (ticket_id TEXT, customer_id TEXT, created_at TEXT, text TEXT)",
    "CREATE TABLE customers (customer_id TEXT, name TEXT, plan TEXT, open_tickets INTEGER)",
)


class SeedError(ValueError):
    """A seed CSV failed validation; nothing was written to the database."""


def _read_csv(path: Path, columns: tuple[str, ...]) -> list[tuple[int, list[str]]]:
    """Return (line number, fields) for each data row, after checking the header."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None or tuple(header) != columns:
                raise SeedError(f"{path}: header must be {','.join(columns)}, got {','.join(header or [])!r}")
            rows = []
            for fields in reader:
                if len(fields) != len(columns):
                    raise SeedError(f"{path} line {reader.line_num}: expected {len(columns)} fields, got {len(fields)}")
                rows.append((reader.line_num, fields))
            return rows
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise SeedError(f"{path}: cannot read as UTF-8 CSV: {exc}") from exc


def _is_count(value: str) -> bool:
    # str.isdigit() also accepts non-ASCII digits such as "²", so require ASCII.
    return value.isascii() and value.isdigit()


def load(seed_dir: Path = SEED_DIR, db_path: Path = DB_PATH) -> tuple[int, int]:
    """Rebuild the tickets and customers tables in db_path from seed_dir.

    Returns (ticket count, customer count). Raises SeedError before touching
    the database if either CSV is invalid.
    """
    seed_dir, db_path = Path(seed_dir), Path(db_path)
    tickets_path = seed_dir / "tickets.csv"
    customers_path = seed_dir / "customers.csv"

    tickets = [tuple(fields) for _, fields in _read_csv(tickets_path, TICKET_COLUMNS)]
    customers = []
    for line, (customer_id, name, plan, open_tickets) in _read_csv(customers_path, CUSTOMER_COLUMNS):
        if not _is_count(open_tickets):
            raise SeedError(
                f"{customers_path} line {line}: customer {customer_id!r} has invalid open_tickets {open_tickets!r}; "
                "expected a whole number of 0 or more"
            )
        customers.append((customer_id, name, plan, int(open_tickets)))

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN")
        try:
            conn.execute("DROP TABLE IF EXISTS tickets")
            conn.execute("DROP TABLE IF EXISTS customers")
            for statement in SCHEMA:
                conn.execute(statement)
            conn.executemany("INSERT INTO tickets VALUES (?, ?, ?, ?)", tickets)
            conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", customers)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return len(tickets), len(customers)


def main() -> int:
    try:
        n_tickets, n_customers = load()
    except SeedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Loaded {n_tickets} tickets and {n_customers} customers into {DB_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
