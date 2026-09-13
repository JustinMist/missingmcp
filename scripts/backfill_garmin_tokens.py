#!/usr/bin/env python3
"""Inspect Garmin token files and recover manager-proven pending captures.

Before the read-back fix (PR #15), `materialize()` was write-only: the worker
(garth) rotated tokens into a per-account directory below
`<DATA_DIR>/users/`
and the store never learned — on 2026-07-27, 84 of 168 garmin accounts' files
were AHEAD of their DB blob, so the next spawn replayed a spent refresh token
and forced a re-login. A different file with a newer mtime is not enough to
prove generation lineage: it may be a late write from a worker predating a
verified browser login. Historical unknown drift is therefore reported for
manual review/re-authorization and is never auto-persisted.

A file is persisted into the store only when ALL of:
  - WorkerManager left a private pending-capture marker after confirming that
    process exited and a store write failed,
  - the marker's expected-generation digest matches the current DB blob,
  - it exists and parses as JSON (a torn write is never persisted),
  - its content differs from the decrypted DB blob,
  - its mtime is NEWER than the account's `updated_at` — a re-login that
    happened after the file was written must win (the same "store wins on
    unknown provenance" rule the gateway itself follows after a restart).

SAFE BY DEFAULT — dry-run prints aggregate counts and masked account keys,
persists nothing. Pass --apply only to recover manager-proven pending captures.
WAL + compare-and-swap protect a concurrent re-login; unknown historical drift
remains fail-closed regardless of mtime. Re-running is idempotent.

Usage (production):
  railway ssh --service gateway "python3 /app/scripts/backfill_garmin_tokens.py"          # dry-run
  railway ssh --service gateway "python3 /app/scripts/backfill_garmin_tokens.py --apply"
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

# Make `missingmcp` importable from a checkout (src/ layout), not only installed.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from missingmcp import store                     # noqa: E402
from missingmcp.adapters.garmin.blob import (    # noqa: E402
    GarminBlobError, is_legacy_blob, normalize_tokens_json, pack_blob, unpack_blob,
)
from missingmcp.workers import _SAFE, account_dir_name  # noqa: E402

_PENDING_CAPTURE_FILE = ".missingmcp-pending-capture.json"


def resolve_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def _updated_at_epoch(s: str) -> float:
    """SQLite datetime('now') string ("YYYY-MM-DD HH:MM:SS", UTC) -> epoch."""
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def _workdir(data_dir: str, key: str, *, legacy: bool = False) -> str:
    name = _SAFE.sub("_", key) if legacy else account_dir_name(key)
    return os.path.join(data_dir, "users", name, "tokens")


def _legacy_owner_is_provable(key: str) -> bool:
    """Whether the old lossy dirname maps uniquely back to this key.

    Historical ownership is not stored. A transformed character, or even an
    existing underscore, admits another original key with the same dirname
    (for example ``a+b`` and ``a_b``). Those files require manual ownership
    verification/re-authentication and are never auto-backfilled.
    """
    return _SAFE.search(key) is None and "_" not in key


def _capture_is_proven(token_path: str, blob: str) -> bool:
    marker_path = os.path.join(os.path.dirname(token_path), _PENDING_CAPTURE_FILE)
    try:
        with open(marker_path, encoding="utf-8") as f:
            marker = json.load(f)
    except (OSError, ValueError):
        return False
    return (isinstance(marker, dict) and marker.get("v") == 1
            and marker.get("expected_sha256")
            == hashlib.sha256(blob.encode("utf-8")).hexdigest())


def _inspect(data_dir: str, key: str, blob: str, updated_at: str,
             *, legacy_ambiguous: bool = False) -> tuple[str, str | None]:
    """Return one account's verdict and the inspected token-file path."""
    try:
        region, db_tokens = unpack_blob(blob)
        legacy_blob = is_legacy_blob(blob)
    except GarminBlobError:
        return "invalid-db", None

    candidates: list[tuple[str, str, float]] = []
    errors: list[tuple[str, str]] = []
    for old_layout in (False, True):
        workdir = _workdir(data_dir, key, legacy=old_layout)
        path = os.path.join(workdir, "garmin_tokens.json")
        if not os.path.exists(path):
            continue
        if old_layout and legacy_ambiguous:
            errors.append(("ambiguous", path))
            continue
        try:
            with open(path, encoding="utf-8") as f:
                file_tokens = normalize_tokens_json(f.read())
        except OSError:
            errors.append(("no-file", path))
            continue
        except GarminBlobError:
            errors.append(("torn", path))
            continue

        marker = os.path.join(workdir, ".garmin_region")
        try:
            with open(marker, encoding="utf-8") as f:
                marker_region = f.read()
        except FileNotFoundError:
            # Only a pre-wrapper Global blob in the old pre-hash layout has a
            # legitimate reason not to have a marker.
            if not (legacy_blob and region == "global" and old_layout):
                errors.append(("region-missing", path))
                continue
        except OSError:
            errors.append(("region-invalid", path))
            continue
        else:
            if marker_region not in ("cn", "global") or marker_region != region:
                errors.append(("region-invalid", path))
                continue
        try:
            file_mtime = os.stat(path).st_mtime
        except OSError:
            errors.append(("no-file", path))
            continue
        candidates.append((path, file_tokens, file_mtime))

    if not candidates:
        if errors:
            priority = {"ambiguous": 0, "region-invalid": 1, "region-missing": 2,
                        "torn": 3, "no-file": 4}
            return min(errors, key=lambda item: priority[item[0]])
        return "no-file", None

    db_epoch = _updated_at_epoch(updated_at)
    # A runtime-created hashed mirror may be newer on disk but contain exactly
    # the stale DB generation. Prefer an actual post-DB rotation from either
    # layout; if both rotated, the last atomic write is authoritative.
    drifted = [c for c in candidates if c[1] != db_tokens and c[2] > db_epoch]
    if drifted:
        latest_mtime = max(candidate[2] for candidate in drifted)
        latest = [candidate for candidate in drifted
                  if candidate[2] == latest_mtime]
        # Equal mtimes with different generations provide no safe ordering
        # signal (common after copies/restores). Never pick by iteration order.
        if len({candidate[1] for candidate in latest}) > 1:
            return "ambiguous", latest[0][0]
        path, _tokens, _mtime = latest[0]
        if _capture_is_proven(path, blob):
            return "pending-capture", path
        return "untrusted-generation", path
    in_sync = [c for c in candidates if c[1] == db_tokens]
    if in_sync:
        path, _tokens, _mtime = max(in_sync, key=lambda item: item[2])
        return "in-sync", path
    path, _tokens, _mtime = max(candidates, key=lambda item: item[2])
    return "db-newer", path


