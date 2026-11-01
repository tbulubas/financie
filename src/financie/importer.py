# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Bank statement import: CSV/XLSX deterministically, PDF and images via local Gemma."""
import csv
import hashlib
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
               "betrag", "betrag (eur)", "umsatz", "buchungsbetrag"],
    "debit": ["debet", "debit", "má dať", "ma dat", "soll"],
    "credit": ["kredit", "credit", "dal", "haben"],
    "description": ["popis", "popis transakcie", "poznámka", "poznamka", "správa pre prijímateľa",
                    "sprava pre prijimatela", "description", "detail", "informácia pre klienta",
                    "verwendungszweck", "buchungstext", "beschreibung", "umsatzbeschreibung",
                    "transaktionsbeschreibung"],
    "counterparty": ["protiúčet", "protiucet", "názov protiúčtu", "nazov protiuctu", "obchodník",
                     "obchodnik", "príjemca", "prijemca", "counterparty", "merchant", "partner",
                     "beguenstigter/zahlungspflichtiger", "begünstigter/zahlungspflichtiger",
                     "name zahlungsbeteiligter", "zahlungsbeteiligter", "empfänger", "empfaenger",
                     "auftraggeber/empfänger", "auftraggeber/empfaenger"],
    "currency": ["mena", "currency", "waehrung", "währung", "buchungswährung", "buchungswaehrung"],
}

# Known statement layouts, recognized by column names they always have (lower case).
# They get a label in reports and, where needed, rules of their own (see _table_transactions).
PROFILES = {
    "sparkasse": ("Sparkasse", {"auftragskonto", "buchungstag", "beguenstigter/zahlungspflichtiger", "betrag"}),
    "paypal": ("PayPal", {"transaktionscode", "brutto", "netto", "auswirkung auf guthaben"}),
    "card_umsatz": ("Credit card (Umsatz getätigt von)", {"umsatz getätigt von", "buchungsbetrag",
                                                          "transaktionsbeschreibung"}),
    "amazon_visa": ("Amazon Visa", {"datum", "karte", "beschreibung", "betrag", "punkte"}),
}

# PayPal types that move money between PayPal and a bank account or card: transfers, not spending
PAYPAL_FUNDING = ("bankgutschrift", "kartengutschrift", "abbuchung auf bank", "auszahlung auf bank",
                  "überweisung auf bank", "einzahlung")

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


