# Beer supporters: paying-user funnel step + beers-per-month on site

Label: wayfinder:map
Status: complete (2026-07-24) — destination reached, spec approved. Only parked task 06 + deferred
fog (BMC automation, notification) remain, none of them decisions.

## Destination

An operator-approved spec that a later build session can implement with nothing left to decide:
(1) Buy Me a Coffee "beer" purchases ingested into a `beer_purchased` PostHog event with
best-effort attribution to a gateway account (login email as `distinct_id`), so a **"paying user"
step can extend the connect funnel**; and (2) a **"beers this month" metric in PostHog** — the
operator's own Growth-dashboard number, not a public site element — off the same `beer_purchased`
events (fed by operator-entered beers for now; BMC automation deferred until its API/webhook is
reachable). Implementation itself is out of scope — it runs as a normal build session once the
spec is approved.

## Notes

- **Not a user-facing connector.** This is operator analytics + a public vanity counter, built on
  the existing Buy Me a Coffee account `buymeacoffee.com/venik` (branded "buy me a beer"). Today
  BMC is only a static outbound link (header button, support section, home support block — in
  `src/missingmcp/templates/_layout.html` and `home.html`); no API, no webhook, no env var.
- **Operator choices locked at charting (2026-07-24):**
  - Output = a spec to hand off (plan, don't build).
  - Attribution = best-effort: try to tie a beer to a gateway account (email); accept that some
    beers stay unattributed (anonymous BMC donors).
  - Site counter = live from BMC (automatic), not a hand-maintained number.
- **Interim pivot (2026-07-24):** BMC's developer portal returns nginx 400 (cookie-too-large) and
  won't issue an API token, so **automated BMC ingestion is deferred**. Bridge = **manual operator
  entry** via a CLI (`scripts/add_beer.py`, run `uv run python -m scripts.add_beer …`) → a SQLite
  `beers` record (local audit + future BMC-dedup anchor) + the `beer_purchased` event. **"Beers
  this month" is a PostHog metric, not a site counter** (scope corrected 2026-07-24; ticket 04).
  The event/attribution *design* (ticket 02) is unchanged; only the source is manual. See ticket
  02's Revision.
- **Inherited invariants** (PostHog telemetry design `docs/superpowers/specs/2026-07-20-posthog-telemetry-design.md`
  + CLAUDE.md): telemetry is fire-and-forget and must never block/crash a request; egress rule =
  identity + metadata only, email travels **only** as `distinct_id`, never content/secrets;
  PostHog event names/properties are a **stable schema** (adding `beer_purchased` is a taxonomy
  addition); dependency-light ethos (`backup.py`'s hand-rolled SigV4 signer is the precedent);
  env-gated like `POSTHOG_*` / `BACKUP_S3_*` (a new `BMC_*` family); single-node, process-local
  state; SQLite on `/data` is the durable store.
- Skills per ticket: `/research` (01); `/grilling` + `/domain-modeling` (02, 03); `/prototype` (04).

## Decisions so far

<!-- one line per closed ticket: gist + link -->

- [What does Buy Me a Coffee expose?](issues/01-bmc-capabilities.md) — BMC has **both** webhooks
  (`donation.created`, HMAC-signed via `x-signature-sha256`) and a Bearer-token REST v1 API; both
  expose the supporter **email** (`support_email`/`payer_email`), so best-effort attribution works
  (a minority of anonymous/name-only donations won't match). **No paid plan needed.** No
  server-side date filter → monthly count via a webhook-incremented tally. Full findings:
  [assets/bmc-capabilities.md](assets/bmc-capabilities.md). Graduated a provisioning task (06).
- [Beer ingestion + identity attribution model](issues/02-ingestion-attribution.md) — **API
  polling only, no webhook**: a lifespan-loop poll of `GET /v1/supporters` (env `BMC_API_TOKEN`)
  emits `beer_purchased` per new one-off donation (dedup via a `support_id`-derived event uuid,
  timestamped at purchase time) and sums `support_coffees` for the calendar-month counter. A beer
  = a one-off donation only; **count beers, not donations**. Attribution = exact lowercased-email
  match to an `account_key` → `distinct_id`=email; unmatched keeps the email (or
  `bmc:anon:<support_id>`) with `matched=false`. Egress: no name/note, email only as distinct_id.
  **Revised (interim, 2026-07-24):** BMC's developer portal is broken (nginx 400), so automated
  ingestion is deferred — beers are entered via a manual CLI (`scripts/add_beer.py`) that writes a
  SQLite `beers` record + the same event; **"beers this month" is a PostHog insight, not a site
  counter** (scope corrected — ticket 04); the table is local audit + future dedup anchor. Swap in
  a BMC writer later.
- ["Paying user" funnel step semantics](issues/03-funnel-step-semantics.md) — **one funnel, 4th
  ordered step "Paying (beer)" = `beer_purchased`**, conversion window widened 14d→**90d** (accepted
  cost: looser activation timing). Paying counts only *attributed* payers (a lower bound), so
  **next to it show `beer_purchased` broken down by `matched`** (attributed vs all beers) to make
  the gap visible. Both on the Growth dashboard; wiring happens at build (event has no data yet).
- ["Beers this month" — the operator's PostHog metric](issues/04-site-counter-display.md) —
  **re-scoped: not on the website.** It's the operator's PostHog metric — a Growth-dashboard trends
  insight summing the `beer_purchased` `beers` property per month (current month = "beers this
  month"), beside the funnel and the matched-vs-all breakdown. No template changes; the "Buy me a
  beer" button stays. `add_beer.py` writes a local SQLite `beers` record (audit + future dedup) and
  emits the event.
- [Write the implementation-ready spec](issues/05-write-spec.md) — **operator-approved** spec at
  [docs/superpowers/specs/2026-07-24-beer-supporters.md](../../docs/superpowers/specs/2026-07-24-beer-supporters.md).
  **Destination reached — the map is complete.**

## Not yet specified

- **Automated BMC ingestion** (API poll or webhook) to replace the manual writer — deferred;
  blocked by BMC's broken developer portal (nginx 400). Revisit when reachable, or pivot to the
  webhook path (research-confirmed live, configured in `studio.buymeacoffee.com`, not the broken
  developers subdomain).
- Whether a beer purchase should also trigger a Slack/PostHog notification (a "someone bought a
  beer!" ping) — a nice-to-have that hangs on the ingestion decision (ticket 02).
- The concrete build — gateway route(s), store schema, template edits, the PostHog funnel-insight
  change — graduates to the build session the spec hands off to (see Out of scope).

## Out of scope

- Implementing the feature. The destination is an approved spec; the build is a separate session
  that consumes it.
