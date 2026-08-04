"""Klient pre lokálny model Gemma cez Ollama API (http://localhost:11434).
Žiadne dáta neopúšťajú tvoj počítač.
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
                    "date": {"type": "string", "description": "ISO formát YYYY-MM-DD"},
                    "amount": {"type": "number", "description": "záporné = výdavok, kladné = príjem"},
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
    """Vytiahne transakcie z textu výpisu alebo z obrázkov (screenshot, sken, PDF stránka)."""
    prompt = (
        "Si asistent na spracovanie bankových výpisov. Zo vstupu vytiahni VŠETKY transakcie. "
        "Pravidlá: dátum vždy ako YYYY-MM-DD. Suma ako číslo: výdavok/odpísanie záporné, "
        "príjem/pripísanie kladné. Desatinná čiarka znamená desatinnú bodku (1 234,56 -> 1234.56). "
        "Ak mena nie je uvedená, použi EUR. Nič si nevymýšľaj — extrahuj len to, čo je vo vstupe.\n\n"
    )
    if text:
        prompt += f"Výpis:\n{text}"
    else:
        prompt += "Transakcie sú na priložených obrázkoch."
    out = chat(prompt, images=images, json_schema=TX_SCHEMA)
    return json.loads(out).get("transactions", [])


def map_csv_columns(header: list[str], sample_rows: list[list[str]]) -> dict:
    """Požiada model o mapovanie stĺpcov neznámeho CSV na naše polia."""
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
                            "description": "Python strptime formát, napr. %d.%m.%Y"},
        },
        "required": ["date", "description"],
    }
    prompt = (
        "Toto je hlavička a ukážkové riadky CSV exportu z banky. Priraď názvy stĺpcov "
        "k poliam: date, amount (jedna suma so znamienkom), alebo debit + credit (oddelené stĺpce), "
        "description, counterparty, currency. Vráť presné názvy stĺpcov z hlavičky, "
        "null ak stĺpec neexistuje. Urči aj date_format (strptime).\n\n"
        f"Hlavička: {header}\nRiadky: {sample_rows[:5]}"
    )
    return json.loads(chat(prompt, json_schema=schema))


def categorize(transactions: list[dict], categories: list[str]) -> list[str]:
    """Priradí kategóriu každej transakcii. Vracia zoznam názvov kategórií v rovnakom poradí."""
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
        f"Priraď každej transakcii jednu kategóriu z tohto zoznamu: {categories}. "
        "Kladné sumy sú príjmy, záporné výdavky. Vráť pole 'categories' s presne "
        f"{len(transactions)} položkami v rovnakom poradí ako transakcie.\n\n" + "\n".join(lines)
    )
    out = json.loads(chat(prompt, json_schema=schema))
    cats = out.get("categories", [])
    # doplň fallback, ak model vráti menej položiek
    while len(cats) < len(transactions):
        cats.append(categories[-1] if categories else "")
    return cats[: len(transactions)]
