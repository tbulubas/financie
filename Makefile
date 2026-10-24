# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

# Development tasks for the Financie project (GNU make).
#
# Works on Windows (recipes run in cmd.exe, also when make is started from
# PowerShell or Git Bash) and on Linux/macOS.
#
#   make venv      create .venv and install the project with all extras
#   make test      run the tests
#   make run       start the web app at http://localhost:8500 (override with ARGS="...")
#
# The project uses Python 3.12 (PYTHON_VERSION). The base interpreter is found
# with the py launcher on Windows and as python3.12 elsewhere, independently of
# which python comes first on the PATH (Miniconda included).

PYTHON_VERSION := 3.12

ifeq ($(OS),Windows_NT)
    SHELL := cmd.exe
    .SHELLFLAGS := /c
    PYTHON := py -$(PYTHON_VERSION)
    VENV_PY := .venv\Scripts\python.exe
else
    PYTHON := python$(PYTHON_VERSION)
    VENV_PY := .venv/bin/python
endif

VENV_CFG := .venv/pyvenv.cfg
STAMP := .venv/.installed
ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help venv test run clean distclean recreate

help:
	@echo Targets:
	@echo   make venv       create .venv and install the project with all extras
	@echo   make test       run the tests with pytest
	@echo   make run        start the web app at http://localhost:8500; options with ARGS="--port ..."
	@echo   make clean      remove caches and build metadata
	@echo   make distclean  clean and remove .venv
	@echo   make recreate   distclean, then venv
	@echo Python $(PYTHON_VERSION), base interpreter: $(PYTHON)

# .venv is created only once. Re-creating an existing one would overwrite its
# python.exe, which fails while an IDE or the running app is using it.
$(VENV_CFG):
	$(PYTHON) scripts/check_venv.py $(PYTHON_VERSION)
	$(PYTHON) -m venv .venv

# The stamp file is rewritten after every install, so the packages are
# refreshed whenever pyproject.toml or this Makefile changes. An existing
# .venv of another Python version is never upgraded in place: check_venv.py
# stops with a hint to run make recreate.
$(STAMP): pyproject.toml Makefile $(VENV_CFG)
	$(PYTHON) scripts/check_venv.py $(PYTHON_VERSION)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -e ".[dev]"
	$(VENV_PY) -c "open('$(STAMP)', 'w').close()"

venv: $(STAMP)

test: $(STAMP)
	$(VENV_PY) -m pytest

run: $(STAMP)
	$(VENV_PY) -m financie $(ARGS)

clean:
	$(PYTHON) -c "import glob, shutil; [shutil.rmtree(p, ignore_errors=True) for p in ['.pytest_cache', 'build', *glob.glob('src/*.egg-info'), *glob.glob('*/**/__pycache__', recursive=True)]]"

distclean: clean
	$(PYTHON) -c "import shutil; shutil.rmtree('.venv', ignore_errors=True)"

recreate: distclean
	$(MAKE) venv
