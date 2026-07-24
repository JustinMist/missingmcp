# Beer Supporters Design

Date: 2026-07-24. Decisions made ticket-by-ticket on the wayfinder map
`.scratch/beer-metrics/` (research asset: `.scratch/beer-metrics/assets/bmc-capabilities.md`,
BMC facts gathered 2026-07-24). Builds directly on the PostHog telemetry design
(`2026-07-20-posthog-telemetry-design.md`) — same `telemetry.py` seam, same egress rule, same
stable-event-schema discipline. **Operator analytics, not a user-facing connector.**

## Goal

Two operator-facing outcomes, both off a single new `beer_purchased` PostHog event:

1. **A "paying user" step in the connect funnel** — someone who bought a beer (a donation on
   `buymeacoffee.com/venik`), best-effort attributed to their gateway account.
2. **A "beers this month" metric in PostHog** — the operator's own Growth-dashboard number (not a
   public website element).

**Interim ingestion is manual** — Buy Me a Coffee automation is deferred (see Deferred), so beers
are entered by the operator via a small CLI. Everything downstream (the event, attribution, the
funnel, the metric) is built now and does not change when automation later replaces the manual
writer.

## Decisions (from the map)

- **[01] BMC capabilities** — BMC exposes both webhooks (`donation.created`, HMAC-signed) and a
  Bearer-token REST API, and both carry the supporter email, so best-effort attribution is
  feasible. No paid plan required. (Asset: `assets/bmc-capabilities.md`.)
- **[02] Ingestion + attribution** — chosen mechanism was API polling; **revised to manual entry**
  after BMC's developer portal proved unusable (nginx `400 Request Header Or Cookie Too Large`,
  even incognito — a BMC-side bug, so no API token can be issued). The event/attribution/taxonomy
  design stands; only the source is manual.
- **[03] Funnel step semantics** — one funnel, 4th ordered step "Paying (beer)", conversion window
  widened 14d→90d; plus a "matched vs all beers" breakdown because the funnel step only counts
  attributed payers (a lower bound).
- **[04] "Beers this month"** — re-scoped: it's a **PostHog metric**, not a site counter. No web
  changes; the "Buy me a beer" button stays.

## Architecture

### Interim ingestion — `scripts/add_beer.py` (new)

A CLI in the existing `scripts/` set, modelled on `scripts/revoke.py` (which already opens the
store *and* emits a telemetry event). Invocation mirrors the other scripts:

```
python scripts/add_beer.py --email honza@example.cz              # 1 beer, 5 EUR
python scripts/add_beer.py --email honza@example.cz --beers 3    # 3 beers, 15 EUR
python scripts/add_beer.py --email x@y.cz --beers 2 --amount 12 --currency USD --at 2026-07-20
# On Railway (where GATEWAY_SECRET/POSTHOG_API_KEY are present in env):
#   railway ssh --service gateway "python3 /app/scripts/add_beer.py --email honza@example.cz"
```

- **Args**: `--email <supporter>` (required), `--beers <n>` (default 1), `--amount` / `--currency`
  (optional overrides), `--at <YYYY-MM-DD>` (default today). DB path resolves exactly like
  `daily_report.py` / `usage.py` (`$DB_PATH` → `$DATA_DIR/gateway.db` → `/data` → `./.localdata`).
- **Amount default**: one beer = **5 EUR**, so `amount = beers × 5`, `currency = EUR` unless
  overridden. The 5 EUR unit price is a manual constant (a module-level `BEER_PRICE_EUR = 5`); the
  future BMC writer will carry BMC's real per-donation amount instead.
- **On run**: (a) normalize the email (strip + lowercase — reuse `adapters.base.normalize_account_key`);
  (b) attribution lookup (below); (c) insert a `beers` row; (d) emit `beer_purchased` to PostHog;
  (e) print a one-line confirmation (email, beers, amount, `matched`). Fire-and-forget telemetry —
  a PostHog failure must not abort the DB write or crash the script.

### Data model — `beers` table (`store.py`)

Additive table appended to `_SCHEMA` (a `CREATE TABLE IF NOT EXISTS`, so it materializes on the
next open — **no `PRAGMA user_version` bump**; the guarded migration is only for the v0→v1
rewrite):

```sql
CREATE TABLE IF NOT EXISTS beers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT,                       -- supporter email, lowercased; NULL if anonymous
    beers       INTEGER NOT NULL DEFAULT 1, -- coffee count for this donation
    amount      REAL,                       -- money value (beers × unit price)
    currency    TEXT,
    matched     INTEGER NOT NULL DEFAULT 0, -- 1 if email matched an account_key
    source      TEXT NOT NULL DEFAULT 'manual',
    created_at  TEXT DEFAULT (datetime('now'))  -- purchase time (--at, else now)
);
```

Role: **local audit/record**, and the future high-water-mark anchor for BMC-dedup when automation
lands. It does **not** feed a website counter (there is none). Small CRUD helpers in `store.py`:
`add_beer(conn, ...)` and `account_key_exists(conn, email) -> bool`
(`SELECT 1 FROM accounts WHERE account_key = ? LIMIT 1` — any adapter).

### Attribution

