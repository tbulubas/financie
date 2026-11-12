# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Recurring payments and the twelve-month outlook. All movements are invented."""

from datetime import date, timedelta

from financie import db, planning
from test_api import csv_bytes, upload

TODAY = date(2026, 11, 10)


def de(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def history() -> bytes:
    rows = []
    for m in range(1, 11):                                   # January .. October 2026
        rent = "-950,00" if m < 9 else "-1000,00"            # the rent went up in September
        rows += [f"{de(date(2026, m, 2))};{rent};Miete;Hausverwaltung Muster",
                 f"{de(date(2026, m, 1))};3000,00;Gehalt {m}/2026;Arbeitgeber Muster"]
        if m <= 4:                                           # cancelled after April
            rows.append(f"{de(date(2026, m, 20))};-9,99;Abo;Streaming Muster")
    for m in (1, 4, 7, 10):
        rows.append(f"{de(date(2026, m, 15))};-120,00;Beitrag;Versicherung Muster")
    rows += [f"{de(date(2025, 3, 10))};-89,00;Jahresgebuehr;Automobilclub Muster",
             f"{de(date(2026, 3, 12))};-89,00;Jahresgebuehr;Automobilclub Muster",
             f"{de(date(2025, 6, 1))};-45,00;Geschenk;Buchladen Muster",           # one year apart,
             f"{de(date(2026, 6, 3))};-180,00;Geschenk;Buchladen Muster"]          # different amounts
    day, amounts = date(2026, 1, 2), [23.5, 61.2, 8.9, 44.0, 17.35]
    for i in range(70):                                      # groceries every few days
        rows.append(f"{de(day)};-{amounts[i % 5]:.2f}".replace(".", ",") + ";Einkauf;Supermarkt Muster")
        day += timedelta(days=3 + i % 5)
        if day >= date(2026, 11, 1):
            break
    return csv_bytes(*rows)


def found(con):
    return {i["name"]: i for i in planning.recurring(con, today=TODAY)["items"]}


def test_recurring_payments_are_found_with_their_period(client, account_id):
    upload(client, account_id, history(), "history.csv")
    con = db.get_db()
    items = found(con)
    rent, insurance, club = items["Hausverwaltung Muster"], items["Versicherung Muster"], items["Automobilclub Muster"]
    assert (rent["period"], rent["amount"], rent["changed_from"], rent["next"]) == ("monthly", 1000.0, 950.0, "2026-11-02")
    assert (insurance["period"], insurance["monthly"], insurance["next"]) == ("quarterly", 40.0, "2027-01-15")
    assert (club["period"], club["monthly"], club["next"]) == ("yearly", 7.42, "2027-03-12")
    assert items["Arbeitgeber Muster"]["kind"] == "income" and items["Arbeitgeber Muster"]["amount"] == 3000.0
    assert items["Streaming Muster"]["active"] is False                      # ended in April
    assert "Supermarkt Muster" not in items and "Buchladen Muster" not in items
    totals = planning.recurring(con, today=TODAY)["totals"]
    assert totals["expense"] == {"monthly": 1047.42, "quarterly": 3142.26, "yearly": 12569.04}

    # a payment the user says is not recurring leaves the totals
    planning.set_ignored(con, insurance["key"], True)
    assert found(con)["Versicherung Muster"]["ignored"] is True
    assert planning.recurring(con, today=TODAY)["totals"]["expense"]["monthly"] == 1007.42
    con.close()


def test_outlook_needs_an_income_that_keeps_the_balance_above_the_floor(client, account_id):
    upload(client, account_id, history(), "history.csv")
    con = db.get_db()
    planning.set_plans(con, [{"name": "Holiday", "month": "2027-07", "amount": 1500}])
    start = planning.forecast(con, today=TODAY)["start_balance"]
    assert planning.forecast(con, today=TODAY)["min_income"] == 0     # today's balance covers a year
    floor = start + 2000                                              # e.g. saving 2000 more
    fc = planning.forecast(con, floor=floor, today=TODAY)
    months = {m["month"]: m for m in fc["months"]}
    assert (fc["from"], fc["to"], len(fc["months"])) == ("2026-12", "2027-11", 12)
    assert fc["basis"]["months"] == 10 and (fc["basis"]["from"], fc["basis"]["to"]) == ("2026-01", "2026-10")
    # recurring payments on their dates: rent every month, insurance in January, the club in March
    assert months["2026-12"]["fixed"] == 1000.0
    assert months["2027-01"]["fixed"] == 1120.0 and months["2027-03"]["fixed"] == 1089.0
    assert months["2027-07"]["planned"] == 1500.0
    # the minimum income keeps every month at or above the floor and touches it once
    lows = [m["balance_min_income"] for m in fc["months"]]
    assert min(lows) >= floor - 0.05 and abs(min(lows) - floor) < 0.13
    assert fc["cover_income"] >= fc["expenses_monthly"] > fc["fixed_monthly"]
    assert fc["regular_income_monthly"] == 3000.0
    con.close()


def test_plans_are_checked(client):
    r = client.put("/api/forecast/plans", json=[{"name": "Car", "month": "2027-13", "amount": 100}])
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_plan"
    assert client.put("/api/forecast/plans", json=[{"name": "Car", "month": "2027-05", "amount": 9000}]).status_code == 200
    fc = client.get("/api/forecast", params={"floor": 0}).json()
    assert fc["plans"] == [{"name": "Car", "month": "2027-05", "amount": 9000.0}]
    assert client.get("/api/recurring").json()["items"] == []
