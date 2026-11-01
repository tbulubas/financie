# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""SQLite database for personal finance. All data stays local in financie.db."""
import os
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

# project root (src/financie/db.py -> ../..); the package is installed in editable mode
PROJECT_DIR = Path(__file__).resolve().parents[2]

# The database can live outside this folder: set FINANCIE_DB=C:\Financie\financie.db
DB_PATH = Path(os.environ.get("FINANCIE_DB", PROJECT_DIR / "financie.db"))

# PRAGMA user_version of the current schema; init_db() migrates older databases
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'checking',       -- checking, savings, credit_card, cash, investment, other
    currency TEXT NOT NULL DEFAULT 'EUR',
    opening_balance REAL NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    key TEXT,                                    -- stable id of a default category, NULL for user-defined ones
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
    type TEXT NOT NULL DEFAULT 'other',          -- real_estate, etf, stocks, crypto, pension, liability, other
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

-- which account a statement source belongs to, learned from past imports
CREATE TABLE IF NOT EXISTS account_sources (
    source_key TEXT PRIMARY KEY,                 -- e.g. 'paypal', 'sparkasse:<hash of IBAN>'
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE
);
"""

# (key, name, kind); the UI shows the translation of cat_<key>, the name is the English fallback
DEFAULT_CATEGORIES = [
    ("groceries", "Groceries", "expense"), ("housing", "Housing", "expense"),
    ("energy", "Energy", "expense"), ("transport", "Transport", "expense"),
    ("car", "Car", "expense"), ("restaurants", "Restaurants", "expense"),
    ("health", "Health", "expense"), ("clothing", "Clothing", "expense"),
    ("entertainment", "Entertainment", "expense"), ("travel", "Travel", "expense"),
    ("education", "Education", "expense"), ("children", "Children", "expense"),
    ("household", "Household", "expense"), ("insurance", "Insurance", "expense"),
    ("fees", "Fees", "expense"), ("gifts", "Gifts", "expense"),
    ("sport", "Sport", "expense"), ("subscriptions", "Subscriptions", "expense"),
    ("savings_investments", "Savings and investments", "expense"),
    ("other_expenses", "Other expenses", "expense"),
    ("salary", "Salary", "income"), ("business", "Business income", "income"),
    ("investment_income", "Investment income", "income"), ("other_income", "Other income", "income"),
    ("transfer", "Transfer between accounts", "transfer"),
]
DEFAULT_ACCOUNT = ("Checking account", "checking")

# schema version 0 stored Slovak values; version 1 stores English codes and names
V0_CATEGORY_KEYS = {
    "Potraviny": "groceries", "Bývanie": "housing", "Energie": "energy", "Doprava": "transport",
    "Auto": "car", "Reštaurácie": "restaurants", "Zdravie": "health", "Oblečenie": "clothing",
    "Zábava": "entertainment", "Cestovanie": "travel", "Vzdelávanie": "education", "Deti": "children",
    "Domácnosť": "household", "Poistenie": "insurance", "Poplatky": "fees", "Darčeky": "gifts",
    "Šport": "sport", "Predplatné": "subscriptions", "Sporenie a investície": "savings_investments",
    "Iné výdavky": "other_expenses", "Mzda": "salary", "Podnikanie": "business",
    "Investičný výnos": "investment_income", "Iné príjmy": "other_income",
    "Prevod medzi účtami": "transfer",
}
V0_ACCOUNT_TYPES = {"bezny": "checking", "sporiaci": "savings", "kreditka": "credit_card",
                    "hotovost": "cash", "investicny": "investment", "ine": "other"}
V0_ASSET_TYPES = {"nehnutelnost": "real_estate", "akcie": "stocks", "krypto": "crypto",
                  "dochodok": "pension", "zavazok": "liability", "ine": "other"}
V0_DEFAULT_ACCOUNT = "Bežný účet"


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    con = get_db()
    version = con.execute("PRAGMA user_version").fetchone()[0]
    existing = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='categories'").fetchone()
    if existing and version < SCHEMA_VERSION:
        backup_db(con, version)
    con.executescript(SCHEMA)
    if existing and version < 1:
        _migrate_v0_to_v1(con)
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cat_key ON categories(key)")
    for key, name, kind in DEFAULT_CATEGORIES:
        con.execute("INSERT OR IGNORE INTO categories(key, name, kind) VALUES (?,?,?)", (key, name, kind))
    if con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
        con.execute("INSERT INTO accounts(name, type) VALUES (?,?)", DEFAULT_ACCOUNT)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()
    con.close()


def backup_db(con, version: int) -> Path:
    """Copy the database next to itself before a migration, e.g. financie-backup-v0-20261030-171200.db."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = DB_PATH.with_name(f"{DB_PATH.stem}-backup-v{version}-{stamp}.db")
    dst = sqlite3.connect(target)
    con.backup(dst)
    dst.close()
    print(f"[db] backup before migration: {target}")
    return target


def _migrate_v0_to_v1(con):
    """Slovak codes and default names -> English. Row ids stay, so all references keep working."""
    con.execute("BEGIN")
    try:
        if "key" not in [r["name"] for r in con.execute("PRAGMA table_info(categories)")]:
            con.execute("ALTER TABLE categories ADD COLUMN key TEXT")
        names = {key: name for key, name, _ in DEFAULT_CATEGORIES}
        for old, key in V0_CATEGORY_KEYS.items():
            try:
                con.execute("UPDATE categories SET key=?, name=? WHERE name=? AND key IS NULL",
                            (key, names[key], old))
            except sqlite3.IntegrityError:  # a user category already has the English name
                con.execute("UPDATE categories SET key=? WHERE name=? AND key IS NULL", (key, old))
        for old, new in V0_ACCOUNT_TYPES.items():
            con.execute("UPDATE accounts SET type=? WHERE type=?", (new, old))
        for old, new in V0_ASSET_TYPES.items():
            con.execute("UPDATE assets SET type=? WHERE type=?", (new, old))
        try:
            con.execute("UPDATE accounts SET name=? WHERE name=?", (DEFAULT_ACCOUNT[0], V0_DEFAULT_ACCOUNT))
        except sqlite3.IntegrityError:
            pass
        con.commit()
    except Exception:
        con.rollback()
        raise
    print("[db] migrated schema v0 -> v1 (English codes and category names)")


def remember_source(con, source_key: str, account_id: int):
    con.execute("""INSERT INTO account_sources(source_key, account_id) VALUES (?,?)
                   ON CONFLICT(source_key) DO UPDATE SET account_id=excluded.account_id""",
                (source_key, account_id))
    con.commit()


def source_account(con, source_key: str | None) -> int | None:
    if not source_key:
        return None
    row = con.execute("SELECT account_id FROM account_sources WHERE source_key=?", (source_key,)).fetchone()
    return row["account_id"] if row else None


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
