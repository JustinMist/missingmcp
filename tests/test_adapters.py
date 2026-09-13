import dataclasses
import json
import os
import stat
import pytest
from missingmcp.adapters import base
from missingmcp.config import load_config
from missingmcp.adapters.garmin import GarminWorkerForward
from missingmcp.adapters.garmin.blob import pack_blob, unpack_blob


def _tokens(generation: int) -> str:
    return json.dumps({
        "di_token": f"access-{generation}",
        "di_refresh_token": f"refresh-{generation}",
        "di_client_id": f"client-{generation}",
    }, sort_keys=True, separators=(",", ":"))


def test_login_ok_is_frozen():
    r = base.LoginOk(account_key="me@x.cz", blob='{"t":1}')
    assert r.account_key == "me@x.cz" and r.blob == '{"t":1}'
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.account_key = "other"


def test_login_error_carries_reason():
    e = base.LoginError("try later", reason="blocked")
    assert str(e) == "try later" and e.reason == "blocked"
    assert base.LoginError("x").reason == "unknown"      # default


def test_second_factor_error_carries_state():
    state = ("pending", "me@x.cz")
    e = base.SecondFactorError("wrong code", state=state)
    assert str(e) == "wrong code" and e.state is state


CFG = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://x",
                   "GARMIN_MCP_CMD": "uvx garmin-mcp"})


def test_garmin_forward_command_comes_from_config():
    assert GarminWorkerForward(CFG).command() == ["uvx", "garmin-mcp"]


@pytest.mark.parametrize(("region", "expected"), [("cn", "true"), ("global", "false")])
def test_garmin_forward_env_is_the_documented_contract(tmp_path, region, expected):
    forward = GarminWorkerForward(CFG)
    forward.materialize(pack_blob(_tokens(1), region), str(tmp_path))
    env = forward.env(9007, str(tmp_path))
    assert env == {
        "GARMIN_MCP_TRANSPORT": "streamable-http",
        "GARMIN_MCP_HOST": "127.0.0.1",
        "GARMIN_MCP_PORT": "9007",
        "GARMINTOKENS": str(tmp_path),
        "GARMIN_IS_CN": expected,
    }


def test_garmin_forward_missing_legacy_marker_defaults_global(tmp_path):
    assert GarminWorkerForward(CFG).env(9007, str(tmp_path))["GARMIN_IS_CN"] == "false"


def test_garmin_forward_materialize_writes_0600_tokens_file(tmp_path):
    path = tmp_path / "garmin_tokens.json"
    marker = tmp_path / ".garmin_region"
    path.write_text("old")
    marker.write_text("global")
    os.chmod(path, 0o644)
    os.chmod(marker, 0o644)
    GarminWorkerForward(CFG).materialize(pack_blob(_tokens(1), "cn"), str(tmp_path))
    assert path.read_text() == _tokens(1)
    assert marker.read_text() == "cn"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(marker).st_mode) == 0o600


from unittest.mock import patch
from missingmcp.adapters import base
from missingmcp.adapters.garmin import GarminAdapter, login


def _adapter():
    return GarminAdapter(CFG)


def test_adapter_attrs():
    a = _adapter()
    assert a.name == "garmin" and a.display_name == "Garmin"
    assert a.authorize_template == "authorize.html"
    assert a.second_factor_template == "mfa.html"
    assert a.forward.command() == ["uvx", "garmin-mcp"]
    assert a.login_hint({"garmin_email": "Me@X.cz"}) == "Me@X.cz"


def test_start_login_ok_normalizes_account_key():
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="ok", tokens_json=_tokens(1))):
        r = _adapter().start_login({"garmin_email": " Me@X.cz ", "garmin_password": "pw"})
    assert isinstance(r, base.LoginOk)
    assert r.account_key == "global:me@x.cz"
    assert unpack_blob(r.blob) == ("global", _tokens(1))


def test_start_login_regions_are_distinct_and_forwarded():
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="ok", tokens_json=_tokens(1))) as call:
        cn = _adapter().start_login({"garmin_email": " Me@X.cz ",
                                     "garmin_password": "pw", "garmin_region": "cn"})
        global_ = _adapter().start_login({"garmin_email": " Me@X.cz ",
                                          "garmin_password": "pw", "garmin_region": "global"})
    assert cn.account_key == "cn:me@x.cz"
    assert global_.account_key == "global:me@x.cz"
    assert cn.account_key != global_.account_key
    assert unpack_blob(cn.blob)[0] == "cn"
    assert unpack_blob(global_.blob)[0] == "global"
    assert [c.kwargs["is_cn"] for c in call.call_args_list] == [True, False]


@pytest.mark.parametrize("region", ["", "CN", "eu"])
def test_invalid_region_is_rejected_before_login(region):
    with patch.object(login, "start_login") as call:
        with pytest.raises(base.LoginError):
            _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw",
                                    "garmin_region": region})
    call.assert_not_called()


