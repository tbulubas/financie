# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Money moved between the user's own accounts is a transfer on both sides. Invented data."""

from test_api import csv_bytes, upload
from test_formats import add_account


def movements(client):
    return {(t["account"], t["description"]): t for t in client.get("/api/transactions", params={"limit": 500}).json()}


def kind(client, t):
    cats = {c["id"]: c for c in client.get("/api/categories").json()}
    return cats[t["category_id"]]["key"] if t["category_id"] else None


def cat(client, key):
    return next(c["id"] for c in client.get("/api/categories").json() if c["key"] == key)


def test_card_repayment_and_savings_move_are_paired(client, account_id):
    card, savings = add_account(client, "Visa"), add_account(client, "Savings")
    upload(client, account_id, csv_bytes(
        "01.02.2026;3000,00;Gehalt;Arbeitgeber Muster",
        "05.02.2026;-400,00;Sparen;Eigenes Konto",
        "20.02.2026;-480,00;Kreditkarte;Kartenbank Muster",
        "21.02.2026;-35,00;Einkauf;Laden Muster"), "giro.csv")
    upload(client, savings, csv_bytes("05.02.2026;400,00;Sparen;Eigenes Konto"), "savings.csv")
    r = upload(client, card, csv_bytes(
        "03.02.2026;-120,00;Kauf;Shop Muster",
        "22.02.2026;480,00;Zahlung;Ausgleich",                 # two days after the debit
        "24.02.2026;25,00;Gutschrift;Shop Muster"), "card.csv").json()   # a refund, from no own account
    assert r["linked_transfers"] == 2
    m = movements(client)
    for key in (("Checking account", "Kreditkarte"), ("Visa", "Zahlung"),
                ("Checking account", "Sparen"), ("Savings", "Sparen")):
        assert kind(client, m[key]) == "transfer" and m[key]["category_source"] == "rule"
    assert m[("Visa", "Gutschrift")]["category_id"] is None              # the refund stays income
    assert m[("Checking account", "Einkauf")]["category_id"] is None
    ov = client.get("/api/overview", params={"date_from": "2026-02-01", "date_to": "2026-02-28"}).json()
    assert (ov["income"], ov["expense"], ov["transfers"]) == (3025.0, -155.0, 0)


def test_the_users_own_choice_is_never_overruled(client, account_id):
    card = add_account(client, "Visa")
    upload(client, account_id, csv_bytes("10.03.2026;-50,00;Abbuchung;Kartenbank Muster",
                                         "11.03.2026;-70,00;Abbuchung 2;Kartenbank Muster"), "giro.csv")
    m = movements(client)
    client.patch(f"/api/transactions/{m[('Checking account', 'Abbuchung')]['id']}", json={"category_id": cat(client, "fees")})
    client.patch(f"/api/transactions/{m[('Checking account', 'Abbuchung 2')]['id']}",
                 json={"category_id": cat(client, "transfer")})
    upload(client, card, csv_bytes("11.03.2026;50,00;Zahlung;Ausgleich",
                                   "12.03.2026;70,00;Zahlung 2;Ausgleich"), "card.csv")
    m = movements(client)
    assert kind(client, m[("Checking account", "Abbuchung")]) == "fees"   # the user said: a fee
    assert m[("Visa", "Zahlung")]["category_id"] is None                  # so it has no partner
    assert kind(client, m[("Visa", "Zahlung 2")]) == "transfer"           # the user's transfer has one


def test_each_movement_pairs_once_and_only_within_a_week(client, account_id):
    card = add_account(client, "Visa")
    upload(client, account_id, csv_bytes("01.04.2026;-100,00;Eins;Kartenbank Muster",
                                         "01.04.2026;-100,00;Zwei;Kartenbank Muster",
                                         "01.04.2026;-30,00;Drei;Kartenbank Muster"), "giro.csv")
    upload(client, card, csv_bytes("02.04.2026;100,00;Zahlung;Ausgleich",
                                   "20.04.2026;30,00;Spaet;Ausgleich"), "card.csv")
    m = movements(client)
    giro = [kind(client, m[("Checking account", d)]) for d in ("Eins", "Zwei")]
    assert sorted(giro, key=str) == [None, "transfer"]
    assert m[("Checking account", "Drei")]["category_id"] is None         # 19 days apart
    assert m[("Visa", "Spaet")]["category_id"] is None
