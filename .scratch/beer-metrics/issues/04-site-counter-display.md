# "Beers this month" — the operator's PostHog metric

Type: prototype  (re-scoped 2026-07-24 → a metric definition, not UI)
Status: resolved
Blocked by: 02

## Question

(Original framing) How does the live "beers this month" figure look and behave on the site —
placement, copy, refresh cadence, zero/failure fallback, caching?

## Answer

Resolved 2026-07-24. **Re-scoped by the operator: this is NOT a website element.** "Beers this
month" is *their* metric, so it lives in **PostHog** (the Growth dashboard), not on the landing
page.

- **No site changes** — no counter on the page, no template edits, no fetch / cache / zero /
  fallback concerns; the existing "Buy me a beer" button stays as-is. (The original UI questions
  above are moot.)
- **The metric = a PostHog trends insight**: sum of the `beer_purchased` **`beers`** property,
  bucketed **by month**; the current month's value is "beers this month." Lives on the **Growth**
  dashboard next to the connect+paying funnel and the matched-vs-all breakdown (ticket 03).
- **Data**: off the same `beer_purchased` events (ticket 02). `add_beer.py` also keeps a local
  SQLite `beers` record (audit + future BMC-dedup high-water-mark), but the metric itself reads
  from PostHog.
- **Built at implementation** (spec 05), when `beer_purchased` has data — no empty insight on the
  live dashboard before then.
