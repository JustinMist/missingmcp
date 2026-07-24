# Write the implementation-ready spec

Type: task
Status: resolved
Blocked by: 02, 03, 04

## Question

Assemble the resolved decisions into a spec at `docs/superpowers/specs/<date>-beer-supporters.md`
that a build session can implement without further decisions: BMC ingestion + attribution (02),
the `beer_purchased` event + funnel step (03), and the live "beers this month" counter (04).
Mirror the shape of the 2026-07-20 PostHog telemetry design spec. Destination reached when the
operator approves it.

## Answer

Resolved 2026-07-24 — **operator approved** the spec
`docs/superpowers/specs/2026-07-24-beer-supporters.md`. It assembles all decisions (01–04) into an
implementation-ready design: the manual `scripts/add_beer.py`, the `beers` table, best-effort email
attribution, the `beer_purchased` event, and the three Growth-dashboard PostHog insights
(connect+paying funnel, matched-vs-all, beers/month). **Map destination reached.** Remaining items
are non-decisions: task 06 (parked — BMC portal broken) and the deferred BMC automation +
"someone bought a beer" notification (Not yet specified). Implementation is a separate build
session from the spec.
