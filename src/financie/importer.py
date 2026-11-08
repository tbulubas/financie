# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Bank statement import: CSV/XLSX deterministically, PDF and images via local Gemma."""
import csv
import hashlib
import io
import re
from datetime import date, datetime
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
    # "Guthaben-relevante Zahlungen": no type, status or effect columns, the type is in Beschreibung
    "paypal_balance": ("PayPal (balance report)", {"transaktionscode", "brutto", "netto", "guthaben",
                                                   "beschreibung"}),
    "card_umsatz": ("Credit card (Umsatz getätigt von)", {"umsatz getätigt von", "buchungsbetrag",
                                                          "transaktionsbeschreibung"}),
    "amazon_visa": ("Amazon Visa", {"datum", "karte", "beschreibung", "betrag", "punkte"}),
    "barclays": ("Barclays credit card", {"referenznummer", "buchungsdatum", "betrag", "beschreibung", "typ",
                                          "status", "kartennummer", "originalbetrag"}),
}

PAYPAL_PROFILES = ("paypal", "paypal_balance")

# Barclays card statuses (lower case) of movements that are not booked yet
BARCLAYS_PENDING = ("vorgemerkt", "ausstehend", "offen", "in bearbeitung", "autorisiert")

# words in a bank movement that funds an account kept here (PayPal, a card): see _link_funding_transfers
FUNDING_PARTNERS = ("paypal", "barclays")

# PayPal types that move money between PayPal and a bank account or card: transfers, not spending.
# A card-funded payment comes with "Allgemeine Abbuchung von Kreditkarte" (+), a refund of it
# goes back to the card with the same type (-).
PAYPAL_FUNDING = ("bankgutschrift", "kartengutschrift", "abbuchung auf bank", "auszahlung auf bank",
                  "überweisung auf bank", "einzahlung", "von kreditkarte", "auf kreditkarte",
                  "von debitkarte", "auf debitkarte", "von bankkonto", "auf bankkonto")

# PayPal statuses (lower case): a pending funding counts, a failed one undoes it
PAYPAL_PENDING = ("ausstehend",)
PAYPAL_FAILED = ("storniert", "fehlgeschlagen", "abgelehnt", "rückgängig gemacht", "zurückgebucht")

DATE_FORMATS = ["%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y", "%m/%d/%Y", "%Y/%m/%d"]


# soft hyphen, zero-width characters and BOM: invisible in Excel, but they break a name match
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"))


def _norm(s) -> str:
    """Lower case, invisible characters removed, any whitespace (line breaks, NBSP) as one space."""
    return " ".join(str(s or "").translate(_INVISIBLE).split()).lower()


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


def _near_profile(header: list[str]) -> str | None:
    """The known layout a header almost matches, with the columns it lacks (for inspect)."""
    names = {_norm(h) for h in header}
    label, required = min(PROFILES.values(), key=lambda p: len(p[1] - names))
    missing = sorted(required - names)
    return f"{label} (missing: {', '.join(missing)})" if len(missing) <= len(required) // 2 else None


def _odd_names(header: list[str]) -> list[str]:
    """Column names with characters other than letters, digits and usual punctuation, escaped."""
    return [ascii(str(h)) for h in header
            if any(not (c.isalnum() or c in " -/._()+&%#'?:") for c in str(h))]


def _locate(all_rows: list[list[str]]) -> tuple[int | None, dict, str | None]:
    """Header row index, alias mapping and known profile of a table."""
    idx, mapping = _find_header(all_rows)
    if idx is None:  # e.g. PayPal: no column the aliases know as the amount
        idx = next((i for i, r in enumerate(all_rows[:20]) if _detect_profile(r)), None)
    return idx, mapping, (_detect_profile(all_rows[idx]) if idx is not None else None)


