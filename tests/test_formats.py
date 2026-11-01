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


def test_reimport_of_paypal_skips_everything(client):
    paypal = add_account(client, "PayPal")
    upload(client, paypal, PAYPAL, "Download.CSV")
    r = upload(client, paypal, PAYPAL, "Download (1).CSV").json()
    assert r["inserted"] == 0 and r["skipped_duplicates"] == 5 and r["possible_duplicates"] == 0


def test_same_movement_in_another_format_is_flagged(client, account_id):
    upload(client, account_id, sparkasse_csv(
        '"DE00";"05.05.26";"05.05.26";"LASTSCHRIFT";"Miete Mai";"Vermieter";"DE03";"X";"-800,00";"EUR";"Umsatz gebucht"'),
        "camt.CSV")
    r = upload(client, account_id, sparkasse_csv(  # MT940-style text of the same movement
        '"DE00";"05.05.26";"05.05.26";"LASTSCHRIFT";"MIETE MAI EREF+123";"Vermieter";"DE03";"X";"-800,00";"EUR";"Umsatz gebucht"'),
        "mt940.CSV").json()
    assert r["inserted"] == 1 and r["possible_duplicates"] == 1
