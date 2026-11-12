# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Local personal finance web app. Run: python -m financie -> http://localhost:8500"""
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from financie import db, gemma, importer, planning


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # created on startup, not on import, so tests can point db.DB_PATH elsewhere first
    db.init_db()
    con = db.get_db()
    try:   # movements imported before the pairing existed, or a pair whose other side came later
        importer.link_own_transfers(con)
    finally:
        con.close()
    yield


app = FastAPI(title="Financie", lifespan=lifespan)

STATIC = Path(__file__).parent / "static"
# the page's own files and the bundled libraries (Chart.js): nothing is loaded from the internet
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return {"gemma": gemma.available(), "model": gemma.MODEL}


# ---------- Overview ----------

@app.get("/api/overview")
def overview(date_from: str | None = None, date_to: str | None = None,
             month: str | None = None, accounts: str | None = None):
    """Income, expenses and transfers of the chosen accounts (comma-separated ids; all when
    omitted). Transfers are kept apart from income and expenses: between two chosen accounts
    they cancel out, so what remains is the money moved to or from the other accounts."""
    if not (date_from and date_to):
        m = month or date.today().strftime("%Y-%m")
        date_from, date_to = m + "-01", m + "-31"
    ids = [int(x) for x in (accounts or "").split(",") if x.strip().isdigit()]
    acc = f" AND t.account_id IN ({','.join('?' * len(ids))})" if accounts is not None else ""
    period = (date_from, date_to, *ids)
    con = db.get_db()
    inc, exp, transfers = con.execute(f"""
        SELECT COALESCE(SUM(CASE WHEN t.amount > 0 AND COALESCE(c.kind,'') != 'transfer' THEN t.amount END), 0),
               COALESCE(SUM(CASE WHEN t.amount < 0 AND COALESCE(c.kind,'') != 'transfer' THEN t.amount END), 0),
               COALESCE(SUM(CASE WHEN c.kind = 'transfer' THEN t.amount END), 0)
        FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.date BETWEEN ? AND ?{acc}""", period).fetchone()
    by_cat = [dict(r) for r in con.execute(f"""
        SELECT COALESCE(c.name,'—') AS category, c.key AS category_key,
               ROUND(SUM(-t.amount),2) AS spent
        FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.date BETWEEN ? AND ? AND t.amount < 0{acc}
          AND COALESCE(c.kind,'') != 'transfer'
        GROUP BY c.id ORDER BY spent DESC""", period)]
    monthly = [dict(r) for r in con.execute(f"""
        SELECT substr(t.date,1,7) AS month,
               ROUND(SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END),2) AS income,
               ROUND(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END),2) AS expense
        FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.date BETWEEN ? AND ? AND COALESCE(c.kind,'') != 'transfer'{acc}
        GROUP BY substr(t.date,1,7) ORDER BY month""", period)]
    con.close()
    return {"from": date_from, "to": date_to, "income": round(inc, 2), "expense": round(exp, 2),
            "transfers": round(transfers, 2), "by_category": by_cat, "monthly": monthly}


@app.get("/api/balances")
def balances(date_from: str, date_to: str, accounts: str | None = None):
    """End-of-day balances of the chosen accounts (comma-separated ids; all when omitted) for the
    balance chart, with their balance checks and a total."""
    con = db.get_db()
    try:
        ids = ([int(x) for x in accounts.split(",") if x.strip().isdigit()] if accounts is not None
               else [r["id"] for r in con.execute("SELECT id FROM accounts")])
        return db.balance_history(con, ids, date_from, date_to)
    finally:
        con.close()


# ---------- Plan: recurring payments and the outlook ----------

def _account_ids(accounts: str | None) -> list[int] | None:
    return None if accounts is None else [int(x) for x in accounts.split(",") if x.strip().isdigit()]


@app.get("/api/recurring")
def recurring_payments(accounts: str | None = None):
    """Recurring payments and income found in the transactions (planning.recurring)."""
    con = db.get_db()
    try:
        return planning.recurring(con, _account_ids(accounts))
    finally:
        con.close()


class RecurringIgnore(BaseModel):
    key: str
    ignored: bool = True