def _paypal_transactions(header: list[str], rows: list[dict]) -> tuple[list[dict], int]:
    """PayPal activity export (DE). Keeps completed EUR movements of the PayPal balance.

    The balance report ("Guthaben-relevante Zahlungen") has the same rows without the memo
    ones; its type is in Beschreibung and, without a status column, every row counts.

    - "Memo" rows (authorizations, holds) and non-completed ones do not move money.
    - A foreign-currency payment appears as the payment and a conversion in that currency
      (they cancel out) plus a conversion row in EUR with the real cost: only EUR rows are
      kept, and the EUR conversion gets the payee of the related payment.
    - Money from or to a bank account or card ("Bankgutschrift auf PayPal-Konto" …) is a
      transfer; the matching bank debit is linked by _link_funding_transfers(). It counts
      while still pending too: the bank debit happens anyway, and without its PayPal side
      the debit would count as a second expense. Should the bank refuse the debit, a later
      export lists the funding as failed, and that row voids the pending one (same code).
    """
    col = {_norm(h): h for h in header}

    def get(row, name):
        return str(row.get(col.get(name, ""), "") or "").strip()

    by_code = {get(r, "transaktionscode"): r for r in rows}
    txs, skipped, stated = [], 0, []   # stated: counted transactions with the balance after them
    for r in rows:
        if not any(str(v).strip() for v in r.values()):
            continue
        typ = get(r, "typ") or get(r, "beschreibung")
        status = _norm(get(r, "status")) if "status" in col else "abgeschlossen"
        date, amount = _parse_date(get(r, "datum")), _parse_amount(get(r, "netto"))
        funding = any(k in typ.lower() for k in PAYPAL_FUNDING)
        counts = status == "abgeschlossen" or (funding and status in PAYPAL_PENDING)
        voids = funding and status in PAYPAL_FAILED
        if (not (counts or voids) or "autorisierung" in typ.lower()
                or _norm(get(r, "auswirkung auf guthaben")) == "memo"
                or get(r, "währung").upper() != "EUR" or not date or amount is None):
            skipped += 1
            continue
        name = get(r, "name")
        detail = get(r, "artikelbezeichnung") or get(r, "betreff")
        if not name and "umrechnung" in typ.lower():
            related = by_code.get(get(r, "zugehöriger transaktionscode"))
            if related:
                name = get(related, "name")
                detail = detail or get(related, "artikelbezeichnung") or get(related, "betreff")
        tx = {"date": date, "amount": amount, "currency": "EUR",
              "description": " · ".join(x for x in (typ, detail) if x), "counterparty": name,
              "ref": get(r, "transaktionscode")}
        if funding:
            tx["category_key"] = "transfer"
        if voids:
            tx["void"] = True
        else:
            stated.append((tx, _parse_amount(get(r, "guthaben"))))
        txs.append(tx)
    _paypal_day_balance(stated)
    return txs, skipped


def _paypal_day_balance(stated: list[tuple[dict, float | None]]):
    """Mark the PayPal balance at the end of the last full day of the export (the day before its
    last one, which may go on after the download) as tx["day_balance"] on a transaction of
    that day.

    Each row states the balance after it, but rows of one second come in any order, so no single
    row is "the last". The export starts at some first-day row's balance minus its amount; the
    true start is the one from which every day's end (start plus the totals so far) is one of
    that day's stated balances. No unique start, no balance.
    """
    days = sorted({tx["date"] for tx, _ in stated})
    if len(days) < 2:
        return
    by_day = {d: [(tx, after) for tx, after in stated if tx["date"] == d] for d in days}
    totals, running = {}, 0.0
    for d in days:
        running += sum(tx["amount"] for tx, _ in by_day[d])
        totals[d] = running
    starts = {round(after - tx["amount"], 2) for tx, after in by_day[days[0]] if after is not None}
    valid = [start for start in starts
             if all(round(start + totals[d], 2) in {round(after, 2) for _, after in by_day[d]}
                    for d in days if all(after is not None for _, after in by_day[d]))]
    if len(valid) == 1:
        by_day[days[-2]][-1][0]["day_balance"] = round(valid[0] + totals[days[-2]], 2)


def _barclays_transactions(header: list[str], rows: list[dict], mapping: dict) -> tuple[list[dict], int]:
    """Barclays credit card export (XLSX). Booked rows only; the monthly repayment from the bank
    account ("Per Lastschrift dankend erhalten") is a transfer, linked to the bank debit by
    _link_funding_transfers(). The numeric reference number goes into the dedup hash."""
    col = {_norm(h): h for h in header}

    def get(row, name):
        return str(row.get(col.get(name, ""), "") or "").strip()

    txs, skipped = [], 0
    for r in rows:
        if not any(str(v).strip() for v in r.values()):
            continue
        if _norm(get(r, "status")) in BARCLAYS_PENDING:
            skipped += 1
            continue
        parsed = _rows_to_transactions(header, [r], mapping)
        if not parsed:
            continue
        tx = parsed[0]
        if get(r, "referenznummer").isdigit():
            tx["ref"] = get(r, "referenznummer")
        text = tx["description"].lower()
        if _norm(get(r, "typ")) == "gutschrift" and ("lastschrift" in text or "dankend erhalten" in text):
            tx["category_key"] = "transfer"
        txs.append(tx)
    return txs, skipped


