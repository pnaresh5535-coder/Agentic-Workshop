import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest

import load_seed
from load_seed import SeedError, load

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = REPO_ROOT / "seed"


def dump(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall())
            for table in ("tickets", "customers")
        }


def make_seed(tmp_path, customers_csv=None, tickets_csv=None):
    """A seed dir under tmp_path: the real CSVs unless replacement text is given."""
    seed = tmp_path / "seed"
    seed.mkdir()
    for name, text in (("tickets.csv", tickets_csv), ("customers.csv", customers_csv)):
        if text is None:
            shutil.copy(SEED_DIR / name, seed / name)
        else:
            (seed / name).write_text(text, encoding="utf-8", newline="")
    return seed


def load_server(db_path):
    spec = importlib.util.spec_from_file_location("triage_server_under_test", REPO_ROOT / "mcp" / "triage_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DB_PATH = db_path
    return module


def test_first_load(tmp_path):
    db = tmp_path / "app.db"
    assert load(SEED_DIR, db) == (24, 20)
    rows = dump(db)
    assert len(rows["tickets"]) == 24
    assert len(rows["customers"]) == 20
    with sqlite3.connect(db) as conn:
        text = conn.execute("SELECT text FROM tickets WHERE ticket_id = 'T-1047'").fetchone()[0]
    assert text == "Refund the duplicate charge, please."


def test_main_prints_one_summary_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(load_seed, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(load_seed, "load", lambda: (24, 20))
    assert load_seed.main() == 0
    assert capsys.readouterr().out == "Loaded 24 tickets and 20 customers into app.db\n"


def test_second_load_is_identical(tmp_path):
    db = tmp_path / "app.db"
    load(SEED_DIR, db)
    first = dump(db)
    assert load(SEED_DIR, db) == (24, 20)
    assert dump(db) == first


def test_open_tickets_stored_as_integer(tmp_path):
    db = tmp_path / "app.db"
    load(SEED_DIR, db)
    with sqlite3.connect(db) as conn:
        types = {t for (t,) in conn.execute("SELECT typeof(open_tickets) FROM customers")}
        other = {t for (t,) in conn.execute(
            "SELECT typeof(customer_id) FROM customers UNION SELECT typeof(name) FROM customers "
            "UNION SELECT typeof(plan) FROM customers UNION SELECT typeof(ticket_id) FROM tickets "
            "UNION SELECT typeof(customer_id) FROM tickets UNION SELECT typeof(created_at) FROM tickets "
            "UNION SELECT typeof(text) FROM tickets"
        )}
    assert types == {"integer"}
    assert other == {"text"}


def test_mcp_tools_read_loaded_data(tmp_path):
    db = tmp_path / "app.db"
    load(SEED_DIR, db)
    server = load_server(db)
    ticket = server.get_ticket("T-1042")
    assert ticket["customer_id"] == "C-77"
    customer = server.get_customer_history("C-77")
    assert customer["name"] == "Northwind"
    assert customer["plan"] == "Enterprise"
    assert customer["open_tickets"] == 2
    assert "T-1042" in customer["ticket_ids"]


BAD_COUNT_CSV = "customer_id,name,plan,open_tickets\nC-01,Acme,Team,1\nC-02,Bad Co,Team,{value}\n"


@pytest.mark.parametrize("value", ["-1", "2.5", "", "three", " 3", "3 ", "²"])
def test_bad_count_writes_nothing(tmp_path, value):
    seed = make_seed(tmp_path, customers_csv=BAD_COUNT_CSV.format(value=value))
    db = tmp_path / "app.db"
    with pytest.raises(SeedError) as exc:
        load(seed, db)
    message = str(exc.value)
    assert "line 3" in message
    assert "C-02" in message
    assert repr(value) in message
    assert not db.exists()


@pytest.mark.parametrize(
    ("file", "bad_header", "expected"),
    [
        ("customers.csv", "customer_id,name,tier,open_tickets", "customer_id,name,plan,open_tickets"),
        ("tickets.csv", "ticket_id,customer,created_at,text", "ticket_id,customer_id,created_at,text"),
    ],
)
def test_bad_header_writes_nothing(tmp_path, file, bad_header, expected):
    real = (SEED_DIR / file).read_text(encoding="utf-8")
    body = real.split("\n", 1)[1]
    kwargs = {"customers_csv" if file == "customers.csv" else "tickets_csv": f"{bad_header}\n{body}"}
    seed = make_seed(tmp_path, **kwargs)
    db = tmp_path / "app.db"
    with pytest.raises(SeedError) as exc:
        load(seed, db)
    assert file in str(exc.value)
    assert expected in str(exc.value)
    assert not db.exists()


def test_failed_reload_leaves_db_unchanged(tmp_path):
    db = tmp_path / "app.db"
    load(SEED_DIR, db)
    before_rows = dump(db)
    before_bytes = db.read_bytes()
    seed = make_seed(tmp_path, customers_csv=BAD_COUNT_CSV.format(value="-1"))
    with pytest.raises(SeedError) as exc:
        load(seed, db)
    assert "line 3" in str(exc.value) and "C-02" in str(exc.value) and "'-1'" in str(exc.value)
    assert db.read_bytes() == before_bytes
    assert dump(db) == before_rows


def test_cli_exits_nonzero_on_seed_error(tmp_path, monkeypatch, capsys):
    seed = make_seed(tmp_path, customers_csv=BAD_COUNT_CSV.format(value="three"))
    monkeypatch.setattr(load_seed, "load", lambda: load(seed, tmp_path / "app.db"))
    assert load_seed.main() == 1
    assert "C-02" in capsys.readouterr().err


def test_default_paths_resolve_from_repo_root():
    assert load_seed.SEED_DIR == REPO_ROOT / "seed"
    assert load_seed.DB_PATH == REPO_ROOT / "app.db"


def test_bom_prefixed_csv_loads(tmp_path):
    real = (SEED_DIR / "customers.csv").read_text(encoding="utf-8")
    seed = make_seed(tmp_path, customers_csv="﻿" + real)
    assert load(seed, tmp_path / "app.db") == (24, 20)


def test_fault_after_drops_rolls_back(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    load(SEED_DIR, db)
    before = dump(db)
    monkeypatch.setattr(load_seed, "SCHEMA", (load_seed.SCHEMA[0], "CREATE TABLE customers ("))
    with pytest.raises(sqlite3.Error):
        load(SEED_DIR, db)
    assert dump(db) == before


CUSTOMERS_HEADER = "customer_id,name,plan,open_tickets\nC-01,Acme,Team,1\n"
TICKETS_HEADER = "ticket_id,customer_id,created_at,text\n"


@pytest.mark.parametrize(
    ("file", "text", "line"),
    [
        ("customers.csv", CUSTOMERS_HEADER + "C-02,Bad Co,Team\n", 3),
        ("customers.csv", CUSTOMERS_HEADER + "C-02,Bad Co,Team,1,extra\n", 3),
        ("tickets.csv", TICKETS_HEADER + "T-9999,C-01,2026-09-01T09:00:00\n", 2),
    ],
)
def test_wrong_field_count_writes_nothing(tmp_path, file, text, line):
    kwargs = {"customers_csv" if file == "customers.csv" else "tickets_csv": text}
    seed = make_seed(tmp_path, **kwargs)
    db = tmp_path / "app.db"
    with pytest.raises(SeedError) as exc:
        load(seed, db)
    assert file in str(exc.value)
    assert f"line {line}" in str(exc.value)
    assert not db.exists()


@pytest.mark.parametrize("fault", ["missing", "not_utf8"])
def test_unreadable_csv_main_returns_1(tmp_path, monkeypatch, capsys, fault):
    seed = make_seed(tmp_path)
    if fault == "missing":
        (seed / "tickets.csv").unlink()
    else:
        (seed / "customers.csv").write_bytes(b"customer_id,name,plan,open_tickets\nC-01,\xff\xfe,Team,1\n")
    db = tmp_path / "app.db"
    monkeypatch.setattr(load_seed, "load", lambda: load(seed, db))
    assert load_seed.main() == 1
    assert capsys.readouterr().err.startswith("Error:")
    assert not db.exists()