@app.post("/api/recurring/ignore")
def recurring_ignore(r: RecurringIgnore):
    """Mark a found group as not recurring (or undo it); it then counts as variable spending."""
    con = db.get_db()
    try:
        planning.set_ignored(con, r.key, r.ignored)
        return {"ok": True}
    finally:
        con.close()


@app.get("/api/forecast")
def forecast(accounts: str | None = None, floor: float = 0.0):
    """The next twelve months: expenses, the income needed to stay above `floor` (planning.forecast)."""
    con = db.get_db()
    try:
        return planning.forecast(con, _account_ids(accounts), floor)
    finally:
        con.close()


class Plan(BaseModel):
    name: str
    month: str
    amount: float


@app.put("/api/forecast/plans")
def forecast_plans(items: list[Plan]):
    """Planned extra expenses of the outlook (a holiday, a new appliance), month YYYY-MM."""
    for p in items:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", p.month) or p.amount <= 0:
            raise HTTPException(400, {"code": "invalid_plan", "info": p.name})
    con = db.get_db()
    try:
        return planning.set_plans(con, [p.model_dump() for p in items])
    finally:
        con.close()


# ---------- Transactions ----------

@app.get("/api/transactions/years")
def transaction_years():
    """The years that have transactions, newest first (for the period picker)."""
    con = db.get_db()
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT substr(date,1,4) AS y FROM transactions ORDER BY y DESC")]
    finally:
        con.close()


def _tx_filter(month=None, year=None, category_id=None, account_id=None, uncategorized=False,
               source=None, q=None, tag_id=None) -> tuple[str, list]:
    """The WHERE part shared by the transaction list and its total."""
    sql, params = " WHERE 1=1", []
    if month:
        sql += " AND substr(t.date,1,7) = ?"; params.append(month)
    if year:
        sql += " AND substr(t.date,1,4) = ?"; params.append(year)
    if category_id:
        sql += " AND t.category_id = ?"; params.append(category_id)
    if account_id:
        sql += " AND t.account_id = ?"; params.append(account_id)
    if uncategorized:
        sql += " AND t.category_id IS NULL"
    if source:   # e.g. "ai": the categories the AI guessed, for checking them
        sql += " AND t.category_id IS NOT NULL AND t.category_source = ?"; params.append(source)
    if q:
        sql += " AND (t.description LIKE ? OR t.counterparty LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if tag_id:
        sql += " AND t.id IN (SELECT transaction_id FROM transaction_tags WHERE tag_id = ?)"; params.append(tag_id)
    return sql, params


@app.get("/api/transactions")
def transactions(month: str | None = None, year: str | None = None, category_id: int | None = None,
                 account_id: int | None = None, uncategorized: bool = False, source: str | None = None,
                 q: str | None = None, tag_id: int | None = None, limit: int = 500):
    where, params = _tx_filter(month, year, category_id, account_id, uncategorized, source, q, tag_id)
    sql = f"""SELECT t.*, c.name AS category, c.key AS category_key, a.name AS account,
                     (SELECT json_group_array(json_object('id', g.id, 'name', g.name))
                      FROM transaction_tags tt JOIN tags g ON g.id = tt.tag_id
                      WHERE tt.transaction_id = t.id) AS tags
              FROM transactions t
              LEFT JOIN categories c ON c.id = t.category_id
              JOIN accounts a ON a.id = t.account_id{where}
              ORDER BY t.date DESC, t.id DESC LIMIT ?"""
    con = db.get_db()
    rows = [dict(r) for r in con.execute(sql, [*params, limit])]
    for r in rows:
        r["tags"] = json.loads(r["tags"] or "[]")
    if account_id:  # one account: the balance after each movement, as in online banking
        running = db.running_balances(con, account_id)
        for r in rows:
            r["running_balance"] = running.get(r["id"])
    con.close()
    return rows


