"""scripts/backfill_garmin_tokens.py — persists worker-rotated token files
into the store (reliability ticket 05). Loaded via importlib like the other
operator scripts."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import pathlib
import sys
import time

import pytest

from missingmcp import store
from missingmcp.adapters.garmin.blob import pack_blob, unpack_blob
from missingmcp.workers import _SAFE, account_dir_name

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
SECRET = "s" * 32

spec = importlib.util.spec_from_file_location(
    "scripts_backfill_gt", SCRIPTS / "backfill_garmin_tokens.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)


def _tokens(generation: int) -> str:
    return json.dumps({
        "di_token": f"access-{generation}",
        "di_refresh_token": f"refresh-{generation}",
        "di_client_id": f"client-{generation}",
    }, sort_keys=True, separators=(",", ":"))


def _seed(tmp_path, key: str, db_blob: str, file_content: str | None,
          conn=None, file_age: float = 0.0, marker: str | None = "auto",
          legacy_layout: bool = False):
    """One garmin account with a DB blob and (optionally) a token file whose
    mtime is now+file_age (positive = file newer than the DB row)."""
    conn = conn or store.init_db(str(tmp_path / "gateway.db"))
    store.upsert_account(conn, "garmin", key, db_blob, SECRET)
    if file_content is not None:
        dirname = _SAFE.sub("_", key) if legacy_layout else account_dir_name(key)
        workdir = tmp_path / "users" / dirname / "tokens"
        workdir.mkdir(parents=True, exist_ok=True)
        f = workdir / "garmin_tokens.json"
        f.write_text(file_content)
        if marker == "auto":
            marker = unpack_blob(db_blob)[0]
        if marker is not None:
            (workdir / ".garmin_region").write_text(marker)
        mtime = time.time() + file_age
        os.utime(f, (mtime, mtime))
    return conn


def _write_pending_marker(tmp_path, key: str, expected_blob: str,
                          *, legacy_layout: bool = False):
    dirname = _SAFE.sub("_", key) if legacy_layout else account_dir_name(key)
    path = (tmp_path / "users" / dirname / "tokens" /
            ".missingmcp-pending-capture.json")
    path.write_text(json.dumps({
        "v": 1,
        "expected_sha256": hashlib.sha256(expected_blob.encode()).hexdigest(),
    }))
    os.chmod(path, 0o600)
    return path


def test_unknown_newer_file_is_never_persisted_even_with_apply(tmp_path):
    old = pack_blob(_tokens(1), "global")
    conn = _seed(tmp_path, "global:a@x.cz", old, _tokens(2), file_age=60)
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=False)
    assert res == {"untrusted-generation": ["glo***"]}
    assert store.get_account_tokens(conn, "garmin", "global:a@x.cz", SECRET) == old
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"untrusted-generation": ["glo***"]}
    assert store.get_account_tokens(conn, "garmin", "global:a@x.cz", SECRET) == old


def test_manager_proven_pending_capture_is_persisted_only_with_apply(tmp_path):
    old = pack_blob(_tokens(1), "global")
    new = pack_blob(_tokens(2), "global")
    key = "global:a@x.cz"
    conn = _seed(tmp_path, key, old, _tokens(2), file_age=60)
    marker = _write_pending_marker(tmp_path, key, old)
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=False) == {
        "pending-capture": ["glo***"]}
    assert marker.exists()
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"persisted": ["glo***"]}
    assert store.get_account_tokens(conn, "garmin", "global:a@x.cz", SECRET) == new
    assert not marker.exists()
    # idempotent: the persisted row now reads as in-sync
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"in-sync": ["glo***"]}


def test_relogin_after_file_write_wins(tmp_path):
    # The DB row is NEWER than the file (user re-signed in after the file was
    # last written): the store must win — persisting the older file would
    # overwrite a fresh login (the gateway's unknown-provenance rule).
    fresh = pack_blob(_tokens(3), "global")
    conn = _seed(tmp_path, "global:b@x.cz", fresh, _tokens(2),
                 file_age=-60)
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"db-newer": ["glo***"]}
    assert store.get_account_tokens(conn, "garmin", "global:b@x.cz", SECRET) == fresh


def test_torn_and_missing_files_never_persist(tmp_path):
    old = pack_blob(_tokens(1), "global")
    conn = _seed(tmp_path, "global:c@x.cz", old, _tokens(2)[:-1],
                 file_age=60)  # torn
    _seed(tmp_path, "global:d@x.cz", old, None, conn=conn)                    # no file
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"torn": ["glo***"], "no-file": ["glo***"]}
    assert store.get_account_tokens(conn, "garmin", "global:c@x.cz", SECRET) == old


@pytest.mark.parametrize("invalid", [
    "{}",
    '{"unrelated":"value"}',
    '{"di_token":"access","di_refresh_token":"refresh"}',
    '{"di_token":"","di_refresh_token":"refresh","di_client_id":"client"}',
])
def test_structurally_invalid_file_never_replaces_valid_db_blob(tmp_path, invalid):
    old = pack_blob(_tokens(1), "global")
    conn = _seed(tmp_path, "global:c@x.cz", old, invalid, file_age=60)
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "torn": ["glo***"]}
    assert store.get_account_tokens(conn, "garmin", "global:c@x.cz", SECRET) == old


def test_other_adapters_untouched(tmp_path):
    conn = _seed(tmp_path, "global:e@x.cz", pack_blob(_tokens(1), "global"),
                 _tokens(2), file_age=60)
    store.upsert_account(conn, "whoop", "e@x.cz", '{"w": 1}', SECRET)
    res = bf.backfill(conn, str(tmp_path), SECRET, apply=True)
    assert res == {"untrusted-generation": ["glo***"]}     # one garmin row only
    assert store.get_account_tokens(conn, "whoop", "e@x.cz", SECRET) == '{"w": 1}'


def test_main_dry_run_output_masks_keys(tmp_path, capsys, monkeypatch):
    _seed(tmp_path, "global:alice@example.com", pack_blob(_tokens(1), "global"),
          _tokens(2), file_age=60)
    monkeypatch.setenv("GATEWAY_SECRET", SECRET)
    monkeypatch.setattr(sys, "argv", [
        "backfill_garmin_tokens.py", "--db", str(tmp_path / "gateway.db"),
        "--data-dir", str(tmp_path)])
    bf.main()
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "untrusted-generation" in out
    assert "glo***" in out and "alice@example.com" not in out


def test_cn_refresh_keeps_cn_region(tmp_path):
    old = pack_blob(_tokens(1), "cn")
    key = "cn:a@x.cz"
    conn = _seed(tmp_path, key, old, _tokens(2), file_age=60)
    _write_pending_marker(tmp_path, key, old)
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "persisted": ["cn:***"]}
    stored = store.get_account_tokens(conn, "garmin", "cn:a@x.cz", SECRET)
    assert unpack_blob(stored) == ("cn", _tokens(2))


def test_region_marker_missing_or_conflicting_is_rejected(tmp_path):
    cn = pack_blob(_tokens(1), "cn")
    conn = _seed(tmp_path, "cn:a@x.cz", cn, _tokens(2), file_age=60, marker=None)
    _seed(tmp_path, "global:b@x.cz", pack_blob(_tokens(1), "global"),
          _tokens(2), conn=conn, file_age=60, marker="cn")
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "region-missing": ["cn:***"], "region-invalid": ["glo***"]}


def test_unambiguous_legacy_raw_global_directory_still_needs_lineage(tmp_path):
    conn = _seed(tmp_path, "old@x.cz", _tokens(1), _tokens(2), file_age=60,
                 marker=None, legacy_layout=True)
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "untrusted-generation": ["old***"]}
    assert store.get_account_tokens(conn, "garmin", "old@x.cz", SECRET) == _tokens(1)


def test_colliding_legacy_directories_are_never_trusted(tmp_path):
    conn = _seed(tmp_path, "a+b@example.com", _tokens(1), _tokens(2),
                 file_age=60, marker=None, legacy_layout=True)
    _seed(tmp_path, "a_b@example.com", _tokens(3), None, conn=conn)
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "ambiguous": ["a+b***", "a_b***"]}


@pytest.mark.parametrize("surviving_key", [
    "a+b@example.com",   # old layout transformed '+' to '_'
    "a_b@example.com",   # literal '_' cannot be distinguished from that transform
])
def test_deleted_historical_collision_owner_is_never_guessed(
        tmp_path, surviving_key):
    # The other historical owner is already gone from the DB, but its last
    # token file remains in the shared lossy directory. Current-row uniqueness,
    # mtime and valid JSON still cannot establish ownership, so fail closed.
    old = _tokens(1)
    conn = _seed(tmp_path, surviving_key, old, _tokens(99), file_age=60,
                 marker=None, legacy_layout=True)
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "ambiguous": [surviving_key[:3] + "***"]}
    assert store.get_account_tokens(
        conn, "garmin", surviving_key, SECRET) == old


def test_concurrent_relogin_wins_over_backfill(tmp_path, monkeypatch):
    old = pack_blob(_tokens(1), "global")
    fresh = pack_blob(_tokens(3), "global")
    conn = _seed(tmp_path, "global:a@x.cz", old, _tokens(2), file_age=60)
    _write_pending_marker(tmp_path, "global:a@x.cz", old)

    real_cas = store.update_account_if_matches

    def relogin_then_cas(c, adapter, key, expected, new, secret):
        store.upsert_account(c, adapter, key, fresh, secret)
        return real_cas(c, adapter, key, expected, new, secret)

    monkeypatch.setattr(bf.store, "update_account_if_matches", relogin_then_cas)
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "db-changed": ["glo***"]}
    assert store.get_account_tokens(conn, "garmin", "global:a@x.cz", SECRET) == fresh


def test_file_disappearing_before_apply_is_skipped(tmp_path, monkeypatch):
    old = pack_blob(_tokens(1), "global")
    conn = _seed(tmp_path, "global:a@x.cz", old, _tokens(2), file_age=60)
    _write_pending_marker(tmp_path, "global:a@x.cz", old)

    monkeypatch.setattr(
        bf, "read_file",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError("worker stopped")),
    )
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "no-file": ["glo***"]}
    assert store.get_account_tokens(conn, "garmin", "global:a@x.cz", SECRET) == old


def test_new_hashed_db_mirror_does_not_trust_newer_legacy_rotation(tmp_path):
    old = _tokens(1)
    conn = _seed(tmp_path, "old@x.cz", old, old, file_age=120, marker="global")
    legacy_dir = tmp_path / "users" / "old@x.cz" / "tokens"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "garmin_tokens.json"
    legacy_file.write_text(_tokens(2))
    os.utime(legacy_file, (time.time() + 60, time.time() + 60))

    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "untrusted-generation": ["old***"]}
    assert store.get_account_tokens(conn, "garmin", "old@x.cz", SECRET) == old


def test_late_old_worker_write_cannot_replace_verified_new_login(tmp_path):
    # G9 was verified and committed first.  A G2 worker writes later, so its
    # mtime is newer than G9's row.  Without manager lineage that ordering is
    # not evidence of freshness and --apply must fail closed.
    verified = pack_blob(_tokens(9), "global")
    key = "global:a@x.cz"
    conn = _seed(tmp_path, key, verified, _tokens(2), file_age=60)
    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "untrusted-generation": ["glo***"]}
    assert store.get_account_tokens(conn, "garmin", key, SECRET) == verified


def test_equal_mtime_conflicting_workdirs_fail_closed(tmp_path):
    old = _tokens(1)
    exact_mtime = time.time() + 60
    conn = _seed(tmp_path, "old@x.cz", old, _tokens(2),
                 marker="global")
    hashed_file = (tmp_path / "users" / account_dir_name("old@x.cz") /
                   "tokens" / "garmin_tokens.json")
    legacy_dir = tmp_path / "users" / "old@x.cz" / "tokens"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "garmin_tokens.json"
    legacy_file.write_text(_tokens(3))
    (legacy_dir / ".garmin_region").write_text("global")
    os.utime(hashed_file, (exact_mtime, exact_mtime))
    os.utime(legacy_file, (exact_mtime, exact_mtime))

    assert bf.backfill(conn, str(tmp_path), SECRET, apply=True) == {
        "ambiguous": ["old***"]}
    assert store.get_account_tokens(conn, "garmin", "old@x.cz", SECRET) == old
