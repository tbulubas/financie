# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""`financie inspect` reports the structure of a statement but none of its content. Invented data."""

import pytest

from financie import cli, importer
from test_importer import CAMT_CSV

# a layout the aliases do not know yet, with the account holder in a preamble line
UNKNOWN_CSV = (
    "Kontoinhaber;Max Mustermann;Musterstraße 1\n"
    "\n"
    "Datum;Uhrzeit;Name;Typ;Brutto;Netto\n"
    "02.05.2026;10:15:00;Beispiel Shop;Zahlung;-12,50;-12,50\n"
    "03.05.2026;11:00:00;Erika Muster;Gutschrift;40,00;40,00\n"
).encode("utf-8")

# a known header below a preamble: the importer must find it and parse
PREAMBLE_CSV = (
    "Umsatzanzeige;Max Mustermann\n"
    "Zeitraum;01.05.2026 - 31.05.2026\n"
    "Buchungsdatum;Beschreibung;Betrag\n"
    "04.05.2026;Online Shop;-19,99\n"
).encode("utf-8")

SECRETS = ["Mustermann", "Musterstraße", "Beispiel", "Erika", "Bäckerei", "Muster Bäcker", "12,50",
           "3.210,00", "DE00123", "DE00999", "Online Shop", "Gehalt"]


def assert_no_secrets(text: str):
    leaked = [s for s in SECRETS if s in text]
    assert not leaked, f"report reveals {leaked}:\n{text}"


@pytest.mark.parametrize("raw, shape", [
    ("02.05.2026", "<date:%d.%m.%Y>"), ("-1.234,56", "-9.999,99"), ("12,50 EUR", "99,99 EUR"),
    ("DE89 3704 0044 0532 0130 00", "<iban>"), ("max@example.com", "<email>"),
    ("Muster Bäcker GmbH", "<text>"), ("", ""),
])
def test_shape(raw, shape):
    assert importer._shape(raw) == shape


def test_known_format():
    r = importer.inspect_file("export.csv", CAMT_CSV)
    assert r["status"] == "ok" and r["parsed"] == 2 and r["header_row"] == 1
    assert r["mapping"]["date"] == "Buchungstag" and r["mapping"]["amount"] == "Betrag"
    assert r["date_range"] == "2026-04-30 .. 2026-05-02"
    text = importer.format_report(r)
    assert "<date:%d.%m.%Y>" in text and "-99,99" in text and "9.999,99" in text
    assert_no_secrets(text)


def test_unknown_format_shows_columns_but_not_the_preamble():
    r = importer.inspect_file("paypal.csv", UNKNOWN_CSV)
    assert r["status"].startswith("not recognized")
    assert r["columns"] == ["Datum", "Uhrzeit", "Name", "Typ", "Brutto", "Netto"]
    assert r["header_row"] == 3 and r["data_rows"] == 2
    text = importer.format_report(r)
    assert "<date:%d.%m.%Y> | <time> | <text> | <text> | -99,99 | -99,99" in text
    assert_no_secrets(text)


def test_header_below_preamble_is_found_and_parsed():
    r = importer.inspect_file("card.csv", PREAMBLE_CSV)
    assert r["status"] == "ok" and r["header_row"] == 3 and r["preamble_rows"] == 2
    assert_no_secrets(importer.format_report(r))
    assert importer.parse_file("card.csv", PREAMBLE_CSV) == [{
        "date": "2026-05-04", "amount": -19.99, "currency": "EUR",
        "description": "Online Shop", "counterparty": ""}]


def test_unsupported_file():
    assert importer.inspect_file("notes.txt", b"x")["status"] == "unsupported"


def test_cli_inspects_a_folder(tmp_path, capsys):
    (tmp_path / "a.csv").write_bytes(CAMT_CSV)
    (tmp_path / "b.csv").write_bytes(UNKNOWN_CSV)
    (tmp_path / "broken.xlsx").write_bytes(b"not a workbook")
    (tmp_path / "ignored.txt").write_text("x")
    cli.main(["inspect", str(tmp_path)])
    out = capsys.readouterr().out
    assert out.count("== ") == 3 and "ignored.txt" not in out
    assert "status:   ok" in out and "not recognized" in out and "error:" in out
    assert_no_secrets(out)
