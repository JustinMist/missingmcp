# What does Buy Me a Coffee expose (for the venik account)?

Type: research
Status: resolved
Blocked by: —

## Question

To attribute beers to users (funnel) and show a live monthly beer count (site), we need to know
what Buy Me a Coffee actually offers. Surface, from high-trust primary sources (BMC's own
help/developer/API docs and dashboard settings pages):

1. **Webhooks**: does BMC send webhooks on a donation / new-supporter event? What event types
   exist, what does the payload contain (supporter name, supporter **email**, amount, currency,
   message, timestamp, one-off vs membership), and is there a signing secret to verify
   authenticity?
2. **REST API**: is there a public API (e.g. supporters / one-time-purchases endpoints)? What auth
   (personal access token?), what data does it return (per-supporter incl. email? aggregate
   totals? date filtering for "this month"?), and any rate limits.
3. **Plan / eligibility**: are webhooks and/or the API gated behind a paid BMC plan or a
   membership feature? What does the free tier get? (State assumptions clearly if the `venik`
   account's plan can't be confirmed from outside.)
4. **Identity signal** (the crux): does either channel expose the **supporter's email** (so it can
   be matched to a gateway login email), or only a display name? How commonly are donations
   anonymous?

Capture findings as `.scratch/beer-metrics/assets/bmc-capabilities.md` and link it from the
`## Answer` here on resolution. This unblocks the ingestion/attribution decision (02) and the
counter's data source.

## Answer

Resolved 2026-07-24 by a `/research` subagent. Full findings:
[assets/bmc-capabilities.md](../assets/bmc-capabilities.md).

- **Webhooks — yes.** 16 event types; one-off coffee = `donation.created` (recurring is a
  separate `recurring_donation.*` / `membership.*` family, so one-off vs recurring is
  distinguishable by `type`). Enveloped payload `{event_id, type, live_mode, created (unix), attempt, data}`.
  **Verifiable**: each webhook has a signing secret; every delivery carries `x-signature-sha256` =
  HMAC-SHA256 over the raw body. Exact `data` key names for donations weren't in the help article
  (they're in a downloadable OpenAPI 3.1 spec the agent couldn't fetch) — inferred from the legacy
  payload + REST schema; **confirm via a test event / the spec** (→ task 06).
- **REST API — yes.** `developers.buymeacoffee.com/api/v1`, **personal access token as Bearer**.
  `GET /v1/supporters` (one-off) returns `support_email` **and** `payer_email` plus
  `supporter_name`, `support_coffees`, `support_coffee_price`, `support_currency`,
  `support_created_on`; `/subscriptions` and `/extras` return `payer_email`. Laravel-style
  pagination, **no server-side date filter** → "this month" is computed client-side on
  `support_created_on`. Rate limits undocumented by BMC.
- **Plan — none required.** Single free plan (~5% fee); no evidence webhooks or API are gated.
  Can't verify the venik account's entitlement from outside → task 06.
- **Identity — yes in the normal case.** Both channels expose supporter email, so beers can be
  matched to a login email. **Gap:** logged-out supporters can go fully anonymous (blank
  name+email) and it's unconfirmed whether "private" strips email from the creator's copy — so a
  minority won't match. Fine for **best-effort** attribution, not 100%.

**Implications for the frontier:** attribution (02) and the counter (04) are both feasible; the
counter should be a webhook-incremented tally (no API date filter). Surfaced a new **task 06** —
provision the BMC webhook + token and capture a real payload from inside the dashboard, to confirm
exact field names, private-payment behavior, and account entitlement before the spec is written.
