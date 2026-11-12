# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Tags (e.g. a trip) across categories and years, and the total of a filter. Invented data."""

import sqlite3

from financie import db
from test_api import csv_bytes, upload


def trip(client, account_id):
    upload(client, account_id, csv_bytes(
        "15.01.2025;-189,00;Flug;Airline Muster",            # booked months before
        "02.06.2025;-420,00;Apartment;Ferienwohnung Muster",
        "03.06.2025;-38,50;Abendessen;Trattoria Muster",
        "04.06.2025;25,00;Rueckerstattung;Ferienwohnung Muster",
        "05.06.2025;-61,20;Einkauf;Supermercato Muster",
        "05.06.2025;-900,00;Miete;Vermieter Muster"), "trip.csv")   # rent at home: not part of it
    return {t["description"]: t["id"] for t in client.get("/api/transactions", params={"limit": 100}).json()}


def cat(client, key):
    return next(c["id"] for c in client.get("/api/categories").json() if c["key"] == key)


def test_a_tag_spans_years_and_sums_up_by_category(client, account_id):
    ids = trip(client, account_id)
    for d, key in (("Flug", "travel"), ("Apartment", "travel"), ("Abendessen", "restaurants"),
                   ("Einkauf", "groceries"), ("Rueckerstattung", "travel")):
        client.patch(f"/api/transactions/{ids[d]}", json={"category_id": cat(client, key)})
    tagged = [ids[d] for d in ("Flug", "Apartment", "Abendessen", "Rueckerstattung", "Einkauf")]
    r = client.post("/api/tags/apply", json={"tag": "  Sardinia   2025 ", "transaction_ids": tagged}).json()
    assert r["added"] == 5
    # the same name in another case is the same tag
    assert client.post("/api/tags/apply", json={"tag": "sardinia 2025", "transaction_ids": tagged}).json() == \
        {"tag_id": r["tag_id"], "added": 0}
    assert client.get("/api/tags").json() == [{"id": r["tag_id"], "name": "Sardinia 2025", "count": 5}]

    rows = client.get("/api/transactions", params={"tag_id": r["tag_id"]}).json()
    assert sorted(t["description"] for t in rows) == ["Abendessen", "Apartment", "Einkauf", "Flug", "Rueckerstattung"]
    assert rows[0]["tags"] == [{"id": r["tag_id"], "name": "Sardinia 2025"}]
    total = client.get("/api/transactions/total", params={"tag_id": r["tag_id"]}).json()
    assert (total["count"], total["expense"], total["income"], total["net"]) == (5, -708.7, 25.0, -683.7)
    assert [(c["category_key"], c["amount"]) for c in total["by_category"]] == \
        [("travel", -584.0), ("groceries", -61.2), ("restaurants", -38.5)]

    # taking the tag off the last movements deletes it; deleting a movement drops its tag
    client.post("/api/tags/remove", json={"tag_id": r["tag_id"], "transaction_ids": [ids["Flug"]]})
    assert client.get("/api/tags").json()[0]["count"] == 4
    client.delete(f"/api/transactions/{ids['Einkauf']}")
    assert client.get("/api/tags").json()[0]["count"] == 3
    client.post("/api/tags/remove", json={"tag_id": r["tag_id"], "transaction_ids": tagged})
    assert client.get("/api/tags").json() == []


def test_total_follows_the_filter_and_leaves_transfers_apart(client, account_id):
    ids = trip(client, account_id)
    client.patch(f"/api/transactions/{ids['Miete']}", json={"category_id": cat(client, "transfer")})
    total = client.get("/api/transactions/total", params={"month": "2025-06"}).json()
    assert (total["count"], total["expense"], total["transfers"]) == (5, -519.7, -900.0)
    assert client.get("/api/transactions/total", params={"q": "Muster", "year": "2025"}).json()["count"] == 6


def test_tags_can_be_renamed_but_not_onto_another(client, account_id):
    ids = list(trip(client, account_id).values())
    a = client.post("/api/tags/apply", json={"tag": "Sardinia", "transaction_ids": ids[:2]}).json()["tag_id"]
    client.post("/api/tags/apply", json={"tag": "Rome", "transaction_ids": ids[2:3]})
    r = client.patch(f"/api/tags/{a}", json={"name": "rome"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "tag_exists"
    assert client.patch(f"/api/tags/{a}", json={"name": "Sardinia 2025"}).status_code == 200
    assert client.post("/api/tags/apply", json={"tag": " ", "transaction_ids": ids}).json()["detail"] == \
        {"code": "tag_required"}
    client.delete(f"/api/tags/{a}")
    assert [g["name"] for g in client.get("/api/tags").json()] == ["Rome"]
    assert len(client.get("/api/transactions").json()) == 6                # the movements stay


def test_v7_database_gets_the_tag_tables(tmp_path, monkeypatch):
    path = tmp_path / "v7.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE transaction_tags")
    con.execute("DROP TABLE tags")
    con.execute("PRAGMA user_version = 7")
    con.commit()
    con.close()
    db.init_db()
    con = sqlite3.connect(path)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tags", "transaction_tags"} <= names
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    con.close()
    assert len(list(tmp_path.glob("v7-backup-v7-*.db"))) == 1