@app.get("/api/transactions/total")
def transactions_total(month: str | None = None, year: str | None = None, category_id: int | None = None,
                       account_id: int | None = None, uncategorized: bool = False, source: str | None = None,
                       q: str | None = None, tag_id: int | None = None):
    """The sums of everything the same filter matches (no limit), e.g. what a trip cost:
    expenses and income without transfers, the transfers apart, and the expenses by category."""
    where, params = _tx_filter(month, year, category_id, account_id, uncategorized, source, q, tag_id)
    base = f"FROM transactions t LEFT JOIN categories c ON c.id = t.category_id{where}"
    con = db.get_db()
    try:
        count, expense, income, transfers = con.execute(f"""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN t.amount < 0 AND COALESCE(c.kind,'') != 'transfer' THEN t.amount END), 0),
                   COALESCE(SUM(CASE WHEN t.amount > 0 AND COALESCE(c.kind,'') != 'transfer' THEN t.amount END), 0),
                   COALESCE(SUM(CASE WHEN c.kind = 'transfer' THEN t.amount END), 0) {base}""", params).fetchone()
        by_category = [dict(r) for r in con.execute(f"""
            SELECT c.name AS category, c.key AS category_key, ROUND(SUM(t.amount), 2) AS amount {base}
              AND COALESCE(c.kind,'') != 'transfer'
            GROUP BY c.id ORDER BY SUM(t.amount)""", params)]
        return {"count": count, "expense": round(expense, 2), "income": round(income, 2),
                "net": round(expense + income, 2), "transfers": round(transfers, 2), "by_category": by_category}
    finally:
        con.close()


# ---------- Tags ----------

@app.get("/api/tags")
def tags():
    con = db.get_db()
    try:
        return [dict(r) for r in con.execute("""
            SELECT g.id, g.name, COUNT(tt.transaction_id) AS count
            FROM tags g LEFT JOIN transaction_tags tt ON tt.tag_id = g.id
            GROUP BY g.id ORDER BY g.name COLLATE NOCASE""")]
    finally:
        con.close()


class TagApply(BaseModel):
    tag: str
    transaction_ids: list[int]


@app.post("/api/tags/apply")
def tag_apply(body: TagApply):
    """Put a tag on transactions; a new name creates the tag (names ignore case)."""
    name = " ".join(body.tag.split())
    if not name:
        raise HTTPException(400, {"code": "tag_required"})
    con = db.get_db()
    try:
        con.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
        tag_id = con.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
        added = sum(con.execute("INSERT OR IGNORE INTO transaction_tags(transaction_id, tag_id) "
                                "SELECT id, ? FROM transactions WHERE id = ?", (tag_id, i)).rowcount
                    for i in body.transaction_ids)
        con.commit()
        return {"tag_id": tag_id, "added": added}
    finally:
        con.close()


class TagRemove(BaseModel):
    tag_id: int
    transaction_ids: list[int]


@app.post("/api/tags/remove")
def tag_remove(body: TagRemove):
    """Take a tag off transactions; a tag left without any is deleted."""
    con = db.get_db()
    try:
        removed = sum(con.execute("DELETE FROM transaction_tags WHERE tag_id=? AND transaction_id=?",
                                  (body.tag_id, i)).rowcount for i in body.transaction_ids)
        con.execute("DELETE FROM tags WHERE id=? AND NOT EXISTS "
                    "(SELECT 1 FROM transaction_tags WHERE tag_id=?)", (body.tag_id, body.tag_id))
        con.commit()
        return {"removed": removed}
    finally:
        con.close()


class TagRename(BaseModel):
    name: str


@app.patch("/api/tags/{tag_id}")
def tag_rename(tag_id: int, body: TagRename):
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(400, {"code": "tag_required"})
    con = db.get_db()
    try:
        taken = con.execute("SELECT id FROM tags WHERE name=? AND id != ?", (name, tag_id)).fetchone()
        if taken:
            raise HTTPException(400, {"code": "tag_exists", "info": name})
        con.execute("UPDATE tags SET name=? WHERE id=?", (name, tag_id))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.delete("/api/tags/{tag_id}")
def tag_delete(tag_id: int):
    """Delete a tag; its transactions stay, they only lose the tag."""
    con = db.get_db()
    try:
        con.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


class TxPatch(BaseModel):
    category_id: int | None = None
    description: str | None = None


