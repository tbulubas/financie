# Financie — local personal finance manager

Everything runs on your own computer. No bank connection, no data sent to third parties.
You download statements manually from internet banking and import them here.

## How it works

- **SQLite database** (`financie.db`) — all data in a single file in the project folder. Back it up.
- **Web app** — runs at `http://localhost:8500`, for you only.
- **Gemma via Ollama** — a local AI model reads PDF statements, screenshots and scans, and categorizes transactions. CSV and Excel files are parsed directly by code, without AI.

## Author

**Taras Bulubas** — taras.bulubas@gmail.com

**Use of AI.** Claude (an AI assistant by Anthropic) was used to help write
the code and the documentation, following my specifications and decisions.

**License.** © 2026 Taras Bulubas. All rights reserved; see [License](#license).

## Development environment

Requires Python 3.12 and GNU make 3.82+. No conda: the project uses its own
virtual environment `.venv` in the project folder.

All dependencies are declared in [pyproject.toml](pyproject.toml):

| Group       | Packages                                     | Used for                     |
|-------------|----------------------------------------------|------------------------------|
| runtime     | `fastapi`, `uvicorn`, `python-multipart`     | the web app and file uploads |
| runtime     | `openpyxl`, `xlrd`                           | XLSX and legacy XLS statements |
| runtime     | `pymupdf`                                    | PDF statements               |
| runtime     | `requests`                                   | the local Ollama API         |
| extra `dev` | `pytest`, `httpx2`                           | tests                        |

### Getting make on Windows

make is not part of Windows. Install it once, e.g. with

```bash
winget install ezwinports.make
```

or `choco install make` (from an administrator shell). Open a new terminal
afterwards so that `make` is on the `PATH`.

### Make targets

```bash
make venv
```

creates `.venv` with **Python 3.12** and installs the project in editable
mode with the `dev` extra. Running it again only reinstalls when
`pyproject.toml` or the `Makefile` has changed.

The version is pinned in the Makefile (`PYTHON_VERSION := 3.12`). On Windows
the base interpreter is found with the py launcher (`py -3.12`), elsewhere as
`python3.12`, so it does not matter which `python` comes first on the `PATH`
(Miniconda included). If `.venv` exists with another Python version,
`make venv` stops and asks for `make recreate`.

| Target               | What it does                                               |
|----------------------|------------------------------------------------------------|
| `make` / `make help` | list the targets                                           |
| `make venv`          | create `.venv` and install the project with all extras     |
| `make test`          | run the tests with pytest                                  |
| `make run`           | start the app at `http://localhost:8500`; another port with `ARGS="--port 8501"` |
| `make clean`         | remove caches and build metadata                           |
| `make distclean`     | `clean` and remove `.venv`                                 |
| `make recreate`      | `distclean`, then `venv` — a fresh environment             |

`test` and `run` create the environment first if it does not exist. The
Makefile works from PowerShell, cmd and Git Bash on Windows as well as on
Linux/macOS.

### Without make

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
```

(On Linux/macOS use `python3.12 -m venv .venv` and `.venv/bin/python`.)

### IntelliJ IDEA

See [docs/intellij.md](docs/intellij.md): register `.venv` as the project
interpreter and use the shared run configurations in `.run/` to run the
tests and the app.

### Ollama + Gemma

Install [Ollama](https://ollama.com/download/windows) and pull the model:

```bash
ollama pull gemma4:e4b
```

Optionally also `ollama pull gemma4:26b-a4b-it-qat` — a more accurate 26B MoE
model, about 15 GB, for a GPU with 16 GB of VRAM. Without Ollama, CSV and Excel
import still work; PDF/image import and AI categorization need it.

### Reference machine

The project is developed and run on this laptop:

| Component | Specification                                              |
|-----------|------------------------------------------------------------|
| GPU       | NVIDIA GeForce RTX 5080 Laptop GPU, 16 GB GDDR7 VRAM       |
| CPU       | Intel Core Ultra 9 275HX, 24 cores                         |
| RAM       | 64 GB DDR5-5600 (2 × 32 GB)                                |
| Storage   | 2 TB NVMe SSD                                              |
| OS        | Windows 11 Pro                                             |
| Ollama    | 0.40.9                                                     |

Both models run on the GPU: `gemma4:e4b` (8.9 GB) leaves plenty of VRAM
free, `gemma4:26b-a4b-it-qat` (14.6 GB) just fits into the 16 GB. Smaller
GPUs work with `gemma4:e4b`; without a GPU Ollama falls back to the CPU,
which is much slower for PDF and image import.

## Usage

```bash
make run
```

or double-click `start.bat` (it uses `.venv` and opens the browser). Both run
`python -m financie` and serve `http://localhost:8500`.

- **Import** — download a statement from your bank (CSV/XLSX ideally; PDF or a screenshot also works) and upload it on the *Import* tab. Duplicates are skipped, so overlapping imports are safe.
- **Transactions** — review the automatic categories and fix what is needed. The app remembers your corrections: next time it assigns the same counterparty by itself, without AI.
- **Budget** — set monthly limits per category (Enter saves).
- **Assets** — add assets (apartment, ETF, crypto…) and liabilities (mortgage — enter a negative value). Record the current valuation from time to time; the chart shows how your net worth develops.

## Privacy

Who sees your data: bank files and the database are processed only by your computer — deterministic Python code and local Gemma via Ollama. Nothing is sent to the cloud. Claude (in Cowork/Code) sees only the files in the folder you give it access to. To be sure, keep the database outside the shared folder:

```
set FINANCIE_DB=C:\Financie\financie.db
make run
```

And import statements from any other folder (e.g. Downloads) — you don't need to copy them into the shared one.

## Settings (optional)

A different model or Ollama address can be set via environment variables:

```
set GEMMA_MODEL=gemma4:26b-a4b-it-qat
set OLLAMA_URL=http://localhost:11434
make run
```

## Tests

```bash
make test
```

The tests run against a throwaway database in a temporary folder, never
`financie.db`, and use invented sample statements only. Ollama is switched
off in them, so they check the deterministic parsing, dedup, learned
categories, transfer handling and balances on their own.

## Layout

```
src/financie/
  app.py              FastAPI routes and static serving
  db.py               schema, connection, balance and net-worth queries
  importer.py         statement parsing (CSV/XLSX/XLS/PDF/images), dedup, categorization
  gemma.py            Ollama client: structured JSON output, vision for scans
  cli.py              command line interface (python -m financie)
  static/index.html   the whole frontend: markup, CSS, JS, i18n
tests/                parsing and API tests with invented data
scripts/              check_venv.py guards the Python version
docs/                 IntelliJ setup
.run/                 shared IntelliJ run configurations
Makefile              environment and task shortcuts
start.bat             double-click launcher
financie.db           your data (created on first run, never committed)
LICENSE               all rights reserved
```

## License

© 2026 Taras Bulubas. **All rights reserved.**

This repository — code, documentation and images — is published for viewing
only. Using, running, copying, modifying or distributing any part of it
requires my prior written permission.

See [LICENSE](LICENSE) for the full terms. To ask for permission, write to
taras.bulubas@gmail.com.
