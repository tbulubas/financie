# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""The demo: invented data in a fresh folder, imported through the app's own import."""

from datetime import date

from fastapi.testclient import TestClient

from financie import db, demo


def test_demo_builds_a_consistent_example(tmp_path):
    path = demo.build(tmp_path, today=date(2026, 11, 8))
    assert path == tmp_path / "financie-demo.db" and db.DB_PATH == path
    from financie.app import app
    with TestClient(app) as client:
        accounts = {a["name"]: a for a in client.get("/api/accounts").json()}
        assert set(accounts) == {"Giro", "Sparkonto", "Visa"} and accounts["Giro"]["is_main"] == 1
        # the CAMT statements state the balance at every month end: the app agrees with all of them
        assert accounts["Giro"]["checks"] and all(c["difference"] == 0 for c in accounts["Giro"]["checks"])
        # the savings account shows what a missing movement looks like
        assert [c["difference"] for c in accounts["Sparkonto"]["checks"]] == [-4.18]
        assert not [t for t in client.get("/api/transactions", params={"uncategorized": "true"}).json()]

        folder = {f["path"]: f for f in client.get("/api/folder").json()["files"]}
        left = [p for p, f in folder.items() if f["state"] == "new"]
        assert left == ["Giro-Sparkasse-202610-umsatz.CSV"]
        r = client.post("/api/folder/import", json={"path": left[0], "account_id": accounts["Giro"]["id"]}).json()
        assert r["inserted"] == 0 and r["skipped_duplicates"] > 20     # already there as CAMT

        assert len(client.get("/api/networth").json()["assets"]) == 3
