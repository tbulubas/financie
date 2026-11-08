# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Overview of chosen accounts: the main account, the satellites, or all. Invented data."""

from test_api import csv_bytes, upload
from test_formats import PAYPAL, add_account, sparkasse_csv

# salary and rent on the joint account, plus the debit that funds a PayPal payment
GIRO = sparkasse_csv(
    '"DE00";"01.05.26";"01.05.26";"GUTSCHRIFT";"Gehalt";"Arbeitgeber";"DE01";"X";"3000,00";"EUR";"Umsatz gebucht"',
    '"DE00";"02.05.26";"02.05.26";"LASTSCHRIFT";"Miete";"Vermieter";"DE02";"X";"-900,00";"EUR";"Umsatz gebucht"',
    '"DE00";"04.05.26";"04.05.26";"FOLGELASTSCHRIFT";"PP.5678.PP";"PayPal Europe";"LU00";"X";'
    '"-50,00";"EUR";"Umsatz gebucht"')


def overview(client, accounts=None):
    params = {"date_from": "2026-05-01", "date_to": "2026-05-31"}
    if accounts is not None:
        params["accounts"] = ",".join(map(str, accounts))
    r = client.get("/api/overview", params=params).json()
    return r["income"], r["expense"], r["transfers"]


def test_main_satellite_and_all(client):
    giro, paypal = add_account(client, "Joint"), add_account(client, "PayPal")
    upload(client, giro, GIRO, "giro.CSV")
    upload(client, paypal, PAYPAL, "Download.CSV")   # links the bank debit as a transfer

    # the joint account alone: the PayPal funding leaves as a transfer, not an expense
    assert overview(client, [giro]) == (3000, -900, -50)
    # PayPal alone: the purchases, funded by a transfer from the bank
    assert overview(client, [paypal]) == (0, -66.2, 50)
    # together the transfers cancel out
    assert overview(client, [giro, paypal]) == (3000, -966.2, 0)
    assert overview(client) == (3000, -966.2, 0)
    assert overview(client, []) == (0, 0, 0)

    by_cat = client.get("/api/overview", params={"date_from": "2026-05-01", "date_to": "2026-05-31",
                                                 "accounts": str(paypal)}).json()["by_category"]
    assert round(sum(c["spent"] for c in by_cat), 2) == 66.2


def test_main_account_flag(client):
    giro = add_account(client, "Joint")
    assert all(a["is_main"] == 0 for a in client.get("/api/accounts").json())
    assert client.patch(f"/api/accounts/{giro}", json={"is_main": True}).status_code == 200
    flags = {a["name"]: a["is_main"] for a in client.get("/api/accounts").json()}
    assert flags == {"Checking account": 0, "Joint": 1}
    client.patch(f"/api/accounts/{giro}", json={"is_main": False})
    assert all(a["is_main"] == 0 for a in client.get("/api/accounts").json())


def test_known_balance_does_not_need_the_whole_history(client, account_id):
    upload(client, account_id, csv_bytes("01.05.2026;-10,00;Nákup;Obchod A", "02.05.2026;-20,00;Nákup;Obchod B",
                                         "05.05.2026;-5,00;Nákup;Obchod C"), "may.csv")
    acc = lambda: next(a for a in client.get("/api/accounts").json() if a["id"] == account_id)
    assert (acc()["balance"], acc()["first_date"], acc()["last_date"]) == (-35.0, "2026-05-01", "2026-05-05")

    # the banking app showed 1000 at the end of 2 May: only later movements change it
    r = client.patch(f"/api/accounts/{account_id}", json={"balance_amount": 1000, "balance_date": "2026-05-02"})
    assert r.status_code == 200
    assert (acc()["balance"], acc()["balance_date"], acc()["is_main"]) == (995.0, "2026-05-02", 0)
    # older history imported later does not move it
    upload(client, account_id, csv_bytes("20.04.2026;-99,00;Nákup;Obchod D"), "april.csv")
    assert (acc()["balance"], acc()["first_date"]) == (995.0, "2026-04-20")

    assert client.patch(f"/api/accounts/{account_id}", json={"is_main": True}).status_code == 200
    assert (acc()["balance"], acc()["is_main"]) == (995.0, 1)          # other fields stay
    r = client.patch(f"/api/accounts/{account_id}", json={"balance_amount": 1, "balance_date": "2.5.2026"})
    assert r.status_code == 400 and r.json()["detail"] == {"code": "invalid_date"}
    client.patch(f"/api/accounts/{account_id}", json={"balance_amount": None})
    assert (acc()["balance"], acc()["balance_date"]) == (-134.0, None)


def test_balance_check_compares_the_bank_with_the_app(client, account_id):
    upload(client, account_id, csv_bytes("01.05.2026;-10,00;Nákup;Obchod A", "03.05.2026;-20,00;Nákup;Obchod B",
                                         "06.05.2026;100,00;Vrátenie;Obchod C"), "may.csv")
    client.patch(f"/api/accounts/{account_id}", json={"balance_amount": 500, "balance_date": "2026-05-01"})
    check = lambda day, amount: client.post(f"/api/accounts/{account_id}/checks",
                                            json={"amount": amount, "date": day}).json()

    c = check("2026-05-03", 480)
    assert (c["expected"], c["difference"], c["source"]) == (480.0, 0.0, "manual")
    c = check("2026-05-06", 600)          # the bank has 20 more: a movement is missing here
    assert (c["expected"], c["difference"]) == (580.0, -20.0)
    assert check("2026-04-30", 510)["expected"] == 510.0   # before the known balance too

    # the missing movement imported: the check is computed again and matches
    upload(client, account_id, csv_bytes("05.05.2026;20,00;Vklad;Banka"), "missing.csv")
    acc = next(a for a in client.get("/api/accounts").json() if a["id"] == account_id)
    assert [(c["date"], c["difference"]) for c in acc["checks"]] == [
        ("2026-05-06", 0.0), ("2026-05-03", 0.0), ("2026-04-30", 0.0)]
    assert check("2026-05-06", 600)["id"] == acc["checks"][0]["id"]   # one check per day
    bad = client.post(f"/api/accounts/{account_id}/checks", json={"amount": 1, "date": "6.5.2026"})
    assert bad.json()["detail"] == {"code": "invalid_date"}


def test_transactions_of_one_account_have_the_running_balance(client, account_id):
    upload(client, account_id, csv_bytes("01.05.2026;-10,00;Nákup;Obchod A", "03.05.2026;-20,00;Nákup;Obchod B",
                                         "03.05.2026;5,00;Vrátenie;Obchod B"), "may.csv")
    client.patch(f"/api/accounts/{account_id}", json={"balance_amount": 100, "balance_date": "2026-05-01"})
    txs = client.get("/api/transactions", params={"account_id": account_id}).json()
    assert [(t["date"], t["amount"], t["running_balance"]) for t in txs] == [
        ("2026-05-03", 5.0, 85.0), ("2026-05-03", -20.0, 80.0), ("2026-05-01", -10.0, 100.0)]
    assert "running_balance" not in client.get("/api/transactions").json()[0]   # all accounts: none


def test_an_account_needs_a_name_of_its_own(client):
    assert client.post("/api/accounts", json={"name": "  "}).json()["detail"]["code"] == "name_required"
    assert client.post("/api/accounts", json={"name": "Savings"}).status_code == 200
    assert client.post("/api/accounts", json={"name": " Savings "}).json()["detail"]["code"] == "name_taken"