@pytest.mark.parametrize("region", ["cn", "global"])
def test_start_login_mfa_state_carries_email_and_region(region):
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="needs_mfa", pending=("P", "S"))):
        r = _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw",
                                    "garmin_region": region})
    assert isinstance(r, base.SecondFactorNeeded)
    assert r.state == (("P", "S"), "me@x.cz", region)


def test_start_login_blocked_maps_message_and_reason():
    with patch.object(login, "start_login",
                      side_effect=login.GarminLoginError("429", reason="blocked")):
        with pytest.raises(base.LoginError) as ei:
            _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw"})
    assert ei.value.reason == "blocked"
    assert "rate-limiting" in str(ei.value) and "not your password" in str(ei.value)
    assert ei.value.__cause__ is None


def test_start_login_auth_error_maps_message():
    with patch.object(login, "start_login",
                      side_effect=login.GarminLoginError("bad", reason="auth")):
        with pytest.raises(base.LoginError) as ei:
            _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw"})
    assert ei.value.reason == "auth" and "check your Garmin email" in str(ei.value)
    assert ei.value.__cause__ is None


def test_resume_ok_returns_login_ok():
    with patch.object(login, "resume_login", return_value=_tokens(9)):
        r = _adapter().resume_second_factor((("P", "S"), "Me@X.cz", "cn"),
                                            {"mfa_code": "123456",
                                             "garmin_region": "global"})
    assert r.account_key == "cn:me@x.cz"
    assert unpack_blob(r.blob) == ("cn", _tokens(9))


def test_resume_failure_is_retryable_with_same_state():
    state = (("P", "S"), "me@x.cz", "global")
    with patch.object(login, "resume_login", side_effect=Exception("wrong code")):
        with pytest.raises(base.SecondFactorError) as ei:
            _adapter().resume_second_factor(state, {"mfa_code": "000000"})
    assert ei.value.state is state
    assert "Incorrect or expired code" in str(ei.value)


def test_verify_ok_and_failure():
    rotated = login.VerifiedTokens(name="Vaclav S", tokens_json=_tokens(2))
    with patch.object(login, "verify_tokens", return_value=rotated) as verify:
        result = _adapter().verify(pack_blob(_tokens(1), "cn"))
        assert result == base.Verification(
            name="Vaclav S", blob=pack_blob(_tokens(2), "cn"))
        verify.assert_called_once_with(_tokens(1), is_cn=True)
    with patch.object(login, "verify_tokens", return_value=rotated) as verify:
        result = _adapter().verify(_tokens(1))
        assert result == base.Verification(
            name="Vaclav S", blob=pack_blob(_tokens(2), "global"))
        verify.assert_called_once_with(_tokens(1), is_cn=False)
    with patch.object(login, "verify_tokens", side_effect=login.GarminLoginError("bad")):
        with pytest.raises(base.LoginError) as ei:
            _adapter().verify(_tokens(1))
    assert "could not be verified" in str(ei.value)


from missingmcp.adapters import build_adapters
from missingmcp import store
from missingmcp.app import _garmin_account_key_resolver


def test_registry_builds_all_adapters():
    adapters = build_adapters(CFG)
    assert set(adapters) == {"garmin"}
    assert adapters["garmin"].name == "garmin"
    assert adapters["garmin"].forward.command() == ["uvx", "garmin-mcp"]


def test_legacy_global_relogin_reuses_existing_physical_key(tmp_path):
    cfg = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://x",
                       "DB_PATH": str(tmp_path / "gateway.db")})
    conn = store.init_db(cfg.db_path)
    store.upsert_account(conn, "garmin", "me@x.cz", _tokens(1), cfg.gateway_secret)
    adapter = GarminAdapter(cfg, _garmin_account_key_resolver(conn, cfg))
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="ok", tokens_json=_tokens(2))):
        result = adapter.start_login({"garmin_email": "Me@X.cz", "garmin_password": "pw",
                                      "garmin_region": "global"})
        cn = adapter.start_login({"garmin_email": "Me@X.cz", "garmin_password": "pw",
                                  "garmin_region": "cn"})
    assert result.account_key == "me@x.cz"
    assert cn.account_key == "cn:me@x.cz"
    conn.close()


def test_duplicate_legacy_and_canonical_global_requires_operator(tmp_path):
    cfg = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://x",
                       "DB_PATH": str(tmp_path / "gateway.db")})
    conn = store.init_db(cfg.db_path)
    store.upsert_account(conn, "garmin", "me@x.cz", _tokens(1), cfg.gateway_secret)
    store.upsert_account(conn, "garmin", "global:me@x.cz",
                         pack_blob(_tokens(2), "global"), cfg.gateway_secret)
    adapter = GarminAdapter(cfg, _garmin_account_key_resolver(conn, cfg))
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="ok", tokens_json=_tokens(3))):
        with pytest.raises(base.LoginError, match="two existing records"):
            adapter.start_login({"garmin_email": "me@x.cz", "garmin_password": "pw",
                                 "garmin_region": "global"})
    conn.close()