def _similar(con, tx_id: int, category_id: int | None) -> tuple[str, str, list[int]] | None:
    """Other transactions of the same payment (same counterparty, else description) that would
    take the new category: uncategorized ones and those a rule, the learning or the AI chose —
    never another choice of the user's own."""
    tx = con.execute("SELECT counterparty, description FROM transactions WHERE id=?", (tx_id,)).fetchone()
    same = tx and importer.similar_field(tx)
    if not same:
        return None
    field, value = same
    ids = [r[0] for r in con.execute(
        f"""SELECT id FROM transactions WHERE {field} = ? AND id != ?
            AND COALESCE(category_source, '') != 'user'
            AND COALESCE(category_id, -1) != COALESCE(?, -1)""", (value, tx_id, category_id))]
    return field, value, ids


@app.patch("/api/transactions/{tx_id}")
def patch_tx(tx_id: int, patch: TxPatch):
    """A category set here is the user's own choice. The answer counts the similar transactions
    the same choice could go to (`similar`), for the UI to offer it."""
    con = db.get_db()
    try:
        out = {"ok": True, "similar": 0}
        if "category_id" in patch.model_fields_set:
            cid = patch.category_id
            con.execute("UPDATE transactions SET category_id=?, category_source=? WHERE id=?",
                        (cid, "user" if cid else None, tx_id))
            same = cid and _similar(con, tx_id, cid)
            if same:
                out.update(similar=len(same[2]), similar_to=same[1])
        if patch.description is not None:
            con.execute("UPDATE transactions SET description=? WHERE id=?", (patch.description, tx_id))
        con.commit()
        return out
    finally:
        con.close()


@app.post("/api/transactions/{tx_id}/apply-similar")
def apply_similar(tx_id: int):
    """Give the similar transactions (see _similar) the category the user just chose for this one."""
    con = db.get_db()
    try:
        cid = con.execute("SELECT category_id FROM transactions WHERE id=?", (tx_id,)).fetchone()
        same = cid and _similar(con, tx_id, cid[0])
        ids = same[2] if same else []
        con.executemany("UPDATE transactions SET category_id=?, category_source=? WHERE id=?",
                        [(cid[0], "user" if cid[0] else None, i) for i in ids])
        con.commit()
        return {"changed": len(ids)}
    finally:
        con.close()


@app.delete("/api/transactions/{tx_id}")
def delete_tx(tx_id: int):
    con = db.get_db()
    con.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    con.commit(); con.close()
    return {"ok": True}


@app.post("/api/categorize")
def categorize(limit: int = 200):
    return importer.categorize_uncategorized(limit)


# ---------- Lookup tables ----------

@app.get("/api/categories")
def categories():
    con = db.get_db()
    rows = [dict(r) for r in con.execute("SELECT * FROM categories ORDER BY kind, name")]
    con.close()
    return rows


class CategoryIn(BaseModel):
    name: str
    kind: str = "expense"


@app.post("/api/categories")
def add_category(cat: CategoryIn):
    if not cat.name.strip():
        raise HTTPException(400, {"code": "empty_name"})
    con = db.get_db()
    try:
        con.execute("INSERT INTO categories(name, kind) VALUES (?,?)",
                    (cat.name.strip(), cat.kind))
        con.commit()
    except Exception:
        raise HTTPException(400, {"code": "duplicate_category"})
    finally:
        con.close()
    return {"ok": True}


@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int):
    con = db.get_db()
    con.execute("UPDATE transactions SET category_id=NULL WHERE category_id=?", (cat_id,))
    con.execute("DELETE FROM budgets WHERE category_id=?", (cat_id,))
    con.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    con.commit(); con.close()
    return {"ok": True}


@app.get("/api/accounts")
def accounts():
    con = db.get_db()
    rows = db.account_balances(con)
    con.close()
    return rows


class AccountIn(BaseModel):
    name: str
    type: str = "checking"
    currency: str = "EUR"
    opening_balance: float = 0


@app.post("/api/accounts")
def add_account(a: AccountIn):
    name = a.name.strip()
    if not name:  # a nameless account cannot be told apart in the overview or the import picker
        raise HTTPException(400, {"code": "name_required"})
    con = db.get_db()
    try:
        if con.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
            raise HTTPException(400, {"code": "name_taken", "info": name})
        con.execute("INSERT INTO accounts(name,type,currency,opening_balance) VALUES (?,?,?,?)",
                    (name, a.type, a.currency, a.opening_balance))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


