#!/usr/bin/env python3
"""Status / stats snapshot for the Garmin MCP Gateway (reads the DB read-only).

Shows how many people have a token, how many devices/clients are connected, the
registered OAuth clients, and per-account token counts. Safe to run while the
gateway is live (opens the SQLite DB read-only).

Usage:
  python scripts/status.py                 # uses ./.localdata/gateway.db
  python scripts/status.py --db /data/gateway.db
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys


def main():
    default = os.environ.get("DB_PATH") or os.path.join(
        os.environ.get("DATA_DIR", "./.localdata"), "gateway.db"
    )
    p = argparse.ArgumentParser(description="Garmin MCP Gateway status snapshot.")
    p.add_argument("--db", default=default, help=f"SQLite DB path (default: {default})")
    args = p.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"DB not found: {args.db}\nSet --db or DATA_DIR/DB_PATH.")

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    one = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731

    accounts = one("SELECT COUNT(*) FROM garmin_accounts")
    tokens = one("SELECT COUNT(*) FROM access_tokens")
    people = one("SELECT COUNT(DISTINCT garmin_user_key) FROM access_tokens")
    clients = one("SELECT COUNT(*) FROM oauth_clients")
    pending = one("SELECT COUNT(*) FROM oauth_codes")

    print(f"\nGarmin MCP Gateway — status  ({args.db})\n")
    print("Summary")
    print(f"  People with a token : {people}")
    print(f"  Access tokens       : {tokens}   (devices/clients connected)")
    print(f"  Garmin accounts     : {accounts}")
    print(f"  OAuth clients       : {clients}   (registered apps)")
    print(f"  Pending auth codes  : {pending}")

    # per-account: token count + last use
    rows = db.execute(
        """
        SELECT a.garmin_user_key AS key, a.created_at AS created,
               COUNT(t.token_hash) AS tokens, MAX(t.last_used) AS last_used
        FROM garmin_accounts a
        LEFT JOIN access_tokens t ON t.garmin_user_key = a.garmin_user_key
        GROUP BY a.garmin_user_key ORDER BY a.created_at
        """
    ).fetchall()
    if rows:
        print("\nAccounts")
        for r in rows:
            print(f"  {r['key']:<32} tokens: {r['tokens']:<3} "
                  f"connected: {r['created']}  last used: {r['last_used'] or '—'}")

    crows = db.execute(
        "SELECT client_name, redirect_uris, created_at FROM oauth_clients ORDER BY created_at"
    ).fetchall()
    if crows:
        print("\nOAuth clients (registered)")
        for r in crows:
            name = r["client_name"] or "(unnamed)"
            print(f"  {name:<32} {r['redirect_uris']}  {r['created_at']}")
    print()


if __name__ == "__main__":
    main()
