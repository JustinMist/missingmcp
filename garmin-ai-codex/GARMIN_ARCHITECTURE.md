# Architecture

```text
MCP clients (Codex / Claude / future ChatGPT)
                  |
                  v
        MissingMCP OAuth 2.1 Gateway
                  |
        Bearer -> account identity
                  |
      encrypted Garmin adapter blob
        {region, session tokens}
                  |
            Worker Manager
       +----------+-----------+
       |                      |
       v                      v
User A worker             User B worker
GARMIN_IS_CN=true         GARMIN_IS_CN=false
GARMINTOKENS=/A           GARMINTOKENS=/B
       |                      |
       v                      v
connect.garmin.cn         connect.garmin.com
```

## Why this is the V1 architecture

MissingMCP already supplies the costly platform pieces: multi-user OAuth, encryption, account persistence, worker isolation, token lifecycle, rate limiting and reverse proxy. Taxuspt/garmin_mcp already supplies Garmin MCP tools and CN switching. The smallest safe change is to make Garmin region an account-scoped property that follows the encrypted token blob into the worker environment.

## Non-goals for V1

Do not add these yet:

- AI training analysis layer
- user-facing iOS app
- Apple Health / Health Connect ingestion
- PostgreSQL migration
- Redis
- billing
- organization/tenant admin console
- background Garmin data warehouse
- cross-user aggregate analytics

Those are Phase 2+ after the CN/Global gateway is proven.
