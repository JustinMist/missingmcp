#!/usr/bin/env python3
"""Live monitor for the Garmin MCP Gateway.

Pretty-prints the gateway's structured JSON events (mcp-request, worker-spawn,
token-issued, authorize-*, *-failed, ...) from the log file as they happen, so
you can watch what's going on in real time.

Usage:
  python scripts/monitor.py                 # follow .localdata/gateway.log
  python scripts/monitor.py --lines 50      # show last 50 events, then follow
  python scripts/monitor.py --all           # also show garminconnect/urllib3 noise
  python scripts/monitor.py --no-follow      # just print and exit
  python scripts/monitor.py --file /data/gateway.log

The log file must exist — set GATEWAY_LOG_FILE when running the gateway
(it's in .env for local dev: ./.localdata/gateway.log).
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from collections import deque

C = {
    "info": "\033[36m", "warn": "\033[33m", "error": "\033[31m",
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m", "green": "\033[32m",
}

# Events worth highlighting in green (something good completed).
GOOD = {"token-issued", "mfa-verify-ok", "login-verify-ok", "worker-started",
        "worker-ensure-ok", "authorize-finish", "gateway-started"}


def use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def format_line(line: str, show_all: bool, color: bool) -> str | None:
    line = line.rstrip("\n")
    if not line:
        return None
    if line.startswith("{"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            return line if show_all else None
        ts = r.get("ts", "")
        lvl = r.get("level", "info")
        ev = r.get("event", "")
        extra = " ".join(
            f"{k}={v}" for k, v in r.items() if k not in ("ts", "level", "event")
        )
        if not color:
            return f"{ts}  {lvl:5}  {ev:22}  {extra}".rstrip()
        lc = C.get(lvl, "")
        ec = C["green"] + C["bold"] if ev in GOOD else lc
        return (f"{C['dim']}{ts}{C['reset']}  {lc}{lvl:5}{C['reset']}  "
                f"{ec}{ev:22}{C['reset']}  {C['dim']}{extra}{C['reset']}").rstrip()
    # plain stdlib line (garminconnect / urllib3 DEBUG)
    if not show_all:
        return None
    return f"{C['dim']}{line}{C['reset']}" if color else line


def follow(path: str, args, color: bool):
    # print the tail history first
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=args.lines)
    except FileNotFoundError:
        sys.exit(f"log file not found: {path}\n"
                 f"Run the gateway with GATEWAY_LOG_FILE set (see .env).")
    for line in tail:
        out = format_line(line, args.all, color)
        if out is not None:
            print(out)
    if not args.follow:
        return
    # then follow new lines
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.3)
                continue
            out = format_line(line, args.all, color)
            if out is not None:
                print(out, flush=True)


def main():
    p = argparse.ArgumentParser(description="Live monitor for the Garmin MCP Gateway log.")
    default = os.environ.get("GATEWAY_LOG_FILE", "./.localdata/gateway.log")
    p.add_argument("--file", default=default, help=f"log file (default: {default})")
    p.add_argument("--lines", type=int, default=20, help="history lines to show first (default: 20)")
    p.add_argument("--all", action="store_true", help="also show garminconnect/urllib3 noise")
    p.add_argument("--no-follow", dest="follow", action="store_false", help="print and exit")
    args = p.parse_args()
    try:
        follow(args.file, args, use_color())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
