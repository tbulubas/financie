# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Recurring payments and a twelve-month outlook — plain code, no LLM.

Recurring payments are found in the transactions themselves: the same counterparty, paid at a
regular interval (monthly, quarterly, half-yearly or yearly). The outlook adds the active ones
up for the next twelve months, together with the usual variable spending and the extras the
user plans, and works out the monthly income that keeps the balance above a floor. Every
figure is computed here, and the assumptions are returned with it, so the UI can show them.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, timedelta
from statistics import mean

from financie import db

# name, mean length in days, months between two payments
PERIODS = (("monthly", 30.44, 1), ("quarterly", 91.31, 3), ("half_yearly", 182.62, 6), ("yearly", 365.25, 12))
MIN_GOOD = {"monthly": 2, "quarterly": 2, "half_yearly": 1, "yearly": 1}   # regular intervals needed
SAME = 0.02          # two amounts within 2 %: the same fixed amount
FEW = 4              # fewer payments than this must have nearly the same amount to count
FEW_SPREAD = 0.10
LOOKBACK_DAYS = 800  # a little over two years: a yearly payment shows up twice
HOLE_DAYS = 45       # no movement for longer: a hole in the history (as in db.GAP_DAYS)
IGNORED_KEY = "recurring_ignored"   # settings: keys the user marked as not recurring
PLANS_KEY = "forecast_plans"        # settings: planned extra expenses of the outlook


def group_key(amount: float, counterparty: str | None, description: str | None) -> str:
    """Payments to (or from) the same party: its name without digits and punctuation, so
    "Miete 11/2026" and "Miete 12/2026" or "SHOP*1234" and "SHOP*5678" fall together."""
    def norm(text):
        return " ".join(re.sub(r"[\W\d_]+", " ", str(text or "").lower()).split())
    name = norm(counterparty) or " ".join(norm(description).split()[:3])
    return ("+" if amount > 0 else "-") + name