class AccountPatch(BaseModel):
    is_main: bool | None = None
    balance_amount: float | None = None     # null clears the known balance
    balance_date: str | None = None         # YYYY-MM-DD, today when omitted


@app.patch("/api/accounts/{account_id}")
def patch_account(account_id: int, patch: AccountPatch):
    """Main-account flag and the known balance; only the fields sent are changed."""
    sent = patch.model_fields_set
    if "balance_amount" in sent and patch.balance_amount is not None:
        try:
            when = date.fromisoformat(patch.balance_date) if patch.balance_date else date.today()
        except ValueError:
            raise HTTPException(400, {"code": "invalid_date"})
    con = db.get_db()
    if patch.is_main is not None:
        con.execute("UPDATE accounts SET is_main=? WHERE id=?", (int(patch.is_main), account_id))
    if "balance_amount" in sent:
        known = (when.isoformat(), patch.balance_amount) if patch.balance_amount is not None else (None, None)
        con.execute("UPDATE accounts SET balance_date=?, balance_amount=? WHERE id=?", (*known, account_id))
    con.commit(); con.close()
    return {"ok": True}


class BalanceCheckIn(BaseModel):
    amount: float
    date: str | None = None                 # YYYY-MM-DD, today when omitted


@app.post("/api/accounts/{account_id}/checks")
def add_balance_check(account_id: int, check: BalanceCheckIn):
    """Compare the real balance at the end of a day with the computed one."""
    try:
        when = date.fromisoformat(check.date) if check.date else date.today()
    except ValueError:
        raise HTTPException(400, {"code": "invalid_date"})
    con = db.get_db()
    result = db.add_balance_check(con, account_id, when.isoformat(), check.amount)
    con.close()
    return result


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int):
    con = db.get_db()
    n = con.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?",
                    (account_id,)).fetchone()[0]
    if n:
        con.close()
        raise HTTPException(400, {"code": "account_not_empty", "info": str(n)})
    con.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    con.commit(); con.close()
    return {"ok": True}


# ---------- Budget ----------

@app.get("/api/budget")
def budget(month: str | None = None):
    month = month or date.today().strftime("%Y-%m")
    con = db.get_db()
    rows = [dict(r) for r in con.execute("""
        SELECT c.id AS category_id, c.name AS category, c.key AS category_key,
               COALESCE(bm.amount, ba.amount) AS budget,
               ROUND(COALESCE((SELECT SUM(-t.amount) FROM transactions t
                     WHERE t.category_id = c.id AND t.amount < 0
                       AND substr(t.date,1,7) = ?), 0), 2) AS spent
        FROM categories c
        LEFT JOIN budgets bm ON bm.category_id = c.id AND bm.month = ?
        LEFT JOIN budgets ba ON ba.category_id = c.id AND ba.month = '*'
        WHERE c.kind = 'expense' ORDER BY c.name""", (month, month))]
    con.close()
    return {"month": month, "items": rows}


class BudgetIn(BaseModel):
    category_id: int
    amount: float
    month: str = "*"


@app.put("/api/budget")
def set_budget(b: BudgetIn):
    con = db.get_db()
    if b.amount <= 0:
        con.execute("DELETE FROM budgets WHERE category_id=? AND month=?", (b.category_id, b.month))
    else:
        con.execute("""INSERT INTO budgets(category_id, month, amount) VALUES (?,?,?)
                       ON CONFLICT(category_id, month) DO UPDATE SET amount=excluded.amount""",
                    (b.category_id, b.month, b.amount))
    con.commit(); con.close()
    return {"ok": True}


# ---------- Assets / net worth ----------

@app.get("/api/networth")
def networth():
    con = db.get_db()
    result = db.net_worth(con)
    history = [dict(r) for r in con.execute("""
        SELECT date, ROUND(SUM(value),2) AS assets_total FROM asset_values
        GROUP BY date ORDER BY date""")]
    con.close()
    result["history"] = history
    return result


class AssetIn(BaseModel):
    name: str
    type: str = "other"
    currency: str = "EUR"
    note: str = ""


