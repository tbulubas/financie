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
- Screenshots for the README come from the demo (`make demo`, `demo.py`) only —
  never from the user's own app. The demo sets `db.DB_PATH` / `db.IMPORTS_DIR` to a
  new temporary folder, so it cannot touch the real database. Keep the folder path,
  the user name and browser add-ons out of the pictures.
- The README and LICENSE write the e-mail as `taras.bulubas [at] gmail.com`.
- `.gitignore` blocks the database, statements (also `*.xml` / `*.zip` for CAMT, except
  `.run/*.xml`) and `.idea/` — keep it that way.
  Before any commit, confirm `git status` shows code files only.

## Stack

- Python 3.12, FastAPI + uvicorn, SQLite, vanilla HTML/JS (no build step)
- Chart.js 4.4.4 bundled in `static/vendor/` (from the npm tarball, with its MIT
  licence), served under `/static`: the page loads nothing from the internet
- Local LLM: Gemma 4 via Ollama at `http://localhost:11434`
  (`gemma4:e4b` default, `gemma4:26b-a4b-it-qat` for better accuracy)
- Runs at `http://localhost:8500`
- Environment variables all start with `FINANCIE_`: `FINANCIE_DB`,
  `FINANCIE_IMPORTS`, `FINANCIE_GEMMA_MODEL`, `FINANCIE_OLLAMA_URL`
  (`gemma.py` still falls back to the old `GEMMA_MODEL` / `OLLAMA_URL`)

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
| `src/financie/demo.py` | `python -m financie demo` / `make demo`: invented data in a new temp folder |
| `docs/images/` | README screenshots, taken from the demo only |
| `src/financie/static/index.html` | entire frontend: markup, CSS, JS, i18n |
| `src/financie/static/vendor/` | Chart.js 4.4.4 and its licence, served under `/static` |
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
  The reference is also stored in `transactions.ref` (schema v4): a row whose
  reference is already in the account is a duplicate even when another export
  format worded it differently.
- **Several files at once.** `POST /api/import/preview` runs
  `describe_statement()` on each file (format, period, count, source key and a
  proposed account; no writes, no Gemma), the UI then imports file by file via
  `POST /api/import`. A successful import stores source key → account in
  `account_sources`, so the next download of the same source is assigned by
  itself. Source keys never contain an account number in clear (`_hashed()`).
- **Statements folder and import history.** `GET /api/folder` lists the
  statement files in the folder from `settings.imports_dir` (default
  `FINANCIE_IMPORTS`) with the same preview plus a state from the `imports`
  table: `new`, `imported`, `changed` (same name, other content) or `failed`,
  judged by the sha256 of the content. Listing writes nothing; the UI imports
  only the ticked rows on GO, via `POST /api/folder/import` (paths relative to
  the folder, nothing outside it). Every import attempt, failed ones too, is a
  row in `imports`, and `transactions.import_id` points to it.
  A file already imported into the same account is refused
  (`already_imported`): dedup cannot catch a second Gemma read. `GET
  /api/imports` reports per import the transactions that match an earlier
  import by date and amount (transfers excluded), `GET
  /api/imports/{id}/duplicates` lists those pairs (clicking ⚠ in the history
  shows them, with "→ transfer" and ✕ per side), and `DELETE
  /api/imports/{id}` undoes an import (status `undone`, file is `new` again).
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
- **Main account.** `accounts.is_main` marks the account salaries go to (schema
  v3); the rest are satellites (PayPal, cards). `GET /api/overview?accounts=1,2`
  limits the overview to those accounts and returns `transfers`, the sum of
  their transfer-category amounts: pairs inside the selection cancel out, so it
  is the money moved to or from the other accounts. The UI defaults to the main
  account(s) and remembers the choice in `localStorage`.
- **Known balance.** Histories are incomplete (the bank exports only recent
  months), so an account can hold a known balance at the end of a day
  (`balance_date`, `balance_amount`, schema v5, set with ✎ on the Import tab via
  `PATCH /api/accounts/{id}`): its balance is that amount plus the later
  movements, so importing older history never moves it. Without one, the balance
  is `opening_balance` plus everything. `account_balances()` also returns
  `first_date` / `last_date`, and the overview warns when a chosen account has no
  data at the start of the period.
- **Balance over time** (`GET /api/balances`, `db.balance_history()`): end-of-day
  balance per chosen account for the overview's period, a point on the first day and
  on each day with movements (a stepped line), the balance checks of the period with
  the difference computed live, and a total that starts once every account with data
  has started. Nothing is drawn before an account's first movement or known balance.
- **Holes in a history** (`db.Ledger`, used by every balance: `balance_at()`, checks,
  `running_balances()`, the chart): no movement for more than `GAP_DAYS` (45) splits the
  history into stretches. Going back from the known balance, an older stretch whose latest
  balance check disagrees with the chain is anchored to that check (the unknown movements of
  the hole would otherwise shift every older balance); a stretch that agrees or has no
  check stays chained, and newer stretches always chain from the known balance, so the
  current balance never moves. The chart breaks the line there (`balance: null`), the
  overview and the import result name the hole (`gaps`, `db.balance_gaps()`).
- **Transactions period.** Year and month lists (`txPeriod` in `index.html`), not
  `<input type="month">`, whose year cannot be changed on its own. `GET
  /api/transactions/years` lists the years with data; `GET /api/transactions` takes
  `month` (`YYYY-MM`) or `year` (the UI raises `limit` for a whole year).
- **Overview panels** (`[data-panel]` in `#tab-overview`): the 📊 menu hides or shows
  them, clicking a title collapses it; `applyPanels()` keeps both in `localStorage`
  (`ovPanels`). A new chart on the overview gets a `data-panel` and a menu entry.
