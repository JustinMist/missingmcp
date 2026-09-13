import json
import os
import pytest
from unittest.mock import patch, MagicMock
from missingmcp.adapters.garmin import login as garmin_login


def _tokens(generation: str) -> str:
    return json.dumps({
        "di_token": f"access-{generation}",
        "di_refresh_token": f"refresh-{generation}",
        "di_client_id": f"client-{generation}",
    }, sort_keys=True, separators=(",", ":"))


def _fake_garmin_factory(needs_mfa=False, dump_payload=None, created=None):
    """Return a fake Garmin class whose .dump writes garmin_tokens.json."""
    dump_payload = dump_payload or _tokens("1")
    def dump(path):
        with open(os.path.join(path, "garmin_tokens.json"), "w") as f:
            f.write(dump_payload)

    def make(*args, **kwargs):
        g = MagicMock()
        if created is not None:
            created.append((args, kwargs, g))
        g.client.dump.side_effect = dump
        g.client.dumps.return_value = dump_payload
        if needs_mfa and (kwargs.get("password") or len(args) >= 2):
            g.login.return_value = ("needs_mfa", "STATE")
        else:
            g.login.return_value = (None, None)
        g.get_full_name.return_value = "Vaclav S"
        return g
    return make


@pytest.mark.parametrize("is_cn", [False, True])
def test_login_no_mfa_returns_tokens(is_cn):
    created = []
    with patch.object(garmin_login, "Garmin",
                      side_effect=_fake_garmin_factory(created=created)):
        r = garmin_login.start_login("me@x.cz", "pw", is_cn=is_cn)
    assert r.status == "ok"
    assert r.tokens_json == _tokens("1")
    assert created[0][1] == {
        "email": "me@x.cz", "password": "pw", "is_cn": is_cn,
        "return_on_mfa": True,
    }
    # An explicit private tokenstore prevents inherited GARMINTOKENS reuse.
    assert created[0][2].login.call_args.args[0]


@pytest.mark.parametrize("is_cn", [False, True])
def test_login_needs_mfa_then_resume(is_cn):
    created = []
    with patch.object(garmin_login, "Garmin",
                      side_effect=_fake_garmin_factory(needs_mfa=True, created=created)):
        r = garmin_login.start_login("me@x.cz", "pw", is_cn=is_cn)
        assert r.status == "needs_mfa"
        assert r.tokens_json is None
        assert r.pending[0].password is None
        tokens = garmin_login.resume_login(r.pending, "123456")
    assert tokens == _tokens("1")
    assert created[0][1]["is_cn"] is is_cn


def test_login_retries_blocked_then_succeeds():
    calls = {"n": 0}

    def dump(path):
        with open(os.path.join(path, "garmin_tokens.json"), "w") as f:
            f.write(_tokens("1"))

    def make(*a, **k):
        g = MagicMock()
        g.client.dump.side_effect = dump

        def login(*la, **lk):
            calls["n"] += 1
            if calls["n"] == 1:
                raise garmin_login.GarminConnectConnectionError("Portal login failed: HTTP 403")
            return (None, None)

        g.login.side_effect = login
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        r = garmin_login.start_login("me@x.cz", "pw", is_cn=False,
                                     attempts=2, backoff=0, sleep=lambda s: None)
    assert r.status == "ok" and calls["n"] == 2      # retried once, then succeeded


def test_login_auth_error_not_retried():
    calls = {"n": 0}

    def make(*a, **k):
        g = MagicMock()

        def login(*la, **lk):
            calls["n"] += 1
            raise garmin_login.GarminConnectAuthenticationError("401 Unauthorized")

        g.login.side_effect = login
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError) as ei:
            garmin_login.start_login("me@x.cz", "wrong", is_cn=False,
                                     attempts=3, sleep=lambda s: None)
    assert ei.value.reason == "auth" and calls["n"] == 1   # wrong password: never retried


def test_login_blocked_exhausted_raises_blocked():
    def make(*a, **k):
        g = MagicMock()
        g.login.side_effect = garmin_login.GarminConnectTooManyRequestsError("429 rate limited")
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError) as ei:
            garmin_login.start_login("me@x.cz", "pw", is_cn=True,
                                     attempts=2, backoff=0, sleep=lambda s: None)
    assert ei.value.reason == "blocked"


@pytest.mark.parametrize("is_cn", [False, True])
def test_verify_tokens_returns_name(is_cn):
    created = []
    with patch.object(garmin_login, "Garmin",
                      side_effect=_fake_garmin_factory(created=created)):
        result = garmin_login.verify_tokens(_tokens("1"), is_cn=is_cn)
    assert result.name == "Vaclav S"
    assert result.tokens_json == _tokens("1")
    assert created[0][1] == {"is_cn": is_cn}


def test_verify_tokens_returns_generation_rewritten_during_login():
    def make(*_args, **_kwargs):
        g = MagicMock()

        g.login.return_value = (None, None)
        g.client.dumps.return_value = _tokens("2")
        g.get_full_name.return_value = "Vaclav S"
        return g

    with patch.object(garmin_login, "Garmin", side_effect=make):
        result = garmin_login.verify_tokens(_tokens("1"), is_cn=True)
    assert result == garmin_login.VerifiedTokens(
        name="Vaclav S", tokens_json=_tokens("2"))


def test_verify_tokens_succeeds_when_name_empty():
    # A valid, authenticated account can legitimately have an empty fullName
    # (garminconnect defaults fullName to ""). Successful login (no exception)
    # already proves authentication, so an empty name must NOT be rejected.
    def make(*a, **k):
        g = MagicMock()
        g.login.return_value = (None, None)
        g.get_full_name.return_value = ""
        g.client.dumps.return_value = _tokens("1")
        return g
    with patch.object(garmin_login, "Garmin", side_effect=make):
        result = garmin_login.verify_tokens(_tokens("1"), is_cn=False)
    assert result.name == ""
    assert result.tokens_json == _tokens("1")


def test_verify_tokens_raises_when_login_fails():
    # A genuine auth failure (login raises) must still surface as GarminLoginError.
    def make(*a, **k):
        g = MagicMock()
        g.login.side_effect = garmin_login.GarminConnectAuthenticationError("401 Unauthorized")
        return g
    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError):
            garmin_login.verify_tokens(_tokens("1"), is_cn=True)


def test_login_ignores_inherited_tokenstore(monkeypatch):
    created = []
    monkeypatch.setenv("GARMINTOKENS", "/tokens/belongs-to-someone-else")
    with patch.object(garmin_login, "Garmin",
                      side_effect=_fake_garmin_factory(created=created)):
        garmin_login.start_login("me@x.cz", "pw", is_cn=False)
    explicit = created[0][2].login.call_args.args[0]
    assert explicit != "/tokens/belongs-to-someone-else"
