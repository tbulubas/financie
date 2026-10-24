# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Bank statement import: CSV/XLSX deterministically, PDF and images via local Gemma."""
import csv
import io
import re
from datetime import datetime
from pathlib import Path

from financie import db, gemma

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}

# Common column names of Slovak/Czech/German banks — tried first, without the LLM
COLUMN_ALIASES = {
    "date": ["dátum", "datum", "dátum valuty", "datum valuty", "dátum zaúčtovania",
             "datum zauctovania", "date", "booking date", "value date",
             "buchungstag", "buchungsdatum", "valutadatum", "wertstellung"],
    "amount": ["suma", "čiastka", "ciastka", "amount", "suma v mene účtu", "objem",
               "betrag", "betrag (eur)", "umsatz"],
    "debit": ["debet", "debit", "má dať", "ma dat", "soll"],
    "credit": ["kredit", "credit", "dal", "haben"],
    "description": ["popis", "popis transakcie", "poznámka", "poznamka", "správa pre prijímateľa",
                    "sprava pre prijimatela", "description", "detail", "informácia pre klienta",
                    "verwendungszweck", "buchungstext", "beschreibung", "umsatzbeschreibung"],
    "counterparty": ["protiúčet", "protiucet", "názov protiúčtu", "nazov protiuctu", "obchodník",
                     "obchodnik", "príjemca", "prijemca", "counterparty", "merchant", "partner",
                     "beguenstigter/zahlungspflichtiger", "begünstigter/zahlungspflichtiger",
                     "name zahlungsbeteiligter", "zahlungsbeteiligter", "empfänger", "empfaenger",
                     "auftraggeber/empfänger", "auftraggeber/empfaenger"],
    "currency": ["mena", "currency", "waehrung", "währung"],
}

DATE_FORMATS = ["%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y", "%m/%d/%Y", "%Y/%m/%d"]


def _norm(s: str) -> str:
    return (s or "").strip().lower().lstrip("﻿")


def _parse_amount(val) -> float | None:
    if val is None:
        return None
    s = str(val).strip().replace("\xa0", "").replace(" ", "")
    if not s or s in ("-", ""):
        return None
    s = s.replace(",", ".")
    # keep only the last dot as the decimal separator (1.234.56 -> 1234.56)
    if s.count(".") > 1:
        parts = s.split(".")
        s = "".join(parts[:-1]) + "." + parts[-1]
    s = re.sub(r"[^0-9.\-+]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(val, fmt_hint: str | None = None) -> str | None:
    s = str(val).strip()
    fmts = ([fmt_hint] if fmt_hint else []) + DATE_FORMATS
    for f in fmts:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
    return None


def _auto_map(header: list[str]) -> dict:
    mapping = {}
    normed = {_norm(h): h for h in header}
    for field, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if a in normed:
                mapping[field] = normed[a]
                break
    return mapping


def _rows_to_transactions(header: list[str], rows: list[dict], mapping: dict) -> list[dict]:
    txs = []
    fmt = mapping.get("date_format")
    for row in rows:
        date = _parse_date(row.get(mapping.get("date", ""), ""), fmt)
        if not date:
            continue
        amount = None
        if mapping.get("amount"):
            amount = _parse_amount(row.get(mapping["amount"]))
        if amount is None and (mapping.get("debit") or mapping.get("credit")):
            debit = _parse_amount(row.get(mapping.get("debit", ""))) or 0
            credit = _parse_amount(row.get(mapping.get("credit", ""))) or 0
            amount = credit - abs(debit)
        if amount is None:
            continue
        txs.append({
            "date": date,
            "amount": amount,
            "currency": (row.get(mapping.get("currency", ""), "") or "EUR").strip() or "EUR",
            "description": str(row.get(mapping.get("description", ""), "") or "").strip(),
            "counterparty": str(row.get(mapping.get("counterparty", ""), "") or "").strip(),
        })
    return txs


def parse_csv(data: bytes) -> list[dict]:
    text = None
    for enc in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t")
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = list(reader)
    header = reader.fieldnames or []
    mapping = _auto_map(header)
    if "date" not in mapping or ("amount" not in mapping and "debit" not in mapping):
        # unknown format -> let Gemma map the columns (locally)
        if gemma.available():
            sample = [list(r.values()) for r in rows[:5]]
            try:
                mapping = {k: v for k, v in gemma.map_csv_columns(header, sample).items() if v}
            except Exception:
                pass
    txs = _rows_to_transactions(header, rows, mapping)
    if not txs:
        raise ValueError(
            f"columns or date format not recognized. "
            f"File header: {header}. Mapping used: {mapping or 'none'}. "
            f"Rows in file: {len(rows)}.")
    return txs


def _read_xlsx_rows(data: bytes) -> list[list[str]]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.active
    return [[("" if c is None else str(c)) for c in r] for r in ws.iter_rows(values_only=True)]


def _read_xls_rows(data: bytes) -> list[list[str]]:
    """Legacy binary .xls (BIFF) — used e.g. by credit card statements."""
    import xlrd
    wb = xlrd.open_workbook(file_contents=data)
    ws = wb.sheet_by_index(0)
    return [[("" if c is None else str(c)) for c in ws.row_values(i)]
            for i in range(ws.nrows)]


def parse_xlsx(data: bytes) -> list[dict]:
    # detect by file signature: OLE (D0CF11E0) = legacy .xls, PK = .xlsx
    if data[:4] == b"\xd0\xcf\x11\xe0":
        all_rows = _read_xls_rows(data)
    else:
        all_rows = _read_xlsx_rows(data)
    # find the header row (first row where date and amount can be mapped)
    header_idx, mapping = 0, {}
    for i, r in enumerate(all_rows[:20]):
        m = _auto_map(r)
        if "date" in m and ("amount" in m or "debit" in m):
            header_idx, mapping = i, m
            break
    header = all_rows[header_idx]
    rows = [dict(zip(header, r)) for r in all_rows[header_idx + 1:]]
    if not mapping and gemma.available():
        sample = [list(r.values()) for r in rows[:5]]
        try:
            mapping = {k: v for k, v in gemma.map_csv_columns(header, sample).items() if v}
        except Exception:
            pass
    txs = _rows_to_transactions(header, rows, mapping)
    if not txs:
        raise ValueError(
            f"columns or date format not recognized. "
            f"Header found: {header}. Mapping used: {mapping or 'none'}. "
            f"Rows in file: {len(rows)}.")
    return txs


def parse_pdf(data: bytes) -> list[dict]:
    import fitz  # PyMuPDF
    doc = fitz.open(stream=data, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)
    if len(text.strip()) > 100:
        return gemma.extract_transactions(text=text)
    # scanned PDF without a text layer -> render pages to images for vision
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=150)
        images.append(pix.tobytes("png"))
    txs = []
    for img in images:  # page by page so the model is not overloaded
        txs.extend(gemma.extract_transactions(images=[img]))
    return txs