def _decode(data: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise ValueError("unknown text encoding")  # latin-1 decodes anything, kept for clarity


def _sniff_delimiter(text: str) -> str:
    try:
        return csv.Sniffer().sniff(text[:4000], delimiters=";,\t").delimiter
    except csv.Error:  # e.g. preamble lines before the table confuse the sniffer
        head = text[:4000]
        return max(";,\t", key=head.count)


def _read_csv_rows(data: bytes) -> tuple[list[list[str]], dict]:
    text, enc = _decode(data)
    delimiter = _sniff_delimiter(text)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return rows, {"format": "CSV", "encoding": enc, "delimiter": delimiter}


def _find_header(all_rows: list[list[str]]) -> tuple[int | None, dict]:
    """First row (of the first 20) where at least date and amount can be mapped."""
    for i, r in enumerate(all_rows[:20]):
        m = _auto_map(r)
        if "date" in m and ("amount" in m or "debit" in m):
            return i, m
    return None, {}


def _detect_profile(header: list[str]) -> str | None:
    names = {_norm(h) for h in header}
    return next((key for key, (_, required) in PROFILES.items() if required <= names), None)


def _locate(all_rows: list[list[str]]) -> tuple[int | None, dict, str | None]:
    """Header row index, alias mapping and known profile of a table."""
    idx, mapping = _find_header(all_rows)
    if idx is None:  # e.g. PayPal: no column the aliases know as the amount
        idx = next((i for i, r in enumerate(all_rows[:20]) if _detect_profile(r)), None)
    return idx, mapping, (_detect_profile(all_rows[idx]) if idx is not None else None)


def _paypal_transactions(header: list[str], rows: list[dict]) -> tuple[list[dict], int]:
    """PayPal activity export (DE). Keeps completed EUR movements of the PayPal balance.

    - "Memo" rows (authorizations, holds) and non-completed ones do not move money.
    - A foreign-currency payment appears as the payment and a conversion in that currency
      (they cancel out) plus a conversion row in EUR with the real cost: only EUR rows are
      kept, and the EUR conversion gets the payee of the related payment.
    - Money from or to a bank account or card ("Bankgutschrift auf PayPal-Konto" …) is a
      transfer; the matching bank debit is linked by _link_paypal_transfers().
    """
    col = {_norm(h): h for h in header}

    def get(row, name):
        return str(row.get(col.get(name, ""), "") or "").strip()

    by_code = {get(r, "transaktionscode"): r for r in rows}
    txs, skipped = [], 0
    for r in rows:
        if not any(str(v).strip() for v in r.values()):
            continue
        date, amount = _parse_date(get(r, "datum")), _parse_amount(get(r, "netto"))
        if (_norm(get(r, "status")) != "abgeschlossen" or _norm(get(r, "auswirkung auf guthaben")) == "memo"
                or get(r, "währung").upper() != "EUR" or not date or amount is None):
            skipped += 1
            continue
        typ, name = get(r, "typ"), get(r, "name")
        detail = get(r, "artikelbezeichnung") or get(r, "betreff")
        if not name and "umrechnung" in typ.lower():
            related = by_code.get(get(r, "zugehöriger transaktionscode"))
            if related:
                name = get(related, "name")
                detail = detail or get(related, "artikelbezeichnung") or get(related, "betreff")
        tx = {"date": date, "amount": amount, "currency": "EUR",
              "description": " · ".join(x for x in (typ, detail) if x), "counterparty": name,
              "ref": get(r, "transaktionscode")}
        if any(k in typ.lower() for k in PAYPAL_FUNDING):
            tx["category_key"] = "transfer"
        txs.append(tx)
    return txs, skipped


def _table_transactions(header: list[str], rows: list[dict], mapping: dict,
                        profile: str | None) -> tuple[list[dict], int]:
    """Transactions of a table plus the number of rows a format rule left out on purpose."""
    if profile == "paypal":
        return _paypal_transactions(header, rows)
    skipped = 0
    if profile == "sparkasse":
        # pending card payments change or vanish before they are booked; import them once booked
        info = next((h for h in header if _norm(h) == "info"), None)
        if info:
            kept = [r for r in rows if not _norm(r.get(info)).startswith("umsatz vorgemerkt")]
            skipped, rows = len(rows) - len(kept), kept
    return _rows_to_transactions(header, rows, mapping), skipped


def _table_parts(all_rows: list[list[str]]) -> tuple[list[str], list[dict], dict, str | None]:
    header_idx, mapping, profile = _locate(all_rows)
    if header_idx is None:
        header_idx = 0
    header = all_rows[header_idx] if all_rows else []
    rows = [dict(zip(header, r)) for r in all_rows[header_idx + 1:]]
    return header, rows, mapping, profile


def _parse_table(all_rows: list[list[str]]) -> tuple[list[dict], str | None]:
    header, rows, mapping, profile = _table_parts(all_rows)
    if not mapping and not profile and gemma.available():
        # unknown format -> let Gemma map the columns (locally)
        sample = [list(r.values()) for r in rows[:5]]
        try:
            mapping = {k: v for k, v in gemma.map_csv_columns(header, sample).items() if v}
        except Exception:
            pass
    txs, _ = _table_transactions(header, rows, mapping, profile)
    if not txs:
        raise ValueError(
            f"columns or date format not recognized. "
            f"Header found: {header}. Mapping used: {mapping or 'none'}. "
            f"Rows in file: {len(rows)}.")
    return txs, profile


def parse_csv(data: bytes) -> list[dict]:
    return _parse_table(_read_csv_rows(data)[0])[0]


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


def _read_excel_rows(data: bytes) -> tuple[list[list[str]], dict]:
    # detect by file signature: OLE (D0CF11E0) = legacy .xls, PK = .xlsx
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return _read_xls_rows(data), {"format": "XLS (legacy BIFF)"}
    return _read_xlsx_rows(data), {"format": "XLSX"}


def parse_xlsx(data: bytes) -> list[dict]:
    return _parse_table(_read_excel_rows(data)[0])[0]


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


def parse_statement(filename: str, data: bytes) -> tuple[list[dict], str | None]:
    """Transactions of a statement file and the label of its recognized format (or None)."""
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        txs, profile = _parse_table(_read_csv_rows(data)[0])
    elif ext in (".xlsx", ".xls"):
        txs, profile = _parse_table(_read_excel_rows(data)[0])
    elif ext == ".pdf":
        return parse_pdf(data), None
    elif ext in IMG_EXT:
        return parse_image(data), None
    else:
        raise ValueError(f"Unsupported format: {ext}")
    return txs, PROFILES[profile][0] if profile else None


def parse_file(filename: str, data: bytes) -> list[dict]:
    return parse_statement(filename, data)[0]


def _hashed(value: str) -> str:
    # an account number is needed only to recognize the source again, not to be stored
    return hashlib.sha256(value.strip().upper().replace(" ", "").encode()).hexdigest()[:16]


def _source_of(profile: str | None, header: list[str], rows: list[dict],
               filename: str) -> tuple[str | None, dict | None]:
    """Stable key of the account a statement belongs to, and a proposal for a new account."""
    col = {_norm(h): h for h in header}
    first = next((r for r in rows if any(str(v).strip() for v in r.values())), {})

    def value(name):
        return str(first.get(col.get(name, ""), "") or "").strip()

    if profile == "sparkasse" and value("auftragskonto"):
        iban = value("auftragskonto")
        return f"sparkasse:{_hashed(iban)}", {"name": f"Sparkasse …{iban[-4:]}", "type": "checking"}
    if profile == "paypal":
        return "paypal", {"name": "PayPal", "type": "other"}
    if profile == "card_umsatz":
        # these exports name the (masked) card only in the file name: umsatz-1234________5678-…
        m = re.search(r"(\d{4})_+(\d{4})", filename)
        if m:
            return f"card:{m.group(1)}-{m.group(2)}", {"name": f"Card …{m.group(2)}", "type": "credit_card"}
        return "card_umsatz", {"name": "Credit card", "type": "credit_card"}
    if profile == "amazon_visa":
        card = value("karte")
        return (f"amazon_visa:{_hashed(card)}" if card else "amazon_visa"), {"name": "Amazon Visa",
                                                                            "type": "credit_card"}
    return None, None


def describe_statement(filename: str, data: bytes) -> dict:
    """What a file is, before importing it: format, period, number of transactions and the
    account it belongs to. Writes nothing and never calls Gemma (PDFs and images are only
    marked as needing it)."""
    ext = Path(filename).suffix.lower()
    d = {"file": Path(filename).name, "format": None, "gemma": False, "key": None, "proposal": None,
         "count": None, "date_from": None, "date_to": None, "error": None}
    if ext == ".pdf" or ext in IMG_EXT:
        d["gemma"] = True
        return d
    if ext == ".csv":
        all_rows = _read_csv_rows(data)[0]
    elif ext in (".xlsx", ".xls"):
        all_rows = _read_excel_rows(data)[0]
    else:
        d["error"] = f"Unsupported format: {ext}"
        return d
    header, rows, mapping, profile = _table_parts(all_rows)
    if not mapping and not profile:
        d["gemma"] = True  # unknown columns: Gemma maps them during the import
        return d
    txs, _ = _table_transactions(header, rows, mapping, profile)
    key, proposal = _source_of(profile, header, rows, filename)
    dates = sorted(t["date"] for t in txs)
    d.update(format=PROFILES[profile][0] if profile else "CSV/Excel", key=key, proposal=proposal,
             count=len(txs), date_from=dates[0] if dates else None, date_to=dates[-1] if dates else None)
    return d


_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9 ]{10,32}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NUMBER = re.compile(r"^[+-]?[\d.,\s\xa0]*\d[\d.,\s\xa0]*(\s?[A-Z€$£]{1,3})?$")


