from __future__ import annotations
import os
import tempfile
from typing import Callable, Mapping
from ..base import (LoginError, LoginOk, SecondFactorError, SecondFactorNeeded,
                    Verification, normalize_account_key)
from . import login
from .blob import (GarminBlobError, REGION_CN, REGION_GLOBAL, pack_blob,
                   unpack_blob, validate_region, normalize_tokens_json)


# The worker's two possible sign-in verdicts, printed exactly once per worker
# life by garmin_mcp's login path. Since the login moved to a background thread
# (garmin_mcp #255, pinned from e8554bc) these lines are the ONLY startup signal
# that the stored tokens still work — the worker answers /healthz either way.
# Substring match, not equality: the worker appends detail after each.
_LOGIN_OK_LINE = "Garmin Connect client initialized successfully"
_LOGIN_FAILED_LINES = (
    "Garmin Connect client failed to initialize",       # >= e8554bc (background login)
    "Failed to initialize Garmin Connect client",       # older pins (exit-on-failure era)
)
_FALLBACK_CREDENTIAL_ENV = frozenset({
    "GARMIN_EMAIL",
    "GARMIN_PASSWORD",
    "GARMIN_EMAIL_FILE",
    "GARMIN_PASSWORD_FILE",
    "GARMINTOKENS_BASE64",
})


class GarminWorkerForward:
    """WorkerForward strategy for the unmodified garmin-mcp worker: its documented
    CLI + env contract (GARMIN_MCP_* / GARMINTOKENS) and token-file materialization."""

    def __init__(self, config):
        self._cfg = config
        # Per-workdir authority established by materialize(). A missing marker
        # defaults to Global only for an unknown legacy workdir; it can never
        # downgrade a known CN workdir after materialization.
        self._expected_regions: dict[str, str] = {}

    def login_outcome(self, line: str) -> str | None:
        """Classify one worker log line as the sign-in outcome — "ok", "failed",
        or None (not a sign-in line). Fed by the worker output pump into the
        spawn's LoginGate; ensure_worker blocks on it so stale tokens still
        become a re-auth 401 instead of per-call "run garmin-mcp-auth" tool
        errors that a missingmcp user can't act on."""
        if _LOGIN_OK_LINE in line:
            return "ok"
        if any(marker in line for marker in _LOGIN_FAILED_LINES):
            return "failed"
        return None

    def command(self) -> list[str]:
        return self._cfg.garmin_mcp_cmd

    @staticmethod
    def sanitize_env(env: Mapping[str, str]) -> dict[str, str]:
        """Remove worker fallback credentials inherited from the gateway.

        A worker is authorized only by its account-specific token directory.
        If those tokens are stale, the pinned worker must fail instead of
        logging in with a deployment-level email/password from another user.
        """
        return {key: value for key, value in env.items()
                if key not in _FALLBACK_CREDENTIAL_ENV}

    def env(self, port: int, workdir: str) -> dict[str, str]:
        region = self._read_region(workdir)
        return {
            "GARMIN_MCP_TRANSPORT": "streamable-http",
            "GARMIN_MCP_HOST": "127.0.0.1",
            "GARMIN_MCP_PORT": str(port),
            "GARMINTOKENS": workdir,
            "GARMIN_IS_CN": "true" if region == REGION_CN else "false",
        }

    def materialize(self, blob: str, workdir: str) -> None:
        region, tokens_json = unpack_blob(blob)
        workdir = os.path.realpath(workdir)
        self._expected_regions[workdir] = region
        self._write_private(os.path.join(workdir, "garmin_tokens.json"), tokens_json)
        self._write_private(os.path.join(workdir, ".garmin_region"), region)

    def prepare_read_back(self, blob: str, workdir: str) -> None:
        """Restore the DB-owned region constraint without touching disk.

        A fresh gateway may recover a stopped worker's pending token rotation
        before its first materialize.  Seeding only this in-memory expectation
        ensures a missing, invalid or opposite-domain sidecar fails closed;
        calling materialize here would destroy the rotation being recovered.
        """
        region, _tokens_json = unpack_blob(blob)
        self._expected_regions[os.path.realpath(workdir)] = region

    @staticmethod
    def _write_private(path: str, content: str) -> None:
        directory = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(prefix=".missingmcp-", dir=directory, text=True)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read_region(self, workdir: str) -> str:
        workdir = os.path.realpath(workdir)
        expected = self._expected_regions.get(workdir)
        try:
            with open(os.path.join(workdir, ".garmin_region"), encoding="utf-8") as f:
                region = f.read()
        except FileNotFoundError:
            if expected is None:
                return REGION_GLOBAL
            raise GarminBlobError("Garmin region marker is missing") from None
        region = validate_region(region)
        if expected is not None and region != expected:
            raise GarminBlobError("Garmin region marker does not match the account")
        return region

    def read_back(self, workdir: str) -> str | None:
        # garth (inside the worker) rewrites this file when Garmin rotates the
        # refresh token, and it may not write atomically — a torn file must
        # never reach the store, so anything unparseable reads as "nothing".
        path = os.path.join(workdir, "garmin_tokens.json")
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            tokens_json = normalize_tokens_json(content)
            region = self._read_region(workdir)
            os.chmod(path, 0o600)
        except (OSError, GarminBlobError, ValueError):
            return None
        return pack_blob(tokens_json, region)