def parse_image(data: bytes) -> list[dict]:
    return gemma.extract_transactions(images=[data])


def parse_file(filename: str, data: bytes) -> list[dict]:
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        return parse_csv(data)
    if ext in (".xlsx", ".xls"):
        return parse_xlsx(data)
    if ext == ".pdf":
        return parse_pdf(data)
    if ext in IMG_EXT:
        return parse_image(data)
    raise ValueError(f"Unsupported format: {ext}")


def _learned_category(con, tx: dict) -> int | None:
    """If a similar transaction was categorized before, reuse its category (no LLM)."""
    for field in ("counterparty", "description"):
        val = (tx.get(field) or "").strip()
        if len(val) < 3:
            continue
        row = con.execute(
            f"""SELECT category_id, COUNT(*) c FROM transactions
                WHERE {field} = ? AND category_id IS NOT NULL
                GROUP BY category_id ORDER BY c DESC LIMIT 1""", (val,)).fetchone()
        if row:
            return row["category_id"]
    return None


def import_transactions(account_id: int, filename: str, txs: list[dict],
                        use_gemma_categorize: bool = False) -> dict:
    con = db.get_db()
    cat_rows = con.execute("SELECT id, name FROM categories").fetchall()
    cat_by_name = {r["name"]: r["id"] for r in cat_rows}

    inserted, skipped, to_categorize = [], 0, []
    for tx in txs:
        h = db.tx_hash(account_id, tx["date"], tx["amount"], tx.get("description", ""))
        cat_id = _learned_category(con, tx)
        try:
            cur = con.execute(
                """INSERT INTO transactions
                   (account_id, date, amount, currency, description, counterparty,
                    category_id, source_file, import_hash)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (account_id, tx["date"], tx["amount"], tx.get("currency", "EUR"),
                 tx.get("description", ""), tx.get("counterparty", ""),
                 cat_id, filename, h))
            tx_id = cur.lastrowid
            inserted.append((tx_id, tx))
            if cat_id is None:
                to_categorize.append((tx_id, tx))
        except Exception:  # duplicate import_hash
            skipped += 1
    con.commit()

    categorized = 0
    if use_gemma_categorize and to_categorize and gemma.available():
        categorized = _gemma_batch_categorize(con, to_categorize, cat_by_name)
    con.close()
    return {"inserted": len(inserted), "skipped_duplicates": skipped,
            "auto_categorized": categorized + len(inserted) - len(to_categorize),
            "uncategorized": len(to_categorize) - categorized}


def _gemma_batch_categorize(con, items: list[tuple[int, dict]], cat_by_name: dict) -> int:
    """Categorize transactions in batches via Gemma. Errors are logged to the terminal."""
    names = list(cat_by_name.keys())
    categorized, batch = 0, 15
    total = (len(items) + batch - 1) // batch
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        try:
            cats = gemma.categorize([t for _, t in chunk], names)
            for (tx_id, _), cat_name in zip(chunk, cats):
                if cat_name in cat_by_name:
                    con.execute("UPDATE transactions SET category_id=? WHERE id=?",
                                (cat_by_name[cat_name], tx_id))
                    categorized += 1
            con.commit()
            print(f"[categorize] batch {i//batch+1}/{total} OK")
        except Exception as e:
            print(f"[categorize] batch {i//batch+1}/{total} failed: {e}")
    return categorized


def categorize_uncategorized(limit: int = 200) -> dict:
    """Categorize uncategorized transactions: learned categories first, the rest via Gemma."""
    con = db.get_db()
    cat_by_name = {r["name"]: r["id"] for r in
                   con.execute("SELECT id, name FROM categories").fetchall()}
    rows = con.execute("""SELECT id, date, amount, description, counterparty
                          FROM transactions WHERE category_id IS NULL
                          ORDER BY date DESC LIMIT ?""", (limit,)).fetchall()
    learned, remaining = 0, []
    for r in rows:
        tx = dict(r)
        cid = _learned_category(con, tx)
        if cid:
            con.execute("UPDATE transactions SET category_id=? WHERE id=?", (cid, r["id"]))
            learned += 1
        else:
            remaining.append((r["id"], tx))
    con.commit()
    categorized = 0
    if remaining and gemma.available():
        categorized = _gemma_batch_categorize(con, remaining, cat_by_name)
    left = con.execute("SELECT COUNT(*) FROM transactions WHERE category_id IS NULL").fetchone()[0]
    con.close()
    return {"learned": learned, "categorized": categorized, "remaining": left}