- **Balance checks** (`balance_checks`, schema v6, ⚖ on the Import tab, `POST
  /api/accounts/{id}/checks`): the real balance at the end of a day next to
  `balance_at()`; the difference is computed live, so fixing a missing or doubled
  movement clears it. They never move the known balance. `GET /api/transactions`
  with `account_id` adds `running_balance` (by date, then id) for finding the day
  a difference starts. A statement that states the balance (PayPal "Guthaben", CAMT
  balances) passes it as `tx["day_balance"]` on a transaction of each day it states
  (PayPal: the last full day, `_paypal_day_balance()`, robust to rows of one second in
  any order). `_statement_balance()` makes the latest the known balance of an account
  without one and records every other day as a check (`db.add_balance_checks()`); the
  import result says on how many of those days the app is off and from which day.

## Running and testing

```bash
make venv      # .venv with Python 3.12 via the py launcher, editable install with [dev]
make test
make run       # or start.bat, which runs .venv\Scripts\python -m financie
```

The user is on Windows 11 and has moved from Miniconda to the local `.venv`;
do not use or suggest conda. Dependencies live in `pyproject.toml` only (no
`requirements.txt`); after changing them run `make venv`.

Tests must never touch the real DB or statements: `conftest.py` drops all
`FINANCIE_*` variables before the package is imported (the user has them set
to the real database and statements folder), the autouse `temp_db` fixture points
`db.DB_PATH` at `tmp_path` and clears `db.IMPORTS_DIR`, and the `client` fixture starts the app inside
`TestClient` so the lifespan creates the schema there. Sample statements are
built inline in the tests (`*.csv`/`*.xlsx` files are gitignored anyway).

## Known data sources

Recognized by their columns in `PROFILES` (`importer.py`); the label shows in
`inspect` and in the import result.

- German savings bank (Sparkasse) — CSV-CAMT V2, camt52v8 and MT940 CSV
  (`;`, cp1250/UTF-8, `dd.mm.yy`, `1.234,56`). Rows with Info "Umsatz
  vorgemerkt" (pending) are left out. The formats word the same movement
  differently, so dedup does not catch them across formats: the import reports
  `possible_duplicates` instead. The Excel export (`*-umsatz-camt52v8.xlsx`) leaves
  the amount (and the payment details) of some real movements empty; those dated rows
  are counted as `no_amount` (`_rows_without_amount()`) in the preview and the import
  result, and `inspect` sums them up in `unread:` / `types:` (generic booking types
  only). Prefer CSV or CAMT for such a period.
- PayPal activity CSV (DE, `,`, `dd.mm.yyyy`). Only completed EUR rows that move
  the balance (no "Memo", no "Autorisierung"); EUR currency-conversion rows take
  the payee of the related payment; funding from bank/card is a transfer
  (`PAYPAL_FUNDING`, e.g. "Allgemeine Abbuchung von Kreditkarte" for a
  card-funded payment, negative for its refund going back to the card), and
  `_link_funding_transfers()` marks the matching bank debit as a transfer too.
  Funding counts while still "Ausstehend" (else the bank debit counts as a
  second expense); a later export with that funding "Storniert"/"Fehlgeschlagen"
  deletes it again (`void`, matched by hash or `ref`). The transaction code goes
  into the dedup hash and `transactions.ref`.
- PayPal balance report "Guthaben-relevante Zahlungen" (profile `paypal_balance`):
  same parser, the type is in "Beschreibung", no Status / Auswirkung columns, so
  every row counts. Same source key `paypal` as the activity export.
- Credit card CSV "Umsatz getätigt von … Buchungsbetrag" (`;`, cp1250, `dd.mm.yy`).
- Amazon Visa — legacy BIFF `.xls`, header row 11, preamble rows above.
- Barclays credit card `.xlsx` (profile `barclays`) — header row 13, two
  "Buchungsdatum" columns, amounts like `-9,99 €`. Only booked rows (Status
  "abgerechnet"; known pending words in `BARCLAYS_PENDING` are left out). A
  "Gutschrift" "Per Lastschrift dankend erhalten" is the repayment from the bank
  account: a transfer, and `_link_funding_transfers()` (words in
  `FUNDING_PARTNERS`) marks the "Barclays …" bank debit as a transfer too. A
  numeric Referenznummer is the `ref`; repayments and fees have none.
- CAMT XML (`.xml`, or a `.zip` of them): camt.052 / camt.053, any version, parsed by
  `_camt_transactions()` without the LLM. Booked entries only (`Sts` BOOK); a batch entry stays
  one transaction with its total. Description = `Ustrd` (the CSV's Verwendungszweck), `ref` =
  `AcctSvcrRef`, source key = the Sparkasse CSV key of the same IBAN, so switching format keeps
  the account. Entries are flagged `other_formats`: `import_transactions()` treats a row without
  `ref` from another file with the same date and amount as the same movement (one to one), so
  months already imported as CSV are not doubled. The other way round too: once an account has rows from a
  CAMT import (`imports.format` 'CAMT …'), a row without `ref` is skipped while the account has
  more rows with its date and amount than the file brought so far (a multiset match), so a
  CSV downloaded after CAMT adds only what is new. Accounts without CAMT keep the old rule
  (reworded rows are flagged as `possible_duplicates`). The closing/interim booked balance (CLBD /
  ITBD, checked against OPBD / PRCD when present) gives `day_balance` for the last complete day
  before the report's `CreDtTm` (`_camt_balance()`). The Sparkasse export "camt52v8Booked" is a
  ZIP with one camt.052.001.08 report per booking day, each with OPBD and CLBD, so every day
  is checked. `inspect` shows the balance types and whether the entries add up, never amounts.
- PDF and screenshots — parsed by Gemma (vision for scans without a text layer).
