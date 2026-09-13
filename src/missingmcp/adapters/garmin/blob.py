"""Versioned Garmin credential blobs.

The gateway stores this wrapper inside the existing encrypted account blob.
Workers still receive only the raw ``garmin_tokens.json`` object.
"""
from __future__ import annotations

import json


REGION_CN = "cn"
REGION_GLOBAL = "global"
REGIONS = frozenset({REGION_CN, REGION_GLOBAL})
_VERSION = 1
_WRAPPER_KEYS = frozenset({"v", "region", "tokens"})
_TOKEN_FIELDS = frozenset({"di_token", "di_refresh_token", "di_client_id"})


class GarminBlobError(ValueError):
    """The stored Garmin credential blob is malformed or unsupported."""


def validate_region(region: object) -> str:
    if not isinstance(region, str) or region not in REGIONS:
        raise GarminBlobError("invalid Garmin region")
    return region


def _json_object(value: str, *, label: str) -> dict:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GarminBlobError(f"invalid {label} JSON") from exc
    if not isinstance(parsed, dict):
        raise GarminBlobError(f"{label} must be a JSON object")
    return parsed


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _validated_tokens(tokens: dict) -> str:
    if _WRAPPER_KEYS.intersection(tokens):
        raise GarminBlobError("Garmin tokens contain credential-wrapper fields")
    missing = _TOKEN_FIELDS.difference(tokens)
    if missing:
        raise GarminBlobError("Garmin tokens are missing required fields")
    if any(not isinstance(tokens[field], str) or not tokens[field]
           for field in _TOKEN_FIELDS):
        raise GarminBlobError("Garmin token fields must be non-empty strings")
    return _canonical(tokens)


def normalize_tokens_json(tokens_json: str) -> str:
    """Validate and deterministically serialize raw Garmin token JSON.

    A raw token object must not itself look like this gateway's wrapper. This
    makes nested wrappers and partially damaged wrappers fail closed instead
    of silently becoming worker credentials.
    """
    tokens = _json_object(tokens_json, label="Garmin tokens")
    return _validated_tokens(tokens)


def pack_blob(tokens_json: str, region: str) -> str:
    region = validate_region(region)
    tokens = json.loads(normalize_tokens_json(tokens_json))
    return _canonical({"v": _VERSION, "region": region, "tokens": tokens})


def unpack_blob(blob: str) -> tuple[str, str]:
    """Return ``(region, raw_tokens_json)``.

    Legacy raw token objects contain none of the wrapper fields and therefore
    retain their historical Global meaning. Any object containing a wrapper
    field is parsed strictly as a wrapper; malformed wrappers never downgrade
    to Global.
    """
    data = _json_object(blob, label="Garmin credential blob")
    if not _WRAPPER_KEYS.intersection(data):
        return REGION_GLOBAL, _validated_tokens(data)
    if set(data) != _WRAPPER_KEYS:
        raise GarminBlobError("malformed Garmin credential wrapper")
    if type(data["v"]) is not int or data["v"] != _VERSION:
        raise GarminBlobError("unsupported Garmin credential version")
    region = validate_region(data["region"])
    if not isinstance(data["tokens"], dict):
        raise GarminBlobError("Garmin tokens must be a JSON object")
    return region, _validated_tokens(data["tokens"])


def is_legacy_blob(blob: str) -> bool:
    """Whether a valid blob is the pre-wrapper raw Global token object."""
    data = _json_object(blob, label="Garmin credential blob")
    if _WRAPPER_KEYS.intersection(data):
        # Validate a wrapper before answering so malformed data still fails.
        unpack_blob(blob)
        return False
    return True
