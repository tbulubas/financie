# Financie — project guide for Claude

Local-first personal finance manager. Everything runs on the user's machine;
no bank connections, no cloud APIs, no data to third parties. That constraint
is the reason this project exists — treat it as non-negotiable.

## Privacy rules (read first)

- **Never read `financie.db`, `imports/`, `*.csv`, `*.xls*` or statement PDFs.**
  They contain the user's real bank data. Work on the code, not the data.
- If a parsing bug needs real input, ask the user to run
  `.venv\Scripts\python -m financie inspect <file or folder>` and paste the
  output: it shows format, encoding, column names and value *shapes* only
  (`<date:%d.%m.%Y>`, `-9.999,99`, `<text>`), never content. Do not run it on
  the user's files yourself and do not open the files.
- Never add real transaction data to tests, fixtures or commits. Invent samples.
- `.gitignore` blocks the database, statements and `.idea/` — keep it that way.
  Before any commit, confirm `git status` shows code files only.

## Stack

- Python 3.12, FastAPI + uvicorn, SQLite, vanilla HTML/JS (no build step)
- Local LLM: Gemma 4 via Ollama at `http://localhost:11434`
  (`gemma4:e4b` default, `gemma4:26b-a4b-it-qat` for better accuracy)
- Runs at `http://localhost:8500`

## Layout

Follows the same standard as the user's `PlanningRopeBridge` and
`PlanningLinesSweeping` projects: `src/` package, `pyproject.toml`, Makefile,
pytest, shared IntelliJ run configs, copyright header in every source file.

| File | Purpose |
|---|---|
| `src/financie/app.py` | FastAPI routes + static serving; `init_db()` runs in the lifespan, not on import |
| `src/financie/db.py` | schema, connection, balance/net-worth queries; `DB_PATH` defaults to the project root |
| `src/financie/importer.py` | statement parsing (CSV/XLSX/XLS/PDF/images), dedup, categorization |
| `src/financie/gemma.py` | Ollama client — structured JSON output, vision for scans |
| `src/financie/cli.py` | `python -m financie [--host] [--port]` |
| `src/financie/static/index.html` | entire frontend: markup, CSS, JS, i18n |
| `tests/` | pytest suite; `conftest.py` gives every test a temp DB and disables Ollama |
| `scripts/check_venv.py` | guards the Python version of `.venv` (shared with the other projects) |
| `Makefile` | `venv`, `test`, `run`, `clean`, `distclean`, `recreate` |
| `.run/`, `docs/intellij.md` | IntelliJ run configurations and setup |

## Conventions

- **Money math is deterministic.** The LLM never computes totals or balances —
  it only parses text and suggests categories. Keep it that way.
- **Deterministic parsing first.** CSV/XLSX are parsed by code via
  `COLUMN_ALIASES`; Gemma is the fallback for unknown layouts only. When a new
  bank format fails, add its header names to the aliases rather than leaning on
  the LLM.
- **Import must stay fast.** Inserting transactions does not call Gemma.
  AI categorization is user-triggered via `POST /api/categorize`.
- **Learned categories beat the LLM.** `_learned_category()` reuses the user's
  own past corrections by counterparty/description — check it before any AI call.
- **Dedup** via `import_hash` (account + date + amount + description, plus the
  source's own reference such as the PayPal transaction code when there is one).
- **Several files at once.** `POST /api/import/preview` runs
  `describe_statement()` on each file (format, period, count, source key and a
  proposed account; no writes, no Gemma), the UI then imports file by file via
  `POST /api/import`. A successful import stores source key → account in
  `account_sources`, so the next download of the same source is assigned by
  itself. Source keys never contain an account number in clear (`_hashed()`).
- **i18n:** all UI strings live in the `I18N` object in `index.html` (sk/de/en).
  Add every new string to all three. Backend returns error *codes*
  (`{"code": "..."}`), never user-facing text; the frontend translates them.
- **Language:** talk to the user in Slovak. All code, comments, docstrings,
  log messages, LLM prompts, the README and other docs are in English.
  The database is English too: `accounts.type` / `assets.type` codes
  (`checking`, `credit_card`, `liability`, …) and default category names.
  Only the `sk`/`de` UI strings in `I18N` and the users' own data (bank texts,
  their own account and category names) stay in other languages.
- **Default categories** have a stable `categories.key` (`groceries`,
  `transfer`, …); the UI shows `cat_<key>` from `I18N` via `catLabel()` and
  falls back to `name` for user-defined categories (`key IS NULL`). A new
  default category needs a key in `DEFAULT_CATEGORIES` and `cat_<key>` in all
  three languages.
- **Schema changes go through migrations.** `PRAGMA user_version` holds the
  version (`SCHEMA_VERSION` in `db.py`); `init_db()` backs the database up next
  to itself (`<stem>-backup-v<old>-<timestamp>.db`) and then runs the
  migration steps. Never require the user to re-import; keep row ids so
  categorized transactions, budgets and learned categories survive. Test each
  migration on an invented old-layout database (see `tests/test_migration.py`).
- Transactions in a category with `kind='transfer'` are excluded from income
  and expense totals (used for credit-card settlements).

## Running and testing

```bash
make venv      # .venv with Python 3.12 via the py launcher, editable install with [dev]
make test
make run       # or start.bat, which runs .venv\Scripts\python -m financie
```

The user is on Windows 11 and has moved from Miniconda to the local `.venv`;
do not use or suggest conda. Dependencies live in `pyproject.toml` only (no
`requirements.txt`); after changing them run `make venv`.

Tests must never touch the real DB: the autouse `temp_db` fixture points
`db.DB_PATH` at `tmp_path`, and the `client` fixture starts the app inside
`TestClient` so the lifespan creates the schema there. Sample statements are
built inline in the tests (`*.csv`/`*.xlsx` files are gitignored anyway).

## Known data sources

Recognized by their columns in `PROFILES` (`importer.py`); the label shows in
`inspect` and in the import result.

- German savings bank (Sparkasse) — CSV-CAMT V2, camt52v8 and MT940 CSV
  (`;`, cp1250/UTF-8, `dd.mm.yy`, `1.234,56`). Rows with Info "Umsatz
  vorgemerkt" (pending) are left out. The formats word the same movement
  differently, so dedup does not catch them across formats: the import reports
  `possible_duplicates` instead.
- PayPal activity CSV (DE, `,`, `dd.mm.yyyy`). Only completed EUR rows that move
  the balance (no "Memo"); EUR currency-conversion rows take the payee of the
  related payment; funding from bank/card is a transfer, and
  `_link_paypal_transfers()` marks the matching bank debit as a transfer too.
  The transaction code goes into the dedup hash (`ref`).
- Credit card CSV "Umsatz getätigt von … Buchungsbetrag" (`;`, cp1250, `dd.mm.yy`).
- Amazon Visa — legacy BIFF `.xls`, header row 11, preamble rows above.
- PDF and screenshots — parsed by Gemma (vision for scans without a text layer).