def _table_transactions(header: list[str], rows: list[dict], mapping: dict,
                        profile: str | None) -> tuple[list[dict], int]:
    """Transactions of a table plus the number of rows a format rule left out on purpose."""
    if profile in PAYPAL_PROFILES:
        return _paypal_transactions(header, rows)
    if profile == "barclays":
        return _barclays_transactions(header, rows, mapping)
    skipped = 0
    if profile == "sparkasse":
        # pending card payments change or vanish before they are booked; import them once booked
        info = next((h for h in header if _norm(h) == "info"), None)
        if info:
            kept = [r for r in rows if not _norm(r.get(info)).startswith("umsatz vorgemerkt")]
            skipped, rows = len(rows) - len(kept), kept
    return _rows_to_transactions(header, rows, mapping), skipped


def _rows_without_amount(rows: list[dict], mapping: dict, profile: str | None) -> int:
    """Rows with a date but no amount: movements the export left incomplete (a Sparkasse Excel
    export does that), which the import has to leave out. A row without a date is a footer or a
    note, not a movement, so it does not count."""
    if profile in PAYPAL_PROFILES or profile == "barclays" or not mapping.get("date") \
            or not any(mapping.get(k) for k in ("amount", "debit", "credit")):
        return 0
    dated = [r for r in rows if _parse_date(r.get(mapping["date"], ""), mapping.get("date_format"))]
    return len(dated) - len(_rows_to_transactions([], dated, mapping))


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
    return [[_cell_text(c) for c in r] for r in ws.iter_rows(values_only=True)]


def _cell_text(c) -> str:
    """An Excel cell as text; a real date cell as YYYY-MM-DD (its time of day dropped)."""
    if c is None:
        return ""
    if isinstance(c, (datetime, date)):
        return c.strftime("%Y-%m-%d")
    return str(c)


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


# ---- CAMT (ISO 20022 XML): camt.052 account report, camt.053 statement, also zipped ----

CAMT_EXT = {".xml", ".zip"}
# the message element and its report element
CAMT_MESSAGES = {"BkToCstmrAcctRpt": "Rpt", "BkToCstmrStmt": "Stmt", "BkToCstmrDbtCdtNtfctn": "Ntfctn"}
# booked balances: at the end of a report (closing, interim) and before its first entry
CAMT_CLOSING = ("CLBD", "ITBD")
CAMT_OPENING = ("OPBD", "PRCD")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _find(el, path: str):
    """First element along a path of local names ('Tp/CdOrPrtry/Cd'), in any namespace."""
    for name in path.split("/"):
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == name), None)
    return el


def _text(el, path: str) -> str:
    found = _find(el, path) if el is not None else None
    return (found.text or "").strip() if found is not None else ""


def _camt_documents(data: bytes) -> list:
    """Root elements of the XML documents of a file: one XML file or a ZIP of them."""
    import xml.etree.ElementTree as ET
    import zipfile
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            blobs = [z.read(n) for n in sorted(z.namelist()) if n.lower().endswith(".xml")]
    else:
        blobs = [data]
    return [ET.fromstring(b) for b in blobs]


def _camt_signed(el) -> float | None:
    try:
        amount = float(_text(el, "Amt"))
    except ValueError:
        return None
    return -amount if _text(el, "CdtDbtInd") == "DBIT" else amount


def _camt_date(el, path: str) -> str | None:
    return _parse_date((_text(el, path + "/Dt") or _text(el, path + "/DtTm"))[:10])


def _camt_status(entry) -> str:
    """BOOK, PDNG or INFO: a code element since camt.052.001.08, plain text before."""
    sts = _find(entry, "Sts")
    if sts is None:
        return "BOOK"
    return (_text(sts, "Cd") or (sts.text or "").strip()).upper() or "BOOK"


def _camt_entry(entry, fields: dict) -> dict | None:
    """One booked entry as a transaction. A batch (several TxDtls) stays one transaction with the
    booked total, so the balance adds up; it gets the entry text instead of one payee."""
    amount = _camt_signed(entry)
    date = _camt_date(entry, "BookgDt") or _camt_date(entry, "ValDt")
    if amount is None or not date:
        return None
    details = [t for d in _kids(entry, "NtryDtls") for t in _kids(d, "TxDtls")]
    one = details[0] if len(details) == 1 else None
    purpose = " ".join(u.text.strip() for u in (one.iter() if one is not None else [])
                       if _local(u.tag) == "Ustrd" and u.text and u.text.strip())
    side = "Cdtr" if amount < 0 else "Dbtr"   # the other party: who was paid, or who paid
    party = next((v for v in (_text(one, f"RltdPties/{side}/Nm"), _text(one, f"RltdPties/{side}/Pty/Nm"),
                              _text(one, f"RltdPties/Ultmt{side}/Nm"), _text(one, f"RltdPties/Ultmt{side}/Pty/Nm"))
                  if v), "")
    info = _text(entry, "AddtlNtryInf")
    ref = _text(entry, "AcctSvcrRef") or _text(one, "Refs/AcctSvcrRef")
    for name, present in (("BookgDt", _find(entry, "BookgDt") is not None),
                          ("ValDt", _find(entry, "ValDt") is not None), ("AcctSvcrRef", bool(ref)),
                          ("Ustrd", bool(purpose)), ("party", bool(party)), ("AddtlNtryInf", bool(info)),
                          ("batch", len(details) > 1)):
        fields[name] = fields.get(name, 0) + present
    amt = _find(entry, "Amt")
    # the purpose is what the bank's CSV exports call Verwendungszweck, so learned categories carry over
    tx = {"date": date, "amount": amount, "currency": (amt.get("Ccy") if amt is not None else None) or "EUR",
          "description": purpose or info, "counterparty": party, "other_formats": True}
    if ref and ref.upper() != "NOTPROVIDED":
        tx["ref"] = ref
    return tx


