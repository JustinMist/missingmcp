from __future__ import annotations
import json
from starlette.responses import JSONResponse
from . import store, security


def metadata(config) -> dict:
    base = config.public_url
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


async def register_client(request, conn) -> JSONResponse:
    body = await security.read_body_limited(request)
    if body is None:
        return JSONResponse({"error": "request too large"}, status_code=413)
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    client_id = security.new_secret(16)
    client_secret = security.new_secret(32)
    store.create_client(conn, client_id, store.hash_token(client_secret), uris, data.get("client_name"))
    return JSONResponse(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": uris,
            "token_endpoint_auth_method": "client_secret_post",
        },
        status_code=201,
    )
