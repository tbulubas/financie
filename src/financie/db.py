# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""SQLite database for personal finance. All data stays local in financie.db."""
import os
import sqlite3
import hashlib
from bisect import bisect_right
from datetime import date, datetime
from pathlib import Path

# project root (src/financie/db.py -> ../..); the package is installed in editable mode
PROJECT_DIR = Path(__file__).resolve().parents[2]

# The database can live outside this folder: set FINANCIE_DB=C:\Financie\financie.db
DB_PATH = Path(os.environ.get("FINANCIE_DB", PROJECT_DIR / "financie.db"))

# Default folder with downloaded statements, until one is chosen in the UI (settings.imports_dir)
IMPORTS_DIR = os.environ.get("FINANCIE_IMPORTS")

# PRAGMA user_version of the current schema; init_db() migrates older databases
SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'checking',       -- checking, savings, credit_card, cash, investment, other
    currency TEXT NOT NULL DEFAULT 'EUR',
    opening_balance REAL NOT NULL DEFAULT 0,
    note TEXT,
    is_main INTEGER NOT NULL DEFAULT 0,          -- 1 = main account (salaries); the rest are satellites
    balance_date TEXT,                           -- a known balance: at the end of this day ...
    balance_amount REAL                          -- ... the account had this much (replaces opening_balance)
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
    category_source TEXT,                        -- who chose it: user / rule / learned / ai (schema v7)
    source_file TEXT,
    import_hash TEXT UNIQUE,                     -- dedup on repeated imports
    created_at TEXT DEFAULT (datetime('now')),
    import_id INTEGER REFERENCES imports(id),    -- the import that brought it, NULL before schema v2
    ref TEXT                                     -- the source's own id (PayPal transaction code), if any
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

-- every import attempt of a statement file, successful or not
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_hash TEXT NOT NULL,                     -- sha256 of the content: recognizes renamed copies
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    format TEXT,
    date_from TEXT,
    date_to TEXT,
    status TEXT NOT NULL,                        -- ok / error / undone
    error_code TEXT,                             -- same codes as the API errors
    error_info TEXT,
    inserted INTEGER NOT NULL DEFAULT 0,
    skipped_duplicates INTEGER NOT NULL DEFAULT 0,
    possible_duplicates INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_imports_hash ON imports(file_hash);