def _shape(val) -> str:
    """Describe a cell without revealing it: '<date:%d.%m.%Y>', '-9.999,99', '<text>' …"""
    s = str(val or "").strip()
    if not s:
        return ""
    for f in DATE_FORMATS:
        try:
            datetime.strptime(s, f)
            return f"<date:{f}>"
        except ValueError:
            pass
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        return "<time>"
    if _IBAN.match(s.upper()):
        return "<iban>"
    if _EMAIL.match(s):
        return "<email>"
    if _NUMBER.match(s):
        return re.sub(r"\d", "9", s)
    return "<text>"


def _looks_like_header(rows: list[list[str]], i: int) -> bool:
    """Short text cells only, followed by a row with a date — a table head, not a preamble line
    such as the account holder's name, which must not appear in a report."""
    cells = [str(c).strip() for c in rows[i] if str(c).strip()]
    if len(cells) < 3 or not all(_shape(c) == "<text>" and len(c) <= 40 for c in cells):
        return False
    following = next((r for r in rows[i + 1:] if any(str(c).strip() for c in r)), [])
    return any(_shape(c).startswith("<date:") for c in following)


def inspect_file(filename: str, data: bytes) -> dict:
    """Structure of a statement for diagnosing an unknown bank format.

    Reveals column names and the *shape* of values only — never descriptions,
    names, amounts or account numbers, so the report can be shared to add a new
    format to COLUMN_ALIASES. Never calls Gemma.
    """
    ext = Path(filename).suffix.lower()
    report = {"file": Path(filename).name}
    if ext == ".pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        text_len = sum(len(page.get_text().strip()) for page in doc)
        report.update(format="PDF", pages=doc.page_count, text_layer=text_len > 100,
                      status="Gemma: text extraction" if text_len > 100 else "Gemma: vision (scanned)")
        return report
    if ext in IMG_EXT:
        report.update(format="image", status="Gemma: vision")
        return report
    if ext == ".csv":
        all_rows, info = _read_csv_rows(data)
    elif ext in (".xlsx", ".xls"):
        all_rows, info = _read_excel_rows(data)
    else:
        report.update(format=ext or "unknown", status="unsupported")
        return report
    report.update(info)

    header_idx, mapping, profile = _locate(all_rows)
    if header_idx is None:
        # show only a row that looks like column names; everything else as shapes
        header_idx = next((i for i in range(min(20, len(all_rows))) if _looks_like_header(all_rows, i)), None)
        report["status"] = "not recognized: Gemma would guess the columns"
    header = all_rows[header_idx] if header_idx is not None else []
    start = header_idx + 1 if header_idx is not None else 0
    data_rows = [r for r in all_rows[start:] if any(str(c).strip() for c in r)]
    known = mapping or _auto_map(header)  # partial mapping of an unrecognized header
    report.update(
        bank=PROFILES[profile][0] if profile else None,
        header_row=None if header_idx is None else header_idx + 1,
        preamble_rows=header_idx or 0,
        columns=[str(h).strip() for h in header],
        mapping=dict(known),
        missing=[] if profile == "paypal" else [
            f for f in ("date", "amount", "description", "counterparty", "currency")
            if f not in known and not (f == "amount" and "debit" in known)],
        sample_shapes=[[_shape(c) for c in r] for r in data_rows[:3]],
        data_rows=len(data_rows),
    )
    if mapping or profile:
        txs, by_rule = _table_transactions(header, [dict(zip(header, r)) for r in data_rows], mapping, profile)
        report["parsed"] = len(txs)
        if txs:
            dates = sorted(t["date"] for t in txs)
            report["date_range"] = f"{dates[0]} .. {dates[-1]}"
        unreadable = len(data_rows) - len(txs) - by_rule
        status = "ok" if not unreadable else f"partly: {unreadable} rows without a valid date or amount"
        if by_rule:
            status += f" ({by_rule} rows left out by the {PROFILES[profile][0]} rules)"
        report["status"] = status if txs else "no transactions"
    return report


