# Implementation Spec — Multi-tenant Garmin CN + Global

## 1. Scope

The upstream gateway is already multi-user. V1 should **only** add per-Garmin-account region routing. Avoid creating a new user system, database schema, worker manager, OAuth server, or token encryption layer unless an upstream gap is proven by tests.

## 2. Region model

Use a closed enum-like value:

- `global`
- `cn`

Any unknown value is rejected. For backward compatibility, a request or legacy stored record with no region is interpreted as `global`.

## 3. Versioned Garmin credential blob

Today the Garmin adapter stores the raw `garmin_tokens.json` serialized string as its encrypted adapter blob. Introduce a versioned adapter blob:

```json
{
  "v": 1,
  "region": "cn",
  "tokens": { "...": "raw garmin token fields" }
}
```

Requirements:

- `pack_blob(tokens_json, region) -> str`
- `unpack_blob(blob) -> (region, tokens_json)`
- Legacy raw Garmin token JSON must still parse and return `global`.
- Do not store Garmin password.
- Keep the wrapper inside the existing encrypted account blob; no database migration is required for V1.

## 4. Account identity

Account identity must include region so the same email can independently exist in both Garmin domains.

Recommended key:

```text
garmin:{region}:{normalized_email}
```

Examples:

```text
garmin:cn:person@example.com
garmin:global:person@example.com
```

If upstream already namespaces account keys by adapter, use `{region}:{normalized_email}` instead of duplicating `garmin:`.

## 5. Login page

Add a required Garmin region selector above credentials:

```text
Garmin account region
( ) China (garmin.cn)
( ) Global (garmin.com)
```

Behavior:

- The server accepts only `cn` or `global`.
- Missing field defaults to `global` for API/backward compatibility.
- Product UI may preselect `cn` for this deployment, but server-side default remains `global`.

## 6. Garmin login wrapper

Change the Garmin login helper contracts:

```python
start_login(email, password, *, is_cn: bool, ...)
verify_tokens(tokens_json, *, is_cn: bool)
```

Construct the `garminconnect.Garmin` client using `is_cn=is_cn` for credential login and token login.

MFA continuation state must retain region. Suggested state:

```text
(pending_garmin_state, email, region)
```

After MFA completes, return a packed versioned blob containing region + tokens.

## 7. Worker forwarding

The upstream Garmin worker already supports `GARMIN_IS_CN`. Region must be propagated per worker.

Recommended implementation without changing the generic worker interface:

### materialize(blob, workdir)

1. `unpack_blob(blob)`.
2. Write only the raw token JSON to `<workdir>/garmin_tokens.json` with mode `0600`.
3. Write the region to `<workdir>/.garmin_region` with mode `0600`.

### env(port, workdir)

Build the existing worker env, plus:

```text
GARMIN_IS_CN=true   # region == cn
GARMIN_IS_CN=false  # region == global
```

Read region from `<workdir>/.garmin_region`. Missing region marker means global for compatibility.

### read_back(workdir)

1. Read and validate the worker's refreshed `garmin_tokens.json`.
2. Read `.garmin_region`.
3. Re-pack as the versioned encrypted adapter blob.
4. Never allow a token refresh to drop or change region.

## 8. Verification

`GarminAdapter.verify(blob)` must unpack region first and verify tokens against the matching Garmin domain.

This prevents a CN account from being incorrectly marked invalid by a Global verification attempt.

## 9. Security invariants

Must remain true after the patch:

- plaintext Garmin password never reaches persistence/logging
- OAuth/MCP bearer tokens are not stored in plaintext if upstream hashes them
- Garmin session tokens remain encrypted at rest
- worker only binds loopback
- per-user workdir remains isolated
- region is never accepted from an MCP request after authentication; it comes from the stored account blob
- no caller can switch an authenticated account from CN to Global by crafting headers or MCP params
- logs must not contain Garmin tokens or passwords

## 10. Test matrix

### Blob

- pack/unpack CN
- pack/unpack Global
- legacy raw token JSON → Global
- malformed wrapper → fail closed
- invalid region → fail closed

### Login

- Global login creates `Garmin(..., is_cn=False)`
- CN login creates `Garmin(..., is_cn=True)`
- Global MFA preserves Global
- CN MFA preserves CN
- invalid region rejected before Garmin login
- password never included in result/state

### Identity

- same email + CN and Global produce different account keys
- email normalization still works

### Worker

- CN blob → `GARMIN_IS_CN=true`
- Global blob → `GARMIN_IS_CN=false`
- legacy blob → `GARMIN_IS_CN=false`
- token file stays `0600`
- region marker stays `0600`
- read_back after token rotation retains original region

### Verify

- CN blob calls token login with `is_cn=True`
- Global/legacy blob calls token login with `is_cn=False`

### Regression

- full MissingMCP test suite passes
- existing Global user records remain usable
- WHOOP/other adapters are untouched

## 11. End-to-end smoke tests

Use two test accounts if available:

1. CN account with MFA.
2. Global account.

For each:

- authorize via browser
- complete MFA if prompted
- connect MCP client
- list tools
- fetch profile or simple daily metric
- stop worker
- reconnect and ensure persisted token works
- force/reproduce token refresh if feasible and confirm region is retained

Never put test credentials in source, shell history committed to git, issue text, or CI secrets unless intentionally configured as protected secrets.