@app.post("/api/assets")
def add_asset(a: AssetIn):
    con = db.get_db()
    con.execute("INSERT INTO assets(name,type,currency,note) VALUES (?,?,?,?)",
                (a.name, a.type, a.currency, a.note))
    con.commit(); con.close()
    return {"ok": True}


@app.delete("/api/assets/{asset_id}")
def delete_asset(asset_id: int):
    con = db.get_db()
    con.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    con.commit(); con.close()
    return {"ok": True}


class AssetValueIn(BaseModel):
    asset_id: int
    date: str
    value: float


@app.post("/api/asset_values")
def add_asset_value(v: AssetValueIn):
    con = db.get_db()
    con.execute("""INSERT INTO asset_values(asset_id, date, value) VALUES (?,?,?)
                   ON CONFLICT(asset_id, date) DO UPDATE SET value=excluded.value""",
                (v.asset_id, v.date, v.value))
    con.commit(); con.close()
    return {"ok": True}


# ---------- Import ----------

def _import_bytes(account_id: int, filename: str, data: bytes) -> dict:
    """Import one statement file and record the attempt in the import history, failed ones too."""
    name, digest = Path(filename).name, importer.file_hash(data)
    con = db.get_db()
    done = con.execute("SELECT id FROM imports WHERE file_hash=? AND account_id=? AND status='ok'",
                       (digest, account_id)).fetchone()
    con.close()
    if done:
        # dedup cannot be trusted for Gemma: it words the same statement differently on every read
        raise HTTPException(400, {"code": "already_imported", "info": f"#{done['id']}"})
    error, txs, fmt = None, [], None
    if importer.file_kind(name) in ("pdf", "image") and not gemma.available():
        error = {"code": "ollama_missing"}
    else:
        try:
            txs, fmt = importer.parse_statement(name, data)
            if not txs:
                error = {"code": "no_transactions"}
        except Exception as e:
            error = {"code": "parse_failed", "info": str(e)}
    con = db.get_db()
    if error:
        con.execute("""INSERT INTO imports(file_name, file_hash, account_id, status, error_code, error_info)
                       VALUES (?,?,?,'error',?,?)""",
                    (name, digest, account_id, error["code"], error.get("info")))
        con.commit(); con.close()
        raise HTTPException(400, error)
    dates = sorted(t["date"] for t in txs)
    import_id = con.execute(
        """INSERT INTO imports(file_name, file_hash, account_id, format, date_from, date_to, status)
           VALUES (?,?,?,?,?,?,'ok')""",
        (name, digest, account_id, fmt, dates[0], dates[-1])).lastrowid
    con.commit()
    result = importer.import_transactions(account_id, name, txs, import_id=import_id)
    con.execute("UPDATE imports SET inserted=?, skipped_duplicates=?, possible_duplicates=? WHERE id=?",
                (result["inserted"], result["skipped_duplicates"], result["possible_duplicates"], import_id))
    con.commit()
    described = _describe(name, data)
    if described["key"]:  # next time a file from this source goes to this account by itself
        db.remember_source(con, described["key"], account_id)
    balance = _statement_balance(con, account_id, txs)
    con.close()
    return {"format": fmt, "import_id": import_id, **result, "balance": balance,
            "no_amount": described.get("no_amount", 0)}


def _statement_balance(con, account_id: int, txs: list[dict]) -> dict | None:
    """Balances a statement states (PayPal's Guthaben, CAMT's booked balance per day): the latest
    becomes the known balance of an account without one, and every other is a balance check.
    The result names the latest balance and how many of the checked days are off."""
    stated = {t["date"]: t["day_balance"] for t in txs if "day_balance" in t}
    if not stated:
        return None
    last = max(stated)
    out = {"date": last, "amount": stated[last], "known": False, "difference": 0}
    known = con.execute("SELECT balance_date FROM accounts WHERE id=?", (account_id,)).fetchone()
    if known["balance_date"] is None:
        con.execute("UPDATE accounts SET balance_date=?, balance_amount=? WHERE id=?",
                    (last, stated.pop(last), account_id))
        con.commit()
        out["known"] = True
    checks = db.add_balance_checks(con, account_id, stated, "statement")
    off = [c for c in checks if c["difference"]]
    if checks and not out["known"]:
        out["difference"] = checks[-1]["difference"]
    out.update(days=len(checks), off=len(off), first_off=off[0]["date"] if off else None,
               first_difference=off[0]["difference"] if off else None,
               gaps=db.balance_gaps(con, account_id))
    return out