def format_report(r: dict) -> str:
    lines = [f"== {r['file']}"]
    fmt = r.get("format", "?")
    if "encoding" in r:
        fmt += f", encoding {r['encoding']}, delimiter {r['delimiter']!r}"
    lines.append(f"format:   {fmt}")
    if r.get("bank"):
        lines.append(f"bank:     {r['bank']}")
    if "pages" in r:
        lines.append(f"pages:    {r['pages']}, text layer: {'yes' if r['text_layer'] else 'no'}")
    if "columns" in r:
        lines.append(f"header:   row {r['header_row']} ({r['preamble_rows']} rows above it)"
                     if r["header_row"] else "header:   not found")
        lines.append(f"columns:  {' | '.join(r['columns'])}")
        lines.append("mapping:  " + (", ".join(f"{k}={v}" for k, v in r["mapping"].items()) or "none"))
        if r["missing"]:
            lines.append(f"missing:  {', '.join(r['missing'])}")
        for shape in r["sample_shapes"]:
            lines.append(f"shape:    {' | '.join(shape)}")
        lines.append(f"rows:     {r['data_rows']}" + (f", parsed {r['parsed']}" if "parsed" in r else "")
                     + (f", {r['date_range']}" if "date_range" in r else ""))
    lines.append(f"status:   {r['status']}")
    return "\n".join(lines)


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