def _camt_balance(report, txs: list[dict], created: str) -> tuple[str, str | None]:
    """Mark the booked balance at the end of the report's last complete day (before the day the
    report was made) as tx["day_balance"] on a transaction of that day. Returns how it went
    ("checked": the entries add up to the opening balance too, "stated", or why not) and the day.

    The closing (or interim) booked balance is the balance after all the report's entries, so a
    day ends at it minus the entries after that day. With an opening balance too, the entries
    must add up from one to the other, else the report states nothing.
    """
    balances: dict[str, list[tuple[str, float]]] = {}
    for b in _kids(report, "Bal"):
        amount = _camt_signed(b)
        if amount is not None:
            balances.setdefault(_text(b, "Tp/CdOrPrtry/Cd"), []).append((_camt_date(b, "Dt") or "", amount))
    closing = next((max(balances[c]) for c in CAMT_CLOSING if c in balances), None)
    if closing is None:
        return "no booked closing balance", None
    if not txs:
        return "no entries", None
    opening = next((min(balances[c]) for c in CAMT_OPENING if c in balances), None)
    if opening is not None and round(opening[1] + sum(t["amount"] for t in txs), 2) != round(closing[1], 2):
        return "entries do not add up to the opening and closing balance", None
    complete = [t["date"] for t in txs if t["date"] < created and t["date"] <= (closing[0] or created)]
    if not complete:
        return "no complete day", None
    day = max(complete)
    [t for t in txs if t["date"] == day][-1]["day_balance"] = round(
        closing[1] - sum(t["amount"] for t in txs if t["date"] > day), 2)
    return ("stated" if opening is None else "checked"), day


def _camt_transactions(data: bytes) -> tuple[list[dict], dict]:
    """Booked entries of a CAMT file (pending and info-only ones are left out, as in the CSV
    exports) plus what inspect and the account proposal need: message, IBAN, bank, counts."""
    info = {"files": 0, "message": None, "iban": "", "servicer": "", "reports": 0,
            "statuses": {}, "balance_codes": set(), "fields": {}, "balance": []}
    txs = []
    for root in _camt_documents(data):
        info["files"] += 1
        msg = next((c for c in root if _local(c.tag) in CAMT_MESSAGES), None)
        if msg is None:
            continue
        ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
        info["message"] = info["message"] or (ns.rsplit(":", 1)[-1] if "camt." in ns else _local(msg.tag))
        created = _parse_date(_text(msg, "GrpHdr/CreDtTm")[:10]) or datetime.now().strftime("%Y-%m-%d")
        for report in _kids(msg, CAMT_MESSAGES[_local(msg.tag)]):
            info["reports"] += 1
            info["iban"] = info["iban"] or _text(report, "Acct/Id/IBAN")
            info["servicer"] = info["servicer"] or _text(report, "Acct/Svcr/FinInstnId/Nm")
            info["balance_codes"] |= {_text(b, "Tp/CdOrPrtry/Cd") for b in _kids(report, "Bal")}
            own = []
            for entry in _kids(report, "Ntry"):
                status = _camt_status(entry)
                info["statuses"][status] = info["statuses"].get(status, 0) + 1
                tx = _camt_entry(entry, info["fields"]) if status == "BOOK" else None
                if tx:
                    own.append(tx)
            info["balance"].append(_camt_balance(report, own, created))
            txs += own
    if not info["message"]:
        raise ValueError("not a CAMT bank statement (camt.052 / camt.053)")
    return txs, info


def _camt_label(info: dict) -> str:
    return f"CAMT ({info['message']})"