@app.post("/api/import")
async def import_file(account_id: int = Form(...), file: UploadFile = File(...)):
    return _import_bytes(account_id, file.filename, await file.read())


def _describe(filename: str, data: bytes) -> dict:
    try:
        return importer.describe_statement(filename, data)
    except Exception as e:
        return {"file": filename, "key": None, "error": str(e)}


def _file_state(con, name: str, digest: str) -> tuple[str, dict | None]:
    """new / imported / failed / changed, judged by the content and the import history.
    The content decides, so a renamed copy of an imported file still counts as imported."""
    def last(where, *args):
        row = con.execute(f"""SELECT imported_at, status, error_code, inserted, skipped_duplicates,
                                     account_id FROM imports WHERE {where} ORDER BY id DESC LIMIT 1""", args).fetchone()
        return dict(row) if row else None

    if done := last("file_hash=? AND status='ok'", digest):
        return "imported", done
    if failed := last("file_hash=? AND status='error'", digest):
        return "failed", failed
    if before := last("file_name=? AND status='ok'", name):
        return "changed", before  # same name, other content (e.g. PayPal's Download.CSV)
    return "new", None


def _preview_entry(con, accounts_by_name: dict, name: str, data: bytes) -> dict:
    d = _describe(name, data)
    proposal = d.get("proposal")
    d["account_id"] = (db.source_account(con, d.get("key"))
                       or (accounts_by_name.get(proposal["name"]) if proposal else None))
    d["kind"] = importer.file_kind(name)
    d["state"], d["last"] = _file_state(con, Path(name).name, importer.file_hash(data))
    if not d["account_id"] and d["last"]:  # a file without a known source: where it went last time
        d["account_id"] = d["last"]["account_id"]
    return d


