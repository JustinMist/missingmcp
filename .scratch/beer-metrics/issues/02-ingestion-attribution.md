# Beer ingestion + identity attribution model

Type: grilling
Status: resolved
Blocked by: 01

## Question

Given what BMC exposes (01), decide the ingestion architecture and the identity bridge:

- **Ingestion**: webhook (BMC → a new gateway endpoint, e.g. `/bmc/webhook`, signature-verified)
  vs polling BMC's API on a schedule vs both. How the `beer_purchased` PostHog event is emitted —
  fire-and-forget via the existing `telemetry.py`, env-gated by a new `BMC_*` family.
- **Attribution**: how a BMC supporter is matched to a gateway account — exact match of the BMC
  supporter email against a known `account_key` (lowercased login email)? What `distinct_id` does
  an **unmatched / anonymous** beer get: a real person keyed by the BMC email, a synthetic
  anonymous id, or dropped from the per-user event but still counted in the monthly total?
- **Counter data path**: the operator chose "live from BMC" — is the monthly total a direct BMC
  API read (cached server-side) or a tally maintained from webhook events? Keep it consistent with
  the ingestion choice.
- **Event taxonomy**: `beer_purchased` properties (amount, currency, `matched` true/false, one-off
  vs membership…), honoring the egress rule — email only as `distinct_id`, never the supporter's
  message body.

## Answer

Resolved 2026-07-24 (grilling with the operator). Headline: **API polling only — no webhook.**

**Product scope (operator's calls):**
- A "beer" = a **one-off donation only** (`donation.created` / `GET /v1/supporters`). Memberships,
  extras/shop and commissions do **not** count.
- **Count beers, not donations**: one donation can be N beers (`support_coffees`) — the monthly
  counter sums coffees and the event carries the count.
- Emit `beer_purchased` for **all** beers, matched or not (a `matched` property flags which).

**Ingestion — one integration, API pull:**
- A scheduled poll in the app lifespan loop (mirrors `backup.py` / `report.py`: `enabled`/`due`/
  `run`, `run` never raises), env-gated by `BMC_API_TOKEN` (unset ⇒ the whole beer feature is a
  no-op). Interval ~15–30 min — freshness is irrelevant for analytics.
- Each poll: `GET /v1/supporters` (Bearer token), page newest-first, stop once past the
  **high-water mark** of already-processed donations (persist the last-seen `support_id` /
  `support_created_on` in SQLite). Each new donation → one `beer_purchased`.
- **No `/bmc/webhook`, no HMAC, no public endpoint.** Rejected because this is analytics (a few
  minutes' delay is fine) and polling is self-correcting — nothing is lost on downtime — with one
  credential and zero attack surface. Webhook-only and webhook+reconciliation were considered and
  dropped (the difference is delivery, not data: from PostHog's view the event is identical either
  way; webhook adds real-time we don't need at the cost of a signed public endpoint and missed
  events on downtime).

**Dedup:** each `beer_purchased` carries a **deterministic event `uuid` derived from `support_id`**,
so a donation re-listed on a later poll is deduped by PostHog. The persisted high-water mark is the
primary guard; the uuid is the safety net.

**Attribution:** normalize the supporter email (strip + lowercase — the `normalize_account_key`
rule) and exact-match it against an existing `account_key` in `accounts` (any adapter).
- Match → `matched=true`, `distinct_id` = that email (same identity as the connect funnel, so the
  beer joins the funnel).
- No match but email present → `matched=false`, `distinct_id` = the email (a supporter-only person;
  PostHog stitches it if they later sign up with the same email).
- Fully anonymous (no email) → `distinct_id` = synthetic `bmc:anon:<support_id>` (no PII), still
  emitted so the stream is complete.

**Counter data path:** the same poll computes **"beers this month"** = Σ `support_coffees` over
donations whose `support_created_on` is in the current **calendar month**, and caches it for the
landing page to read. "Live from BMC," self-correcting, no separate mechanism.

**`beer_purchased` taxonomy** (stable schema; egress rule honored):
- `distinct_id`: email, or `bmc:anon:<support_id>` — identity travels only as distinct_id.
- Properties: `beers` (int; coffee count), `amount` (`support_coffees × support_coffee_price`),
  `currency`, `matched` (bool).
- Event **timestamp = `support_created_on`** (the real purchase time, not poll time) so the funnel
  reflects when the beer was actually bought despite delayed ingestion.
- **Never** sent: supporter name, the donation note/message (content), the API token.

**Knock-on:** ticket 06 re-scoped to API-only (issue token + confirm `GET /v1/supporters` fields,
private-payment email behavior, date-param probe); its webhook-provisioning parts are dropped.
Ticket 04's counter data source is now settled (read the cached monthly total); it stays a
prototype about placement/copy/fallback.

## Revision — 2026-07-24 (interim: manual entry)

Working task 06 surfaced that BMC's **developer portal is unusable**:
`developers.buymeacoffee.com/dashboard` returns nginx **400 "Request Header Or Cookie Too Large"**
even in a clean incognito session — a BMC-side bug (their login sets a cookie too large for that
subdomain's nginx buffer). So an API token **can't currently be provisioned** and the "API polling
only" mechanism above is blocked.

Operator's call: **bridge with manual entry for now** (BMC automation deferred, not abandoned).
Everything else in this ticket **stands unchanged** — the `beer_purchased` event, email
attribution, the `matched` flag, taxonomy, and "beers this month = Σ coffees in the calendar
month." **Only the ingestion source changes**: an operator-run CLI instead of a BMC poll.

**Interim ingestion — `scripts/add_beer.py`** (fits the existing `scripts/` set; invoked
`python scripts/add_beer.py …`, the repo's script convention):

- Args: `--email <supporter>` (**required** — the manual CLI never produces an anonymous beer;
  that path is reserved for the future BMC writer), `--beers <n>` (default 1),
  `--at <YYYY-MM-DD>` (default today).
  **Amount is derived by default** — one beer = **5 EUR**, so `amount = beers × 5` and
  `currency = EUR` unless overridden with the optional `--amount` / `--currency`. So the normal
  invocation is just `--email` (+ `--beers` if more than one). The 5 EUR unit price is a manual
  constant; when the BMC writer lands later it carries BMC's real per-donation amount instead.
- On run: (a) normalize the email (strip + lowercase) and best-effort match it against an existing
  `account_key` → `matched` bool; `distinct_id` = the email (matched or not), or `manual:anon`
  when no email is given; (b) insert a row into a new SQLite **`beers`** table
  `(email, beers, amount, currency, matched, created_at, source='manual')`; (c) fire
  `beer_purchased` to PostHog via `telemetry.py` (props `beers` / `amount` / `currency` /
  `matched` / `source='manual'`, event timestamp = `created_at`).
- **No site counter.** "beers this month" is the operator's **PostHog metric** (a Growth-dashboard
  insight — see ticket 04), not a landing-page element. The `beers` table's role is a **local
  audit/record + future BMC-dedup high-water-mark**, not feeding a page. (Scope corrected
  2026-07-24: the operator wants this metric in PostHog, not on the website.)

**Swap-in path:** when BMC becomes reachable, add an automated writer (API poll or webhook) that
writes the same `beers` row + the same event; the manual script stays as a fallback. **Task 06 is
parked** until then.
