# Garmin MCP Gateway

A multi-user OAuth 2.1 gateway that lets a small trusted circle connect their
own Garmin account to Claude (mobile/desktop/web), by wrapping the unmodified
[`garmin_mcp`](https://github.com/Taxuspt/garmin_mcp) worker.

```
Claude → POST /mcp (Bearer) → Gateway → 127.0.0.1:<port>/mcp (per-user garmin-mcp) → connect.garmin.com
```

## Quick start

```bash
cp .env.example .env          # set GATEWAY_SECRET, PUBLIC_URL, GARMIN_MCP_REF
docker compose up -d --build
```

Put nginx in front for TLS + your domain (see `nginx.conf.example`), then add
`https://<your-domain>/mcp` as a remote MCP server in Claude.

## How it works

1. Claude registers a client (DCR) and starts OAuth 2.1 (Authorization Code + PKCE).
2. On the authorize page the user signs in with Garmin (email + password, + MFA
   if prompted). The gateway logs in via `garminconnect`, stores **only the
   resulting tokens** (encrypted), and discards the password.
3. Claude exchanges the code for a Bearer token.
4. On each `/mcp` call the gateway ensures the user's `garmin-mcp` worker is
   running (its own tokens, bound to `127.0.0.1`) and reverse-proxies to it.

## Security

- Garmin password is never persisted.
- Tokens encrypted at rest (AES-256-GCM); the DB is useless without `GATEWAY_SECRET`.
- Bearer tokens stored only as SHA-256 hashes.
- OAuth 2.1 PKCE (S256), one-time 10-min codes, CSRF on forms, per-IP/-token rate limits.
- Workers bind `127.0.0.1` only; `garmin_mcp` is pinned to a reviewed commit.

> Deploy only on infrastructure you control and trust. Back up `/data`; keep
> `GATEWAY_SECRET` separately.

## Development

```bash
uv pip install -e ".[dev]"
uv run pytest -v
```
