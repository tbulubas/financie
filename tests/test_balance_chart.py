# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Balance over time per account, for the chart on the overview. Invented data."""

from test_api import csv_bytes, upload
from test_formats import add_account


def history(client, accounts=None, date_from="2026-05-01", date_to="2026-05-31"):
    params = {"date_from": date_from, "date_to": date_to}
    if accounts is not None:
        params["accounts"] = ",".join(map(str, accounts))
    return client.get("/api/balances", params=params).json()


def test_end_of_day_balances_from_the_known_balance(client, account_id):
    upload(client, account_id, csv_bytes("03.05.2026;-10,00;Nákup;Obchod A", "03.05.2026;-5,00;Nákup;Obchod B",
                                         "10.05.2026;100,00;Vrátenie;Obchod C"), "may.csv")
    client.patch(f"/api/accounts/{account_id}", json={"balance_amount": 500, "balance_date": "2026-05-03"})
    client.post(f"/api/accounts/{account_id}/checks", json={"amount": 600, "date": "2026-05-10"})
    client.post(f"/api/accounts/{account_id}/checks", json={"amount": 590, "date": "2026-05-12"})

    acc = history(client, [account_id])["accounts"][0]
    # nothing before the first movement; one point per day, the last day of the period closes it
    assert acc["points"] == [{"date": "2026-05-03", "balance": 500.0}, {"date": "2026-05-10", "balance": 600.0},
                             {"date": "2026-05-31", "balance": 600.0}]
    assert acc["checks"] == [{"date": "2026-05-10", "amount": 600.0, "difference": 0.0},
                             {"date": "2026-05-12", "amount": 590.0, "difference": 10.0}]

    # a later period starts with the balance carried over
    later = history(client, [account_id], "2026-05-05", "2026-05-31")["accounts"][0]["points"]
    assert later[0] == {"date": "2026-05-05", "balance": 500.0}


def test_total_starts_when_every_account_has_data(client, account_id):
    other = add_account(client, "Savings")
    upload(client, account_id, csv_bytes("02.05.2026;100,00;Vklad;Banka", "20.05.2026;-30,00;Nákup;Obchod"), "a.csv")
    upload(client, other, csv_bytes("10.05.2026;50,00;Vklad;Banka"), "b.csv")
    h = history(client)
    lines = {a["name"]: a["points"] for a in h["accounts"]}
    assert lines["Savings"][0] == {"date": "2026-05-10", "balance": 50.0}
    assert h["total"] == [{"date": "2026-05-10", "balance": 150.0}, {"date": "2026-05-20", "balance": 120.0},
                          {"date": "2026-05-31", "balance": 120.0}]


def test_accounts_without_data_have_no_line(client, account_id):
    empty = add_account(client, "Empty")
    upload(client, account_id, csv_bytes("02.05.2026;100,00;Vklad;Banka"), "a.csv")
    h = history(client, [empty])
    assert h["accounts"][0]["points"] == [] and h["total"] == []
    assert history(client, [])["accounts"] == []
    # a period after the data ends still shows the balance carried over
    june = history(client, [account_id], "2026-06-01", "2026-06-30")["accounts"][0]["points"]
    assert june == [{"date": "2026-06-01", "balance": 100.0}, {"date": "2026-06-30", "balance": 100.0}]


def test_the_chart_library_is_served_locally(client):
    page = client.get("/").text
    assert "https://" not in page.split("<style>")[0]          # no script from the internet
    r = client.get("/static/vendor/chart.umd.js")
    assert r.status_code == 200 and "Chart.js v4" in r.text[:500]


def _old_and_new_history(client, account_id, old_bank_balance):
    """Movements in March 2021 and May 2026 with nothing in between, the known balance in 2026
    and the bank's balances of the two days in 2021."""
    upload(client, account_id, csv_bytes("01.03.2021;100,00;Vklad;Banka", "10.03.2021;-20,00;Nákup;Obchod A",
                                         "01.05.2026;50,00;Vklad;Banka", "03.05.2026;-10,00;Nákup;Obchod B"), "a.csv")
    client.patch(f"/api/accounts/{account_id}", json={"balance_amount": 500, "balance_date": "2026-05-03"})
    for day, amount in (("2021-03-01", old_bank_balance + 20), ("2021-03-10", old_bank_balance)):
        client.post(f"/api/accounts/{account_id}/checks", json={"amount": amount, "date": day})


def test_balances_before_a_hole_follow_the_bank(client, account_id):
    # going back from 500 across five unknown years would give 460 on 10 March 2021; the bank says 1080
    _old_and_new_history(client, account_id, 1080)
    acc = next(a for a in client.get("/api/accounts").json() if a["id"] == account_id)
    assert acc["balance"] == 500.0                                   # today's balance does not move
    assert [c["difference"] for c in acc["checks"]] == [0, 0]
    txs = client.get("/api/transactions", params={"account_id": account_id}).json()
    assert {t["date"]: t["running_balance"] for t in txs}["2021-03-01"] == 1100.0

    h = history(client, [account_id], "2021-01-01", "2026-05-31")["accounts"][0]
    assert h["gaps"] == [{"from": "2021-03-10", "to": "2026-05-01"}]
    assert h["points"] == [{"date": "2021-03-01", "balance": 1100.0}, {"date": "2021-03-10", "balance": 1080.0},
                           {"date": "2026-05-01", "balance": None},     # the line breaks here
                           {"date": "2026-05-01", "balance": 510.0}, {"date": "2026-05-03", "balance": 500.0},
                           {"date": "2026-05-31", "balance": 500.0}]

    # a wrong balance inside the old stretch is still a difference
    c = client.post(f"/api/accounts/{account_id}/checks", json={"amount": 1105, "date": "2021-03-05"}).json()
    assert c["difference"] == -5.0
    # a period that ends inside the hole draws nothing there
    assert history(client, [account_id], "2021-03-01", "2022-12-31")["accounts"][0]["points"][-1] == \
        {"date": "2022-12-31", "balance": None}


def test_a_hole_that_agrees_with_the_bank_changes_nothing(client, account_id):
    _old_and_new_history(client, account_id, 460)                    # nothing happened in between
    h = history(client, [account_id], "2021-01-01", "2026-05-31")["accounts"][0]
    assert h["gaps"] == [] and None not in [p["balance"] for p in h["points"]]
    assert [c["difference"] for c in h["checks"]] == [0, 0]


def test_balance_gaps_names_the_hole(client, account_id):
    from financie import db
    _old_and_new_history(client, account_id, 1080)
    con = db.get_db()
    try:
        assert db.balance_gaps(con, account_id) == [{"from": "2021-03-10", "to": "2026-05-01"}]
    finally:
        con.close()
