# Financie — lokálna správa osobných financií

Všetko beží len na tvojom počítači. Žiadne pripojenie k banke, žiadne dáta tretím stranám.
Výpisy sťahuješ ručne z internet bankingu a importuješ ich sem.

## Ako to funguje

- **SQLite databáza** (`financie.db`) — všetky dáta v jednom súbore v tomto priečinku. Zálohuj ho.
- **Web appka** — beží na `http://localhost:8500`, len pre teba.
- **Gemma cez Ollama** — lokálny AI model číta PDF výpisy, screenshoty a skeny a kategorizuje transakcie. CSV a Excel sa spracujú priamo kódom, bez AI.

## Prvé spustenie

1. **Python (miniconda)** — otvor *Anaconda Prompt (miniconda3)* zo Štart menu a vytvor prostredie:
   ```
   conda create -n financie python=3.12 -y
   conda activate financie
   pip install -r C:\AI\Claude\requirements.txt
   ```
   (`start.bat` si prostredie `financie` nájde sám, minicondu netreba pridávať do PATH.)
3. **Ollama + Gemma** — nainštaluj [Ollama](https://ollama.com/download/windows) a stiahni model:
   ```
   ollama pull gemma4:e4b
   ```
   (Voliteľne aj `ollama pull gemma4:26b-a4b-it-qat` — presnejší 26B MoE model, 16 GB, pre GPU s dostatkom VRAM.)
4. **Spusti appku** — dvojklik na `start.bat`, alebo v termináli `python app.py`. Otvorí sa `http://localhost:8500`.

## Používanie

- **Import** — stiahni výpis z banky (CSV/XLSX ideálne, PDF alebo screenshot tiež funguje) a nahraj ho v záložke *Import*. Duplicity sa preskočia, takže sa neboj importovať s presahom.
- **Transakcie** — skontroluj automatické kategórie, oprav čo treba. Appka si pamätá tvoje opravy: rovnakú protistranu nabudúce zaradí sama, bez AI.
- **Rozpočet** — nastav mesačné limity na kategórie (Enter uloží).
- **Majetok** — pridaj aktíva (byt, ETF, krypto…) a záväzky (hypotéka — zadávaj zápornú hodnotu). Raz za čas ulož aktuálne ocenenie; graf ukáže vývoj čistého imania.

## Súkromie

Kto vidí tvoje dáta: bankové súbory a databázu spracúva len tvoj počítač — deterministický Python kód a lokálna Gemma cez Ollamu. Nič sa neposiela do cloudu. Claude (v Cowork/Code) vidí len súbory v priečinku, ktorý mu sprístupníš. Ak chceš mať istotu, ulož databázu mimo zdieľaného priečinka:

```
set FINANCIE_DB=C:\Financie\financie.db
python app.py
```

A výpisy importuj z ľubovoľného iného priečinka (napr. Stiahnuté) — do zdieľaného ich nemusíš kopírovať.

## Nastavenia (voliteľné)

Iný model alebo adresa Ollamy sa dá zmeniť cez premenné prostredia:

```
set GEMMA_MODEL=gemma4:26b-a4b-it-qat
set OLLAMA_URL=http://localhost:11434
python app.py
```

## Súbory

| Súbor | Účel |
|---|---|
| `app.py` | web server a API |
| `db.py` | databázová schéma a výpočty |
| `importer.py` | spracovanie výpisov (CSV/XLSX/PDF/obrázky) |
| `gemma.py` | komunikácia s lokálnym modelom cez Ollama |
| `static/index.html` | dashboard |
| `financie.db` | tvoje dáta (vznikne pri prvom spustení) |
