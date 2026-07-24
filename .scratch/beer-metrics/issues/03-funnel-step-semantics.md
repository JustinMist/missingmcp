# "Paying user" funnel step semantics

Type: grilling
Status: resolved
Blocked by: 02

## Question

How does "paying user = bought a beer" join the connect funnel (today: Visit → Account connected
→ First tool call)?

- Is it an **ordered 4th step** appended after "First tool call" (visit → connect → use → beer),
  or a separate funnel / insight?
- **Conversion window**: the existing funnel uses 14 days, but beers can come long after first
  use. Does the window need to change, or should this be an unordered / long-window funnel?
- **Honest presentation given partial attribution** (from 02): unattributed beers won't appear as
  a per-user step, so the step will structurally undercount payers. How do we present that so the
  number isn't misread — an annotation, a separate "attributed beers" note, or accept-and-document?

## Answer

Resolved 2026-07-24 (grilling with the operator).

**Funnel shape — one funnel, 4th step.** Extend the existing connect funnel (Visit → Account
connected → First tool call) with a 4th **ordered** step **Paying (beer)** = the `beer_purchased`
event. This is the "add paying into the funnel" the operator originally asked for (one view).

**Window — ~90 days (tunable).** PostHog applies one conversion window to the whole funnel and
beers arrive late, so the window widens from the activation funnel's 14d to **90d**. Accepted cost:
the 90d window also loosens the visit→connect→first-tool-call timing, so this funnel no longer
measures *fast* activation — it measures the fuller journey to paying. (If a tight activation view
is later missed, split it back out — not now.)

**Ordering / edge case.** Ordered, Paying strictly after First tool call — matches the "buy a beer
once a connector earns its spot" model. Accepted edge case: a rare person who pays before ever
calling a tool won't count in the funnel's paying step (they still show in the beer-count
comparison below).

**Attribution honesty — show attributed vs all beers.** The Paying step counts only **attributed**
payers (a beer whose email matches a gateway account), so it's a lower bound — unmatched/anonymous
beers can't join a per-person funnel. To stop that being misread, add **alongside the funnel a
`beer_purchased` count broken down by `matched`** (true vs false) = "attributed vs all beers", so
the gap is visible at a glance. Cheap — `matched` is already a property from ticket 02. (Interim
mitigation: in manual entry the operator types the login email, so matching is usually complete;
the gap mainly appears later with the automated BMC feed, where the donor's BMC email may differ.)

**Placement.** Both on the **Growth** dashboard: the 4-step funnel updates the current
connect-funnel tile (`44rnAeDc`); the matched-breakdown beer count sits next to it.

**Build-time, not now.** `beer_purchased` has no data until `scripts/add_beer.py` exists and beers
are entered, so the actual PostHog wiring (adding step 4 + the comparison insight) happens in the
implementation (spec 05) — not in this planning ticket, to avoid empty steps on the live dashboard.