def classify(data_dir: str, key: str, blob: str, updated_at: str,
             *, legacy_ambiguous: bool = False) -> str:
    return _inspect(data_dir, key, blob, updated_at,
                    legacy_ambiguous=legacy_ambiguous)[0]


def read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def backfill(conn, data_dir: str, secret: str, apply: bool = False) -> dict:
    """Classify accounts; `apply` recovers only proven pending captures.
    Returns {verdict: [masked keys]} for reporting (aggregates + masks only —
    per-account detail stays in the DB)."""
    rows = conn.execute(
        "SELECT account_key, blob_enc, updated_at FROM accounts "
        "WHERE adapter='garmin' ORDER BY account_key"
    ).fetchall()
    legacy_names: dict[str, int] = {}
    for r in rows:
        name = _SAFE.sub("_", r["account_key"])
        legacy_names[name] = legacy_names.get(name, 0) + 1
    out: dict[str, list[str]] = {}
    for r in rows:
        key = r["account_key"]
        blob = store.decrypt(secret, r["blob_enc"])
        ambiguous = (legacy_names[_SAFE.sub("_", key)] > 1
                     or not _legacy_owner_is_provable(key))
        verdict, path = _inspect(data_dir, key, blob, r["updated_at"],
                                 legacy_ambiguous=ambiguous)
        if verdict == "pending-capture" and apply:
            # Re-inspect immediately before the persist read. Files are replaced
            # atomically by the gateway, but they may still disappear between
            # these operations during a concurrent worker restart.
            verdict, path = _inspect(data_dir, key, blob, r["updated_at"],
                                     legacy_ambiguous=ambiguous)
            if verdict == "pending-capture":
                try:
                    content = read_file(path)
                except OSError:
                    verdict = "no-file"
                else:
                    try:
                        tokens = normalize_tokens_json(content)
                        region, old_tokens = unpack_blob(blob)
                    except GarminBlobError:
                        verdict = "torn"         # changed between the reads
                    else:
                        if tokens == old_tokens:
                            verdict = "in-sync"
                        else:
                            try:
                                file_mtime = os.stat(path).st_mtime
                            except OSError:
                                verdict = "no-file"
                            else:
                                if file_mtime <= _updated_at_epoch(r["updated_at"]):
                                    verdict = "db-newer"
                        if verdict == "pending-capture":
                            # The marker is the lineage proof. Recheck after
                            # reading the token file so concurrent manager
                            # recovery/rematerialization cannot turn this into
                            # an unknown-generation write.
                            if _capture_is_proven(path, blob):
                                packed = pack_blob(tokens, region)
                            else:
                                verdict = "untrusted-generation"
            if verdict == "pending-capture":
                if store.update_account_if_matches(
                        conn, "garmin", key, blob, packed, secret):
                    verdict = "persisted"
                    try:
                        os.unlink(os.path.join(
                            os.path.dirname(path), _PENDING_CAPTURE_FILE))
                    except FileNotFoundError:
                        pass
                    # Upgrade the sole safe legacy Global directory so a repeat
                    # run recognizes the now-versioned DB blob as in sync.
                    marker = os.path.join(os.path.dirname(path), ".garmin_region")
                    if not os.path.exists(marker):
                        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            f.write(region)
                        os.chmod(marker, 0o600)
                else:
                    verdict = "db-changed"
        out.setdefault(verdict, []).append(key[:3] + "***")
    return out