def _camt_source(info: dict) -> tuple[str, dict]:
    iban = info["iban"]
    if not iban:
        return "camt", {"name": "Bank account", "type": "checking"}
    servicer = info["servicer"]
    bank = "Sparkasse" if "sparkasse" in servicer.lower() else (servicer or "Account")
    # the key of the Sparkasse CSV exports of the same IBAN: a change of format keeps the account
    return f"sparkasse:{_hashed(iban)}", {"name": f"{bank} …{iban[-4:]}", "type": "checking"}


def _camt_balance_summary(results: list[tuple[str, str | None]]) -> str:
    """'stated for 34 days, 2026-09-01 .. 2026-10-30, each adds up to its opening balance' and
    the reports that state none, by reason."""
    days = sorted(day for how, day in results if day)
    parts = []
    if days:
        parts.append(f"stated for the end of {days[0]}" if len(days) == 1
                     else f"stated for {len(days)} days, {days[0]} .. {days[-1]}")
        checked = sum(how == "checked" for how, _ in results)
        if checked:
            parts[-1] += (", entries add up to the opening balance" if checked == len(days)
                          else f", entries add up to the opening balance in {checked}")
    reasons = [how for how, day in results if not day]
    parts += [f"{reasons.count(r)}× {r}" if len(results) > 1 else r for r in dict.fromkeys(reasons)]
    return "; ".join(parts) or "none"


def _inspect_camt(data: bytes, report: dict) -> dict:
    """Structure of a CAMT file: message, balance types, entry statuses and which fields the
    entries fill — never names, texts, amounts or the IBAN."""
    txs, info = _camt_transactions(data)
    zipped = data[:2] == b"PK"
    report.update(
        format=f"CAMT {info['message']} (" + (f"XML in ZIP, {info['files']} files)" if zipped else "XML)"),
        bank="CAMT" + (" (<iban>)" if info["iban"] else " (no IBAN)"),
        reports=info["reports"],
        entries=", ".join(f"{k} {v}" for k, v in sorted(info["statuses"].items())) or "none",
        balance_codes=", ".join(sorted(c for c in info["balance_codes"] if c)) or "none",
        fields=", ".join(f"{k} {v}" for k, v in info["fields"].items()),
        balance=_camt_balance_summary(info["balance"]),
        parsed=len(txs),
        status="ok" if txs else "no transactions",
    )
    if txs:
        dates = sorted(t["date"] for t in txs)
        report["date_range"] = f"{dates[0]} .. {dates[-1]}"
    return report


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
    elif ext in CAMT_EXT:
        txs, info = _camt_transactions(data)
        if not txs:
            raise ValueError("no booked entries in the CAMT file")
        return txs, _camt_label(info)
    else:
        raise ValueError(f"Unsupported format: {ext}")
    return txs, PROFILES[profile][0] if profile else None


def parse_file(filename: str, data: bytes) -> list[dict]:
    return parse_statement(filename, data)[0]


FILE_KINDS = ({".csv": "csv", ".xlsx": "excel", ".xls": "excel", ".pdf": "pdf"}
              | {e: "image" for e in IMG_EXT} | {e: "camt" for e in CAMT_EXT})


def file_kind(filename: str) -> str | None:
    """csv / excel / pdf / image / camt, or None for files that are not statements."""
    return FILE_KINDS.get(Path(filename).suffix.lower())


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    if profile in PAYPAL_PROFILES:  # both exports belong to the same PayPal account
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
    if profile == "barclays":
        card = value("kartennummer")
        return (f"barclays:{_hashed(card)}" if card else "barclays"), {"name": "Barclays Visa",
                                                                      "type": "credit_card"}
    return None, None


def describe_statement(filename: str, data: bytes) -> dict:
    """What a file is, before importing it: format, period, number of transactions and the
    account it belongs to. Writes nothing and never calls Gemma (PDFs and images are only
    marked as needing it)."""
    ext = Path(filename).suffix.lower()
    d = {"file": Path(filename).name, "format": None, "gemma": False, "key": None, "proposal": None,
         "count": None, "date_from": None, "date_to": None, "error": None, "no_amount": 0}
    if ext == ".pdf" or ext in IMG_EXT:
        d["gemma"] = True
        return d
    if ext in CAMT_EXT:
        txs, info = _camt_transactions(data)
        key, proposal = _camt_source(info)
        dates = sorted(t["date"] for t in txs)
        d.update(format=_camt_label(info), key=key, proposal=proposal, count=len(txs),
                 date_from=dates[0] if dates else None, date_to=dates[-1] if dates else None)
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
    txs = [t for t in txs if not t.get("void")]
    key, proposal = _source_of(profile, header, rows, filename)
    dates = sorted(t["date"] for t in txs)
    d.update(format=PROFILES[profile][0] if profile else "CSV/Excel", key=key, proposal=proposal,
             count=len(txs), date_from=dates[0] if dates else None, date_to=dates[-1] if dates else None,
             no_amount=_rows_without_amount(rows, mapping, profile))
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


