import json

import pytest

from missingmcp.adapters.garmin.blob import (
    GarminBlobError,
    pack_blob,
    unpack_blob,
    validate_region,
)


def _tokens(generation: str = "1", **extra) -> str:
    return json.dumps({
        "di_token": f"access-{generation}",
        "di_refresh_token": f"refresh-{generation}",
        "di_client_id": f"client-{generation}",
        **extra,
    }, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("region", ["cn", "global"])
def test_pack_unpack_round_trip_is_deterministic(region):
    tokens = _tokens("1", z=2, a=1)
    packed = pack_blob(tokens, region)
    assert packed == json.dumps(
        {"v": 1, "region": region, "tokens": json.loads(tokens)},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert unpack_blob(packed) == (region, tokens)


def test_legacy_raw_tokens_default_to_global():
    tokens = _tokens()
    assert unpack_blob(tokens) == ("global", tokens)


@pytest.mark.parametrize("region", ["", "CN", "Global", "eu", None, True])
def test_invalid_region_fails_closed(region):
    with pytest.raises(GarminBlobError):
        validate_region(region)


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "null",
        "[]",
        '{"v":1}',
        '{"v":1,"region":"cn","tokens":[]}',
        '{"v":1,"region":"cn","tokens":{"v":1}}',
        '{"region":"cn","oauth":"token"}',
    ],
)
def test_malformed_or_unsupported_blob_fails_closed(value):
    with pytest.raises(GarminBlobError):
        unpack_blob(value)


@pytest.mark.parametrize("bad_version", [True, 2, None])
def test_wrapper_version_is_independently_validated(bad_version):
    wrapper = json.loads(pack_blob(_tokens(), "cn"))
    if bad_version is None:
        del wrapper["v"]
    else:
        wrapper["v"] = bad_version
    with pytest.raises(GarminBlobError):
        unpack_blob(json.dumps(wrapper))


@pytest.mark.parametrize("bad_region", ["eu", "CN", "", True, None])
def test_wrapper_region_is_independently_validated(bad_region):
    wrapper = json.loads(pack_blob(_tokens(), "global"))
    wrapper["region"] = bad_region
    with pytest.raises(GarminBlobError):
        unpack_blob(json.dumps(wrapper))


def test_wrapper_extra_field_is_rejected_with_otherwise_valid_tokens():
    wrapper = json.loads(pack_blob(_tokens(), "cn"))
    wrapper["unexpected"] = "value"
    with pytest.raises(GarminBlobError):
        unpack_blob(json.dumps(wrapper))


def test_pack_rejects_nested_or_non_object_tokens():
    with pytest.raises(GarminBlobError):
        pack_blob('["token"]', "cn")
    with pytest.raises(GarminBlobError):
        pack_blob('{"v":1,"region":"cn","tokens":{}}', "cn")


@pytest.mark.parametrize(
    "tokens",
    [
        "{}",
        '{"unrelated":"value"}',
        '{"di_token":"a","di_refresh_token":"r"}',
        '{"di_token":"","di_refresh_token":"r","di_client_id":"c"}',
        '{"di_token":"a","di_refresh_token":null,"di_client_id":"c"}',
    ],
)
def test_structurally_invalid_token_objects_fail_closed(tokens):
    with pytest.raises(GarminBlobError):
        pack_blob(tokens, "global")
    with pytest.raises(GarminBlobError):
        unpack_blob(tokens)