def _link_paypal_transfers(con) -> int:
    """Mark bank or card movements that only fund PayPal as transfers.

    A PayPal payment funded from a bank account shows up twice: as the payment in
    PayPal and as a debit "PayPal Europe …" in the bank. With the PayPal side
    imported, the PayPal funding row is a transfer, and so must be the bank debit
    with the opposite amount within a few days — else the expense counts twice.
    Runs after every import, so the order of PayPal and bank imports does not matter.
    """
    transfer = con.execute("SELECT id FROM categories WHERE key='transfer'").fetchone()
    if not transfer:
        return 0
    linked = 0
    funding = con.execute("""SELECT id, account_id, date, amount FROM transactions
                             WHERE category_id = ? ORDER BY date""", (transfer["id"],)).fetchall()
    for f in funding:
        match = con.execute("""
            SELECT id FROM transactions
            WHERE account_id != ? AND ROUND(amount + ?, 2) = 0
              AND ABS(julianday(date) - julianday(?)) <= 5
              AND (category_id IS NULL OR category_id != ?)
              AND (LOWER(counterparty) LIKE '%paypal%' OR LOWER(description) LIKE '%paypal%')
            ORDER BY ABS(julianday(date) - julianday(?)) LIMIT 1""",
            (f["account_id"], f["amount"], f["date"], transfer["id"], f["date"])).fetchone()
        if match:
            con.execute("UPDATE transactions SET category_id=? WHERE id=?", (transfer["id"], match["id"]))
            linked += 1
    con.commit()
    return linked


def import_transactions(account_id: int, filename: str, txs: list[dict],
                        use_gemma_categorize: bool = False) -> dict:
    con = db.get_db()
    cat_rows = con.execute("SELECT id, name, key FROM categories").fetchall()
    cat_by_name = {r["name"]: r["id"] for r in cat_rows}
    cat_by_key = {r["key"]: r["id"] for r in cat_rows if r["key"]}

    inserted, skipped, to_categorize = [], 0, []
    for tx in txs:
        # a reference of the source (PayPal transaction code) keeps identical payments apart
        dedup_text = tx.get("description", "") + (f"|{tx['ref']}" if tx.get("ref") else "")
        h = db.tx_hash(account_id, tx["date"], tx["amount"], dedup_text)
        cat_id = cat_by_key.get(tx.get("category_key")) or _learned_category(con, tx)
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

    # Same account, date and amount, but from another file with another text: most likely the
    # same movement exported in another format (e.g. Sparkasse CSV-CAMT vs. MT940)
    possible_duplicates = sum(1 for tx_id, tx in inserted if con.execute(
        """SELECT 1 FROM transactions WHERE account_id=? AND date=? AND ROUND(amount - ?, 2) = 0
           AND id != ? AND COALESCE(source_file,'') != ? LIMIT 1""",
        (account_id, tx["date"], tx["amount"], tx_id, filename)).fetchone())
    linked = _link_paypal_transfers(con)

    categorized = 0
    if use_gemma_categorize and to_categorize and gemma.available():
        categorized = _gemma_batch_categorize(con, to_categorize, cat_by_name)
    con.close()
    return {"inserted": len(inserted), "skipped_duplicates": skipped,
            "auto_categorized": categorized + len(inserted) - len(to_categorize),
            "uncategorized": len(to_categorize) - categorized,
            "linked_transfers": linked, "possible_duplicates": possible_duplicates}


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
