# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Demo mode: the app on a throwaway database and statements folder, filled with invented
accounts, statements and assets — to try the app without bank data of your own, and to make
the README screenshots. It never touches the real database or statements: everything lives
in a new temporary folder, whatever FINANCIE_DB and FINANCIE_IMPORTS say.

Every name, IBAN and amount below is made up.
"""

from __future__ import annotations

import random
import tempfile
from datetime import date, timedelta
from pathlib import Path

from financie import db

GIRO_IBAN = "DE00500105170000000001"
SAVINGS_IBAN = "DE00500105170000000002"
BANK = "Sparkasse Musterstadt"
MONTHS = 10   # full months of history before the current one

# counterparty -> default category key, applied as if the user had sorted them once
CATEGORY_OF = {
    "Muster GmbH": "salary", "Hausverwaltung Muster": "housing", "Stadtwerke Musterstadt": "energy",
    "Versicherung Muster AG": "insurance", "Kfz-Versicherung Muster": "car", "Mobilfunk Muster": "internet_phone", "Hilfswerk Muster": "donations",
    "Streaming Muster": "subscriptions", "Verkehrsverbund Muster": "transport",
    "Supermarkt Nord": "groceries", "Bio-Markt Muster": "groceries", "Discounter Sued": "groceries",
    "Pizzeria Muster": "restaurants", "Cafe am Markt": "restaurants", "Tankstelle Muster": "car",
    "Apotheke Muster": "health", "Sportverein Muster": "sport", "Kino Muster": "entertainment",
    "Onlineshop Muster": "household", "Modehaus Muster": "clothing", "Bahn Muster": "travel",
    "Hotel Muster": "travel", "Buchhandlung Muster": "education", "Sparkonto": "transfer",
    "Kreditkarte Muster": "transfer", "Giro": "transfer", "Zinsen": "investment_income",
}


# ---------- invented movements ----------

def _months(today: date) -> list[date]:
    first = today.replace(day=1)
    out = []
    for _ in range(MONTHS + 1):
        out.insert(0, first)
        first = (first - timedelta(days=1)).replace(day=1)
    return out


def _movements(today: date) -> dict[str, list[tuple[date, float, str, str]]]:
    """(day, amount, counterparty, purpose) per account, up to yesterday."""
    rnd = random.Random(7)
    giro, savings, card = [], [], []
    for m in _months(today):
        def day(d, m=m):
            return m.replace(day=min(d, 28))

        giro += [(day(1), 3250.00, "Muster GmbH", f"Gehalt {m:%m/%Y}"),
                 (day(1), -48.90, "Versicherung Muster AG", "Haftpflicht und Hausrat"),
                 (day(2), -1050.00, "Hausverwaltung Muster", f"Miete {m:%m/%Y}"),
                 (day(4), -49.00, "Verkehrsverbund Muster", "Deutschlandticket"),
                 (day(5), -400.00, "Sparkonto", "Dauerauftrag Sparen"),
                 (day(12), -29.99, "Mobilfunk Muster", "Rechnung Mobilfunk"),
                 (day(15), -95.00, "Stadtwerke Musterstadt", "Abschlag Strom"),
                 (day(18), -12.99, "Streaming Muster", "Abo"),
                 (day(22), -35.00, "Sportverein Muster", "Mitgliedsbeitrag"),
                 (day(25), -20.00, "Hilfswerk Muster", "Spende")]
        savings.append((day(5), 400.00, "Giro", "Dauerauftrag Sparen"))
        if m.month in (1, 4, 7, 10):
            giro.append((day(10), -142.80, "Kfz-Versicherung Muster", "Beitrag Quartal"))
        if m.month in (1, 4, 7, 10):
            savings.append((day(1), round(rnd.uniform(18, 26), 2), "Zinsen", "Zinsgutschrift"))
        for d in range(1, 29, 3):
            shop = rnd.choice(["Supermarkt Nord", "Bio-Markt Muster", "Discounter Sued"])
            giro.append((day(d), -round(rnd.uniform(18, 92), 2), shop, "Kartenzahlung"))
        for d in rnd.sample(range(2, 28), 4):
            giro.append((day(d), -round(rnd.uniform(9, 58), 2),
                         rnd.choice(["Pizzeria Muster", "Cafe am Markt"]), "Kartenzahlung"))
        giro.append((day(rnd.randint(6, 26)), -round(rnd.uniform(52, 78), 2), "Tankstelle Muster", "Kartenzahlung"))
        if rnd.random() < 0.5:
            giro.append((day(rnd.randint(3, 26)), -round(rnd.uniform(8, 40), 2), "Apotheke Muster", "Kartenzahlung"))
        card.append((day(1), -9.99, "Buchhandlung Muster", "E-Book Abo"))
        for _ in range(rnd.randint(3, 5)):
            card.append((day(rnd.randint(1, 27)), -round(rnd.uniform(15, 140), 2),
                         rnd.choice(["Onlineshop Muster", "Modehaus Muster", "Kino Muster", "Buchhandlung Muster"]),
                         "Kartenumsatz"))
        if m.month in (5, 8, 12):
            card += [(day(9), -189.00, "Bahn Muster", "Fahrkarte"), (day(10), -264.50, "Hotel Muster", "2 Naechte")]
    # the card is settled from the Giro on the 20th: last month's card spending
    for m in _months(today)[1:]:
        prev = (m - timedelta(days=1)).replace(day=1)
        spent = round(-sum(a for d, a, *_ in card if d.replace(day=1) == prev and a < 0), 2)
        if spent:
            settle = m.replace(day=20)
            giro.append((settle, -spent, "Kreditkarte Muster", "Abrechnung Kreditkarte"))
            card.append((settle, spent, "Giro", "Ausgleich Lastschrift"))
    keep = lambda xs: sorted((x for x in xs if x[0] < today), key=lambda x: x[0])
    return {"giro": keep(giro), "savings": keep(savings), "card": keep(card)}


# ---------- invented statement files ----------

def _camt_month(month: date, rows, opening: float, today: date) -> tuple[bytes, float]:
    """A camt.053.001.08 statement of one month (up to yesterday), with its opening and closing
    booked balance, as the bank would send it."""
    def amt(x):
        return f"<Amt Ccy='EUR'>{abs(x):.2f}</Amt><CdtDbtInd>{'DBIT' if x < 0 else 'CRDT'}</CdtDbtInd>"

    def bal(code, day, x):
        return f"<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp>{amt(x)}<Dt><Dt>{day}</Dt></Dt></Bal>"

    nxt = (month + timedelta(days=32)).replace(day=1)
    created = min(nxt, today)
    closing = round(opening + sum(a for _d, a, _p, _u in rows), 2)
    entries = []
    for i, (d, a, party, purpose) in enumerate(rows):
        side = "Cdtr" if a < 0 else "Dbtr"
        entries.append(
            f"<Ntry>{amt(a)}<Sts><Cd>BOOK</Cd></Sts><BookgDt><Dt>{d}</Dt></BookgDt><ValDt><Dt>{d}</Dt></ValDt>"
            f"<AcctSvcrRef>{month:%Y%m}{i:04d}</AcctSvcrRef><NtryDtls><TxDtls><RltdPties>"
            f"<{side}><Pty><Nm>{party}</Nm></Pty></{side}></RltdPties>"
            f"<RmtInf><Ustrd>{purpose}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>")
    last = created - timedelta(days=1)
    xml = (f"<?xml version='1.0' encoding='UTF-8'?>"
           f"<Document xmlns='urn:iso:std:iso:20022:tech:xsd:camt.053.001.08'><BkToCstmrStmt>"
           f"<GrpHdr><MsgId>DEMO{month:%Y%m}</MsgId><CreDtTm>{created}T06:00:00</CreDtTm></GrpHdr>"
           f"<Stmt><Id>S{month:%Y%m}</Id><Acct><Id><IBAN>{GIRO_IBAN}</IBAN></Id>"
           f"<Svcr><FinInstnId><Nm>{BANK}</Nm></FinInstnId></Svcr></Acct>"
           + bal("PRCD", month - timedelta(days=1), opening) + "".join(entries) + bal("CLBD", last, closing)
           + "</Stmt></BkToCstmrStmt></Document>")
    return xml.encode("utf-8"), closing


def _de(amount: float) -> str:
    return f"{amount:.2f}".replace(".", ",")


def _sparkasse_rows(iban: str, rows) -> bytes:
    """A Sparkasse CSV-CAMT export of the given movements."""
    head = ('"Auftragskonto";"Buchungstag";"Valutadatum";"Buchungstext";"Verwendungszweck";'
            '"Beguenstigter/Zahlungspflichtiger";"Kontonummer/IBAN";"BIC (SWIFT-Code)";"Betrag";"Waehrung";"Info"\n')
    lines = [f'"{iban}";"{d:%d.%m.%y}";"{d:%d.%m.%y}";"{"GUTSCHRIFT" if a > 0 else "LASTSCHRIFT"}";"{u}";"{p}";'
             f'"DE00000000000000000000";"DEMOXXXX";"{_de(a)}";"EUR";"Umsatz gebucht"\n' for d, a, p, u in rows]
    return (head + "".join(lines)).encode("cp1252")


def _card_csv(rows) -> bytes:
    """A credit card export with plain German column names."""
    lines = ["Datum;Beschreibung;Empfänger;Betrag"] + [f"{d:%d.%m.%Y};{u};{p};{_de(a)}" for d, a, p, u in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_statements(folder: Path, today: date) -> dict[str, list[Path]]:
    """The statement files of the demo, as downloaded from the banks: the Giro as one CAMT file
    per month, the savings account and the card as CSV. Returns the files per account."""
    folder.mkdir(parents=True, exist_ok=True)
    mv = _movements(today)
    files: dict[str, list[Path]] = {"giro": [], "savings": [], "card": []}
    balance = 2140.37   # the Giro before the first month
    for month in _months(today):
        rows = [r for r in mv["giro"] if r[0].replace(day=1) == month]
        data, balance = _camt_month(month, rows, balance, today)
        path = folder / f"Giro-{BANK.split()[0]}-{month:%Y%m}-camt053.xml"
        path.write_bytes(data)
        files["giro"].append(path)
    path = folder / "Sparkonto-Sparkasse-umsatz.CSV"
    path.write_bytes(_sparkasse_rows(SAVINGS_IBAN, mv["savings"]))
    files["savings"].append(path)
    path = folder / "Kreditkarte-Umsaetze.csv"
    path.write_bytes(_card_csv(mv["card"]))
    files["card"].append(path)
    # the last full month of the Giro again, as CSV: left to import by hand, it shows that a
    # movement already there in another format is not added twice
    last = _months(today)[-2]
    path = folder / f"Giro-Sparkasse-{last:%Y%m}-umsatz.CSV"
    path.write_bytes(_sparkasse_rows(GIRO_IBAN, [r for r in mv["giro"] if r[0].replace(day=1) == last]))
    return files


# ---------- the demo database ----------

def build(folder: Path, today: date | None = None) -> Path:
    """Create the demo database and statements in `folder` (it must be a fresh one) and import
    them through the app's own import, as a user would."""
    from financie import app as webapp

    today = today or date.today()
    db.DB_PATH = folder / "financie-demo.db"
    db.IMPORTS_DIR = str(folder / "statements")
    assert not db.DB_PATH.exists(), "the demo needs a fresh folder"
    db.init_db()
    files = write_statements(folder / "statements", today)

    con = db.get_db()
    con.execute("UPDATE accounts SET name='Giro', is_main=1 WHERE id=(SELECT MIN(id) FROM accounts)")
    con.execute("INSERT INTO accounts(name, type) VALUES ('Sparkonto', 'savings'), ('Visa', 'credit_card')")
    con.commit()
    ids = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM accounts")}
    con.close()

    for path in files["giro"]:
        webapp._import_bytes(ids["Giro"], path.name, path.read_bytes())
    webapp._import_bytes(ids["Sparkonto"], files["savings"][0].name, files["savings"][0].read_bytes())
    webapp._import_bytes(ids["Visa"], files["card"][0].name, files["card"][0].read_bytes())

    con = db.get_db()
    keys = {r["key"]: r["id"] for r in con.execute("SELECT id, key FROM categories WHERE key IS NOT NULL")}
    for party, key in CATEGORY_OF.items():
        con.execute("UPDATE transactions SET category_id=?, category_source='user' WHERE counterparty=?",
                    (keys[key], party))
    # balances as the user would enter them (✎ and ⚖): the card from its statement, the savings
    # account from online banking — whose last check shows a movement missing in the app
    yesterday = (today - timedelta(days=1)).isoformat()
    for name, amount in (("Visa", 0.0), ("Sparkonto", 9310.55)):
        con.execute("UPDATE accounts SET balance_date=?, balance_amount=? WHERE id=?",
                    (_months(today)[0].isoformat(), amount, ids[name]))
    con.commit()
    savings = db.balance_at(con, ids["Sparkonto"], yesterday)
    db.add_balance_check(con, ids["Sparkonto"], yesterday, round(savings + 4.18, 2))
    month = today.strftime("%Y-%m")
    con.executemany("INSERT INTO budgets(category_id, month, amount) VALUES (?, '*', ?)",
                    [(keys["groceries"], 450), (keys["restaurants"], 120), (keys["subscriptions"], 50),
                     (keys["car"], 90)])
    con.execute("INSERT INTO budgets(category_id, month, amount) VALUES (?, ?, ?)", (keys["travel"], month, 300))
    # two trips, tagged as the user would: the train, the hotel and the meals of those days
    for tag, mm in (("Hamburg weekend", "05"), ("Lake Constance", "08")):
        tag_id = con.execute("INSERT INTO tags(name) VALUES (?)", (f"{tag} {today.year}",)).lastrowid
        con.execute("""INSERT INTO transaction_tags(transaction_id, tag_id)
                       SELECT id, ? FROM transactions WHERE substr(date, 6, 2) = ? AND (
                           counterparty IN ('Bahn Muster', 'Hotel Muster') OR
                           (counterparty IN ('Pizzeria Muster', 'Cafe am Markt') AND substr(date, 9, 2) BETWEEN '09' AND '12'))""",
                    (tag_id, mm))
    from financie import planning
    planning.set_plans(con, [{"name": "Summer holiday", "month": f"{today.year + 1}-07", "amount": 1800},
                             {"name": "New washing machine", "month": f"{today.year + 1}-03", "amount": 650}])
    for name, kind, start, step in (("Apartment", "real_estate", 285000, 1200), ("ETF portfolio", "etf", 18400, 410),
                                    ("Mortgage", "liability", -182000, 610)):
        asset = con.execute("INSERT INTO assets(name, type) VALUES (?, ?)", (name, kind)).lastrowid
        con.executemany("INSERT INTO asset_values(asset_id, date, value) VALUES (?, ?, ?)",
                        [(asset, m.isoformat(), start + i * step) for i, m in enumerate(_months(today))])
    con.commit()
    con.close()
    return db.DB_PATH


def run(host: str = "127.0.0.1", port: int = 8501) -> None:
    import uvicorn

    from financie.app import app

    folder = Path(tempfile.mkdtemp(prefix="financie-demo-"))
    build(folder)
    print(f"Demo data (invented) in {folder}; your own database is not used.", flush=True)
    print(f"Open http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port)
