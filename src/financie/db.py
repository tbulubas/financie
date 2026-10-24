# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""SQLite database for personal finance. All data stays local in financie.db."""
import os
import sqlite3
import hashlib
from pathlib import Path

# project root (src/financie/db.py -> ../..); the package is installed in editable mode
PROJECT_DIR = Path(__file__).resolve().parents[2]

# The database can live outside this folder: set FINANCIE_DB=C:\Financie\financie.db
DB_PATH = Path(os.environ.get("FINANCIE_DB", PROJECT_DIR / "financie.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'bezny',          -- bezny, sporiaci, kreditka, hotovost, investicny, ine
    currency TEXT NOT NULL DEFAULT 'EUR',
    opening_balance REAL NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'expense'         -- expense / income / transfer
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,                          -- ISO YYYY-MM-DD
    amount REAL NOT NULL,                        -- + income, - expense
    currency TEXT NOT NULL DEFAULT 'EUR',
    description TEXT,
    counterparty TEXT,
    category_id INTEGER REFERENCES categories(id),
    source_file TEXT,
    import_hash TEXT UNIQUE,                     -- dedup on repeated imports
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_cat ON transactions(category_id);

CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    month TEXT NOT NULL DEFAULT '*',             -- 'YYYY-MM' or '*' = every month
    amount REAL NOT NULL,
    UNIQUE(category_id, month)
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'ine',            -- nehnutelnost, etf, akcie, krypto, dochodok, zavazok, ine
    currency TEXT NOT NULL DEFAULT 'EUR',
    note TEXT
);

CREATE TABLE IF NOT EXISTS asset_values (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    value REAL NOT NULL,                         -- negative for liabilities (mortgage, loan)
    UNIQUE(asset_id, date)
);
"""

DEFAULT_CATEGORIES = [
    ("Potraviny", "expense"), ("Bývanie", "expense"), ("Energie", "expense"),
    ("Doprava", "expense"), ("Auto", "expense"), ("Reštaurácie", "expense"),
    ("Zdravie", "expense"), ("Oblečenie", "expense"), ("Zábava", "expense"),
    ("Cestovanie", "expense"), ("Vzdelávanie", "expense"), ("Deti", "expense"),
    ("Domácnosť", "expense"), ("Poistenie", "expense"), ("Poplatky", "expense"),
    ("Darčeky", "expense"), ("Šport", "expense"), ("Predplatné", "expense"),
    ("Sporenie a investície", "expense"), ("Iné výdavky", "expense"),
    ("Mzda", "income"), ("Podnikanie", "income"),
    ("Investičný výnos", "income"), ("Iné príjmy", "income"),
    ("Prevod medzi účtami", "transfer"),
]


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    con = get_db()
    con.executescript(SCHEMA)
    for name, kind in DEFAULT_CATEGORIES:
        con.execute("INSERT OR IGNORE INTO categories(name, kind) VALUES (?,?)", (name, kind))
    if con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
        con.execute("INSERT INTO accounts(name, type) VALUES ('Bežný účet', 'bezny')")
    con.commit()
    con.close()


def tx_hash(account_id: int, date: str, amount: float, description: str) -> str:
    raw = f"{account_id}|{date}|{amount:.2f}|{(description or '').strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def account_balances(con) -> list[dict]:
    rows = con.execute("""
        SELECT a.id, a.name, a.type, a.currency,
               a.opening_balance + COALESCE(SUM(t.amount), 0) AS balance
        FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
        GROUP BY a.id ORDER BY a.name
    """).fetchall()
    return [dict(r) for r in rows]


def latest_asset_values(con) -> list[dict]:
    rows = con.execute("""
        SELECT a.id, a.name, a.type, a.currency, v.date, v.value
        FROM assets a
        LEFT JOIN asset_values v ON v.asset_id = a.id
          AND v.date = (SELECT MAX(date) FROM asset_values WHERE asset_id = a.id)
        ORDER BY a.name
    """).fetchall()
    return [dict(r) for r in rows]


def net_worth(con) -> dict:
    accounts = account_balances(con)
    assets = latest_asset_values(con)
    total_accounts = sum(a["balance"] for a in accounts)
    total_assets = sum(a["value"] or 0 for a in assets)
    return {
        "accounts": accounts,
        "assets": assets,
        "total_accounts": round(total_accounts, 2),
        "total_assets": round(total_assets, 2),
        "net_worth": round(total_accounts + total_assets, 2),
    }