def _uncached_formulas(data: bytes) -> int:
    """Formula cells without a stored result: they read as empty, though Excel shows a value
    (it computes them on opening; a file written by a program may never store the result)."""
    import openpyxl
    raw = openpyxl.load_workbook(io.BytesIO(data)).active
    values = openpyxl.load_workbook(io.BytesIO(data), data_only=True).active
    return sum(a.data_type == "f" and b.value is None
               for ra, rb in zip(raw.iter_rows(), values.iter_rows()) for a, b in zip(ra, rb))


# Generic booking types a bank writes in "Buchungstext": safe to name in an inspect report
# (anything else shows as <other>, it may be free text).
BOOKING_TYPE_COLUMNS = ("buchungstext", "umsatzart", "buchungsart", "vorgang")
BOOKING_TYPES = {
    "gutschrift", "lastschrift", "folgelastschrift", "erstlastschrift", "einmal lastschrift",
    "einmallastschrift", "basislastschrift", "firmenlastschrift", "sepa-lastschrift",
    "ueberweisung", "überweisung", "online-ueberweisung", "online-überweisung",
    "echtzeit-ueberweisung", "echtzeit-überweisung", "echtzeitüberweisung", "dauerauftrag",
    "gutschr. ueberweisung", "gutschr. überweisung", "gutschr. ueberw.", "sepa-gutschrift",
    "sepa ueberweisung", "sepa überweisung", "sammel-ls", "sammel-überweisung",
    "sammelueberweisung", "kartenzahlung", "kartenzahlung online", "debitkarte", "girocard",
    "bargeldauszahlung", "geldautomat", "auszahlung", "einzahlung", "bargeldeinzahlung",
    "entgeltabschluss", "abschluss", "rechnungsabschluss", "kontoabschluss", "zinsen",
    "zinsen/entgelt", "entgelt", "umbuchung", "ruecklastschrift", "rücklastschrift", "retoure",
    "storno", "lohn gehalt", "lohn/gehalt", "gehalt", "rente", "gehalt/rente", "info", "saldo",
    "abrechnung", "kreditkartenabrechnung", "wertpapiere", "scheck",
}


