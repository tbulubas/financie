# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Command line interface: start the local web app, try it on invented data, or inspect
statement files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

def inspect(paths: list[str]) -> None:
    from financie import importer

    # UTF-8 also when redirected to a file (> inspect.out), not the Windows ANSI code page
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if importer.file_kind(f.name))
        else:
            files.append(p)
    for f in files:
        try:
            print(importer.format_report(importer.inspect_file(f.name, f.read_bytes())))
        except Exception as e:  # one broken file must not hide the others
            print(f"== {f.name}\nstatus:   error: {type(e).__name__}: {e}")
        print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="financie", description="Local personal finance manager.")
    # 127.0.0.1 only: the app has no authentication and must not be reachable from the network
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8500, help="port (default 8500)")
    sub = parser.add_subparsers(dest="command")
    p_inspect = sub.add_parser(
        "inspect", help="show the structure of statement files (columns, formats), not their content")
    p_inspect.add_argument("paths", nargs="+", help="files or folders")
    p_demo = sub.add_parser(
        "demo", help="try the app on invented data in a temporary folder (your database is not used)")
    p_demo.add_argument("--port", type=int, default=8501, help="port (default 8501)")
    args = parser.parse_args(argv)

    if args.command == "inspect":
        inspect(args.paths)
        return
    if args.command == "demo":
        from financie import demo
        demo.run(args.host, args.port)
        return

    import uvicorn

    from financie.app import app

    uvicorn.run(app, host=args.host, port=args.port)
