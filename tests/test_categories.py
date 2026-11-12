# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Who chose a category: the user's own choice wins over learned ones and AI guesses, and can be
given to the other movements of the same counterparty. All movements are invented."""

import sqlite3

from financie import db, gemma, importer
from test_api import csv_bytes, upload


def cat(client, key):
    return next(c["id"] for c in client.get("/api/categories").json() if c["key"] == key)


def txs(client, **params):
    return {t["id"]: t for t in client.get("/api/transactions", params=params).json()}


def five_payments(client, account_id):
    upload(client, account_id, csv_bytes(*[f"0{d}.05.2026;-9,99;Abo;Telefon Muster" for d in range(1, 6)]), "a.csv")
    return sorted(txs(client))


def test_the_users_choice_beats_ai_guesses_when_learning(client, account_id):
    ids = five_payments(client, account_id)
    con = db.get_db()
    con.executemany("UPDATE transactions SET category_id=?, category_source='ai' WHERE id=?",
                    [(cat(client, "subscriptions"), i) for i in ids[:4]])
    con.commit()
    assert importer._learned_category(con, {"counterparty": "Telefon Muster"}) == cat(client, "subscriptions")
    # one correction by the user outweighs four guesses
    client.patch(f"/api/transactions/{ids[4]}", json={"category_id": cat(client, "internet_phone")})
    assert importer._learned_category(con, {"counterparty": "Telefon Muster"}) == cat(client, "internet_phone")
    con.close()


def test_a_choice_can_go_to_the_similar_movements_but_not_over_the_users_own(client, account_id):
    ids = five_payments(client, account_id)
    phone, subs, gifts = cat(client, "internet_phone"), cat(client, "subscriptions"), cat(client, "gifts")
    con = db.get_db()
    con.execute("UPDATE transactions SET category_id=?, category_source='ai' WHERE id IN (?, ?)", (subs, ids[0], ids[1]))
    con.execute("UPDATE transactions SET category_id=?, category_source='user' WHERE id=?", (gifts, ids[2]))
    con.commit()
    con.close()
    r = client.patch(f"/api/transactions/{ids[4]}", json={"category_id": phone}).json()
    assert (r["similar"], r["similar_to"]) == (3, "Telefon Muster")      # 2 AI guesses + 1 uncategorized
    assert client.post(f"/api/transactions/{ids[4]}/apply-similar").json() == {"changed": 3}
    after = txs(client)
    assert [after[i]["category_id"] for i in ids] == [phone, phone, gifts, phone, phone]
    assert {after[i]["category_source"] for i in ids} == {"user"}
    # the user's earlier choice stayed; clearing a category clears its source too
    client.patch(f"/api/transactions/{ids[0]}", json={"category_id": None})
    assert (txs(client)[ids[0]]["category_id"], txs(client)[ids[0]]["category_source"]) == (None, None)


def test_ai_guesses_can_be_listed_and_never_become_transfers(client, account_id, monkeypatch):
    five_payments(client, account_id)
    offered = []

    def fake(transactions, categories):
        offered.extend(categories)
        return ["Subscriptions"]                  # one answer for five movements
    monkeypatch.setattr(gemma, "available", lambda: True)
    monkeypatch.setattr(gemma, "categorize", fake)
    r = client.post("/api/categorize").json()
    assert "Transfer between accounts" not in offered and "Subscriptions" in offered
    assert (r["categorized"], r["remaining"]) == (1, 4)    # the missing answers stay uncategorized
    assert [t["category_source"] for t in txs(client, source="ai").values()] == ["ai"]


def test_v6_database_gets_the_category_source(tmp_path, monkeypatch):
    path = tmp_path / "v6.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE transactions DROP COLUMN category_source")
    con.execute("""INSERT INTO transactions(account_id, date, amount, description, category_id, import_hash)
                   VALUES (1, '2026-05-01', -9.99, 'Abo', (SELECT id FROM categories WHERE key='subscriptions'), 'h1'),
                          (1, '2026-05-02', -5, 'Kiosk', NULL, 'h2')""")
    con.execute("PRAGMA user_version = 6")
    con.commit()
    con.close()
    db.init_db()
    con = sqlite3.connect(path)
    assert con.execute("SELECT description, category_source FROM transactions ORDER BY id").fetchall() == \
        [("Abo", "user"), ("Kiosk", None)]
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    con.close()
    assert len(list(tmp_path.glob("v6-backup-v6-*.db"))) == 1
