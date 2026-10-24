# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Command line interface: start the local web app."""

from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="financie", description="Local personal finance manager.")
    # 127.0.0.1 only: the app has no authentication and must not be reachable from the network
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8500, help="port (default 8500)")
    args = parser.parse_args(argv)

    from financie.app import app

    uvicorn.run(app, host=args.host, port=args.port)