- Match rule: normalize the supporter email and check whether it exists as an `account_key` in
  `accounts` (any adapter). This is the same identity the connect funnel keys on (`distinct_id` =
  plain login email, per the telemetry design).
- **Matched** → `matched=1`, `distinct_id` = the email (so the beer joins that person's funnel).
- **Unmatched but email present** → `matched=0`, `distinct_id` = the email (a supporter-only
  person; PostHog stitches it if they later sign up with the same email).
- **Anonymous** (no `--email`) → `matched=0`, `distinct_id` = synthetic `manual:anon:<beers.id>`
  (no PII), still emitted so the event stream is complete.
- Interim note: in manual entry the operator types the login email, so matching is usually
  complete; the undercount mainly bites later with the automated BMC feed (donor's BMC email may
  differ from their login).

### Event — `beer_purchased` (`telemetry.py`)

Emitted via the existing `telemetry.capture(...)` seam (a no-op when `POSTHOG_API_KEY` is unset).
Part of the stable event schema — same discipline as the log events.

| Field | Value |
|---|---|
| `distinct_id` | email (matched or unmatched-with-email), else `manual:anon:<id>` |
| property `beers` | coffee count (int) |
| property `amount` | `beers × unit price` |
| property `currency` | e.g. `EUR` |
| property `matched` | `true` / `false` |
| property `source` | `manual` (later `bmc` for the automated writer) |
| event **timestamp** | `created_at` (the purchase time — so the funnel reflects when the beer was bought, not when it was entered) |

**Egress rule (inherited):** the supporter **name** and the donation **note/message** are never
captured; the email travels **only** as `distinct_id`. No secrets.

**Small `telemetry.py` extension**: `capture(...)` gains an optional `timestamp` kwarg passed
through to the SDK client's `timestamp` (needed for `--at` backdating). Keep it optional — existing
call sites are unaffected.

### PostHog insights (built at implementation, once `beer_purchased` has data)

All on the **Growth** dashboard (id 837142). Built only after a first beer exists — no empty
insights on the live dashboard.

1. **Connect + paying funnel** — modify the existing connect funnel insight (`44rnAeDc`): append a
   4th ordered step `beer_purchased`, widen the conversion window 14d→**90d**, and rename (e.g.
   "Connect → paying funnel"). Accepted cost: the 90d window loosens the visit→connect→use timing,
   so it no longer measures *fast* activation.
2. **Matched vs all beers** — a `beer_purchased` count broken down by `matched` (true/false), so
   the gap between attributed payers (in the funnel) and all beers is visible at a glance.
3. **Beers this month** — a trends insight summing the `beers` property, bucketed by month; the
   current month's value is "beers this month."

### Config / env

No new **required** env for the interim — the event path reuses `POSTHOG_API_KEY` (already the
gate for all telemetry). The `BMC_*` family is deferred with the automation. The script builds its
config via `load_config(os.environ)` (which needs `GATEWAY_SECRET` — present on Railway, where the
script runs) but does **not** decrypt any blob (attribution only checks `account_key` existence).

## Invariants (inherited, must hold)

- **Egress**: identity + metadata only — email as `distinct_id`, `beers`/`amount`/`currency`/
  `matched`/`source` as properties; never the supporter name or note, never secrets.
- **Fire-and-forget telemetry**: a PostHog outage never blocks or fails the beer write.
- **Stable event schema**: `beer_purchased` and its property names/value-types are fixed (heed the
  2026-07-21 lesson — never reuse one property name for two value types across events).
- **Secrets**: the script needs no new secret; `GATEWAY_SECRET` is only read to build config, never
  logged.

## Testing

- `tests/test_beers.py` (unit): `account_key_exists` + `add_beer` against a temp DB; attribution
  outcomes (matched / unmatched-with-email / anonymous → correct `matched` + `distinct_id`);
  `beer_purchased` properties + timestamp asserted via an injected telemetry stub (mirrors the
  telemetry-stub pattern in the OAuth/proxy tests).
- No-op contract: with `POSTHOG_API_KEY` unset the script still writes the `beers` row and prints,
  emitting nothing (existing telemetry-off tests keep passing).
- **Release gate** (matches repo culture — manual): enter a test beer for a known test account →
  verify the `beers` row, the `beer_purchased` event in PostHog (correct props + timestamp), and
  the person joining the funnel's paying step; enter an unmatched email → `matched=false` and no
  funnel join.

## Deferred / Out of scope

- **Automated BMC ingestion** (API poll or webhook) — blocked by BMC's broken developer portal
  (nginx 400; task 06 parked). Swap-in path: an automated writer that writes the **same** `beers`
  row + emits the **same** `beer_purchased` event (`source='bmc'`, real amounts, dedup via a
  high-water-mark on the `beers` table); the manual `add_beer.py` stays as a fallback. Revisit when
  the portal is reachable, or pivot to the webhook path (research-confirmed live, configured in
  `studio.buymeacoffee.com`).
- **"Someone bought a beer" notification** (Slack/PostHog ping) — nice-to-have, not in this cut.
- **Any website change** — explicitly out; the counter is a PostHog metric.
- The **5 EUR** unit price is a manual constant for hand entry, not a real per-donation figure.
