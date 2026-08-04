"""Lokálna web appka na správu financií. Spustenie: python app.py -> http://localhost:8500"""
from datetime import date
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import db
import gemma
import importer

app = FastAPI(title="Financie")
db.init_db()

STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return {"gemma": gemma.available(), "model": gemma.MODEL}


# ---------- Prehľad ----------

@app.get("/api/overview")
def overview(date_from: str | None = None, date_to: str | None = None,
             month: str | None = None):
    if not (date_from and date_to):
        m = month or date.today().strftime("%Y-%m")
        date_from, date_to = m + "-01", m + "-31"
    con = db.get_db()
    # prevody medzi účtami (kind='transfer') sa do príjmov/výdavkov nepočítajú
    inc, exp = con.execute("""
        SELECT COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount END), 0),
               COALESCE(SUM(CASE WHEN t.amount < 0 THEN t.amount END), 0)
        FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.date BETWEEN ? AND ? AND COALESCE(c.kind,'') != 'transfer'""",
        (date_from, date_to)).fetchone()
    by_cat = [dict(r) for r in con.execute("""
        SELECT COALESCE(c.name,'—') AS category, ROUND(SUM(-t.amount),2) AS spent
        FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.date BETWEEN ? AND ? AND t.amount < 0
          AND COALESCE(c.kind,'') != 'transfer'
        GROUP BY c.name ORDER BY spent DESC""", (date_from, date_to))]
    monthly = [dict(r) for r in con.execute("""
        SELECT substr(t.date,1,7) AS month,
               ROUND(SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END),2) AS income,
               ROUND(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END),2) AS expense
        FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.date BETWEEN ? AND ? AND COALESCE(c.kind,'') != 'transfer'
        GROUP BY substr(t.date,1,7) ORDER BY month""", (date_from, date_to))]
    con.close()
    return {"from": date_from, "to": date_to, "income": round(inc, 2),
            "expense": round(exp, 2), "by_category": by_cat, "monthly": monthly}


# ---------- Transakcie ----------

@app.get("/api/transactions")
def transactions(month: str | None = None, category_id: int | None = None,
                 account_id: int | None = None, uncategorized: bool = False,
                 q: str | None = None, limit: int = 500):
    sql = """SELECT t.*, c.name AS category, a.name AS account
             FROM transactions t
             LEFT JOIN categories c ON c.id = t.category_id
             JOIN accounts a ON a.id = t.account_id WHERE 1=1"""
    params = []
    if month:
        sql += " AND substr(t.date,1,7) = ?"; params.append(month)
    if category_id:
        sql += " AND t.category_id = ?"; params.append(category_id)
    if account_id:
        sql += " AND t.account_id = ?"; params.append(account_id)
    if uncategorized:
        sql += " AND t.category_id IS NULL"
    if q:
        sql += " AND (t.description LIKE ? OR t.counterparty LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY t.date DESC, t.id DESC LIMIT ?"; params.append(limit)
    con = db.get_db()
    rows = [dict(r) for r in con.execute(sql, params)]
    con.close()
    return rows


class TxPatch(BaseModel):
    category_id: int | None = None
    description: str | None = None


@app.patch("/api/transactions/{tx_id}")
def patch_tx(tx_id: int, patch: TxPatch):
    con = db.get_db()
    if patch.category_id is not None:
        con.execute("UPDATE transactions SET category_id=? WHERE id=?", (patch.category_id, tx_id))
    if patch.description is not None:
        con.execute("UPDATE transactions SET description=? WHERE id=?", (patch.description, tx_id))
    con.commit(); con.close()
    return {"ok": True}


@app.delete("/api/transactions/{tx_id}")
def delete_tx(tx_id: int):
    con = db.get_db()
    con.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    con.commit(); con.close()
    return {"ok": True}


@app.post("/api/categorize")
def categorize(limit: int = 200):
    return importer.categorize_uncategorized(limit)


# ---------- Číselníky ----------

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
    type: str = "bezny"
    currency: str = "EUR"
    opening_balance: float = 0


@app.post("/api/accounts")
def add_account(a: AccountIn):
    con = db.get_db()
    con.execute("INSERT INTO accounts(name,type,currency,opening_balance) VALUES (?,?,?,?)",
                (a.name, a.type, a.currency, a.opening_balance))
    con.commit(); con.close()
    return {"ok": True}


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


# ---------- Rozpočet ----------

@app.get("/api/budget")
def budget(month: str | None = None):
    month = month or date.today().strftime("%Y-%m")
    con = db.get_db()
    rows = [dict(r) for r in con.execute("""
        SELECT c.id AS category_id, c.name AS category,
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


# ---------- Majetok / net worth ----------

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
    type: str = "ine"
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

@app.post("/api/import")
async def import_file(account_id: int = Form(...), file: UploadFile = File(...)):
    data = await file.read()
    ext = Path(file.filename).suffix.lower()
    needs_gemma = ext in ({".pdf"} | importer.IMG_EXT)
    if needs_gemma and not gemma.available():
        raise HTTPException(400, {"code": "ollama_missing"})
    try:
        txs = importer.parse_file(file.filename, data)
    except Exception as e:
        raise HTTPException(400, {"code": "parse_failed", "info": str(e)})
    if not txs:
        raise HTTPException(400, {"code": "no_transactions"})
    return importer.import_transactions(account_id, file.filename, txs)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8500)
