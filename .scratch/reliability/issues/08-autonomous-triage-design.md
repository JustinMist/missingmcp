# 08 — Design the autonomous triage mechanism (ping = proposed action)

Type: grilling
Status: open
Blocked by: 01

## Question

Replace "there are N errors" pings with a mechanism that analyzes problems
and messages the operator **only when action is needed** — carrying the
diagnosis and a proposed next step, not a raw count. The operator's framing:
"an autonomous mechanism that analyzes the problems and proposes what to do
next; write to me only with actions, not all day that errors exist."

Decide, informed by [01](01-digest-error-breakdown.md)'s error breakdown:

- **Where it runs** — extend `scripts/hourly_digest.py` with an analysis
  step, a scheduled Claude agent, or something else entirely.
- **What it reads** — Railway logs, PostHog, the production DB.
- **When it may message** — known signature → mapped recommended action;
  unknown pattern → deeper analysis before any ping; below-threshold noise →
  silence or a daily/weekly summary. The daily heartbeat's fate.
- **Message format** — what happened → what it means → proposed action.
- **Autonomy boundary** — the mechanism proposes, it never auto-remediates
  (out of scope for this map); whether it may file `.scratch/` tickets is a
  separate follow-up question (see the map's Not yet specified).

The implementation becomes a follow-up ticket once this decides the shape.