def _login_error_message(reason: str) -> str:
    if reason == "blocked":
        # Garmin (via Cloudflare) rate-limits fresh logins on the mobile SSO
        # endpoint — per-account, not per-IP (garth#217, garminconnect#344) — and
        # the widget/portal fallback can flake. Not the user's fault; a retry usually works.
        return ("Garmin is temporarily rate-limiting new sign-ins (a limit on "
                "Garmin's side, not your password). Please wait a couple of minutes and try again.")
    if reason == "auth":
        return "Garmin sign-in failed — check your Garmin email and password."
    return "Garmin sign-in failed, please try again."


class GarminAdapter:
    name = "garmin"
    display_name = "Garmin"
    authorize_template = "authorize.html"
    second_factor_template = "mfa.html"
    landing_template = "garmin.html"

    def __init__(self, config, account_key_resolver: Callable[[str, str], str] | None = None):
        self.forward = GarminWorkerForward(config)
        self._account_key_resolver = account_key_resolver

    def login_hint(self, form: Mapping[str, str]) -> str:
        return form.get("garmin_email", "")

    @staticmethod
    def _region(form: Mapping[str, str]) -> str:
        value = form.get("garmin_region")
        if value is None:
            return REGION_GLOBAL
        try:
            return validate_region(value)
        except GarminBlobError:
            raise LoginError("Choose a valid Garmin account region.", reason="auth") from None

    def _account_key(self, email: str, region: str) -> str:
        normalized = normalize_account_key(email)
        if self._account_key_resolver is not None:
            return self._account_key_resolver(normalized, region)
        return f"{region}:{normalized}"

    def start_login(self, form: Mapping[str, str]) -> LoginOk | SecondFactorNeeded:
        email = form.get("garmin_email", "")
        password = form.get("garmin_password", "")
        region = self._region(form)
        try:
            result = login.start_login(email, password, is_cn=region == REGION_CN)
        except login.GarminLoginError as e:
            reason = getattr(e, "reason", "unknown")
            # Do not retain/log an upstream exception chain: third-party error
            # strings are not guaranteed to exclude the submitted password.
            raise LoginError(_login_error_message(reason), reason=reason) from None
        finally:
            password = ""  # never returned or placed in MFA state
        if result.status == "needs_mfa":
            return SecondFactorNeeded(state=(result.pending, email, region))
        return LoginOk(account_key=self._account_key(email, region),
                       blob=pack_blob(result.tokens_json, region))

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        pending, email, region = state
        try:
            region = validate_region(region)
        except GarminBlobError:
            raise LoginError("Garmin MFA session is invalid; please sign in again.") from None
        try:
            tokens = login.resume_login(pending, form.get("mfa_code", ""))
        except Exception as e:  # noqa: BLE001 - wrong/expired code: caller re-prompts
            raise SecondFactorError("Incorrect or expired code, try again", state=state) from None
        return LoginOk(account_key=self._account_key(email, region),
                       blob=pack_blob(tokens, region))

    def verify(self, blob: str) -> Verification:
        try:
            region, tokens_json = unpack_blob(blob)
            verified = login.verify_tokens(tokens_json, is_cn=region == REGION_CN)
            # Compatibility for injected/older helpers that returned only the
            # display name. The production helper returns VerifiedTokens.
            if isinstance(verified, str):
                return Verification(name=verified, blob=pack_blob(tokens_json, region))
            return Verification(name=verified.name,
                                blob=pack_blob(verified.tokens_json, region))
        except (login.GarminLoginError, GarminBlobError):
            raise LoginError("Garmin sign-in could not be verified") from None
