from __future__ import annotations

import base64
from importlib.metadata import version
import inspect
import json
import os
import stat
import tempfile
import time
from unittest.mock import patch
from pathlib import Path

import pytest
import requests
from garminconnect import Garmin
from missingmcp.adapters.garmin import (GarminWorkerForward,
                                        login as garmin_login)
from missingmcp.adapters.garmin.blob import pack_blob, unpack_blob
from missingmcp.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_reviewed_garminconnect_version_is_installed():
    assert version("garminconnect") == "0.3.6"


def test_gateway_and_worker_builds_pin_the_reviewed_dependency():
    pyproject = (ROOT / "pyproject.toml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    worker_override = (ROOT / "garmin-worker-override.txt").read_text()
    assert '"garminconnect==0.3.6"' in pyproject
    assert "garminconnect==0.3.6" in worker_override
    assert "--require-hashes -r /app/garmin-worker-override.txt" in dockerfile
    assert "--require-hashes -r garmin-worker-override.txt" in workflow
    assert "sha256:" in worker_override
    assert "/opt/garmin-mcp/.venv/bin/python" in dockerfile
    assert 'garmin-mcp/.venv/bin/python' in workflow


def test_reviewed_mfa_continuation_contract():
    """Our minimal pending state relies on the reviewed 0.3.6 widget contract."""
    source = inspect.getsource(Garmin.resume_login)
    assert "self.client.resume_login(client_state, mfa_code)" in source
    client_source = inspect.getsource(type(Garmin().client)._complete_mfa_widget)
    assert "_widget_last_resp" in client_source
    assert ".text" in client_source
    assert ".request" not in client_source
    init_source = inspect.getsource(type(Garmin().client).__init__)
    assert 'self._di_token_url = f"https://diauth.{domain}/' in init_source


def test_real_pinned_token_dump_is_forced_private_under_permissive_umask(
        tmp_path, monkeypatch):
    """The dependency and gateway wrapper both enforce private token mode."""
    token_dir = tmp_path / "kept-token-dir"
    token_dir.mkdir()

    class KeptTemporaryDirectory:
        def __enter__(self):
            return str(token_dir)

        def __exit__(self, *_args):
            return False

    client = Garmin().client
    client.di_token = "access"
    client.di_refresh_token = "refresh"
    client.di_client_id = "client"
    monkeypatch.setattr(
        garmin_login.tempfile, "TemporaryDirectory", KeptTemporaryDirectory)
    previous_umask = os.umask(0)
    try:
        dumped = garmin_login._dump_tokens(client)
    finally:
        os.umask(previous_umask)

    assert json.loads(dumped) == {
        "di_token": "access",
        "di_refresh_token": "refresh",
        "di_client_id": "client",
    }
    assert stat.S_IMODE(
        (token_dir / "garmin_tokens.json").stat().st_mode) == 0o600


def _contains_secret(value, secret: str, seen: set[int] | None = None) -> bool:
    seen = seen or set()
    if isinstance(value, str):
        return secret in value
    if isinstance(value, bytes):
        return secret.encode() in value
    if value is None or isinstance(value, (int, float, bool)):
        return False
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    if isinstance(value, dict):
        return any(_contains_secret(k, secret, seen) or
                   _contains_secret(v, secret, seen) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_secret(v, secret, seen) for v in value)
    return any(_contains_secret(v, secret, seen)
               for v in getattr(value, "__dict__", {}).values())


def test_real_pinned_widget_mfa_state_drops_password_before_and_after_rejection():
    secret = "UNIQUE-MFA-PASSWORD-5ee7928d"
    garmin = Garmin(email="person@example.com", password=secret,
                    is_cn=True, return_on_mfa=True)
    request = requests.Request(
        "POST", "https://sso.garmin.cn/sso/signin",
        data={"username": "person@example.com", "password": secret},
    ).prepare()
    response = requests.Response()
    response.status_code = 200
    response.request = request
    response.url = request.url
    response._content = b'<input name="_csrf" value="csrf"><title>MFA</title>'

    class BadMfaResponse:
        status_code = 200
        text = "<title>Invalid Code</title>"

    class MfaSession:
        def post(self, *_args, **_kwargs):
            return BadMfaResponse()

    garmin.client._mfa_flow = "widget"
    garmin.client._mfa_session = MfaSession()
    garmin.client._mfa_login_params = {}
    garmin.client._mfa_post_headers = {"Referer": response.url}
    garmin.client._widget_last_resp = response
    garmin.login = lambda _tokenstore: ("needs_mfa", None)

    with patch.object(garmin_login, "Garmin", return_value=garmin):
        result = garmin_login.start_login(
            "person@example.com", secret, is_cn=True, attempts=1)
    assert result.status == "needs_mfa"
    assert not _contains_secret(result.pending, secret)
    assert result.pending[0].client._widget_last_resp.text.endswith("<title>MFA</title>")

    with pytest.raises(Exception):
        garmin_login.resume_login(result.pending, "000000")
    assert not _contains_secret(result.pending, secret)


@pytest.mark.parametrize("is_cn", [False, True])
def test_real_mfa_refresh_cannot_recreate_released_tokenstore(
        tmp_path, is_cn):
    """Exercise real Garmin.login/resume_login and Client.load/refresh/dump.

    Only network edges are replaced. Client.load still records the explicit
    request directory before the MFA path, and the real refresh path attempts a
    dump only when that reference survives.
    """
    Client = type(Garmin().client)
    real_load = Client.load
    real_dump = Client.dump
    real_temp = tempfile.TemporaryDirectory
    load_paths = []
    dump_paths = []
    di_urls = []

    def recording_load(self, path):
        load_paths.append(path)
        return real_load(self, path)

    def recording_dump(self, path):
        dump_paths.append(path)
        return real_dump(self, path)

    def complete_mfa(self, _code):
        self.di_token = "access-1"
        self.di_refresh_token = "refresh-1"
        self.di_client_id = "client"

    class Reply:
        def __init__(self, status, data):
            self.status_code = status
            self.data = data
            self.text = "synthetic"
            self.content = b""
            self.ok = status < 400

        def json(self):
            return self.data

    class Network:
        def __init__(self):
            self.calls = 0

        def request(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return Reply(401, {})
            if self.calls == 2:
                return Reply(200, {"displayName": "probe", "fullName": "Synthetic"})
            return Reply(200, {"userData": {}})

    network = Network()

    def refresh_http(self, url, **_kwargs):
        di_urls.append(url)
        return Reply(200, {
            "access_token": "access-2",
            "refresh_token": "refresh-2",
        })

    with patch.object(
            garmin_login.tempfile, "TemporaryDirectory",
            side_effect=lambda: real_temp(dir=tmp_path)), \
         patch.object(Client, "load", recording_load), \
         patch.object(Client, "login", return_value=("needs_mfa", None)), \
         patch.object(Client, "_complete_mfa", complete_mfa), \
         patch.object(Client, "_http_post", refresh_http), \
         patch.object(Client, "dump", recording_dump):
        pending = garmin_login.start_login(
            "person@example.com", "temporary-password",
            is_cn=is_cn, attempts=1)
        released_path = load_paths[0]
        assert not os.path.exists(released_path)
        assert pending.pending[0].client._tokenstore_path is None
        pending.pending[0].client._api_session = network
        tokens = garmin_login.resume_login(pending.pending, "123456")

    assert json.loads(tokens)["di_refresh_token"] == "refresh-2"
    assert not os.path.exists(released_path)
    assert released_path not in dump_paths
    assert dump_paths and all(not os.path.exists(path) for path in dump_paths)
    assert pending.pending[0].client._tokenstore_path is None
    expected_domain = "garmin.cn" if is_cn else "garmin.com"
    assert di_urls == [
        f"https://diauth.{expected_domain}/di-oauth2-service/oauth/token"]


@pytest.mark.parametrize("is_cn", [False, True])
def test_real_mfa_error_keeps_released_tokenstore_detached(tmp_path, is_cn):
    Client = type(Garmin().client)
    real_load = Client.load
    real_temp = tempfile.TemporaryDirectory
    load_paths = []
    continuation_paths = []

    def recording_load(self, path):
        load_paths.append(path)
        return real_load(self, path)

    def rejected_mfa(self, _code):
        continuation_paths.append(self._tokenstore_path)
        raise RuntimeError("synthetic MFA rejection")

    with patch.object(
            garmin_login.tempfile, "TemporaryDirectory",
            side_effect=lambda: real_temp(dir=tmp_path)), \
         patch.object(Client, "load", recording_load), \
         patch.object(Client, "login", return_value=("needs_mfa", None)), \
         patch.object(Client, "_complete_mfa", rejected_mfa):
        pending = garmin_login.start_login(
            "person@example.com", "temporary-password",
            is_cn=is_cn, attempts=1)
        released_path = load_paths[0]
        assert not os.path.exists(released_path)
        with pytest.raises(RuntimeError, match="synthetic MFA rejection"):
            garmin_login.resume_login(pending.pending, "000000")

    assert continuation_paths == [None]
    assert not os.path.exists(released_path)
    assert pending.pending[0].client._tokenstore_path is None


class _TokenReply:
    status_code = 200
    ok = True
    text = "synthetic"
    content = b"synthetic"

    def json(self):
        return {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
        }


def _expired_access_token() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({
        "exp": int(time.time()) - 60,
    }).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _tokens_for_refresh() -> str:
    return json.dumps({
        "di_token": _expired_access_token(),
        "di_refresh_token": "old-refresh",
        "di_client_id": "client",
    }, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("is_cn,domain", [
    (False, "garmin.com"),
    (True, "garmin.cn"),
])
def test_service_ticket_exchange_uses_account_di_domain(is_cn, domain):
    client = Garmin(is_cn=is_cn).client
    urls = []

    def http_post(url, **_kwargs):
        urls.append(url)
        return _TokenReply()

    client._http_post = http_post
    client._exchange_service_ticket("ST-synthetic")
    assert urls == [
        f"https://diauth.{domain}/di-oauth2-service/oauth/token"]


@pytest.mark.parametrize("is_cn,domain", [
    (False, "garmin.com"),
    (True, "garmin.cn"),
])
def test_token_verification_refresh_uses_account_di_domain(
        tmp_path, is_cn, domain):
    Client = type(Garmin().client)
    urls = []

    def http_post(self, url, **_kwargs):
        urls.append(url)
        return _TokenReply()

    with patch.object(Client, "_http_post", http_post), \
         patch.object(Garmin, "_load_profile_and_settings", return_value=None):
        verified = garmin_login.verify_tokens(
            _tokens_for_refresh(), is_cn=is_cn)

    assert urls == [
        f"https://diauth.{domain}/di-oauth2-service/oauth/token"]
    assert json.loads(verified.tokens_json)["di_refresh_token"] == "fresh-refresh"


@pytest.mark.parametrize("is_cn,domain", [
    (False, "garmin.com"),
    (True, "garmin.cn"),
])
def test_token_verification_returns_in_memory_refresh_when_auto_dump_fails(
        is_cn, domain):
    """A successful refresh must not fall back to the stale input token file.

    garminconnect intentionally suppresses the automatic dump exception in its
    refresh path. Exercise that exact dependency behavior for both regions and
    prove the gateway returns the authenticated in-memory generation.
    """
    Client = type(Garmin().client)
    urls = []

    def http_post(self, url, **_kwargs):
        urls.append(url)
        return _TokenReply()

    def failed_dump(self, _path):
        raise OSError("synthetic read-only token directory")

    with patch.object(Client, "_http_post", http_post), \
         patch.object(Client, "dump", failed_dump), \
         patch.object(Garmin, "_load_profile_and_settings", return_value=None):
        verified = garmin_login.verify_tokens(
            _tokens_for_refresh(), is_cn=is_cn)

    assert urls == [
        f"https://diauth.{domain}/di-oauth2-service/oauth/token"]
    assert json.loads(verified.tokens_json) == {
        "di_token": "fresh-access",
        "di_refresh_token": "fresh-refresh",
        "di_client_id": "client",
    }


@pytest.mark.parametrize("region,domain", [
    ("global", "garmin.com"),
    ("cn", "garmin.cn"),
])
def test_worker_token_refresh_uses_materialized_account_domain(
        tmp_path, region, domain):
    cfg = load_config({
        "GATEWAY_SECRET": "s" * 40,
        "DATA_DIR": str(tmp_path),
        "PUBLIC_URL": "https://gateway.example.com",
    })
    forward = GarminWorkerForward(cfg)
    workdir = tmp_path / "worker-tokens"
    workdir.mkdir()
    forward.materialize(pack_blob(_tokens_for_refresh(), region), str(workdir))
    worker_env = forward.env(9000, str(workdir))
    urls = []
    Client = type(Garmin().client)

    def http_post(self, url, **_kwargs):
        urls.append(url)
        return _TokenReply()

    with patch.object(Client, "_http_post", http_post), \
         patch.object(Garmin, "_load_profile_and_settings", return_value=None):
        Garmin(is_cn=worker_env["GARMIN_IS_CN"] == "true").login(str(workdir))

    assert urls == [
        f"https://diauth.{domain}/di-oauth2-service/oauth/token"]
    assert unpack_blob(forward.read_back(str(workdir)))[0] == region
