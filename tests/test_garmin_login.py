import json
import os
import pytest
from unittest.mock import patch, MagicMock
from garmin_gateway import garmin_login


def _fake_garmin_factory(needs_mfa=False, dump_payload='{"oauth":"tok"}'):
    """Return a fake Garmin class whose .dump writes garmin_tokens.json."""
    def dump(path):
        with open(os.path.join(path, "garmin_tokens.json"), "w") as f:
            f.write(dump_payload)

    def make(*args, **kwargs):
        g = MagicMock()
        g.client.dump.side_effect = dump
        if needs_mfa and (kwargs.get("password") or len(args) >= 2):
            g.login.return_value = ("needs_mfa", "STATE")
        else:
            g.login.return_value = (None, None)
        g.get_full_name.return_value = "Vaclav S"
        return g
    return make


def test_login_no_mfa_returns_tokens():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory()):
        r = garmin_login.start_login("me@x.cz", "pw")
    assert r.status == "ok"
    assert json.loads(r.tokens_json) == {"oauth": "tok"}


def test_login_needs_mfa_then_resume():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory(needs_mfa=True)):
        r = garmin_login.start_login("me@x.cz", "pw")
        assert r.status == "needs_mfa"
        assert r.tokens_json is None
        tokens = garmin_login.resume_login(r.pending, "123456")
    assert json.loads(tokens) == {"oauth": "tok"}


def test_verify_tokens_returns_name():
    with patch.object(garmin_login, "Garmin", side_effect=_fake_garmin_factory()):
        name = garmin_login.verify_tokens('{"oauth":"tok"}')
    assert name == "Vaclav S"


def test_verify_tokens_raises_when_no_profile():
    def make(*a, **k):
        g = MagicMock()
        g.get_full_name.return_value = None
        return g
    with patch.object(garmin_login, "Garmin", side_effect=make):
        with pytest.raises(garmin_login.GarminLoginError):
            garmin_login.verify_tokens('{"oauth":"tok"}')
