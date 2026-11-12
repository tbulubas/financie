# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Schema migrations on an invented database in the old (version 0) layout."""

import sqlite3

import pytest

from financie import db

# the schema before version 1: Slovak codes, categories without a key
V0_SCHEMA = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'bezny', currency TEXT NOT NULL DEFAULT 'EUR',
    opening_balance REAL NOT NULL DEFAULT 0, note TEXT);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'expense');
CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'EUR',
    description TEXT, counterparty TEXT, category_id INTEGER REFERENCES categories(id),
    source_file TEXT, import_hash TEXT UNIQUE, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE budgets (id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id),
    month TEXT NOT NULL DEFAULT '*', amount REAL NOT NULL, UNIQUE(category_id, month));
CREATE TABLE assets (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'ine', currency TEXT NOT NULL DEFAULT 'EUR', note TEXT);
CREATE TABLE asset_values (id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    date TEXT NOT NULL, value REAL NOT NULL, UNIQUE(asset_id, date));
"""


@pytest.fixture
def v0_db(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(V0_SCHEMA)
    for name, kind in [(slovak, "expense") for slovak in db.V0_CATEGORY_KEYS]:
        con.execute("INSERT INTO categories(name, kind) VALUES (?,?)", (name, kind))
    con.execute("UPDATE categories SET kind='income' WHERE name IN ('Mzda','Podnikanie','Investičný výnos','Iné príjmy')")
    con.execute("UPDATE categories SET kind='transfer' WHERE name='Prevod medzi účtami'")
    con.execute("INSERT INTO categories(name, kind) VALUES ('Pes', 'expense')")  # user-defined
    con.execute("INSERT INTO accounts(name, type) VALUES ('Bežný účet', 'bezny')")
    con.execute("INSERT INTO accounts(name, type) VALUES ('Karta', 'kreditka')")
    groceries = con.execute("SELECT id FROM categories WHERE name='Potraviny'").fetchone()[0]
    dog = con.execute("SELECT id FROM categories WHERE name='Pes'").fetchone()[0]
    con.execute("INSERT INTO transactions(account_id, date, amount, description, category_id, import_hash)"
                " VALUES (1, '2026-05-01', -10, 'Nákup', ?, 'h1')", (groceries,))
    con.execute("INSERT INTO transactions(account_id, date, amount, description, category_id, import_hash)"
                " VALUES (2, '2026-05-02', -20, 'Granule', ?, 'h2')", (dog,))
    con.execute("INSERT INTO budgets(category_id, amount) VALUES (?, 300)", (groceries,))
    con.execute("INSERT INTO assets(name, type) VALUES ('Hypotéka', 'zavazok')")
    con.execute("INSERT INTO assets(name, type) VALUES ('ETF', 'etf')")
    con.commit()
    con.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def rows(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_migration_renames_codes_and_keeps_references(v0_db):
    before = dict(rows(v0_db, "SELECT name, id FROM categories"))
    db.init_db()

    assert rows(v0_db, "PRAGMA user_version") == [(db.SCHEMA_VERSION,)]
    cats = {name: (cid, key) for cid, key, name in rows(v0_db, "SELECT id, key, name FROM categories")}
    assert cats["Groceries"] == (before["Potraviny"], "groceries")
    assert cats["Transfer between accounts"] == (before["Prevod medzi účtami"], "transfer")
    assert cats["Pes"] == (before["Pes"], None)
    assert not set(db.V0_CATEGORY_KEYS) & set(cats)
    assert len(cats) == len(db.DEFAULT_CATEGORIES) + 1  # nothing duplicated by the default seeding

    assert rows(v0_db, "SELECT name, type FROM accounts ORDER BY id") == [
        ("Checking account", "checking"), ("Karta", "credit_card")]
    assert rows(v0_db, "SELECT name, type FROM assets ORDER BY id") == [("Hypotéka", "liability"), ("ETF", "etf")]
    assert rows(v0_db, "SELECT description, category_id FROM transactions ORDER BY id") == [
        ("Nákup", before["Potraviny"]), ("Granule", before["Pes"])]
    assert rows(v0_db, "SELECT category_id, amount FROM budgets") == [(before["Potraviny"], 300.0)]


def test_migration_backs_up_the_old_database_once(v0_db):
    db.init_db()
    backups = list(v0_db.parent.glob("old-backup-v0-*.db"))
    assert len(backups) == 1
    assert rows(backups[0], "PRAGMA user_version") == [(0,)]
    assert ("Potraviny",) in rows(backups[0], "SELECT name FROM categories")

    db.init_db()  # already at the current version: no second backup, no changes
    assert len(list(v0_db.parent.glob("old-backup-v0-*.db"))) == 1
    assert rows(v0_db, "SELECT COUNT(*) FROM categories") == [(len(db.DEFAULT_CATEGORIES) + 1,)]


def test_english_name_taken_by_user_category(v0_db):
    con = sqlite3.connect(v0_db)
    con.execute("INSERT INTO categories(name, kind) VALUES ('Sport', 'expense')")
    con.commit()
    con.close()
    db.init_db()
    assert ("Šport", "sport") in rows(v0_db, "SELECT name, key FROM categories")
    assert ("Sport", None) in rows(v0_db, "SELECT name, key FROM categories")


def test_fresh_database_is_created_in_english_without_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "new.db")
    db.init_db()
    assert rows(db.DB_PATH, "PRAGMA user_version") == [(db.SCHEMA_VERSION,)]
    assert rows(db.DB_PATH, "SELECT name, type FROM accounts") == [db.DEFAULT_ACCOUNT]
    assert rows(db.DB_PATH, "SELECT COUNT(*) FROM categories WHERE key IS NOT NULL") == [(len(db.DEFAULT_CATEGORIES),)]
    assert list(tmp_path.glob("*-backup-*.db")) == []


def test_v1_database_gets_the_import_history(tmp_path, monkeypatch):
    path = tmp_path / "v1.db"
    con = sqlite3.connect(path)
    con.executescript(V0_SCHEMA)
    con.execute("ALTER TABLE categories ADD COLUMN key TEXT")
    con.execute("INSERT INTO accounts(name, type) VALUES ('Checking account', 'checking')")
    con.execute("INSERT INTO transactions(account_id, date, amount, description, import_hash)"
                " VALUES (1, '2026-05-01', -10, 'Shop', 'h1')")
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    monkeypatch.setattr(db, "DB_PATH", path)

    db.init_db()
    assert rows(path, "PRAGMA user_version") == [(db.SCHEMA_VERSION,)]
    assert rows(path, "SELECT id, description, import_id FROM transactions") == [(1, "Shop", None)]
    assert rows(path, "SELECT COUNT(*) FROM imports") == [(0,)]
    assert rows(path, "SELECT name, is_main FROM accounts") == [("Checking account", 0)]   # v3
    assert rows(path, "SELECT import_hash, ref FROM transactions") == [("h1", None)]       # v4
    assert rows(path, "SELECT balance_date, balance_amount FROM accounts") == [(None, None)]   # v5
    assert rows(path, "SELECT COUNT(*) FROM balance_checks") == [(0,)]                       # v6
    assert len(list(tmp_path.glob("v1-backup-v1-*.db"))) == 1


def test_new_default_categories_take_over_the_users_own(tmp_path, monkeypatch):
    """A database from before "Internet and phone" and "Donations" were defaults, where the
    user made "Spenden" himself: it becomes the default one, with its transactions."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old.db")
    db.init_db()
    con = sqlite3.connect(db.DB_PATH)
    con.execute("DELETE FROM categories WHERE key IN ('internet_phone', 'donations')")
    own = con.execute("INSERT INTO categories(name, kind) VALUES ('Spenden', 'expense')").lastrowid
    con.execute("""INSERT INTO transactions(account_id, date, amount, description, category_id, import_hash)
                   VALUES (1, '2026-05-01', -20, 'Spende', ?, 'h1')""", (own,))
    con.commit()
    con.close()
    db.init_db()
    assert rows(db.DB_PATH, "SELECT id, name, key FROM categories WHERE key='donations'") == [(own, "Spenden", "donations")]
    assert rows(db.DB_PATH, "SELECT category_id FROM transactions") == [(own,)]
    assert rows(db.DB_PATH, "SELECT name FROM categories WHERE key='internet_phone'") == [("Internet and phone",)]
    assert rows(db.DB_PATH, "SELECT COUNT(*) FROM categories WHERE name IN ('Donations', 'Spenden')") == [(1,)]
