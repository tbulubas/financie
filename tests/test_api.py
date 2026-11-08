# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""API behaviour against a throwaway database. All sample rows are invented."""

TRANSFER = "Transfer between accounts"
GROCERIES = "Groceries"


def csv_bytes(*rows: str) -> bytes:
    return ("Dátum;Suma;Popis;Príjemca\n" + "\n".join(rows) + "\n").encode("utf-8")


def upload(client, account_id, data: bytes, name: str = "vypis.csv"):
    return client.post("/api/import", data={"account_id": account_id},
                       files={"file": (name, data, "text/csv")})


def category_id(client, name: str) -> int:
    return next(c["id"] for c in client.get("/api/categories").json() if c["name"] == name)


def test_import_and_dedup(client, account_id):
    data = csv_bytes("01.05.2026;-10,00;Nákup;Obchod A", "02.05.2026;1500,00;Výplata;Zamestnávateľ")
    first = upload(client, account_id, data).json()
    assert first["inserted"] == 2 and first["skipped_duplicates"] == 0

    # an overlapping statement: the known rows are skipped, the new one is added
    longer = data + "03.05.2026;-5,00;Káva;Kaviareň\n".encode("utf-8")
    again = upload(client, account_id, longer, "vypis-2.csv").json()
    assert again["inserted"] == 1 and again["skipped_duplicates"] == 2

    # the very same file again is refused as a whole
    same = upload(client, account_id, data)
    assert same.status_code == 400 and same.json()["detail"] == {"code": "already_imported", "info": "#1"}


def test_learned_category_reused_on_next_import(client, account_id):
    upload(client, account_id, csv_bytes("01.05.2026;-10,00;Nákup;Obchod A"))
    tx = client.get("/api/transactions").json()[0]
    client.patch(f"/api/transactions/{tx['id']}", json={"category_id": category_id(client, GROCERIES)})

    result = upload(client, account_id, csv_bytes("08.05.2026;-22,40;Nákup;Obchod A")).json()
    assert result["auto_categorized"] == 1 and result["uncategorized"] == 0
    newest = client.get("/api/transactions").json()[0]
    assert newest["date"] == "2026-05-08" and newest["category"] == GROCERIES


def test_overview_excludes_transfers(client, account_id):
    upload(client, account_id, csv_bytes(
        "01.05.2026;-10,00;Nákup;Obchod A",
        "02.05.2026;1500,00;Výplata;Zamestnávateľ",
        "03.05.2026;-500,00;Splátka karty;Kreditná karta",
    ))
    card = next(t for t in client.get("/api/transactions").json() if t["counterparty"] == "Kreditná karta")
    client.patch(f"/api/transactions/{card['id']}", json={"category_id": category_id(client, TRANSFER)})

    ov = client.get("/api/overview", params={"date_from": "2026-05-01", "date_to": "2026-05-31"}).json()
    assert ov["income"] == 1500.0
    assert ov["expense"] == -10.0
    assert TRANSFER not in [c["category"] for c in ov["by_category"]]


def test_balance_is_opening_plus_transactions(client):
    client.post("/api/accounts", json={"name": "Test", "opening_balance": 100.0})
    acc = next(a for a in client.get("/api/accounts").json() if a["name"] == "Test")
    upload(client, acc["id"], csv_bytes("01.05.2026;-30,25;Nákup;Obchod A", "02.05.2026;5,00;Vratka;Obchod A"))
    acc = next(a for a in client.get("/api/accounts").json() if a["name"] == "Test")
    assert round(acc["balance"], 2) == 74.75


def test_error_codes(client, account_id):
    assert upload(client, account_id, b"foo;bar\n1;2\n").json()["detail"]["code"] == "parse_failed"
    assert upload(client, account_id, b"%PDF-1.4", "scan.pdf").json()["detail"]["code"] == "ollama_missing"

    upload(client, account_id, csv_bytes("01.05.2026;-10,00;Nákup;Obchod A"))
    r = client.delete(f"/api/accounts/{account_id}")
    assert r.status_code == 400 and r.json()["detail"] == {"code": "account_not_empty", "info": "1"}

    r = client.post("/api/categories", json={"name": GROCERIES})
    assert r.json()["detail"]["code"] == "duplicate_category"
    assert client.post("/api/categories", json={"name": "  "}).json()["detail"]["code"] == "empty_name"


def test_net_worth_includes_liabilities(client):
    client.post("/api/assets", json={"name": "Byt", "type": "real_estate"})
    client.post("/api/assets", json={"name": "Hypotéka", "type": "liability"})
    assets = {a["name"]: a["id"] for a in client.get("/api/networth").json()["assets"]}
    client.post("/api/asset_values", json={"asset_id": assets["Byt"], "date": "2026-01-01", "value": 200000})
    client.post("/api/asset_values", json={"asset_id": assets["Hypotéka"], "date": "2026-01-01", "value": -150000})
    nw = client.get("/api/networth").json()
    assert nw["total_assets"] == 50000.0
    assert nw["net_worth"] == nw["total_accounts"] + 50000.0


def test_transactions_by_year_and_the_years_list(client, account_id):
    upload(client, account_id, csv_bytes("15.03.2021;-10,00;Nákup;Obchod A", "02.01.2025;-20,00;Nákup;Obchod B",
                                         "20.12.2025;-30,00;Nákup;Obchod C", "05.11.2026;-40,00;Nákup;Obchod D"))
    assert client.get("/api/transactions/years").json() == ["2026", "2025", "2021"]
    year = client.get("/api/transactions", params={"year": "2025"}).json()
    assert [t["date"] for t in year] == ["2025-12-20", "2025-01-02"]
    month = client.get("/api/transactions", params={"month": "2025-12"}).json()
    assert [t["amount"] for t in month] == [-30.0]