def main():
    p = argparse.ArgumentParser(
        description="Inspect Garmin token files and recover proven pending captures.")
    p.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    p.add_argument("--data-dir", default=None,
                   help="DATA_DIR holding per-account worker tokens (default: the DB's directory)")
    p.add_argument("--apply", action="store_true",
                   help="recover proven pending captures (unknown drift is never written)")
    args = p.parse_args()

    secret = os.environ.get("GATEWAY_SECRET", "")
    if not secret:
        sys.exit("GATEWAY_SECRET not set — needed to decrypt blobs for comparison.")
    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")
    data_dir = args.data_dir or os.environ.get("DATA_DIR") or os.path.dirname(db_path)

    conn = store.init_db(db_path)
    conn.execute("PRAGMA busy_timeout=5000")     # the live gateway shares this DB
    try:
        result = backfill(conn, data_dir, secret, apply=args.apply)
    finally:
        conn.close()

    mode = "APPLIED" if args.apply else "DRY-RUN (nothing written — pass --apply)"
    print(f"garmin token backfill — {mode}")
    order = ["persisted", "pending-capture", "untrusted-generation", "in-sync",
             "db-newer", "db-changed",
             "region-missing", "region-invalid", "ambiguous", "invalid-db",
             "torn", "no-file"]
    for verdict in order:
        keys = result.get(verdict, [])
        if not keys:
            continue
        line = f"  {verdict:10} {len(keys):4}"
        if verdict not in ("in-sync", "no-file"):
            line += "   " + " ".join(keys)
        print(line)
    total = sum(len(v) for v in result.values())
    print(f"  total: {total} garmin account(s)")


if __name__ == "__main__":
    main()
