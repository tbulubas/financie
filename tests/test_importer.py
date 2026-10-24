# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Deterministic statement parsing. All sample rows are invented."""

import io

import pytest

from financie import importer

# German bank CSV-CAMT V2 layout: ';', cp1252, dd.mm.yyyy, 1.234,56
CAMT_CSV = (
    '"Auftragskonto";"Buchungstag";"Valutadatum";"Buchungstext";"Verwendungszweck";'
    '"Beguenstigter/Zahlungspflichtiger";"Kontonummer/IBAN";"Betrag";"Waehrung";"Info"\n'
    '"DE00123";"02.05.2026";"02.05.2026";"LASTSCHRIFT";"Einkauf Bäckerei";'
    '"Muster Bäcker GmbH";"DE00999";"-12,50";"EUR";"Umsatz gebucht"\n'
    '"DE00123";"30.04.2026";"30.04.2026";"GUTSCHRIFT";"Gehalt April";'
    '"Beispiel AG";"DE00888";"3.210,00";"EUR";"Umsatz gebucht"\n'
).encode("cp1252")

# Slovak-style export with separate debit and credit columns
DEBIT_CREDIT_CSV = (
    "Dátum;Popis;Debet;Kredit;Mena\n"
    "05.03.2026;Potraviny Test;45,90;;EUR\n"
    "06.03.2026;Vratka;;10,00;EUR\n"
).encode("utf-8-sig")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1.234,56", 1234.56),
        ("-12,50", -12.5),
        ("1\xa0234,56", 1234.56),
        ("1 234,56 EUR", 1234.56),
        ("+3,00", 3.0),
        ("", None),
        ("-", None),
        (None, None),
    ],
)
def test_parse_amount(raw, expected):
    assert importer._parse_amount(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("02.05.2026", "2026-05-02"), ("2026-05-02", "2026-05-02"), ("02.05.26", "2026-05-02"), ("garbage", None)],
)
def test_parse_date(raw, expected):
    assert importer._parse_date(raw) == expected


def test_camt_csv():
    txs = importer.parse_file("export.csv", CAMT_CSV)
    assert txs == [
        {"date": "2026-05-02", "amount": -12.5, "currency": "EUR",
         "description": "Einkauf Bäckerei", "counterparty": "Muster Bäcker GmbH"},
        {"date": "2026-04-30", "amount": 3210.0, "currency": "EUR",
         "description": "Gehalt April", "counterparty": "Beispiel AG"},
    ]


def test_debit_credit_columns():
    txs = importer.parse_file("vypis.csv", DEBIT_CREDIT_CSV)
    assert [(t["date"], t["amount"]) for t in txs] == [("2026-03-05", -45.9), ("2026-03-06", 10.0)]


def test_unknown_csv_without_gemma_fails_with_header_in_message():
    data = b"foo;bar\n1;2\n3;4\n"
    with pytest.raises(ValueError, match="foo"):
        importer.parse_file("x.csv", data)


def test_xlsx_with_preamble_rows():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Kreditkartenabrechnung"])
    ws.append(["Zeitraum", "01.06.2026 - 30.06.2026"])
    ws.append([])
    ws.append(["Buchungsdatum", "Beschreibung", "Betrag"])
    ws.append(["03.06.2026", "Online Shop", "-19,99"])
    ws.append(["10.06.2026", "Gutschrift", "5,00"])
    buf = io.BytesIO()
    wb.save(buf)
    txs = importer.parse_file("card.xlsx", buf.getvalue())
    assert [(t["date"], t["amount"], t["description"]) for t in txs] == [
        ("2026-06-03", -19.99, "Online Shop"),
        ("2026-06-10", 5.0, "Gutschrift"),
    ]


def test_unsupported_extension():
    with pytest.raises(ValueError, match="Unsupported format"):
        importer.parse_file("notes.txt", b"hello")