-- tags such as a trip ("Sardinia 2025"): a transaction keeps its category and can carry tags
-- across categories, accounts and years (schema v8); tags never take part in learning
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS transaction_tags (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (transaction_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_tx_tags_tag ON transaction_tags(tag_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- real balances from the bank or a statement, to check the computed balance against (schema v6)
CREATE TABLE IF NOT EXISTS balance_checks (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,                          -- at the end of this day ...
    amount REAL NOT NULL,                        -- ... the account had this much
    source TEXT NOT NULL DEFAULT 'manual',       -- manual / statement
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(account_id, date)
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
    ("internet_phone", "Internet and phone", "expense"), ("donations", "Donations", "expense"),
    ("savings_investments", "Savings and investments", "expense"),
    ("other_expenses", "Other expenses", "expense"),
    ("salary", "Salary", "income"), ("business", "Business income", "income"),
    ("investment_income", "Investment income", "income"), ("other_income", "Other income", "income"),
    ("transfer", "Transfer between accounts", "transfer"),
]
DEFAULT_ACCOUNT = ("Checking account", "checking")

# Default categories added later: a category the user already made for the same purpose
# (by one of these names, any case) becomes the default one, so there are never two.
ADOPT_NAMES = {
    "internet_phone": ("internet and phone", "internet und telefon", "internet a telefón", "internet a telefon",
                       "telefon und internet", "telefón a internet", "telefon a internet", "internet & telefon",
                       "internet/telefon", "internet", "telefon", "telefón", "telefonie", "phone"),
    "donations": ("donations", "donation", "spenden", "spende", "dary", "dar", "charity"),
}

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
    if existing and version < 2:
        _migrate_v1_to_v2(con)
    if existing and version < 3:
        _migrate_v2_to_v3(con)
    if existing and version < 4:
        _migrate_v3_to_v4(con)
    if existing and version < 5:
        _migrate_v4_to_v5(con)
    if existing and version < 7:
        _migrate_v6_to_v7(con)
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cat_key ON categories(key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tx_import ON transactions(import_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tx_ref ON transactions(account_id, ref)")
    _adopt_user_categories(con)
    for key, name, kind in DEFAULT_CATEGORIES:
        con.execute("INSERT OR IGNORE INTO categories(key, name, kind) VALUES (?,?,?)", (key, name, kind))
    if con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
        con.execute("INSERT INTO accounts(name, type) VALUES (?,?)", DEFAULT_ACCOUNT)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()
    con.close()


def _adopt_user_categories(con):
    """Give a user's own category the key of a default one added later (see ADOPT_NAMES): its
    id, name and transactions stay, the UI then shows it translated."""
    taken = {r["key"] for r in con.execute("SELECT key FROM categories WHERE key IS NOT NULL")}
    own = con.execute("SELECT id, name FROM categories WHERE key IS NULL ORDER BY id").fetchall()
    for key, names in ADOPT_NAMES.items():
        match = next((r for r in own if r["name"].strip().casefold() in names), None)
        if key not in taken and match:
            con.execute("UPDATE categories SET key=?, kind='expense' WHERE id=?", (key, match["id"]))
            own = [r for r in own if r["id"] != match["id"]]


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


def _migrate_v1_to_v2(con):
    """Import history: transactions remember their import. Older rows keep import_id NULL."""
    if "import_id" not in [r["name"] for r in con.execute("PRAGMA table_info(transactions)")]:
        con.execute("ALTER TABLE transactions ADD COLUMN import_id INTEGER REFERENCES imports(id)")
    con.commit()
    print("[db] migrated schema v1 -> v2 (import history)")


def _migrate_v2_to_v3(con):
    """Main account flag; no account is main until the user marks one."""
    if "is_main" not in [r["name"] for r in con.execute("PRAGMA table_info(accounts)")]:
        con.execute("ALTER TABLE accounts ADD COLUMN is_main INTEGER NOT NULL DEFAULT 0")
    con.commit()
    print("[db] migrated schema v2 -> v3 (main account)")


def _migrate_v3_to_v4(con):
    """Source reference of a transaction, for dedup across export formats. Older rows keep NULL
    and are still deduplicated by their import_hash."""
    if "ref" not in [r["name"] for r in con.execute("PRAGMA table_info(transactions)")]:
        con.execute("ALTER TABLE transactions ADD COLUMN ref TEXT")
    con.commit()
    print("[db] migrated schema v3 -> v4 (transaction reference)")


def _migrate_v4_to_v5(con):
    """A known balance per account, for accounts whose history is not complete."""
    columns = [r["name"] for r in con.execute("PRAGMA table_info(accounts)")]
    if "balance_date" not in columns:
        con.execute("ALTER TABLE accounts ADD COLUMN balance_date TEXT")
    if "balance_amount" not in columns:
        con.execute("ALTER TABLE accounts ADD COLUMN balance_amount REAL")
    con.commit()
    print("[db] migrated schema v4 -> v5 (known balance)")


def _migrate_v6_to_v7(con):
    """Who chose a transaction's category. Earlier choices cannot be told apart, so they all
    count as the user's own: nothing the user set is ever overruled by a guess."""
    if "category_source" not in [r["name"] for r in con.execute("PRAGMA table_info(transactions)")]:
        con.execute("ALTER TABLE transactions ADD COLUMN category_source TEXT")
        con.execute("UPDATE transactions SET category_source='user' WHERE category_id IS NOT NULL")
    con.commit()
    print("[db] migrated schema v6 -> v7 (category source)")


def get_setting(con, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] else default


def set_setting(con, key: str, value: str | None):
    con.execute("""INSERT INTO settings(key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))
    con.commit()


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


GAP_DAYS = 45   # longer than this without a single movement: a hole in the history, not a quiet month


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


class Ledger:
    """End-of-day balances of one account, aware of holes in its history.

    Without a known balance the balance is the opening balance plus everything. With one, it
    is that amount plus the later movements (minus the earlier ones going back) — across the
    whole history, unless the history has a hole: no movement for more than GAP_DAYS, so the
    statements of that time were never imported. Going back across such a hole would carry the
    unknown sum of its movements into every older balance. So an older stretch that holds a
    balance the bank stated (a balance check) and disagrees with the chain is anchored to its
    own latest check instead; the chart breaks the line there and the hole is reported.
    Only older stretches are anchored: the current balance always comes from the known
    balance, and a stretch that agrees with the chain, or has no check, stays chained, so a
    movement missing inside a complete stretch still shows as a difference.
    """

    def __init__(self, con, account_id: int):
        a = con.execute("SELECT opening_balance, balance_date, balance_amount FROM accounts WHERE id=?",
                        (account_id,)).fetchone()
        rows = con.execute("""SELECT date, SUM(amount) FROM transactions WHERE account_id=?
                              GROUP BY date ORDER BY date""", (account_id,)).fetchall()
        self.dates = [r[0] for r in rows]
        self.day_total = {r[0]: r[1] for r in rows}
        self.cum, running = [], 0.0
        for r in rows:
            running += r[1]
            self.cum.append(running)
        self.gaps: list[dict] = []
        if a["balance_date"] is None:
            self.starts, self.anchors = [""], [("", a["opening_balance"])]
            return
        d = self.dates
        # a stretch starts at the first movement and after every hole
        self.starts = [d[0] if d else ""] + [d[i] for i in range(1, len(d)) if _days_between(d[i - 1], d[i]) > GAP_DAYS]
        known = (a["balance_date"], a["balance_amount"])
        self.anchors = [known] * len(self.starts)
        latest = {}   # stretch -> its latest stated balance
        for c in con.execute("SELECT date, amount FROM balance_checks WHERE account_id=? ORDER BY date",
                             (account_id,)):
            latest[self._stretch(c["date"])] = (c["date"], c["amount"])
        for i in range(self._stretch(known[0]) - 1, -1, -1):
            chained, check = self.anchors[i + 1], latest.get(i)
            disagrees = check is not None and round(self._from(chained, check[0]) - check[1], 2) != 0
            self.anchors[i] = check if disagrees else chained
        self.gaps = [{"from": d[bisect_right(d, self.starts[i + 1]) - 2], "to": self.starts[i + 1]}
                     for i in range(len(self.starts) - 1) if self.anchors[i] != self.anchors[i + 1]]

    def _stretch(self, day: str) -> int:
        """The stretch a day belongs to; a day inside a hole belongs to the one before it."""
        return max(bisect_right(self.starts, day) - 1, 0)

    def _upto(self, day: str) -> float:
        i = bisect_right(self.dates, day)
        return self.cum[i - 1] if i else 0.0

    def _from(self, anchor: tuple[str, float], day: str) -> float:
        anchor_day, amount = anchor
        return amount + self._upto(day) - self._upto(anchor_day)   # forward or back from the anchor

    def at(self, day: str) -> float:
        """Computed balance at the end of a day."""
        return round(self._from(self.anchors[self._stretch(day)], day), 2)

    def in_gap(self, day: str) -> bool:
        return any(g["from"] < day < g["to"] for g in self.gaps)


def balance_at(con, account_id: int, day: str) -> float:
    """Computed balance at the end of a day: from the known balance when there is one."""
    return Ledger(con, account_id).at(day)


def balance_gaps(con, account_id: int) -> list[dict]:
    """Holes in the history across which older balances follow the bank's own (see Ledger)."""
    return Ledger(con, account_id).gaps


def balance_checks(con, account_id: int, limit: int = 10) -> list[dict]:
    """The latest real balances of an account with the computed one next to them. Computed live,
    so fixing a missing or doubled transaction shows at once."""
    rows = [dict(r) for r in con.execute(
        """SELECT id, date, amount, source, created_at FROM balance_checks
           WHERE account_id=? ORDER BY date DESC LIMIT ?""", (account_id, limit))]
    ledger = Ledger(con, account_id) if rows else None
    for r in rows:
        r["expected"] = ledger.at(r["date"])
        r["difference"] = round(r["expected"] - r["amount"], 2)   # the app minus the bank
    return rows


def add_balance_check(con, account_id: int, day: str, amount: float, source: str = "manual") -> dict:
    con.execute("""INSERT INTO balance_checks(account_id, date, amount, source) VALUES (?,?,?,?)
                   ON CONFLICT(account_id, date) DO UPDATE SET amount=excluded.amount,
                   source=excluded.source, created_at=datetime('now', 'localtime')""",
                (account_id, day, amount, source))
    con.commit()
    return next(c for c in balance_checks(con, account_id, 1000) if c["date"] == day)


def add_balance_checks(con, account_id: int, balances: dict[str, float], source: str) -> list[dict]:
    """Several real balances at once (a statement with one per day), oldest first, each with the
    computed balance and the difference."""
    con.executemany("""INSERT INTO balance_checks(account_id, date, amount, source) VALUES (?,?,?,?)
                       ON CONFLICT(account_id, date) DO UPDATE SET amount=excluded.amount,
                       source=excluded.source, created_at=datetime('now', 'localtime')""",
                    [(account_id, day, amount, source) for day, amount in balances.items()])
    con.commit()
    ledger, out = Ledger(con, account_id), []
    for day, amount in sorted(balances.items()):
        expected = ledger.at(day)
        out.append({"date": day, "amount": amount, "expected": expected, "difference": round(expected - amount, 2)})
    return out


def running_balances(con, account_id: int) -> dict[int, float]:
    """Balance after each transaction of an account (by date, then id). Within a day the bank
    may order them differently; the balance at the end of the day is the same."""
    ledger = Ledger(con, account_id)
    out, day, running = {}, None, 0.0
    for r in con.execute("SELECT id, date, amount FROM transactions WHERE account_id=? ORDER BY date, id",
                         (account_id,)):
        if r["date"] != day:   # start of the day = its end minus its movements
            day = r["date"]
            running = ledger.at(day) - ledger.day_total[day]
        running += r["amount"]
        out[r["id"]] = round(running, 2)
    return out


def balance_history(con, account_ids: list[int], date_from: str, date_to: str) -> dict:
    """End-of-day balance of each account over a period, for a chart: a point on the first day
    and on every day with movements (the balance holds until the next one), and the balance
    checks of the period with the difference computed live. Nothing is drawn before an account
    has data (its first movement or its known balance) — the bank exports only recent months,
    so a line starting at zero would show a jump that never happened. A hole in the history
    (see Ledger) breaks the line: a point without a balance where the next stretch starts. The
    total starts once every account with data has started and is left out inside a hole."""
    end = min(date_to, date.today().isoformat())
    marks = ",".join("?" * len(account_ids))
    accounts, ledgers = [], {}
    rows = con.execute(f"""SELECT id, name, type, is_main, balance_date FROM accounts
                           WHERE id IN ({marks}) ORDER BY name""", account_ids).fetchall()
    for a in rows:
        ledger = Ledger(con, a["id"])
        item = {"id": a["id"], "name": a["name"], "type": a["type"], "is_main": a["is_main"],
                "points": [], "checks": [], "gaps": []}
        accounts.append(item)
        starts = [x for x in (ledger.dates[0] if ledger.dates else None, a["balance_date"]) if x]
        if not starts or max(date_from, min(starts)) > end:
            continue
        start = max(date_from, min(starts))
        ledgers[a["id"]] = (start, ledger)
        days = [start] + [d for d in ledger.dates if start < d <= end] + [end]
        points = [{"date": d, "balance": None if ledger.in_gap(d) else ledger.at(d)} for d in dict.fromkeys(days)]
        item["gaps"] = [g for g in ledger.gaps if g["to"] > start and g["from"] < end]
        breaks = [{"date": g["to"], "balance": None} for g in item["gaps"] if start < g["to"] <= end]
        item["points"] = sorted(points + breaks, key=lambda p: (p["date"], p["balance"] is not None))
        item["checks"] = [{"date": c["date"], "amount": c["amount"],
                           "difference": round(ledger.at(c["date"]) - c["amount"], 2)}
                          for c in con.execute("""SELECT date, amount FROM balance_checks
                                                  WHERE account_id=? AND date BETWEEN ? AND ? ORDER BY date""",
                                               (a["id"], start, end))]
    total = []
    if ledgers:
        begin = max(start for start, _ in ledgers.values())
        days = sorted({p["date"] for a in accounts for p in a["points"] if p["date"] >= begin} | {begin})
        total = [{"date": d, "balance": None if any(l.in_gap(d) for _, l in ledgers.values())
                  else round(sum(l.at(d) for _, l in ledgers.values()), 2)} for d in days]
    return {"from": date_from, "to": end, "accounts": accounts, "total": total}


def account_balances(con) -> list[dict]:
    """Balance and covered period of every account. With a known balance the history before
    its date does not matter, so an incomplete or later completed history keeps it right."""
    rows = con.execute("""
        SELECT a.id, a.name, a.type, a.currency, a.is_main, a.balance_date, a.balance_amount,
               ROUND(CASE WHEN a.balance_date IS NULL
                          THEN a.opening_balance + COALESCE(SUM(t.amount), 0)
                          ELSE a.balance_amount
                               + COALESCE(SUM(CASE WHEN t.date > a.balance_date THEN t.amount END), 0)
                     END, 2) AS balance,
               MIN(t.date) AS first_date, MAX(t.date) AS last_date
        FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
        GROUP BY a.id ORDER BY a.name
    """).fetchall()
    return [{**dict(r), "checks": balance_checks(con, r["id"])} for r in rows]


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
