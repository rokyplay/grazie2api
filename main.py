#!/usr/bin/env python3
"""grazie2api Proxy -- entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from src.config import load_settings
from cli.commands import serve, cli_login, cli_list, cli_remove, cli_stats, cli_add_from_json


def main():
    parser = argparse.ArgumentParser(
        description="grazie2api — Grazie AI API proxy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Subcommands:
  serve        Start the API proxy server (default)
  login        Run browser OAuth and store credential
  list         List credentials
  remove <id>  Remove a credential
  stats        Print aggregated stats
  add          Import credential(s) from a JSON file

Examples:
  python main.py serve --port 8800 --strategy round_robin
  python main.py list
  python main.py remove cred-abc123
  python main.py add --file ./credentials.json --label my-acc
""",
    )

    sub = parser.add_subparsers(dest="command")

    sp = sub.add_parser("serve", help="Start the FastAPI proxy server")
    sp.add_argument("--port", type=int, default=8800)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--api-key", default="")
    sp.add_argument("--strategy", default="round_robin",
                    choices=["round_robin", "least_used", "most_quota"])
    sp.add_argument("--trusted-host", action="append", default=[],
                    help="Additional trusted hostnames (repeatable)")

    lp = sub.add_parser("login", help="Browser OAuth login, store credential")
    lp.add_argument("--license-id", default="")

    sub.add_parser("list", help="List credentials")

    rp = sub.add_parser("remove", help="Remove a credential by id")
    rp.add_argument("cred_id")

    stp = sub.add_parser("stats", help="Print aggregated stats")
    stp.add_argument("--hours", type=int, default=24)

    ap = sub.add_parser("add", help="Add credential from JSON file")
    ap.add_argument("--file", required=True, help="JSON file with refresh_token")
    ap.add_argument("--label", default="")
    ap.add_argument("--license-id", default="")

    args = parser.parse_args()
    settings = load_settings()

    if args.command is None:
        args = parser.parse_args(["serve"])

    cmd = args.command
    if cmd == "serve":
        serve(args, settings)
    elif cmd == "login":
        sys.exit(cli_login(args, settings))
    elif cmd == "list":
        cli_list(settings)
    elif cmd == "remove":
        sys.exit(cli_remove(args.cred_id, settings))
    elif cmd == "stats":
        cli_stats(hours=args.hours, settings=settings)
    elif cmd == "add":
        sys.exit(cli_add_from_json(Path(args.file), label=args.label, license_id=args.license_id, settings=settings))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