def add_months(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def _period(days: list[date]) -> tuple[str, float, int] | None:
    """The interval most payments follow, allowing a skipped payment and holes in the history."""
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    for name, length, months in PERIODS:
        tol = max(4.0, 0.12 * length)
        good = sum(abs(g - length) <= tol for g in gaps)
        skipped = sum(abs(g - 2 * length) <= tol for g in gaps)
        counted = len(gaps) - sum(g > 2.5 * length for g in gaps)
        if good >= MIN_GOOD[name] and counted and good >= skipped and (good + skipped) / counted >= 0.75:
            return name, length, months
    return None


def _ignored(con) -> set[str]:
    return set(json.loads(db.get_setting(con, IGNORED_KEY, "[]")))


def set_ignored(con, key: str, ignored: bool) -> None:
    keys = _ignored(con)
    (keys.add if ignored else keys.discard)(key)
    db.set_setting(con, IGNORED_KEY, json.dumps(sorted(keys)))


def _rows(con, account_ids: list[int] | None, since: str) -> list:
    acc = f" AND t.account_id IN ({','.join('?' * len(account_ids))})" if account_ids is not None else ""
    return con.execute(f"""
        SELECT t.date, t.amount, t.counterparty, t.description, c.key AS category_key,
               c.name AS category, a.name AS account
        FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
        JOIN accounts a ON a.id = t.account_id
        WHERE t.date >= ? AND COALESCE(c.kind, '') != 'transfer'{acc}
        ORDER BY t.date""", (since, *(account_ids or []))).fetchall()


def recurring(con, account_ids: list[int] | None = None, today: date | None = None) -> dict:
    """Recurring payments and income of the chosen accounts (all when None), transfers left out.

    Each item: the party, its period, the current amount (the last one when the amount is
    fixed, else the mean of the last six), whether it changed lately, the monthly equivalent,
    the last and the next expected date, and whether it is still active (paid within one and a
    half periods of the end of the data). Totals count the active items not ignored."""
    today = today or date.today()
    rows = _rows(con, account_ids, (today - timedelta(days=LOOKBACK_DAYS)).isoformat())
    data_end = date.fromisoformat(rows[-1]["date"]) if rows else today
    ignored = _ignored(con)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(group_key(r["amount"], r["counterparty"], r["description"]), []).append(r)

    items = []
    for key, txs in groups.items():
        by_day: dict[date, float] = {}
        for t in txs:
            d = date.fromisoformat(t["date"])
            by_day[d] = by_day.get(d, 0.0) + abs(t["amount"])
        days, amounts = sorted(by_day), [round(by_day[d], 2) for d in sorted(by_day)]
        found = _period(days) if len(days) >= 2 else None
        if not found:
            continue
        name, length, months = found
        current = amounts[-1]
        if len(amounts) < FEW and any(abs(a - current) > FEW_SPREAD * current for a in amounts):
            continue   # two purchases a year apart are not a yearly payment
        same = lambda a: abs(a - current) <= SAME * max(a, current)
        fixed = len(amounts) >= 2 and same(amounts[-2])
        amount = current if fixed or len(amounts) < FEW else round(mean(amounts[-6:]), 2)
        recent = [a for d, a in zip(days, amounts) if (days[-1] - d).days <= 365][:-1]
        changed = next((a for a in reversed(recent) if not same(a)), None) if fixed else None
        cats = Counter((t["category_key"], t["category"]) for t in txs if t["category"])
        (cat_key, cat), _ = (cats.most_common(1) or [((None, None), 0)])[0]
        last = txs[-1]
        items.append({
            "key": key, "kind": "income" if key[0] == "+" else "expense",
            "name": last["counterparty"] or last["description"] or key[1:],
            "period": name, "count": len(days), "first": days[0].isoformat(), "last": days[-1].isoformat(),
            "next": add_months(days[-1], months).isoformat(),
            "amount": amount, "fixed": fixed, "changed_from": changed,
            "monthly": round(amount / months, 2),   # the monthly equivalent
            "category_key": cat_key, "category": cat,
            "accounts": sorted({t["account"] for t in txs}),
            "active": (data_end - days[-1]).days <= 1.5 * length + max(4.0, 0.12 * length),
            "ignored": key in ignored,
        })
    items.sort(key=lambda i: (i["kind"], not i["active"], -i["monthly"]))

    def total(kind):
        monthly = round(sum(i["monthly"] for i in items if i["kind"] == kind and i["active"] and not i["ignored"]), 2)
        return {"monthly": monthly, "quarterly": round(monthly * 3, 2), "yearly": round(monthly * 12, 2)}
    return {"data_end": data_end.isoformat(), "items": items,
            "totals": {"expense": total("expense"), "income": total("income")}}


# ---------- the outlook ----------

def plans(con) -> list[dict]:
    return json.loads(db.get_setting(con, PLANS_KEY, "[]"))


def set_plans(con, items: list[dict]) -> list[dict]:
    clean = [{"name": str(p["name"]).strip(), "month": p["month"], "amount": round(float(p["amount"]), 2)}
             for p in items if str(p.get("name", "")).strip()]
    db.set_setting(con, PLANS_KEY, json.dumps(clean))
    return clean


def _basis_months(dates: list[str], today: date) -> list[str]:
    """The last (up to) twelve complete months before this one that the data fully covers:
    within the latest stretch without a hole, starting with the first month the stretch covers
    from its first days."""
    if not dates:
        return []
    days = sorted({date.fromisoformat(d) for d in dates})
    start = days[0]
    for a, b in zip(days, days[1:]):
        if (b - a).days > HOLE_DAYS:
            start = b
    month = start.replace(day=1) if start.day <= 3 else add_months(start.replace(day=1), 1)
    out, this = [], today.replace(day=1)
    while month < this:
        out.append(month.strftime("%Y-%m"))
        month = add_months(month, 1)
    return out[-12:]


def forecast(con, account_ids: list[int] | None = None, floor: float = 0.0,
             today: date | None = None, horizon: int = 12) -> dict:
    """The next `horizon` months from the first of the next month, for the chosen accounts.

    Expenses per month = the active recurring payments falling due in it + the variable
    spending (the mean month of the basis period: everything that is neither a transfer nor a
    recurring payment) + the planned extras. The balance starts at today's balance of the
    accounts. `min_income` is the smallest equal monthly income that keeps the balance at or
    above `floor` at the end of every month; `cover_income` the one that covers the expenses
    without touching today's balance at all."""
    today = today or date.today()
    if account_ids is None:
        account_ids = [r["id"] for r in con.execute("SELECT id FROM accounts WHERE type != 'investment'")]
    rec = recurring(con, account_ids, today)
    months = [add_months(today.replace(day=1), i + 1) for i in range(horizon)]
    keys = [m.strftime("%Y-%m") for m in months]
    end = add_months(months[-1], 1)

    def due(kind):
        out = dict.fromkeys(keys, 0.0)
        for item in rec["items"]:
            if item["kind"] != kind or not item["active"] or item["ignored"]:
                continue
            step = next(m for name, _, m in PERIODS if name == item["period"])
            day = date.fromisoformat(item["next"])
            while day < end:
                if day >= months[0]:
                    out[day.strftime("%Y-%m")] += item["amount"]
                day = add_months(day, step)
        return out
    fixed, regular_income = due("expense"), due("income")

    recurring_keys = {i["key"] for i in rec["items"] if not i["ignored"]}
    rows = _rows(con, account_ids, (today - timedelta(days=LOOKBACK_DAYS)).isoformat())
    basis = _basis_months([r["date"] for r in rows], today)
    variable_total = sum(-r["amount"] for r in rows if r["amount"] < 0 and r["date"][:7] in basis
                         and group_key(r["amount"], r["counterparty"], r["description"]) not in recurring_keys)
    variable = round(variable_total / len(basis), 2) if basis else 0.0

    planned = dict.fromkeys(keys, 0.0)
    for p in plans(con):
        if p["month"] in planned:
            planned[p["month"]] += p["amount"]

    start_balance = round(sum(a["balance"] for a in db.account_balances(con) if a["id"] in account_ids), 2)
    rows_out, cum, need, cover = [], 0.0, [], []
    for i, k in enumerate(keys, 1):
        expenses = round(fixed[k] + variable + planned[k], 2)
        cum += expenses
        need.append((cum - (start_balance - floor)) / i)
        cover.append(cum / i)
        rows_out.append({"month": k, "fixed": round(fixed[k], 2), "variable": variable,
                         "planned": round(planned[k], 2), "expenses": expenses,
                         "regular_income": round(regular_income[k], 2)})
    min_income = round(max(0.0, max(need)), 2) if need else 0.0
    tight = keys[need.index(max(need))] if need and max(need) > 0 else None
    bal_min, bal_regular = start_balance, start_balance
    for r in rows_out:
        bal_min += min_income - r["expenses"]
        bal_regular += r["regular_income"] - r["expenses"]
        r["balance_min_income"] = round(bal_min, 2)
        r["balance_regular_income"] = round(bal_regular, 2)
    total = round(sum(r["expenses"] for r in rows_out), 2)
    return {
        "from": keys[0], "to": keys[-1], "accounts": account_ids, "floor": floor,
        "start_balance": start_balance, "months": rows_out,
        "basis": {"months": len(basis), "from": basis[0] if basis else None, "to": basis[-1] if basis else None,
                  "variable_monthly": variable},
        "fixed_monthly": rec["totals"]["expense"]["monthly"],
        "expenses_total": total, "expenses_monthly": round(total / horizon, 2),
        "min_income": min_income, "tight_month": tight,
        "cover_income": round(max(cover), 2) if cover else 0.0,
        "regular_income_monthly": rec["totals"]["income"]["monthly"],
        "lowest_regular": min((r["balance_regular_income"], r["month"]) for r in rows_out) if rows_out else None,
        "plans": plans(con),
    }
