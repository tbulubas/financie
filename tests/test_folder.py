# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Import history, file states and the statements folder. Invented data only."""

import pytest

from financie import db
from test_api import csv_bytes, upload
from test_formats import PAYPAL

MAY = csv_bytes("01.05.2026;-10,00;Nákup;Obchod A", "02.05.2026;1500,00;Výplata;Zamestnávateľ")
JUNE = csv_bytes("01.06.2026;-12,00;Nákup;Obchod A")
EMPTY = csv_bytes()  # a header without rows: the parser rejects it


def preview(client, *files):
    r = client.post("/api/import/preview",
                    files=[("files", (name, data, "application/octet-stream")) for name, data in files])
    return {d["file"]: d for d in r.json()}


def test_history_records_successful_and_failed_imports(client, account_id):
    upload(client, account_id, MAY, "may.csv")
    assert upload(client, account_id, EMPTY, "empty.csv").status_code == 400
    assert upload(client, account_id, b"%PDF-1.4", "scan.pdf").json()["detail"] == {"code": "ollama_missing"}

    failed_pdf, failed_csv, ok = client.get("/api/imports").json()
    assert (ok["file_name"], ok["status"], ok["inserted"], ok["skipped_duplicates"]) == ("may.csv", "ok", 2, 0)
    assert (ok["date_from"], ok["date_to"], ok["account"]) == ("2026-05-01", "2026-05-02", "Checking account")
    assert ok["imported_at"]
    assert (failed_csv["status"], failed_csv["error_code"]) == ("error", "parse_failed")
    assert failed_pdf["error_code"] == "ollama_missing"

    # every transaction knows the import it came from
    assert {t["import_id"] for t in client.get("/api/transactions").json()} == {ok["id"]}


def test_identical_file_is_not_imported_twice_into_one_account(client, account_id):
    upload(client, account_id, MAY, "may.csv")
    r = upload(client, account_id, MAY, "renamed copy.csv")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "already_imported"
    assert len(client.get("/api/imports").json()) == 1   # a refusal is not an import attempt

    client.post("/api/accounts", json={"name": "Other"})
    other = next(a["id"] for a in client.get("/api/accounts").json() if a["name"] == "Other")
    assert upload(client, other, MAY, "may.csv").json()["inserted"] == 2
    second, first = client.get("/api/imports").json()
    assert (second["duplicates"], second["duplicate_of"]) == (2, [first["id"]])   # flagged


def test_reworded_statement_is_flagged_and_can_be_undone(client, account_id):
    # what a second Gemma read of the same PDF looks like: same movements, other wording
    upload(client, account_id, MAY, "statement.csv")
    reworded = csv_bytes("01.05.2026;-10,00;Nakup;OBCHOD A", "02.05.2026;1500,00;Vyplata;Zamestnavatel",
                         "03.05.2026;-4,00;Pekáreň;Pekár")
    upload(client, account_id, reworded, "statement-again.csv")
    second, first = client.get("/api/imports").json()
    assert (second["duplicates"], second["duplicate_of"], second["remaining"]) == (2, [first["id"]], 3)
    assert first["duplicates"] == 0          # only the later import is flagged
    assert client.get("/api/overview", params={"month": "2026-05"}).json()["income"] == 3000

    assert client.delete(f"/api/imports/{second['id']}").json() == {"deleted": 3}
    second, first = client.get("/api/imports").json()
    assert (second["status"], second["remaining"], first["remaining"]) == ("undone", 0, 2)
    assert client.get("/api/overview", params={"month": "2026-05"}).json()["income"] == 1500
    # an undone file counts as not imported, so it can be imported again
    assert preview(client, ("statement-again.csv", reworded))["statement-again.csv"]["state"] == "new"
    assert upload(client, account_id, reworded, "statement-again.csv").status_code == 200


def test_transfers_are_not_flagged_as_duplicates(client, account_id):
    upload(client, account_id, csv_bytes("03.05.2026;-50,00;PayPal Europe;PayPal"), "bank.csv")
    bank_tx = client.get("/api/transactions").json()[0]
    transfer = next(c["id"] for c in client.get("/api/categories").json() if c["key"] == "transfer")
    client.patch(f"/api/transactions/{bank_tx['id']}", json={"category_id": transfer})
    client.post("/api/accounts", json={"name": "PayPal"})
    paypal = next(a["id"] for a in client.get("/api/accounts").json() if a["name"] == "PayPal")
    upload(client, paypal, csv_bytes("03.05.2026;-50,00;Kabel;Beispiel Shop"), "paypal.csv")
    assert client.get("/api/imports").json()[0]["duplicates"] == 0


def test_duplicate_pairs_can_be_reviewed_and_resolved(client, account_id):
    upload(client, account_id, MAY, "bank.csv")
    client.post("/api/accounts", json={"name": "PayPal"})
    paypal = next(a["id"] for a in client.get("/api/accounts").json() if a["name"] == "PayPal")
    upload(client, paypal, csv_bytes("01.05.2026;-10,00;Kabel;Beispiel Shop", "02.05.2026;1500,00;Lohn;Firma",
                                     "04.05.2026;-3,00;Kiosk;Kiosk"), "paypal.csv")
    later, first = client.get("/api/imports").json()
    pairs = client.get(f"/api/imports/{later['id']}/duplicates").json()
    assert [(p["date"], p["amount"], p["tx"]["account"], p["twin"]["account"], p["twin"]["import_id"],
             p["tx"]["counterparty"], p["twin"]["counterparty"]) for p in pairs] == [
        ("2026-05-01", -10.0, "PayPal", "Checking account", first["id"], "Beispiel Shop", "Obchod A"),
        ("2026-05-02", 1500.0, "PayPal", "Checking account", first["id"], "Firma", "Zamestnávateľ"),
    ]
    # the bank side of the first pair funded PayPal; the second pair is the same salary twice
    transfer = next(c["id"] for c in client.get("/api/categories").json() if c["key"] == "transfer")
    client.patch(f"/api/transactions/{pairs[0]['twin']['id']}", json={"category_id": transfer})
    client.delete(f"/api/transactions/{pairs[1]['tx']['id']}")
    assert client.get(f"/api/imports/{later['id']}/duplicates").json() == []
    assert client.get("/api/imports").json()[0]["duplicates"] == 0