def _unread_rows(header: list[str], rows: list[dict], mapping: dict) -> dict | None:
    """What the rows without a valid date or amount have in common — counts and generic booking
    types only, so the report can be shared (are they continuation lines of a movement above,
    or movements of their own that would be lost?)."""
    fmt = mapping.get("date_format")
    date_col, desc_col, party_col = (mapping.get(k, "") for k in ("date", "description", "counterparty"))
    type_col = next((h for h in header if _norm(h) in BOOKING_TYPE_COLUMNS), None)
    info_col = next((h for h in header if _norm(h) == "info"), None)

    def amount(row):
        a = _parse_amount(row.get(mapping["amount"])) if mapping.get("amount") else None
        if a is None and (mapping.get("debit") or mapping.get("credit")):
            debit, credit = (_parse_amount(row.get(mapping.get(k, ""))) for k in ("debit", "credit"))
            a = None if debit is None and credit is None else (credit or 0) - abs(debit or 0)
        return a

    def key(row):
        return str(row.get(date_col, "")).strip(), _norm(row.get(type_col)) if type_col else ""

    rows = [r for r in rows if not (info_col and _norm(r.get(info_col)).startswith("umsatz vorgemerkt"))]
    readable = [bool(_parse_date(r.get(date_col, ""), fmt)) and amount(r) is not None for r in rows]
    unread = [i for i, ok in enumerate(readable) if not ok]
    if not unread:
        return None
    texts = {_norm(r.get(desc_col)) for r, ok in zip(rows, readable) if ok}
    types: dict[str, int] = {}
    out = {"rows": len(unread), "no_date": 0, "no_amount": 0, "counterparty": 0,
           "like_above": 0, "like_below": 0, "text_on_a_read_row": 0}
    for i in unread:
        row = rows[i]
        out["no_date"] += not _parse_date(row.get(date_col, ""), fmt)
        out["no_amount"] += amount(row) is None
        out["counterparty"] += bool(str(row.get(party_col, "") or "").strip())
        above = next((rows[j] for j in range(i - 1, -1, -1) if readable[j]), None)
        below = next((rows[j] for j in range(i + 1, len(rows)) if readable[j]), None)
        out["like_above"] += above is not None and key(above) == key(row)
        out["like_below"] += below is not None and key(below) == key(row)
        out["text_on_a_read_row"] += bool(_norm(row.get(desc_col))) and _norm(row.get(desc_col)) in texts
        t = _norm(row.get(type_col)) if type_col else ""
        label = t.upper() if t in BOOKING_TYPES else ("<other>" if t else "<empty>")
        types[label] = types.get(label, 0) + 1
    out["types"] = dict(sorted(types.items(), key=lambda kv: -kv[1]))
    return out


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
    if ext in CAMT_EXT:
        return _inspect_camt(data, report)
    if ext == ".csv":
        all_rows, info = _read_csv_rows(data)
    elif ext in (".xlsx", ".xls"):
        all_rows, info = _read_excel_rows(data)
    else:
        report.update(format=ext or "unknown", status="unsupported")
        return report
    report.update(info)
    if info["format"] == "XLSX":
        report["formulas"] = _uncached_formulas(data)

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
        missing=[] if profile in PAYPAL_PROFILES else [
            f for f in ("date", "amount", "description", "counterparty", "currency")
            if f not in known and not (f == "amount" and "debit" in known)],
        sample_shapes=[[_shape(c) for c in r] for r in data_rows[:3]],
        data_rows=len(data_rows),
    )
    if not profile and header:
        near, odd = _near_profile(header), _odd_names(header)
        if near:
            report["near"] = near
        if odd:
            report["odd_names"] = odd
    if mapping or profile:
        txs, by_rule = _table_transactions(header, [dict(zip(header, r)) for r in data_rows], mapping, profile)
        report["parsed"] = len(txs)
        if txs:
            dates = sorted(t["date"] for t in txs)
            report["date_range"] = f"{dates[0]} .. {dates[-1]}"
        unreadable = len(data_rows) - len(txs) - by_rule
        if unreadable and profile not in PAYPAL_PROFILES and profile != "barclays":
            report["unread"] = _unread_rows(header, [dict(zip(header, r)) for r in data_rows], mapping)
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
        if r.get("near"):
            lines.append(f"near:     {r['near']}")
        if r.get("odd_names"):
            lines.append(f"odd:      {' | '.join(r['odd_names'])}")
        if r.get("formulas"):
            lines.append(f"formulas: {r['formulas']} cells without a stored value (they read as empty)")
        for shape in r["sample_shapes"]:
            lines.append(f"shape:    {' | '.join(shape)}")
        lines.append(f"rows:     {r['data_rows']}" + (f", parsed {r['parsed']}" if "parsed" in r else "")
                     + (f", {r['date_range']}" if "date_range" in r else ""))
        if r.get("unread"):
            u = r["unread"]
            lines.append(
                f"unread:   {u['rows']} rows: no amount {u['no_amount']}, no date {u['no_date']}, "
                f"with a counterparty {u['counterparty']}; same day and booking type as the read row "
                f"above {u['like_above']}, below {u['like_below']}; text also on a read row "
                f"{u['text_on_a_read_row']}")
            lines.append("types:    " + ", ".join(f"{k} {v}" for k, v in u["types"].items()))
    if "entries" in r:
        lines.append(f"reports:  {r['reports']}")
        lines.append(f"entries:  {r['entries']}")
        lines.append(f"fields:   {r['fields']}")
        lines.append(f"balances: {r['balance_codes']}")
        lines.append(f"rows:     parsed {r['parsed']}" + (f", {r['date_range']}" if "date_range" in r else ""))
        lines.append(f"balance:  {r['balance']}")
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


def _link_funding_transfers(con) -> int:
    """Mark bank movements that only fund PayPal or a credit card as transfers.

    A PayPal payment funded from a bank account shows up twice: as the payment in
    PayPal and as a debit "PayPal Europe …" in the bank. The same goes for the monthly
    repayment of a card (a "Barclays …" debit in the bank). With the PayPal or card side
    imported, its funding row is a transfer, and so must be the bank debit with the
    opposite amount within a few days — else the expense counts twice.
    Runs after every import, so the order of the imports does not matter.
    """
    transfer = con.execute("SELECT id FROM categories WHERE key='transfer'").fetchone()
    if not transfer:
        return 0
    linked = 0
    partner = " OR ".join("LOWER(counterparty) LIKE ? OR LOWER(description) LIKE ?" for _ in FUNDING_PARTNERS)
    words = [f"%{w}%" for w in FUNDING_PARTNERS for _ in range(2)]
    funding = con.execute("""SELECT id, account_id, date, amount FROM transactions
                             WHERE category_id = ? ORDER BY date""", (transfer["id"],)).fetchall()
    for f in funding:
        match = con.execute(f"""
            SELECT id FROM transactions
            WHERE account_id != ? AND ROUND(amount + ?, 2) = 0
              AND ABS(julianday(date) - julianday(?)) <= 5
              AND (category_id IS NULL OR category_id != ?)
              AND ({partner})
            ORDER BY ABS(julianday(date) - julianday(?)) LIMIT 1""",
            (f["account_id"], f["amount"], f["date"], transfer["id"], *words, f["date"])).fetchone()
        if match:
            con.execute("UPDATE transactions SET category_id=? WHERE id=?", (transfer["id"], match["id"]))
            linked += 1
    con.commit()
    return linked