def _accounts_by_name(con) -> dict:
    return {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts")}


@app.post("/api/import/preview")
async def import_preview(files: list[UploadFile] = File(...)):
    """For each file: format, period, count, the account it should go to and whether it was
    imported before. Writes nothing."""
    con = db.get_db()
    by_name = _accounts_by_name(con)
    out = [_preview_entry(con, by_name, f.filename, await f.read()) for f in files]
    con.close()
    return out


# A transaction t of an import and an earlier one o with the same date and amount, any account.
# A PayPal payment and its bank debit match too, but the debit is marked a transfer.
_TWINS_JOIN = """
    FROM transactions t JOIN transactions o
      ON o.date = t.date AND ROUND(o.amount - t.amount, 2) = 0 AND o.id != t.id
     AND (o.import_id < t.import_id OR o.import_id IS NULL)"""
_TWINS_WHERE = """
    WHERE t.import_id IS NOT NULL
      AND COALESCE(t.category_id, 0) NOT IN (SELECT id FROM categories WHERE kind='transfer')
      AND COALESCE(o.category_id, 0) NOT IN (SELECT id FROM categories WHERE kind='transfer')"""


@app.get("/api/imports")
def import_history(limit: int = 200):
    """Every import with the transactions it still has, and how many of them look like a
    movement already brought by an earlier import: same date and amount, any account."""
    con = db.get_db()
    rows = [dict(r) for r in con.execute("""
        SELECT i.*, a.name AS account,
               (SELECT COUNT(*) FROM transactions t WHERE t.import_id = i.id) AS remaining
        FROM imports i LEFT JOIN accounts a ON a.id = i.account_id
        ORDER BY i.id DESC LIMIT ?""", (limit,))]
    twins = {r["import_id"]: dict(r) for r in con.execute(f"""
        SELECT t.import_id, COUNT(DISTINCT t.id) AS duplicates,
               GROUP_CONCAT(DISTINCT COALESCE(o.import_id, 0)) AS duplicate_of
        {_TWINS_JOIN} {_TWINS_WHERE}
        GROUP BY t.import_id""")}
    con.close()
    for r in rows:
        twin = twins.get(r["id"], {})
        r["duplicates"] = twin.get("duplicates", 0)
        # ids of the earlier imports; 0 stands for transactions from before the import history
        r["duplicate_of"] = sorted(int(x) for x in twin["duplicate_of"].split(",")) if twin else []
    return rows


@app.get("/api/imports/{import_id}/duplicates")
def import_duplicates(import_id: int):
    """The pairs behind the duplicate count of an import: its transaction and the earlier one,
    so the user can tell a real duplicate from an unlinked transfer or a coincidence."""
    con = db.get_db()
    side = lambda x: f"""{x}.id AS {x}_id, {x}.import_id AS {x}_import, a{x}.name AS {x}_account,
        {x}.description AS {x}_description, {x}.counterparty AS {x}_counterparty"""
    rows = [dict(r) for r in con.execute(f"""
        SELECT t.date, t.amount, {side("t")}, {side("o")}
        {_TWINS_JOIN}
        JOIN accounts at ON at.id = t.account_id JOIN accounts ao ON ao.id = o.account_id
        {_TWINS_WHERE} AND t.import_id = ?
        ORDER BY t.date, t.id, o.id""", (import_id,))]
    con.close()
    return [{"date": r["date"], "amount": r["amount"],
             **{key: {"id": r[f"{x}_id"], "import_id": r[f"{x}_import"], "account": r[f"{x}_account"],
                      "description": r[f"{x}_description"], "counterparty": r[f"{x}_counterparty"]}
                for key, x in (("tx", "t"), ("twin", "o"))}} for r in rows]


@app.delete("/api/imports/{import_id}")
def undo_import(import_id: int):
    """Delete the transactions an import brought; the history keeps the entry as 'undone',
    so the file counts as not imported again."""
    con = db.get_db()
    deleted = con.execute("DELETE FROM transactions WHERE import_id=?", (import_id,)).rowcount
    con.execute("UPDATE imports SET status='undone' WHERE id=? AND status='ok'", (import_id,))
    con.commit(); con.close()
    return {"deleted": deleted}


# ---------- Statements folder ----------

def _imports_dir(con) -> str | None:
    return db.get_setting(con, "imports_dir", db.IMPORTS_DIR)


@app.get("/api/settings")
def settings():
    con = db.get_db()
    result = {"imports_dir": _imports_dir(con)}
    con.close()
    return result


class SettingsIn(BaseModel):
    imports_dir: str | None = None


@app.put("/api/settings")
def save_settings(s: SettingsIn):
    folder = (s.imports_dir or "").strip().strip('"')
    if folder and not Path(folder).is_dir():
        raise HTTPException(400, {"code": "folder_missing", "info": folder})
    con = db.get_db()
    db.set_setting(con, "imports_dir", folder or None)  # empty: back to FINANCIE_IMPORTS
    result = {"imports_dir": _imports_dir(con)}
    con.close()
    return result


@app.get("/api/folder")
def folder():
    """Statement files in the folder (and its subfolders): what they are and whether they were
    imported. Reads the files, writes nothing; importing waits for POST /api/folder/import."""
    con = db.get_db()
    root = _imports_dir(con)
    if not root:
        con.close()
        return {"dir": None, "files": []}
    base = Path(root)
    if not base.is_dir():
        con.close()
        raise HTTPException(400, {"code": "folder_missing", "info": root})
    by_name = _accounts_by_name(con)
    files = []
    for f in sorted(f for f in base.rglob("*") if f.is_file() and importer.file_kind(f.name)):
        d = _preview_entry(con, by_name, f.name, f.read_bytes())
        d["path"] = f.relative_to(base).as_posix()
        d["modified"] = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        files.append(d)
    con.close()
    return {"dir": str(base), "files": files}


class FolderImportIn(BaseModel):
    path: str           # relative to the statements folder, as GET /api/folder lists it
    account_id: int


@app.post("/api/folder/import")
def folder_import(req: FolderImportIn):
    con = db.get_db()
    root = _imports_dir(con)
    con.close()
    base = Path(root or "").resolve()
    file = (base / req.path).resolve()
    if not root or base not in file.parents or not file.is_file():  # nothing outside the folder
        raise HTTPException(400, {"code": "file_missing", "info": req.path})
    return _import_bytes(req.account_id, file.name, file.read_bytes())
