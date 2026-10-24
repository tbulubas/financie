# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Client for the local Gemma model via the Ollama API (http://localhost:11434).
No data leaves your machine.
"""
import base64
import json
import os

import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("GEMMA_MODEL", "gemma4:e4b")

TX_SCHEMA = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "ISO format YYYY-MM-DD"},
                    "amount": {"type": "number", "description": "negative = expense, positive = income"},
                    "currency": {"type": "string"},
                    "description": {"type": "string"},
                    "counterparty": {"type": "string"},
                },
                "required": ["date", "amount", "description"],
            },
        }
    },
    "required": ["transactions"],
}


def available() -> bool:
    try:
        requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return True
    except requests.RequestException:
        return False


def chat(prompt: str, images: list[bytes] | None = None, json_schema: dict | None = None,
         timeout: int = 600) -> str:
    msg = {"role": "user", "content": prompt}
    if images:
        msg["images"] = [base64.b64encode(i).decode() for i in images]
    payload = {"model": MODEL, "messages": [msg], "stream": False,
               "options": {"temperature": 0.1, "num_ctx": 8192}}
    if json_schema:
        payload["format"] = json_schema
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["message"]["content"]


def extract_transactions(text: str = "", images: list[bytes] | None = None) -> list[dict]:
    """Extract transactions from statement text or images (screenshot, scan, PDF page)."""
    prompt = (
        "You are an assistant that processes bank statements. Extract ALL transactions from the input. "
        "Rules: date always as YYYY-MM-DD. Amount as a number: expense/debit negative, "
        "income/credit positive. A decimal comma means a decimal point (1 234,56 -> 1234.56). "
        "If no currency is given, use EUR. Do not invent anything — extract only what is in the input.\n\n"
    )
    if text:
        prompt += f"Statement:\n{text}"
    else:
        prompt += "The transactions are in the attached images."
    out = chat(prompt, images=images, json_schema=TX_SCHEMA)
    return json.loads(out).get("transactions", [])


def map_csv_columns(header: list[str], sample_rows: list[list[str]]) -> dict:
    """Ask the model to map the columns of an unknown CSV to our fields."""
    schema = {
        "type": "object",
        "properties": {
            "date": {"type": ["string", "null"]},
            "amount": {"type": ["string", "null"]},
            "debit": {"type": ["string", "null"]},
            "credit": {"type": ["string", "null"]},
            "description": {"type": ["string", "null"]},
            "counterparty": {"type": ["string", "null"]},
            "currency": {"type": ["string", "null"]},
            "date_format": {"type": ["string", "null"],
                            "description": "Python strptime format, e.g. %d.%m.%Y"},
        },
        "required": ["date", "description"],
    }
    prompt = (
        "This is the header and sample rows of a bank CSV export. Map the column names "
        "to the fields: date, amount (a single signed amount), or debit + credit (separate columns), "
        "description, counterparty, currency. Return the exact column names from the header, "
        "null if a column does not exist. Also determine date_format (strptime).\n\n"
        f"Header: {header}\nRows: {sample_rows[:5]}"
    )
    return json.loads(chat(prompt, json_schema=schema))


def categorize(transactions: list[dict], categories: list[str]) -> list[str]:
    """Assign a category to each transaction. Returns category names in the same order."""
    schema = {
        "type": "object",
        "properties": {
            "categories": {"type": "array", "items": {"type": "string", "enum": categories}}
        },
        "required": ["categories"],
    }
    lines = [f"{i+1}. {t.get('date','')} | {t.get('amount','')} | "
             f"{t.get('description','')} | {t.get('counterparty','')}"
             for i, t in enumerate(transactions)]
    prompt = (
        f"Assign each transaction exactly one category from this list: {categories}. "
        "Positive amounts are income, negative are expenses. Return a 'categories' array with exactly "
        f"{len(transactions)} items in the same order as the transactions.\n\n" + "\n".join(lines)
    )
    out = json.loads(chat(prompt, json_schema=schema))
    cats = out.get("categories", [])
    # pad with a fallback if the model returns fewer items
    while len(cats) < len(transactions):
        cats.append(categories[-1] if categories else "")
    return cats[: len(transactions)]
