from __future__ import annotations
import os
import tempfile
import time
from dataclasses import dataclass
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .blob import normalize_tokens_json


class GarminLoginError(Exception):
    def __init__(self, message: str, reason: str = "unknown"):
        super().__init__(message)
        self.reason = reason   # "auth" (bad credentials) | "blocked" (429/403/exhausted) | "unknown"


@dataclass
class LoginResult:
    status: str                 # "ok" | "needs_mfa"
    tokens_json: str | None = None
    pending: object | None = None


@dataclass(frozen=True)
class VerifiedTokens:
    name: str
    tokens_json: str


@dataclass(frozen=True)
class _MfaResponseState:
    """Minimal widget response state required by garminconnect 0.3.6 MFA."""
    text: str


def _sanitize_pending_client(client) -> None:
    """Remove password-bearing dependency state while preserving MFA context.

    garminconnect 0.3.6's widget flow stores the credentials POST response on
    ``client._widget_last_resp``. Its PreparedRequest body contains the plain
    password, while resume_login needs only response.text to recover the CSRF.
    """
    client.password = None
    dependency_client = getattr(client, "client", None)
    # Client.load() assigns this private path before it discovers that the
    # request-private token file does not exist. The MFA continuation outlives
    # that TemporaryDirectory; retaining the path lets a later refresh recreate
    # it with dependency-default permissions, outside our cleanup lifecycle.
    if dependency_client is not None:
        dependency_client._tokenstore_path = None
    # Read the concrete instance dictionary so mocks/proxies cannot fabricate
    # a response attribute that the dependency never stored.
    response = getattr(dependency_client, "__dict__", {}).get("_widget_last_resp")
    if response is None:
        return
    text = getattr(response, "text", None)
    if not isinstance(text, str):
        raise GarminLoginError("unsupported Garmin MFA response state")
    request = getattr(response, "request", None)
    if request is not None and hasattr(request, "body"):
        request.body = None
    dependency_client._widget_last_resp = _MfaResponseState(text=text)


def _dump_tokens(client) -> str:
    """Mirror garmin_mcp/auth_cli.py: dump to a dir, read garmin_tokens.json."""
    with tempfile.TemporaryDirectory() as d:
        # macOS commonly exposes its temp root through a symlink; recent
        # garminconnect releases intentionally reject symlinked token paths.
        d = os.path.realpath(d)
        client.dump(d)
        path = os.path.join(d, "garmin_tokens.json")
        os.chmod(path, 0o600)
        with open(path, encoding="utf-8") as f:
            return f.read()


def start_login(email: str, password: str, *, is_cn: bool, attempts: int = 2,
                backoff: float = 6.0, sleep=time.sleep) -> LoginResult:
    """Log in, retrying transient/blocked failures a couple of times with a short
    backoff. Garmin (via Cloudflare) 429-rate-limits fresh logins on the mobile SSO
    endpoint — per-account, not per-IP (garth#217, garminconnect#344) — and the
    widget/portal fallback can flake (403); a quick retry usually gets through.
    Wrong credentials ('auth') are NOT retried. Retries are deliberately small/short
    so the synchronous authorize POST stays under the OAuth callback timeout.

    Raises GarminLoginError with .reason in {auth, blocked, unknown}."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            g = Garmin(email=email, password=password, is_cn=is_cn, return_on_mfa=True)
            # Pass an explicit, request-private tokenstore so a gateway-level
            # GARMINTOKENS value can never substitute another account's cached
            # credentials for the email/password submitted on this form.
            with tempfile.TemporaryDirectory() as tokenstore:
                tokenstore = os.path.realpath(tokenstore)
                try:
                    result1, result2 = g.login(tokenstore)
                finally:
                    # Detach before TemporaryDirectory removes the path. This
                    # also covers dependency exceptions and non-MFA success.
                    dependency_client = getattr(g, "client", None)
                    if dependency_client is not None:
                        dependency_client._tokenstore_path = None
            if result1 == "needs_mfa":
                # garminconnect returns before its normal password cleanup on
                # the MFA path. Continuation uses the opaque client state and
                # does not need the password, so remove it before stashing g.
                _sanitize_pending_client(g)
                return LoginResult(status="needs_mfa", pending=(g, result2))
            return LoginResult(status="ok", tokens_json=_dump_tokens(g.client))
        except GarminConnectAuthenticationError as e:
            raise GarminLoginError(str(e), reason="auth") from e   # bad password — never retry
        except (GarminConnectTooManyRequestsError, GarminConnectConnectionError) as e:
            last = e                                               # rate-limited / blocked / flaky
        except Exception as e:  # noqa: BLE001 - unexpected; retry once, then surface
            last = e
        if attempt + 1 < attempts:
            sleep(backoff)
    reason = "blocked" if isinstance(
        last, (GarminConnectTooManyRequestsError, GarminConnectConnectionError)) else "unknown"
    raise GarminLoginError(str(last) if last else "login failed", reason=reason)


def resume_login(pending, mfa_code: str) -> str:
    client, state = pending
    try:
        # Defense in depth for pending states created by an older gateway
        # process, or a dependency that restored this private field.
        client.client._tokenstore_path = None
        client.resume_login(state, mfa_code)
    finally:
        # Defensive for garminconnect versions that restore or retain it, and
        # for a rejected code whose pending state will be stashed again.
        _sanitize_pending_client(client)
    return _dump_tokens(client.client)


def verify_tokens(tokens_json: str, *, is_cn: bool) -> VerifiedTokens:
    """Authenticate tokens and return the exact post-verification generation.

    A successful g.login() without exception already proves authentication —
    stale/invalid tokens raise GarminConnectAuthenticationError (surfaced below).
    The name is only a log field, so an empty fullName (which a valid account may
    legitimately have) must NOT be treated as an auth failure."""
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        path = os.path.join(d, "garmin_tokens.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tokens_json)
        try:
            g = Garmin(is_cn=is_cn)
            g.login(d)
            name = g.get_full_name()
            # Refresh happens in memory before garminconnect attempts its
            # best-effort token-file dump. That dependency deliberately
            # suppresses dump failures, so reading the input file here could
            # return the stale pre-refresh generation after a successful
            # authentication. Serialize the authenticated client itself.
            verified_tokens = normalize_tokens_json(g.client.dumps())
        except Exception as e:  # noqa: BLE001 - surface as our error type
            raise GarminLoginError(str(e).split(":")[0].strip() or e.__class__.__name__)
    return VerifiedTokens(name=name or "", tokens_json=verified_tokens)