def import_transactions(account_id: int, filename: str, txs: list[dict],
                        use_gemma_categorize: bool = False, import_id: int | None = None) -> dict:
    con = db.get_db()
    cat_rows = con.execute("SELECT id, name, key FROM categories").fetchall()
    cat_by_name = {r["name"]: r["id"] for r in cat_rows}
    cat_by_key = {r["key"]: r["id"] for r in cat_rows if r["key"]}

    inserted, skipped, voided, to_categorize = [], 0, 0, []
    unreferenced = {}   # (date, amount) -> rows of other exports still to match
    known = {}          # (date, amount) -> rows of the account still to match, once it has CAMT data
    has_camt = con.execute("""SELECT 1 FROM transactions t JOIN imports i ON i.id = t.import_id
                              WHERE t.account_id=? AND i.format LIKE 'CAMT%' LIMIT 1""", (account_id,)).fetchone()
    for tx in txs:
        # a reference of the source (PayPal transaction code) keeps identical payments apart
        dedup_text = tx.get("description", "") + (f"|{tx['ref']}" if tx.get("ref") else "")
        h = db.tx_hash(account_id, tx["date"], tx["amount"], dedup_text)
        ref = tx.get("ref") or None
        if tx.get("void"):  # failed after all: remove it if an earlier import brought it as pending
            voided += con.execute("DELETE FROM transactions WHERE import_hash=? OR (account_id=? AND ref=?)",
                                  (h, account_id, ref)).rowcount
            continue
        # the same reference from another export of the source (PayPal activity vs. balance report)
        if ref and con.execute("SELECT 1 FROM transactions WHERE account_id=? AND ref=?",
                               (account_id, ref)).fetchone():
            skipped += 1
            continue
        # A CAMT entry is the same movement as a row of another export of the account (CSV, MT940)
        # with its date and amount: those have no bank reference and word it differently. One to
        # one, so two equal payments on one day stay two.
        if tx.get("other_formats"):
            same = (tx["date"], round(tx["amount"], 2))
            if same not in unreferenced:
                unreferenced[same] = con.execute(
                    """SELECT COUNT(*) FROM transactions WHERE account_id=? AND date=? AND ROUND(amount - ?, 2) = 0
                       AND ref IS NULL AND COALESCE(source_file,'') != ?""",
                    (account_id, tx["date"], tx["amount"], filename)).fetchone()[0]
            if unreferenced[same]:
                unreferenced[same] -= 1
                skipped += 1
                continue
        # The other way round: once the account has CAMT data, a row without a reference (a CSV
        # export after all) is a movement the account already has while the account has more rows
        # with its date and amount than this file brought so far, whatever their wording. Two
        # known equal payments on a day take two rows of the file; a third one is added.
        elif not ref and has_camt:
            same = (tx["date"], round(tx["amount"], 2))
            if same not in known:
                known[same] = con.execute(
                    "SELECT COUNT(*) FROM transactions WHERE account_id=? AND date=? AND ROUND(amount - ?, 2) = 0",
                    (account_id, tx["date"], tx["amount"])).fetchone()[0]
            if known[same]:
                known[same] -= 1
                skipped += 1
                continue
        cat_id = cat_by_key.get(tx.get("category_key")) or _learned_category(con, tx)
        try:
            cur = con.execute(
                """INSERT INTO transactions
                   (account_id, date, amount, currency, description, counterparty,
                    category_id, source_file, import_hash, import_id, ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, tx["date"], tx["amount"], tx.get("currency", "EUR"),
                 tx.get("description", ""), tx.get("counterparty", ""),
                 cat_id, filename, h, import_id, ref))
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
    linked = _link_funding_transfers(con)

    categorized = 0
    if use_gemma_categorize and to_categorize and gemma.available():
        categorized = _gemma_batch_categorize(con, to_categorize, cat_by_name)
    con.close()
    return {"inserted": len(inserted), "skipped_duplicates": skipped,
            "auto_categorized": categorized + len(inserted) - len(to_categorize),
            "uncategorized": len(to_categorize) - categorized,
            "linked_transfers": linked, "possible_duplicates": possible_duplicates, "voided": voided}


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
