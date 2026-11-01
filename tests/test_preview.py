# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Several statements at once: what each file is and which account it goes to. Invented data."""

from test_formats import CARD_CSV, PAYPAL, paypal_csv, sparkasse_csv
from test_api import upload

GIRO = sparkasse_csv(
    '"DE89370400440532013000";"05.05.26";"05.05.26";"LASTSCHRIFT";"Miete";"Vermieter";"DE03";"X";'
    '"-800,00";"EUR";"Umsatz gebucht"')
CARD_NAME = "umsatz-1234________5678-20260601.CSV"


def preview(client, *files):
    r = client.post("/api/import/preview",
                    files=[("files", (name, data, "application/octet-stream")) for name, data in files])
    assert r.status_code == 200
    return {d["file"]: d for d in r.json()}


def test_preview_says_what_each_file_is(client):
    p = preview(client, ("giro.CSV", GIRO), ("Download.CSV", PAYPAL), (CARD_NAME, CARD_CSV),
                ("Download (2).CSV", paypal_csv()), ("statement.pdf", b"%PDF-1.4"),
                ("notes.txt", b"x"))
    giro = p["giro.CSV"]
    assert (giro["format"], giro["count"], giro["date_from"]) == ("Sparkasse", 1, "2026-05-05")
    assert giro["proposal"] == {"name": "Sparkasse …3000", "type": "checking"}
    assert giro["key"].startswith("sparkasse:") and "0532013000" not in giro["key"]  # IBAN is hashed

    assert p["Download.CSV"]["proposal"] == {"name": "PayPal", "type": "other"}
    assert p["Download.CSV"]["count"] == 5
    assert p[CARD_NAME]["key"] == "card:1234-5678"
    assert p[CARD_NAME]["proposal"] == {"name": "Card …5678", "type": "credit_card"}
    assert p["Download (2).CSV"]["count"] == 0          # an empty export
    assert p["statement.pdf"]["gemma"] is True    # not parsed in the preview
    assert p["notes.txt"]["error"].startswith("Unsupported")
    assert all(d["account_id"] is None for d in p.values())


def test_import_remembers_the_account_of_a_source(client):
    client.post("/api/accounts", json={"name": "Mein Giro"})
    giro_id = next(a["id"] for a in client.get("/api/accounts").json() if a["name"] == "Mein Giro")
    upload(client, giro_id, GIRO, "giro-2026-05.CSV")

    later = sparkasse_csv(  # next month's download of the same account
        '"DE89370400440532013000";"05.06.26";"05.06.26";"LASTSCHRIFT";"Miete";"Vermieter";"DE03";"X";'
        '"-800,00";"EUR";"Umsatz gebucht"')
    assert preview(client, ("giro-2026-06.CSV", later))["giro-2026-06.CSV"]["account_id"] == giro_id


def test_existing_account_with_the_proposed_name_is_suggested(client):
    client.post("/api/accounts", json={"name": "PayPal", "type": "other"})
    paypal_id = next(a["id"] for a in client.get("/api/accounts").json() if a["name"] == "PayPal")
    assert preview(client, ("Download.CSV", PAYPAL))["Download.CSV"]["account_id"] == paypal_id


def test_deleting_an_account_forgets_its_sources(client):
    client.post("/api/accounts", json={"name": "Tmp"})
    tmp = next(a["id"] for a in client.get("/api/accounts").json() if a["name"] == "Tmp")
    upload(client, tmp, PAYPAL, "Download.CSV")
    for t in client.get("/api/transactions", params={"account_id": tmp}).json():
        client.delete(f"/api/transactions/{t['id']}")
    assert client.delete(f"/api/accounts/{tmp}").status_code == 200
    assert preview(client, ("Download.CSV", PAYPAL))["Download.CSV"]["account_id"] is None