def test_file_state_follows_the_content(client, account_id):
    assert preview(client, ("may.csv", MAY))["may.csv"]["state"] == "new"
    upload(client, account_id, MAY, "may.csv")
    upload(client, account_id, EMPTY, "empty.csv")

    p = preview(client, ("may.csv", MAY), ("copy of may.csv", MAY), ("Download.CSV", PAYPAL),
                ("may.csv", MAY), ("empty.csv", EMPTY))
    assert p["may.csv"]["state"] == "imported"
    assert p["may.csv"]["last"]["inserted"] == 2
    assert p["copy of may.csv"]["state"] == "imported"   # renamed, same content
    assert p["Download.CSV"]["state"] == "new"
    assert p["empty.csv"]["state"] == "failed"
    assert p["empty.csv"]["last"]["error_code"] == "parse_failed"

    # the same name with other content: e.g. the next PayPal or bank download
    june = preview(client, ("may.csv", JUNE))["may.csv"]
    assert june["state"] == "changed"
    assert june["account_id"] == account_id   # a generic CSV has no source key: the last account
    assert preview(client, ("may.csv", MAY))["may.csv"]["kind"] == "csv"


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "Statements"
    (root / "bank").mkdir(parents=True)
    (root / "bank" / "may.csv").write_bytes(MAY)
    (root / "june.CSV").write_bytes(JUNE)
    (root / "scan.pdf").write_bytes(b"%PDF-1.4")
    (root / "notes.txt").write_text("not a statement")
    return root


def test_folder_lists_statements_and_imports_only_on_request(client, account_id, folder):
    assert client.get("/api/folder").json() == {"dir": None, "files": []}
    assert client.put("/api/settings", json={"imports_dir": str(folder)}).json()["imports_dir"] == str(folder)

    listing = client.get("/api/folder").json()
    files = {f["path"]: f for f in listing["files"]}
    assert set(files) == {"bank/may.csv", "june.CSV", "scan.pdf"}   # notes.txt is not a statement
    assert {p: f["kind"] for p, f in files.items()} == {"bank/may.csv": "csv", "june.CSV": "csv", "scan.pdf": "pdf"}
    assert all(f["state"] == "new" for f in files.values())
    assert files["bank/may.csv"]["count"] == 2 and files["scan.pdf"]["gemma"] is True
    assert client.get("/api/imports").json() == []                  # looking imports nothing

    r = client.post("/api/folder/import", json={"path": "bank/may.csv", "account_id": account_id})
    assert r.json()["inserted"] == 2
    files = {f["path"]: f for f in client.get("/api/folder").json()["files"]}
    assert files["bank/may.csv"]["state"] == "imported"
    assert files["june.CSV"]["state"] == "new"


def test_folder_import_stays_inside_the_folder(client, account_id, folder):
    client.put("/api/settings", json={"imports_dir": str(folder)})
    (folder.parent / "outside.csv").write_bytes(MAY)
    for path in ("../outside.csv", str(folder.parent / "outside.csv"), "missing.csv", "bank"):
        r = client.post("/api/folder/import", json={"path": path, "account_id": account_id})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "file_missing", path


def test_folder_setting(client, folder, monkeypatch):
    r = client.put("/api/settings", json={"imports_dir": str(folder / "nope")})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "folder_missing"

    monkeypatch.setattr(db, "IMPORTS_DIR", str(folder))   # FINANCIE_IMPORTS
    assert client.get("/api/settings").json()["imports_dir"] == str(folder)
    other = folder / "bank"
    client.put("/api/settings", json={"imports_dir": f'"{other}"'})   # pasted with quotes
    assert client.get("/api/settings").json()["imports_dir"] == str(other)
    client.put("/api/settings", json={"imports_dir": ""})              # back to the default
    assert client.get("/api/settings").json()["imports_dir"] == str(folder)


def test_deleting_an_account_keeps_its_history(client):
    client.post("/api/accounts", json={"name": "Tmp"})
    tmp = next(a["id"] for a in client.get("/api/accounts").json() if a["name"] == "Tmp")
    upload(client, tmp, MAY, "may.csv")
    for t in client.get("/api/transactions", params={"account_id": tmp}).json():
        client.delete(f"/api/transactions/{t['id']}")
    assert client.delete(f"/api/accounts/{tmp}").status_code == 200
    entry = client.get("/api/imports").json()[0]
    assert (entry["file_name"], entry["account_id"], entry["account"]) == ("may.csv", None, None)


def test_tests_never_see_the_users_settings():
    import os
    assert db.IMPORTS_DIR is None
    assert not [n for n in os.environ if n.startswith("FINANCIE_")]
