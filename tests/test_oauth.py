import json
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from garmin_gateway import store, oauth
from garmin_gateway.config import load_config

CONFIG = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://gw.example.com"})


@pytest.fixture
def conn():
    c = store.init_db(":memory:")
    yield c
    c.close()


def test_metadata_shape():
    m = oauth.metadata(CONFIG)
    assert m["issuer"] == "https://gw.example.com"
    assert m["authorization_endpoint"] == "https://gw.example.com/oauth/authorize"
    assert m["token_endpoint"] == "https://gw.example.com/oauth/token"
    assert m["registration_endpoint"] == "https://gw.example.com/oauth/register"
    assert "S256" in m["code_challenge_methods_supported"]


def _client_app(conn):
    async def reg(request):
        return await oauth.register_client(request, conn)
    return TestClient(Starlette(routes=[Route("/oauth/register", reg, methods=["POST"])]))


def test_register_returns_client_id(conn):
    c = _client_app(conn)
    resp = c.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/cb"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"]
    assert body["client_secret"]
    assert body["redirect_uris"] == ["https://claude.ai/cb"]
    assert store.get_client(conn, body["client_id"]) is not None


def test_register_rejects_missing_redirect_uris(conn):
    c = _client_app(conn)
    resp = c.post("/oauth/register", json={})
    assert resp.status_code == 400
