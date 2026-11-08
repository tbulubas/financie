# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""CAMT (ISO 20022 XML) statements: entries, the booked balance and the switch from the CSV
export of the same account. All entries, names and the IBAN are invented."""

import io
import zipfile

from financie import importer
from test_api import upload
from test_folder import preview
from test_formats import sparkasse_csv

IBAN = "DE00999988887777666655"
V08 = "urn:iso:std:iso:20022:tech:xsd:camt.052.001.08"
V02 = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"


def bal(code, day, amount):
    sign = "DBIT" if amount < 0 else "CRDT"
    return (f"<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp><Amt Ccy='EUR'>{abs(amount):.2f}</Amt>"
            f"<CdtDbtInd>{sign}</CdtDbtInd><Dt><Dt>{day}</Dt></Dt></Bal>")


def ntry(day, amount, party="", purpose="", ref="", status="BOOK", info="", old=False, batch=0):
    sign = "DBIT" if amount < 0 else "CRDT"
    side = "Cdtr" if amount < 0 else "Dbtr"
    name = f"<{side}><Nm>{party}</Nm></{side}>" if old else f"<{side}><Pty><Nm>{party}</Nm></Pty></{side}>"
    one = (f"<TxDtls><RltdPties>{name if party else ''}</RltdPties>"
           f"<RmtInf><Ustrd>{purpose}</Ustrd></RmtInf></TxDtls>")
    details = "".join("<TxDtls><RmtInf><Ustrd>part</Ustrd></RmtInf></TxDtls>" for _ in range(batch)) or one
    sts = f"<Sts>{status}</Sts>" if old else f"<Sts><Cd>{status}</Cd></Sts>"
    return (f"<Ntry><Amt Ccy='EUR'>{abs(amount):.2f}</Amt><CdtDbtInd>{sign}</CdtDbtInd>{sts}"
            f"<BookgDt><Dt>{day}</Dt></BookgDt><ValDt><Dt>{day}</Dt></ValDt>"
            + (f"<AcctSvcrRef>{ref}</AcctSvcrRef>" if ref else "")
            + (f"<AddtlNtryInf>{info}</AddtlNtryInf>" if info else "")
            + f"<NtryDtls>{details}</NtryDtls></Ntry>")


def camt(*parts, ns=V08, created="2026-05-08T08:00:00"):
    msg, rpt = ("BkToCstmrAcctRpt", "Rpt") if "camt.052" in ns else ("BkToCstmrStmt", "Stmt")
    return (f"<?xml version='1.0' encoding='UTF-8'?><Document xmlns='{ns}'><{msg}>"
            f"<GrpHdr><MsgId>M1</MsgId><CreDtTm>{created}</CreDtTm></GrpHdr>"
            f"<{rpt}><Id>R1</Id><Acct><Id><IBAN>{IBAN}</IBAN></Id>"
            f"<Svcr><FinInstnId><Nm>Sparkasse Musterstadt</Nm></FinInstnId></Svcr></Acct>"
            + "".join(parts) + f"</{rpt}></{msg}></Document>").encode("utf-8")


# 1000 before the report, five booked days, one pending card payment, closing at 1420
ENTRIES = (
    bal("PRCD", "2026-04-30", 1000),
    ntry("2026-05-01", 3000, "Arbeitgeber Muster", "Gehalt Mai", "R001"),
    ntry("2026-05-02", -900, "Vermieter Muster", "Miete Mai", "R002"),
    ntry("2026-05-04", -50, "PayPal Europe", "PP.5678.PP", "R003"),
    ntry("2026-05-06", -3.5, "Kiosk Muster", "Karte", "R004"),
    ntry("2026-05-06", -3.5, "Kiosk Muster", "Karte", "R005"),
    ntry("2026-05-07", -23, "Buchladen Muster", "Karte", "R006", status="PDNG"),
    ntry("2026-05-08", -1623, "", "", "R007", info="SAMMEL-LS", batch=2),
    bal("CLBD", "2026-05-08", 1420),
)
REPORT = camt(*ENTRIES)


def test_camt_entries_and_the_stated_balance():
    txs, fmt = importer.parse_statement("2026-05-08_camt.xml", REPORT)
    assert fmt == "CAMT (camt.052.001.08)"
    got = [(t["date"], t["amount"], t["counterparty"], t["description"], t.get("ref")) for t in txs]
    assert got == [
        ("2026-05-01", 3000.0, "Arbeitgeber Muster", "Gehalt Mai", "R001"),
        ("2026-05-02", -900.0, "Vermieter Muster", "Miete Mai", "R002"),
        ("2026-05-04", -50.0, "PayPal Europe", "PP.5678.PP", "R003"),
        ("2026-05-06", -3.5, "Kiosk Muster", "Karte", "R004"),
        ("2026-05-06", -3.5, "Kiosk Muster", "Karte", "R005"),
        ("2026-05-08", -1623.0, "", "SAMMEL-LS", "R007"),   # a batch stays one booked entry
    ]
    # made on the 8th: the 6th is the last complete day, 1420 + 1623 at its end
    stated = [(t["date"], t["day_balance"]) for t in txs if "day_balance" in t]
    assert stated == [("2026-05-06", 3043.0)]


def test_old_camt_053_in_a_zip():
    xml = camt(bal("OPBD", "2026-05-01", -100), ntry("2026-05-01", 250, "Muster GmbH", "Erstattung", old=True),
               ntry("2026-05-02", -20, "Laden Muster", "Einkauf", status="PDNG", old=True),
               bal("CLBD", "2026-05-01", 150), ns=V02, created="2026-05-02T07:00:00")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("camt053_1.xml", xml)
    txs, fmt = importer.parse_statement("Umsaetze.zip", buf.getvalue())
    assert fmt == "CAMT (camt.053.001.02)"
    assert [(t["date"], t["amount"], t["counterparty"], t.get("day_balance")) for t in txs] == [
        ("2026-05-01", 250.0, "Muster GmbH", 150.0)]


def test_no_balance_when_the_entries_do_not_add_up():
    xml = camt(bal("PRCD", "2026-04-30", 1000), ntry("2026-05-01", -10, "Laden", "x"),
               bal("CLBD", "2026-05-01", 995))
    txs, _ = importer.parse_statement("camt.xml", xml)
    assert not any("day_balance" in t for t in txs)
    assert "do not add up" in importer.inspect_file("camt.xml", xml)["balance"]


def test_inspect_shows_the_structure_only():
    text = importer.format_report(importer.inspect_file("2026-05-08_camt.xml", REPORT))
    assert "camt.052.001.08" in text and "BOOK 6, PDNG 1" in text and "CLBD, PRCD" in text
    assert "stated for the end of 2026-05-06, entries add up to the opening balance" in text
    for secret in (IBAN, "Muster", "Gehalt", "3000", "1420", "R001"):
        assert secret not in text


# the same account exported earlier as CSV-CAMT: the same movements, worded differently
CSV = sparkasse_csv(
    f'"{IBAN}";"01.05.26";"01.05.26";"GUTSCHRIFT";"Gehalt";"Arbeitgeber";"DE01";"X";"3000,00";"EUR";"Umsatz gebucht"',
    f'"{IBAN}";"02.05.26";"02.05.26";"LASTSCHRIFT";"Miete";"Vermieter";"DE02";"X";"-900,00";"EUR";"Umsatz gebucht"',
    f'"{IBAN}";"06.05.26";"06.05.26";"KARTENZAHLUNG";"Kiosk";"Kiosk";"DE03";"X";"-3,50";"EUR";"Umsatz gebucht"')


def test_switch_from_csv_to_camt_keeps_the_account_and_counts_each_movement_once(client, account_id):
    upload(client, account_id, CSV, "umsatz.csv")
    assert preview(client, ("2026-05-08_camt.xml", REPORT))["2026-05-08_camt.xml"]["account_id"] == account_id

    r = upload(client, account_id, REPORT, "2026-05-08_camt.xml").json()
    # salary, rent and one of the two kiosk payments were in the CSV; the second one was not
    assert (r["inserted"], r["skipped_duplicates"]) == (3, 3)
    assert r["balance"] == {"date": "2026-05-06", "amount": 3043.0, "known": True, "difference": 0,
                            "days": 0, "off": 0, "first_off": None, "first_difference": None, "gaps": []}
    acc = next(a for a in client.get("/api/accounts").json() if a["id"] == account_id)
    assert acc["balance"] == 1420.0

    # the same entries downloaded a day later: all six are known by the bank's reference, and
    # the 8th is complete now, so its balance is a check
    again = upload(client, account_id, camt(*ENTRIES, created="2026-05-09T08:00:00"), "camt-2.xml").json()
    assert (again["inserted"], again["skipped_duplicates"]) == (0, 6)
    assert (again["balance"]["date"], again["balance"]["amount"], again["balance"]["difference"]) == (
        "2026-05-08", 1420.0, 0)


def daily_zip() -> bytes:
    """Like the Sparkasse "camt52v8Booked" ZIP: one report per booking day, each with its opening
    and closing booked balance."""
    days = [("2026-05-01", 1000.0, [ntry("2026-05-01", 3000, "Arbeitgeber Muster", "Gehalt", "R001")]),
            ("2026-05-02", 4000.0, [ntry("2026-05-02", -900, "Vermieter Muster", "Miete", "R002")]),
            ("2026-05-06", 3100.0, [ntry("2026-05-06", -3.5, "Kiosk Muster", "Karte", "R004"),
                                    ntry("2026-05-06", -3.5, "Kiosk Muster", "Karte", "R005")])]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for day, opening, entries in days:
            closing = opening + {"2026-05-01": 3000, "2026-05-02": -900, "2026-05-06": -7}[day]
            z.writestr(f"{day}.xml", camt(bal("OPBD", day, opening), *entries, bal("CLBD", day, closing)))
    return buf.getvalue()


def test_inspect_sums_up_one_balance_per_day():
    r = importer.inspect_file("Giro-camt52v8Booked.ZIP", daily_zip())
    assert r["format"] == "CAMT camt.052.001.08 (XML in ZIP, 3 files)"
    assert r["balance"] == "stated for 3 days, 2026-05-01 .. 2026-05-06, entries add up to the opening balance"


def test_every_stated_day_is_checked(client, account_id):
    # a wrong known balance: the app is 100 below the bank on every day
    client.patch(f"/api/accounts/{account_id}", json={"balance_date": "2026-05-02", "balance_amount": 3000})
    b = upload(client, account_id, daily_zip(), "Giro-camt52v8Booked.ZIP").json()["balance"]
    assert (b["known"], b["days"], b["off"], b["first_off"], b["first_difference"]) == (
        False, 3, 3, "2026-05-01", -100.0)
    assert (b["date"], b["difference"]) == ("2026-05-06", -100.0)

    client.patch(f"/api/accounts/{account_id}", json={"balance_date": "2026-05-02", "balance_amount": 3100})
    acc = next(a for a in client.get("/api/accounts").json() if a["id"] == account_id)
    assert [c["difference"] for c in acc["checks"]] == [0, 0, 0]   # computed live


def test_csv_after_camt_adds_only_what_camt_does_not_have(client, account_id):
    upload(client, account_id, REPORT, "2026-05-08_camt.xml")
    later = sparkasse_csv(
        f'"{IBAN}";"06.05.26";"06.05.26";"KARTENZAHLUNG";"Kiosk";"Kiosk";"DE03";"X";"-3,50";"EUR";"Umsatz gebucht"',
        f'"{IBAN}";"06.05.26";"06.05.26";"KARTENZAHLUNG";"Kiosk 2";"Kiosk";"DE03";"X";"-3,50";"EUR";"Umsatz gebucht"',
        # a third equal payment on that day: CAMT has two, so this one is new
        f'"{IBAN}";"06.05.26";"06.05.26";"KARTENZAHLUNG";"Kiosk 3";"Kiosk";"DE03";"X";"-3,50";"EUR";"Umsatz gebucht"',
        f'"{IBAN}";"08.05.26";"08.05.26";"SAMMEL-LS";"Sammler";"";"";"";"-1623,00";"EUR";"Umsatz gebucht"',
        f'"{IBAN}";"09.05.26";"09.05.26";"LASTSCHRIFT";"Strom";"Stadtwerke";"DE04";"X";"-60,00";"EUR";"Umsatz gebucht"')
    r = upload(client, account_id, later, "umsatz-later.csv").json()
    assert (r["inserted"], r["skipped_duplicates"], r["possible_duplicates"]) == (2, 3, 1)
    rows = client.get("/api/transactions", params={"account_id": account_id}).json()
    assert sorted(t["description"] for t in rows if t["date"] in ("2026-05-06", "2026-05-09")) == [
        "Karte", "Karte", "Kiosk 3", "Strom"]


def test_accounts_without_camt_keep_the_csv_rules(client, account_id):
    # two exports of a CSV-only account: a reworded movement is flagged, not dropped
    upload(client, account_id, CSV, "umsatz.csv")
    reworded = sparkasse_csv(
        f'"{IBAN}";"01.05.26";"01.05.26";"GUTSCHRIFT";"Lohn Mai";"Arbeitgeber";"DE01";"X";"3000,00";"EUR";"Umsatz gebucht"')
    r = upload(client, account_id, reworded, "mt940.csv").json()
    assert (r["inserted"], r["possible_duplicates"]) == (1, 1)
