# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Bank-specific layouts: PayPal, credit card CSV, Sparkasse pending rows. All rows are invented;
only the column layouts follow the real exports."""

import csv
import io

from financie import importer
from test_api import upload

PAYPAL_HEADER = [
    "Datum", "Uhrzeit", "Zeitzone", "Name", "Typ", "Status", "Währung", "Brutto", "Gebühr", "Netto",
    "Absender E-Mail-Adresse", "Empfänger E-Mail-Adresse", "Transaktionscode", "Lieferadresse",
    "Adress-Status", "Artikelbezeichnung", "Artikelnummer", "Versand- und Bearbeitungsgebühr",
    "Versicherungsbetrag", "Umsatzsteuer", "Option 1 Name", "Option 1 Wert", "Option 2 Name",
    "Option 2 Wert", "Zugehöriger Transaktionscode", "Rechnungsnummer", "Zollnummer", "Anzahl",
    "Empfangsnummer", "Guthaben", "Adresszeile 1", "Adresszusatz", "Ort", "Bundesland", "PLZ", "Land",
    "Telefon", "Betreff", "Hinweis", "Ländervorwahl", "Auswirkung auf Guthaben",
]


def paypal_csv(*rows: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(PAYPAL_HEADER)
    for r in rows:
        w.writerow([r.get(h, "") for h in PAYPAL_HEADER])
    return buf.getvalue().encode("utf-8-sig")


def pp(date, name, typ, cur, net, code, effect, status="Abgeschlossen", related="", item=""):
    return {"Datum": date, "Uhrzeit": "10:00:00", "Zeitzone": "CET", "Name": name, "Typ": typ,
            "Status": status, "Währung": cur, "Brutto": net, "Gebühr": "0,00", "Netto": net,
            "Transaktionscode": code, "Zugehöriger Transaktionscode": related,
            "Artikelbezeichnung": item, "Auswirkung auf Guthaben": effect}


PAYPAL = paypal_csv(
    # EUR purchase funded from the bank account
    pp("03.05.2026", "Beispiel Shop", "Handyzahlung", "EUR", "-50,00", "P1", "Soll", item="Kabel"),
    pp("03.05.2026", "", "Bankgutschrift auf PayPal-Konto", "EUR", "50,00", "P2", "Haben", related="P1"),
    # authorization: no money moves
    pp("04.05.2026", "Beispiel Shop", "Allgemeine Autorisierung", "EUR", "-20,00", "P3", "Memo"),
    # pending
    pp("05.05.2026", "Anderer Shop", "Handyzahlung", "EUR", "-7,00", "P4", "Soll", status="Ausstehend"),
    # USD purchase: payment and conversion in USD cancel out, the EUR conversion is the cost
    pp("06.05.2026", "US Store", "Allgemeine Zahlung", "USD", "-10,00", "P5", "Soll", item="E-Book"),
    pp("06.05.2026", "", "Allgemeine Währungsumrechnung", "USD", "10,00", "P6", "Haben", related="P5"),
    pp("06.05.2026", "", "Allgemeine Währungsumrechnung", "EUR", "-9,20", "P7", "Soll", related="P5"),
    # two identical payments on one day are two transactions
    pp("07.05.2026", "Kiosk", "Handyzahlung", "EUR", "-3,50", "P8", "Soll"),
    pp("07.05.2026", "Kiosk", "Handyzahlung", "EUR", "-3,50", "P9", "Soll"),
)

CARD_HEADER = ("Umsatz getätigt von;Belegdatum;Buchungsdatum;Originalbetrag;Originalwährung;Umrechnungskurs;"
               "Buchungsbetrag;Buchungswährung;Transaktionsbeschreibung;Transaktionsbeschreibung Zusatz;"
               "Buchungsreferenz;Gebührenschlüssel;Länderkennzeichen;BAR-Entgelt+Buchungsreferenz;"
               "AEE+Buchungsreferenz;Abrechnungskennzeichen")
CARD_CSV = (CARD_HEADER + "\n"
            "Hauptkarte;01.06.26;02.06.26;0,00;;0,00;-42,10;EUR;TANKSTELLE MUSTER;MUSTERSTADT;11;1000;;;;\n"
            "Hauptkarte;03.06.26;04.06.26;-12,00;USD;1,10;-10,91;EUR;APP STORE;;12;1000;US;;;\n"
            "Hauptkarte;20.06.26;20.06.26;0,00;;0,00;300,00;EUR;AUSGLEICH;;13;;;;;\n").encode("cp1252")

SPARKASSE_HEADER = ('"Auftragskonto";"Buchungstag";"Valutadatum";"Buchungstext";"Verwendungszweck";'
                    '"Beguenstigter/Zahlungspflichtiger";"Kontonummer/IBAN";"BIC (SWIFT-Code)";'
                    '"Betrag";"Waehrung";"Info"\n')


def sparkasse_csv(*rows: str) -> bytes:
    return (SPARKASSE_HEADER + "".join(r + "\n" for r in rows)).encode("cp1252")


def test_paypal_keeps_completed_eur_movements():
    txs, fmt = importer.parse_statement("Download.CSV", PAYPAL)
    assert fmt == "PayPal"
    got = [(t["date"], t["amount"], t["counterparty"], t["description"], t.get("category_key")) for t in txs]
    assert got == [
        ("2026-05-03", -50.0, "Beispiel Shop", "Handyzahlung · Kabel", None),
        ("2026-05-03", 50.0, "", "Bankgutschrift auf PayPal-Konto", "transfer"),
        ("2026-05-06", -9.2, "US Store", "Allgemeine Währungsumrechnung · E-Book", None),
        ("2026-05-07", -3.5, "Kiosk", "Handyzahlung", None),
        ("2026-05-07", -3.5, "Kiosk", "Handyzahlung", None),
    ]


def test_card_csv():
    txs, fmt = importer.parse_statement("umsatz-card.CSV", CARD_CSV)
    assert fmt == "Credit card (Umsatz getätigt von)"
    assert [(t["date"], t["amount"], t["currency"], t["description"]) for t in txs] == [
        ("2026-06-02", -42.1, "EUR", "TANKSTELLE MUSTER"),
        ("2026-06-04", -10.91, "EUR", "APP STORE"),
        ("2026-06-20", 300.0, "EUR", "AUSGLEICH"),
    ]


def test_sparkasse_leaves_out_pending_rows():
    data = sparkasse_csv(
        '"DE00";"05.05.26";"05.05.26";"KARTENZAHLUNG";"Einkauf";"Markt";"DE01";"X";"-20,00";"EUR";"Umsatz gebucht"',
        '"DE00";"06.05.26";"06.05.26";"KARTENZAHLUNG";"Einkauf";"Markt";"DE01";"X";"-5,00";"EUR";"Umsatz vorgemerkt"',
    )
    txs, fmt = importer.parse_statement("umsatz.CSV", data)
    assert fmt == "Sparkasse" and [t["amount"] for t in txs] == [-20.0]
    report = importer.inspect_file("umsatz.CSV", data)
    assert report["status"] == "ok (1 rows left out by the Sparkasse rules)"


def test_inspect_recognizes_paypal():
    r = importer.inspect_file("Download.CSV", PAYPAL)
    assert r["bank"] == "PayPal" and r["parsed"] == 5 and r["missing"] == []
    assert r["status"] == "ok (4 rows left out by the PayPal rules)"
    assert "Beispiel" not in importer.format_report(r)


def add_account(client, name):
    client.post("/api/accounts", json={"name": name})
    return next(a["id"] for a in client.get("/api/accounts").json() if a["name"] == name)


def expenses(client):
    return client.get("/api/overview", params={"date_from": "2026-05-01", "date_to": "2026-05-31"}).json()["expense"]


def test_paypal_payment_from_bank_counts_once_in_either_import_order(client):
    bank, paypal = add_account(client, "Giro"), add_account(client, "PayPal")
    bank_csv = sparkasse_csv(
        '"DE00";"04.05.26";"04.05.26";"FOLGELASTSCHRIFT";"1234 PP.5678.PP . Beispiel Shop";'
        '"PayPal Europe S.a.r.l. et Cie S.C.A";"LU00";"X";"-50,00";"EUR";"Umsatz gebucht"')

    r = upload(client, bank, bank_csv).json()
    assert r["format"] == "Sparkasse" and r["linked_transfers"] == 0
    r = upload(client, paypal, PAYPAL, "Download.CSV").json()
    assert r["format"] == "PayPal" and r["inserted"] == 5 and r["linked_transfers"] == 1

    txs = client.get("/api/transactions", params={"account_id": bank}).json()
    assert txs[0]["category_key"] == "transfer"
    # 50 (shop) + 9.20 (USD book) + 2 × 3.50 (kiosk); the bank debit and the funding are transfers
    assert expenses(client) == -66.2


def test_bank_imported_after_paypal_is_linked_too(client):
    bank, paypal = add_account(client, "Giro"), add_account(client, "PayPal")
    upload(client, paypal, PAYPAL, "Download.CSV")
    r = upload(client, bank, sparkasse_csv(
        '"DE00";"05.05.26";"05.05.26";"FOLGELASTSCHRIFT";"PP.5678.PP";"PayPal Europe";"LU00";"X";'
        '"-50,00";"EUR";"Umsatz gebucht"')).json()
    assert r["linked_transfers"] == 1
    assert expenses(client) == -66.2


def test_unrelated_bank_debit_is_not_linked(client):
    bank, paypal = add_account(client, "Giro"), add_account(client, "PayPal")
    upload(client, paypal, PAYPAL, "Download.CSV")
    r = upload(client, bank, sparkasse_csv(  # same amount, but not PayPal and 20 days later
        '"DE00";"23.05.26";"23.05.26";"LASTSCHRIFT";"Strom";"Stadtwerke";"DE02";"X";"-50,00";"EUR";"Umsatz gebucht"',
        '"DE00";"25.05.26";"25.05.26";"LASTSCHRIFT";"PP.1";"PayPal Europe";"LU00";"X";"-50,00";"EUR";"Umsatz gebucht"',
    )).json()
    assert r["linked_transfers"] == 0
    assert all(t["category_key"] != "transfer" for t in client.get(
        "/api/transactions", params={"account_id": bank}).json())


def test_reimport_of_paypal_skips_known_payments(client):
    paypal = add_account(client, "PayPal")
    upload(client, paypal, PAYPAL, "Download.CSV")
    # the next download covers the same days and one more payment
    newer = PAYPAL + paypal_csv(pp("08.05.2026", "Kiosk", "Handyzahlung", "EUR", "-2,00", "P10", "Soll")
                                ).split(b"\r\n", 1)[1]
    r = upload(client, paypal, newer, "Download (1).CSV").json()
    assert r["inserted"] == 1 and r["skipped_duplicates"] == 5 and r["possible_duplicates"] == 0


def test_same_movement_in_another_format_is_flagged(client, account_id):
    upload(client, account_id, sparkasse_csv(
        '"DE00";"05.05.26";"05.05.26";"LASTSCHRIFT";"Miete Mai";"Vermieter";"DE03";"X";"-800,00";"EUR";"Umsatz gebucht"'),
        "camt.CSV")
    r = upload(client, account_id, sparkasse_csv(  # MT940-style text of the same movement
        '"DE00";"05.05.26";"05.05.26";"LASTSCHRIFT";"MIETE MAI EREF+123";"Vermieter";"DE03";"X";"-800,00";"EUR";"Umsatz gebucht"'),
        "mt940.CSV").json()
    assert r["inserted"] == 1 and r["possible_duplicates"] == 1


# a payment from the bank while the bank debit is still pending, as PayPal exports it
PENDING_FUNDING = paypal_csv(
    pp("10.05.2026", "Hofladen Muster", "Allgemeine Autorisierung", "EUR", "-42,00", "Q1", "Memo",
       status="Ausstehend"),
    pp("10.05.2026", "Hofladen Muster", "Website-Zahlung", "EUR", "-42,00", "Q2", "Soll"),
    pp("10.05.2026", "Hofladen Muster", "Bankgutschrift auf PayPal-Konto", "EUR", "42,00", "Q3", "Haben",
       status="Ausstehend"),
    # an authorization that is "completed" still moves no money, whatever the effect column says
    pp("10.05.2026", "Hofladen Muster", "Allgemeine Autorisierung", "EUR", "-42,00", "Q4", ""),
)
BANK_DEBIT = sparkasse_csv(
    '"DE00";"12.05.26";"12.05.26";"FOLGELASTSCHRIFT";"PP.9999.PP . Hofladen Muster";"PayPal Europe";'
    '"LU00";"X";"-42,00";"EUR";"Umsatz gebucht"')


def test_pending_funding_from_the_bank_counts_as_a_transfer():
    txs, _ = importer.parse_statement("Download.CSV", PENDING_FUNDING)
    assert [(t["amount"], t["description"], t.get("category_key")) for t in txs] == [
        (-42.0, "Website-Zahlung", None),
        (42.0, "Bankgutschrift auf PayPal-Konto", "transfer"),
    ]


def test_payment_with_pending_funding_is_one_expense(client):
    bank, paypal = add_account(client, "Giro"), add_account(client, "PayPal")
    upload(client, paypal, PENDING_FUNDING, "Download.CSV")
    assert upload(client, bank, BANK_DEBIT).json()["linked_transfers"] == 1
    assert expenses(client) == -42.0
    balances = {a["name"]: a["balance"] for a in client.get("/api/accounts").json()}
    assert (balances["PayPal"], balances["Giro"]) == (0, -42.0)

    # the bank debit cleared: the next export has the funding as completed, nothing changes
    cleared = paypal_csv(pp("10.05.2026", "Hofladen Muster", "Bankgutschrift auf PayPal-Konto", "EUR",
                            "42,00", "Q3", "Haben"))
    r = upload(client, paypal, cleared, "Download (1).CSV").json()
    assert (r["inserted"], r["skipped_duplicates"], r["voided"]) == (0, 1, 0)


def test_failed_funding_voids_the_pending_one(client):
    paypal = add_account(client, "PayPal")
    upload(client, paypal, PENDING_FUNDING, "Download.CSV")
    failed = paypal_csv(pp("10.05.2026", "Hofladen Muster", "Bankgutschrift auf PayPal-Konto", "EUR",
                           "42,00", "Q3", "Haben", status="Storniert"))
    r = upload(client, paypal, failed, "Download (1).CSV").json()
    assert (r["inserted"], r["voided"]) == (0, 1)
    txs = client.get("/api/transactions", params={"account_id": paypal}).json()
    assert [t["description"] for t in txs] == ["Website-Zahlung"]   # PayPal is owed the money now
    assert next(a for a in client.get("/api/accounts").json() if a["name"] == "PayPal")["balance"] == -42.0


# PayPal "Guthaben-relevante Zahlungen": the type is in Beschreibung, no status or effect column
PAYPAL_BALANCE_HEADER = [
    "Datum", "Uhrzeit", "Zeitzone", "Beschreibung", "Währung", "Brutto", "Entgelt", "Netto", "Guthaben",
    "Transaktionscode", "Absender E-Mail-Adresse", "Name", "Name der Bank", "Bankkonto",
    "Versand- und Bearbeitungsgebühr", "Umsatzsteuer", "Rechnungsnummer", "Zugehöriger Transaktionscode",
]


def paypal_balance_csv(*rows: tuple) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(PAYPAL_BALANCE_HEADER)
    for date, name, typ, cur, net, code, related in rows:
        r = {"Datum": date, "Uhrzeit": "10:00:00", "Zeitzone": "CET", "Beschreibung": typ, "Währung": cur,
             "Brutto": net, "Entgelt": "0,00", "Netto": net, "Guthaben": "0,00", "Transaktionscode": code,
             "Name": name, "Zugehöriger Transaktionscode": related}
        w.writerow([r.get(h, "") for h in PAYPAL_BALANCE_HEADER])
    return buf.getvalue().encode("utf-8-sig")


# the movements of PAYPAL as the balance report lists them, plus one more payment
PAYPAL_BALANCE = paypal_balance_csv(
    ("03.05.2026", "Beispiel Shop", "Handyzahlung", "EUR", "-50,00", "P1", ""),
    ("03.05.2026", "", "Bankgutschrift auf PayPal-Konto", "EUR", "50,00", "P2", "P1"),
    ("06.05.2026", "US Store", "Allgemeine Zahlung", "USD", "-10,00", "P5", ""),
    ("06.05.2026", "", "Allgemeine Währungsumrechnung", "USD", "10,00", "P6", "P5"),
    ("06.05.2026", "", "Allgemeine Währungsumrechnung", "EUR", "-9,20", "P7", "P5"),
    ("09.05.2026", "Kiosk", "Handyzahlung", "EUR", "-1,50", "P11", ""),
)


def test_paypal_balance_report():
    txs, fmt = importer.parse_statement("PayPal-20260501-20260531.CSV", PAYPAL_BALANCE)
    assert fmt == "PayPal (balance report)"
    assert [(t["date"], t["amount"], t["counterparty"], t["description"], t.get("category_key"), t["ref"])
            for t in txs] == [
        ("2026-05-03", -50.0, "Beispiel Shop", "Handyzahlung", None, "P1"),
        ("2026-05-03", 50.0, "", "Bankgutschrift auf PayPal-Konto", "transfer", "P2"),
        ("2026-05-06", -9.2, "US Store", "Allgemeine Währungsumrechnung", None, "P7"),
        ("2026-05-09", -1.5, "Kiosk", "Handyzahlung", None, "P11"),
    ]
    d = importer.describe_statement("PayPal-20260501-20260531.CSV", PAYPAL_BALANCE)
    assert (d["gemma"], d["key"], d["count"]) == (False, "paypal", 4)   # same account as the activity export
    r = importer.inspect_file("PayPal-20260501-20260531.CSV", PAYPAL_BALANCE)
    assert r["bank"] == "PayPal (balance report)" and r["missing"] == []
    assert r["status"] == "ok (2 rows left out by the PayPal (balance report) rules)"


def test_both_paypal_exports_count_each_payment_once(client):
    paypal = add_account(client, "PayPal")
    upload(client, paypal, PAYPAL, "Download.CSV")
    # same transaction codes, but other descriptions than the activity export gave them
    r = upload(client, paypal, PAYPAL_BALANCE, "PayPal-20260501-20260531.CSV").json()
    assert (r["inserted"], r["skipped_duplicates"], r["possible_duplicates"]) == (1, 3, 0)
    assert expenses(client) == -67.7


# Barclays credit card XLSX: preamble rows, two "Buchungsdatum" columns, amounts with "€"
BARCLAYS_HEADER = ["Referenznummer", "Buchungsdatum", "Buchungsdatum", "Betrag", "Beschreibung", "Typ", "Status",
                   "Kartennummer", "Originalbetrag", "Mögliche Zahlpläne", "Land", "Karteninhaber",
                   "Kartennetzwerk", "Kontaktlose Bezahlung", "Details"]


def barclays_xlsx(*rows: tuple) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Umsätze"])
    ws.append(["Kontoinhaber", "Erika Muster"])
    ws.append([])
    ws.append(BARCLAYS_HEADER)
    for ref, date, amount, text, typ, status in rows:
        ws.append([ref, date, date, amount, text, typ, status, "", amount, "Nein", "", "", "-", "Nein", text])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


BARCLAYS = barclays_xlsx(
    ("10000000000000000000001", "02.10.2026", "-12,50 €", "Buchladen Muster", "Belastung", "abgerechnet"),
    ("-", "08.10.2026", "+45,79 €", "Per Lastschrift dankend erhalten", "Gutschrift", "abgerechnet"),
    ("10000000000000000000002", "09.10.2026", "+8,00 €", "Buchladen Muster", "Gutschrift", "abgerechnet"),
    ("-", "11.10.2026", "-2,00 €", "Belastung Gebühr Kreditkartennutzung", "Belastung", "abgerechnet"),
    ("10000000000000000000003", "30.10.2026", "-20,00 €", "Tankstelle Muster", "Belastung", "vorgemerkt"),
)


def test_barclays_card():
    txs, fmt = importer.parse_statement("Barclays-Umsätze.xlsx", BARCLAYS)
    assert fmt == "Barclays credit card"
    assert [(t["date"], t["amount"], t["description"], t.get("category_key"), t.get("ref")) for t in txs] == [
        ("2026-10-02", -12.5, "Buchladen Muster", None, "10000000000000000000001"),
        ("2026-10-08", 45.79, "Per Lastschrift dankend erhalten", "transfer", None),   # repayment
        ("2026-10-09", 8.0, "Buchladen Muster", None, "10000000000000000000002"),      # refund
        ("2026-10-11", -2.0, "Belastung Gebühr Kreditkartennutzung", None, None),
    ]
    d = importer.describe_statement("Barclays-Umsätze.xlsx", BARCLAYS)
    assert (d["key"], d["proposal"], d["count"]) == ("barclays", {"name": "Barclays Visa", "type": "credit_card"}, 4)
    r = importer.inspect_file("Barclays-Umsätze.xlsx", BARCLAYS)
    assert r["bank"] == "Barclays credit card"
    assert r["status"] == "ok (1 rows left out by the Barclays credit card rules)"
    assert "Erika" not in importer.format_report(r)


def test_barclays_repayment_is_linked_to_the_bank_debit(client):
    bank, card = add_account(client, "Giro"), add_account(client, "Barclays Visa")
    upload(client, card, BARCLAYS, "Barclays-Umsätze.xlsx")
    r = upload(client, bank, sparkasse_csv(
        '"DE00";"07.10.26";"07.10.26";"LASTSCHRIFT";"Kreditkarte 10/2026";"Barclays Bank Ireland PLC";"DE04";"X";'
        '"-45,79";"EUR";"Umsatz gebucht"')).json()
    assert r["linked_transfers"] == 1
    overview = client.get("/api/overview", params={"date_from": "2026-10-01", "date_to": "2026-10-31"}).json()
    assert (overview["income"], overview["expense"]) == (8.0, -14.5)


def test_invisible_characters_in_column_names_do_not_hide_the_layout():
    header = ["Referenz­nummer", "Buchungs​datum", "Betrag ", "Beschreibung", "Typ", "Status",
              "Karten-\nnummer".replace("-\n", ""), "Original betrag".replace(" ", "")]
    assert importer._detect_profile(header) == "barclays"


def test_inspect_names_the_layout_a_header_almost_matches():
    data = barclays_xlsx(("1", "02.10.2026", "-1,00 €", "Muster", "Belastung", "abgerechnet"))
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data))
    wb.active.cell(row=4, column=8).value = "Karten⁣nummer"   # a character _norm does not know
    buf = io.BytesIO()
    wb.save(buf)
    r = importer.inspect_file("Barclays.xlsx", buf.getvalue())
    assert r["bank"] is None and r["near"] == "Barclays credit card (missing: kartennummer)"
    assert r["odd_names"] == [r"'Karten\u2063nummer'"]   # escaped, so it shows in the report
    assert "near:     Barclays credit card" in importer.format_report(r)


def test_card_funded_paypal_payment_and_its_refund_count_once(client):
    card, paypal = add_account(client, "Card"), add_account(client, "PayPal")
    upload(client, card, (CARD_HEADER + "\n"
                          "Hauptkarte;05.10.26;05.10.26;0,00;;0,00;-60,00;EUR;PAYPAL *SPORTSHOP MUSTER;;21;;;;;\n"
                          "Hauptkarte;09.10.26;09.10.26;0,00;;0,00;60,00;EUR;PAYPAL *SPORTSHOP MUSTER;;22;;;;;\n"
                          ).encode("cp1252"), "umsatz-card.CSV")
    statement = paypal_balance_csv(
        ("05.10.2026", "Sportshop Muster", "Allgemeine Zahlung", "EUR", "-60,00", "P21", ""),
        ("05.10.2026", "", "Allgemeine Abbuchung von Kreditkarte", "EUR", "60,00", "P22", "P21"),
        ("08.10.2026", "Sportshop Muster", "Rückzahlung", "EUR", "60,00", "P23", "P21"),
        ("08.10.2026", "", "Allgemeine Abbuchung von Kreditkarte", "EUR", "-60,00", "P24", "P23"),
    )
    r = upload(client, paypal, statement, "PayPal-20261001-20261031.CSV").json()
    assert (r["inserted"], r["linked_transfers"]) == (4, 2)
    assert client.get("/api/imports").json()[0]["duplicates"] == 0
    overview = client.get("/api/overview", params={"date_from": "2026-10-01", "date_to": "2026-10-31"}).json()
    assert (overview["income"], overview["expense"]) == (60.0, -60.0)   # the refund, the payment


def test_paypal_balance_report_states_the_balance(client):
    paypal = add_account(client, "PayPal")

    def statement(*rows):   # (date, type, amount, balance after it), all in one second
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_ALL)
        w.writerow(PAYPAL_BALANCE_HEADER)
        for day, typ, net, after in rows:
            r = {"Datum": day, "Uhrzeit": "10:00:00", "Beschreibung": typ, "Währung": "EUR", "Brutto": net,
                 "Netto": net, "Guthaben": after, "Transaktionscode": f"{day} {typ}", "Name": "Muster"}
            w.writerow([r.get(h, "") for h in PAYPAL_BALANCE_HEADER])
        return buf.getvalue().encode("utf-8-sig")

    # PayPal lists a payment and its card funding of the same second in either order; the last
    # day may go on after the download, so the day before it is the one with a known end
    first = statement(("01.10.2026", "Allgemeine Zahlung", "-30,00", "-10,00"),
                      ("01.10.2026", "Allgemeine Abbuchung von Kreditkarte", "30,00", "20,00"),
                      ("02.10.2026", "Allgemeine Abbuchung von Kreditkarte", "5,00", "25,00"),
                      ("02.10.2026", "Allgemeine Zahlung", "-5,00", "20,00"),
                      ("03.10.2026", "Rückzahlung", "4,00", "24,00"))
    r = upload(client, paypal, first, "PayPal-1.CSV").json()
    assert (r["balance"]["date"], r["balance"]["amount"], r["balance"]["known"], r["balance"]["days"]) == (
        "2026-10-02", 20.0, True, 0)
    acc = lambda: next(a for a in client.get("/api/accounts").json() if a["id"] == paypal)
    assert (acc()["balance"], acc()["balance_date"]) == (24.0, "2026-10-02")

    # the next download: checked against the known balance, nothing set again
    second = statement(("03.10.2026", "Rückzahlung", "4,00", "24,00"),
                       ("04.10.2026", "Allgemeine Zahlung", "-24,00", "0,00"),
                       ("05.10.2026", "Allgemeine Zahlung", "-1,00", "-1,00"))
    r = upload(client, paypal, second, "PayPal-2.CSV").json()
    assert (r["balance"]["date"], r["balance"]["difference"], r["balance"]["days"], r["balance"]["off"]) == (
        "2026-10-04", 0.0, 1, 0)
    assert acc()["balance_date"] == "2026-10-02"


def test_no_stated_balance_when_the_start_is_ambiguous():
    tx = lambda day, amount: {"date": day, "amount": amount}
    # +5 then -5 from 0, or -5 then +5 from 5: both fit, and the next day states nothing
    rows = [(tx("2026-10-01", 5.0), 5.0), (tx("2026-10-01", -5.0), 0.0), (tx("2026-10-02", 1.0), None)]
    importer._paypal_day_balance(rows)
    assert not any("day_balance" in t for t, _ in rows)


def test_sparkasse_excel_export_with_real_date_cells():
    import datetime
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Auftragskonto", "Buchungstag", "Valutadatum", "Buchungstext", "Verwendungszweck",
               "Beguenstigter/Zahlungspflichtiger", "Kontonummer/IBAN", "BIC (SWIFT-Code)", "Betrag",
               "Waehrung", "Info"])
    ws.append(["DE00999988887777666655", datetime.datetime(2026, 8, 28), datetime.datetime(2026, 8, 28),
               "LASTSCHRIFT", "Strom", "Stadtwerke Muster", "DE02", "X", -1234.5, "EUR", "Umsatz gebucht"])
    ws.append(["DE00999988887777666655", datetime.date(2026, 8, 29), datetime.date(2026, 8, 29),
               "KARTENZAHLUNG", "Karte", "Laden Muster", "DE03", "X", -0.1, "EUR", "Umsatz vorgemerkt"])
    buf = io.BytesIO()
    wb.save(buf)
    txs, fmt = importer.parse_statement("Giro-umsatz-camt52v8.xlsx", buf.getvalue())
    assert fmt == "Sparkasse"
    assert [(t["date"], t["amount"], t["counterparty"]) for t in txs] == [("2026-08-28", -1234.5, "Stadtwerke Muster")]
    d = importer.describe_statement("Giro-umsatz-camt52v8.xlsx", buf.getvalue())
    assert d["key"].startswith("sparkasse:") and d["count"] == 1


def test_inspect_counts_formulas_without_a_stored_value():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Datum", "Betrag", "Beschreibung"])
    ws.append(["01.05.2026", "=-10-2", "Laden"])   # openpyxl stores no result for a formula
    ws.append(["02.05.2026", -5, "Kiosk"])
    buf = io.BytesIO()
    wb.save(buf)
    r = importer.inspect_file("export.xlsx", buf.getvalue())
    assert r["formulas"] == 1
    assert "formulas: 1 cells without a stored value" in importer.format_report(r)


def test_inspect_sums_up_the_unread_rows_without_their_content():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Auftragskonto", "Buchungstag", "Valutadatum", "Buchungstext", "Verwendungszweck",
               "Beguenstigter/Zahlungspflichtiger", "Kontonummer/IBAN", "Betrag", "Waehrung", "Info"])
    acct = "DE00999988887777666655"
    ws.append([acct, "03.06.26", "03.06.26", "LASTSCHRIFT", "Miete Juni", "Vermieter Muster", "DE02", -700, "", ""])
    ws.append([acct, "03.06.26", "03.06.26", "LASTSCHRIFT", "Wohnung Teststr. 1", "", "", None, "", ""])
    ws.append([acct, "04.06.26", "04.06.26", "Geheimer Freitext", "Notiz Muster", "", "", None, "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    r = importer.inspect_file("Giro-umsatz.xlsx", buf.getvalue())
    assert r["unread"] == {"rows": 2, "no_date": 0, "no_amount": 2, "counterparty": 0, "like_above": 1,
                           "like_below": 0, "text_on_a_read_row": 0, "types": {"LASTSCHRIFT": 1, "<other>": 1}}
    out = importer.format_report(r)
    assert "unread:   2 rows: no amount 2" in out and "types:    LASTSCHRIFT 1, <other> 1" in out
    for secret in ("Geheimer", "Teststr", "Miete", "Muster", "700"):
        assert secret not in out


def _sparkasse_xlsx_with_a_row_without_amount() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Auftragskonto", "Buchungstag", "Valutadatum", "Buchungstext", "Verwendungszweck",
               "Beguenstigter/Zahlungspflichtiger", "Kontonummer/IBAN", "Betrag", "Waehrung", "Info"])
    acct = "DE00999988887777666655"
    ws.append([acct, "03.06.26", "03.06.26", "LASTSCHRIFT", "Miete", "Vermieter Muster", "DE02", -700, "", ""])
    ws.append([acct, "05.06.26", "05.06.26", "FOLGELASTSCHRIFT", "Beitrag", "", "", None, "", ""])
    ws.append(["Summe", "", "", "", "", "", "", None, "", ""])   # no date: a footer, not a movement
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_describe_counts_dated_rows_without_an_amount():
    d = importer.describe_statement("Giro-umsatz.xlsx", _sparkasse_xlsx_with_a_row_without_amount())
    assert d["count"] == 1 and d["no_amount"] == 1


def test_import_result_reports_rows_left_out_without_an_amount(client, account_id):
    r = client.post("/api/import", data={"account_id": account_id},
                    files={"file": ("Giro-umsatz.xlsx", _sparkasse_xlsx_with_a_row_without_amount())})
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1 and r.json()["no_amount"] == 1
