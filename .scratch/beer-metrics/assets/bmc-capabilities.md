# Buy Me a Coffee — programmatic integration capabilities

Research date: **2026-07-24**. Sources are primarily BMC's own help center and
developer portal, with third-party sources used only to corroborate and marked
`(third-party)`. BMC has changed its API/webhook offering over time, so dated
caveats are noted inline.

Two distinct integration surfaces exist and appear to coexist today:

- **Webhooks** — the *current* real-time push system, documented at
  `studio.buymeacoffee.com/webhooks` (redirects to the help center article
  below). Uses an enveloped payload + HMAC signing. This looks like the
  actively-maintained path (help article carries a 2025 article ID).
- **REST API v1** — the pull API at `https://developers.buymeacoffee.com`
  (`/v1/...`, Bearer token). Older; best field-level documentation is the
  Microsoft Power Platform connector reference (generated 2024-03, updated
  2025-04) which mirrors the v1 endpoints and their response schemas.

---

## 1. Webhooks

**Yes — BMC sends webhooks on donation / new-supporter / membership events.**
Setup and event catalog: <https://help.buymeacoffee.com/en/articles/15743173-how-to-setup-and-use-buy-me-a-coffee-webhooks>
(reached via <https://studio.buymeacoffee.com/webhooks/docs>).

### Event types (16, across 5 categories)

- **Donations:** `donation.created`, `donation.refunded`
- **Shop / Extras:** `extra_purchase.created`, `extra_purchase.updated`, `extra_purchase.refunded`
- **Commissions:** `commission_order.created`, `commission_order.refunded`
- **Wishlists:** `wishlist_payment.created`, `wishlist_payment.refunded`
- **Recurring support:** `recurring_donation.started`, `recurring_donation.updated`, `recurring_donation.cancelled`
- **Memberships:** `membership.started`, `membership.updated`, `membership.cancelled`, `membership.paused`

The one-off "coffee" is **`donation.created`** ("a one-time donation"); recurring
support is a **separate** event family (`recurring_donation.*` / `membership.*`),
so one-off vs. recurring **is** distinguishable by event `type`.

### Envelope (documented verbatim)

```json
{
  "event_id": 1234,
  "type": "donation.created",
  "live_mode": true,
  "created": 1719825600,
  "attempt": 1,
  "data": { }
}
```

- `created` is a Unix timestamp (event time).
- `live_mode` is `false` for test events.
- `attempt` = delivery-retry counter.

### Donation payload fields (inside `data`)

BMC's help article does **not** spell out the `data` field names for
`donation.created`; it points to a downloadable **OpenAPI 3.1 spec** for the full
per-event schemas. I could not fetch that spec directly (SPA/asset behind the
studio app), so the exact new-format field names are **inferred**, from two
converging sources:

- The **legacy** BMC donation webhook (documented in a 2021 walkthrough,
  `(third-party)` <https://dev.to/thinkverse/how-to-use-buymeacoffees-webhooks-with-laravel-2d4a>)
  delivered, under a `response` key: **`supporter_email`**, `number_of_coffees`,
  `total_amount`, `support_created_on`. So email **was** present in the legacy
  donation payload.
- The **REST API v1** one-time-supporter object (authoritative field list in
  §2) carries `supporter_name`, `support_email`, `support_note`,
  `support_coffees`, `support_coffee_price`, `support_currency`,
  `support_created_on`, plus `payer_email` / `payer_name` / `country`.

Conclusion: the `donation.created` `data` object almost certainly contains the
supporter **name, email, coffee count, amount, currency, note/message, and a
created-on timestamp** — but the **exact new-format key names should be confirmed
against the OpenAPI 3.1 spec** (download link on the webhook docs page) or a live
test delivery, since the wire format was reworked from the flat 2021 `response`
shape into the enveloped `data` shape.

### Signature verification — YES

- Each webhook has a unique **Signing Secret**, shown on the webhook's detail
  page in the dashboard.
- Every delivery carries header **`x-signature-sha256`**.
- Verification = **HMAC-SHA256** over the **raw request body** (message) keyed by
  the **signing secret**; compare to the header. (Source: the help article above.)

### Plan gating for webhooks

The webhook help article states **no** plan restriction. See §3.

---

## 2. REST API v1

**Yes — there is a public pull API.** Portal + token dashboard:
<https://developers.buymeacoffee.com/> and `…/dashboard`. Field-level schema
mirrored by the Microsoft Power Platform connector reference `(third-party but
authoritative — it's a generated mirror of the v1 endpoints)`:
<https://learn.microsoft.com/en-us/connectors/buymeacoffeeip/>.

### Auth

- **Personal access token** generated at `developers.buymeacoffee.com/dashboard`,
  sent as a **Bearer token** (`Authorization: Bearer <token>`). Not OAuth — a
  single account-scoped token. (Corroborated `(third-party)` by the
  `buymeacoffee.js` wrapper and Rust/Node community clients.)

### Endpoints and key response fields

Base path pattern `https://developers.buymeacoffee.com/api/v1/...`. All list
endpoints return **Laravel-style paginated** envelopes: `current_page`, `data[]`,
`per_page`, `total`, `from`, `to`, `last_page`, `next_page_url`, `prev_page_url`,
`first_page_url`, `last_page_url`, `path`.

**One-time supporters** — `GET /v1/supporters` ("Get onetime-supporters"), plus
`GET /v1/supporters/{id}`. Per-item fields (verbatim):
`support_id`, `support_note`, **`support_coffees`**, `transaction_id`,
`support_visibility`, `support_created_on`, `support_updated_on`, `transfer_id`,
**`supporter_name`**, `support_coffee_price`, **`support_email`**, `is_refunded`,
**`support_currency`**, `support_note_pinned`, `referer`, `country`,
**`payer_email`**, `payment_platform`, **`payer_name`**.
→ **Email IS returned** (both `support_email` and `payer_email`). Amount is
derivable from `support_coffees × support_coffee_price` (+ `support_currency`).

**Members / subscriptions** — `GET /v1/subscriptions` ("Get members",
`?status=active|inactive|all`), plus `GET /v1/subscriptions/{id}`. Fields:
`subscription_id`, `subscription_created_on`, `subscription_updated_on`,
`subscription_cancelled_on`, `subscription_current_period_start`,
`subscription_current_period_end`, `subscription_coffee_price`,
`subscription_coffee_num`, `subscription_is_cancelled`,
`subscription_is_cancelled_at_period_end`, `subscription_currency`,
`subscription_message`, `message_visibility`, `subscription_duration_type`,
`referer`, `country`, `transaction_id`, **`payer_email`**, **`payer_name`**.
→ Email returned as `payer_email`.

**Extras / shop purchases (BETA)** — `GET /v1/extras`, `GET /v1/extras/{id}`.
Fields include `purchase_id`, `purchased_on`, `purchase_amount`,
`purchase_currency`, `purchase_question`, **`payer_email`**, **`payer_name`**,
and a nested `extra.reward_*` block. → Email returned as `payer_email`.

### Date filtering / pagination

- **Pagination:** yes (page-based, see the envelope fields above).
- **Date filtering:** **no documented date/`since` query parameter.** The only
  documented filter is `status` on the members endpoint. To compute
  **"this month"** you must **page through results and filter client-side** on
  `support_created_on` (one-time) / `subscription_created_on` (members). Since
  results are returned newest-first per page, this is cheap for a small account
  (stop paging once you cross the month boundary). **Flag:** an undocumented
  date param may exist — worth a quick probe from inside the dashboard.

### Rate limits

- **Not found in BMC's own docs.** The only concrete number is the Microsoft
  connector's throttle of **100 calls / 60s per connection** — that is the
  *connector's* limit, **not confirmed to be BMC's API limit**. Treat BMC's real
  rate limit as **unknown**; for a low-volume monthly counter it is unlikely to
  matter.

---

## 3. Plan & eligibility

- BMC runs on a **single free plan**; it monetizes via a **~5% platform fee** on
  every donation/membership/sale (plus Stripe/PayPal processor fees). Multiple
  2026 pricing reviews state "no paid upgrades, no premium tiers, no
  subscriptions to unlock advanced features."
  `(third-party)` <https://www.schoolmaker.com/blog/buy-me-a-coffee-pricing>,
  <https://toolradar.com/tools/buy-me-a-coffee/pricing>.
- **Terminology trap:** "Membership" on BMC means a *creator offering paid
  memberships to their own supporters* — it is **not** a paid BMC tier for the
  creator. "Extras" / "Shop" are features, not paywalls. Occasional mentions of a
  "$5/mo Gold" plan appear in low-quality listicles and are **not corroborated**
  by BMC; treat as noise.
- **Neither the webhook docs nor the API portal state a plan requirement.** The
  webhook article gives setup steps with no tier gate, and the API token
  dashboard is available to accounts generally. **Best read: webhooks and the
  REST API are available to all accounts at no extra cost.**
- **Cannot verify a *specific* account's entitlements from outside.** Assumption:
  the target ("venik") account, being an ordinary free BMC creator account, has
  both the webhook dashboard and an API token available. This must be confirmed
  from inside the dashboard (see §Confidence & gaps).

---

## 4. Identity signal (most important)

**Bottom line: yes — in the normal case both channels expose the supporter's
email, so a purchase can be matched against a known login email.** But it is
**not guaranteed for every donation.**

- **API:** `support_email` **and** `payer_email` on one-time supporters;
  `payer_email` on members and extras. Explicit fields.
- **Webhook:** legacy donation payload carried `supporter_email`; the new
  enveloped `data` almost certainly carries the same (confirm key name via the
  OpenAPI spec / a test event).
- **BMC's own FAQ** reinforces that supporters (and their contact details)
  belong to the creator: *"your supporters are strictly yours … You can export
  their list any time you like."*
  <https://help.buymeacoffee.com/en/articles/4539170-frequently-asked-questions>

**When email is NOT available (attribution gaps):**

1. **Fully anonymous supporter** — a *logged-out* payer may leave the **Name and
   Email fields blank** at checkout; then no name/email reaches the creator.
   <https://help.buymeacoffee.com/en/articles/3364461-can-i-make-my-payment-private>
2. **Private payment** — a payer can tick "make this private," hiding the
   name/message from the *public* supporter wall. (What "private" hides from the
   *creator's own dashboard/API* is under-documented by BMC — the private flag is
   primarily about public display; `support_visibility` / `message_visibility`
   fields exist in the API. **Uncertain** whether email is stripped from the
   creator's copy for private payments — needs a live check.)
3. Note: a *logged-in* BMC user paying with their account email **cannot** make
   the payment private, so those always carry an email.

**Conflicting third-party claim (flagged):** one blog asserts "creators cannot
see your email address" `(third-party)`
<https://coffeeplusthree.com/is-buy-me-a-coffee-anonymous/>. This is
**contradicted** by BMC's explicit API `support_email`/`payer_email` fields, the
legacy webhook `supporter_email`, and the FAQ's "export their list." The blog
likely conflates *public* visibility (other supporters can't see your email) with
*creator* visibility. **We trust the primary sources: creators do receive
supporter email, except for the anonymous/private cases above.**

**How common are anonymous/name-only donations?** BMC does not publish a rate.
Anonymity requires a deliberate opt-out (blank fields while logged out), so the
*default* is that name + email flow through; expect the **majority** of donations
to be attributable, with a **minority tail** of anonymous/name-only ones that
simply won't match. This is fine for *best-effort* attribution, not for anything
that must be 100% complete.

---

## Bottom line for our use case

- **(a) Per-donation event with supporter email for best-effort attribution —
  YES.** Subscribe to the `donation.created` webhook (verify via
  `x-signature-sha256` HMAC-SHA256 with the signing secret), read the supporter
  email from `data`, and match it against a known login email. Realtime, push,
  self-verifiable. Fall back to / backfill from `GET /v1/supporters` (Bearer
  token) which explicitly returns `support_email` + `payer_email`. Caveat:
  anonymous/name-only donations (a minority) carry no email and can't be matched.
- **(b) Live monthly beer/coffee count for a public counter — YES, with
  client-side date bucketing.** No server-side date filter exists, so either
  (i) increment a stored counter on each `donation.created` webhook (cleanest for
  a live counter — sum `support_coffees` / `number_of_coffees`), or (ii) poll
  `GET /v1/supporters`, page through, and sum `support_coffees` where
  `support_created_on` falls in the current month. Webhook-driven counter is the
  better fit for a public live count.
- **(c) BMC plan required — none identified.** Single free plan (~5% fee); no
  evidence webhooks or the API are gated behind a paid tier. Assume the free
  account suffices; confirm from inside the dashboard.

## Confidence & gaps

**Confident:** webhook event catalog + envelope + `x-signature-sha256`
HMAC-SHA256 signing; REST v1 endpoint set, Bearer auth, and that
`support_email`/`payer_email` are returned; pagination shape; single free-plan
pricing with ~5% fee; one-off vs. recurring distinguishable by event type.

**Could NOT confirm — check from inside the `venik` BMC dashboard:**

1. **Exact `donation.created` `data` field names in the *new* enveloped format**
   — download the OpenAPI 3.1 spec from the webhook docs page (or trigger a test
   delivery, `live_mode:false`) and read the real keys. My donation-field list is
   inferred from the legacy payload + the REST schema.
2. **Whether the "private payment" flag strips email from the creator's own
   API/webhook copy** (vs. only hiding it on the public wall). Test with a
   private test donation.
3. **BMC's real API rate limit** — undocumented publicly; the 100/60s figure is
   the Microsoft connector's, not confirmed as BMC's.
4. **Whether the `developers.buymeacoffee.com` v1 API is still first-class / not
   being sunset** in favor of webhooks — both appear live, but confirm a token
   still issues and the endpoints respond.
5. **Account entitlement** — confirm the venik account can (a) open the Webhooks
   section and create a webhook + see its signing secret, and (b) generate an API
   token. Expected yes on the free plan, but unverifiable from outside.
6. **Undocumented date/`since` query param** on the list endpoints — worth a
   quick probe before committing to client-side month bucketing.
