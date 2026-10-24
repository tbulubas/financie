# Financie — project guide for Claude

Local-first personal finance manager. Everything runs on the user's machine;
no bank connections, no cloud APIs, no data to third parties. That constraint
is the reason this project exists — treat it as non-negotiable.

## Privacy rules (read first)

- **Never read `financie.db`, `imports/`, `*.csv`, `*.xls*` or statement PDFs.**
  They contain the user's real bank data. Work on the code, not the data.
- If a parsing bug needs real input, ask the user to paste the **header row
  only**, or to provide an anonymized sample — do not open the file yourself.
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
- **Dedup** via `import_hash` (account + date + amount + description).
- **i18n:** all UI strings live in the `I18N` object in `index.html` (sk/de/en).
  Add every new string to all three. Backend returns error *codes*
  (`{"code": "..."}`), never user-facing text; the frontend translates them.
- **Language:** talk to the user in Slovak. All code, comments, docstrings,
  log messages, LLM prompts, the README and other docs are in English.
  Exceptions: the `sk`/`de` UI strings in `I18N`, and values stored in the DB
  (default category names, `accounts.type` / `assets.type` codes such as
  `bezny`, `ine`) — renaming those would break existing data.
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

- German bank CSV-CAMT V2 export (German headers, `;`, cp1252, `dd.mm.yyyy`, `1.234,56`)
- Credit card statement — legacy BIFF `.xls` (OLE), header row ~11, preamble rows above
- PDF and screenshots — parsed by Gemma (vision for scans without a text layer)
