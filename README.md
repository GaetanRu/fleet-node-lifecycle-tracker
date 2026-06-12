# Fleet Node Lifecycle Tracker

**Live dashboard**: https://GaetanRu.github.io/fleet-node-lifecycle-tracker/dashboard.html

A state-machine-based tracking system that models the lifecycle of GPU
compute nodes — from failure through ticketing, repair, and re-integration —
with an analytics layer that surfaces fleet health, GPU utilization loss,
and ticket SLA status.

This mirrors the operational reality of running a large GPU fleet: nodes
fail, get ticketed with external providers, get repaired, and re-join the
pool. The goal of this project is to make that process visible and
accountable through tracking and dashboards.

## What it does

- **State machine** (`fleet_db.py`) - defines valid node lifecycle states
  (Healthy, Degraded, Down, Ticketed, In repair, Repaired, Reintegrating,
  Decommissioned) and enforces valid transitions. Every transition is
  logged with a timestamp, actor, and notes - a full audit trail per node.
- **SQLite persistence** - nodes, transition history, and repair tickets
  (with provider and SLA) are stored in a local database.
- **Sample data generator** (`generate_sample_data.py`) - simulates a
  40-node fleet across two datacenters over a 30-day window, with randomized
  failures, repair tickets, SLA breaches, and re-integrations.
- **Analytics** (`analytics.py`) - computes:
  - Current fleet state distribution
  - GPU-hours lost to downtime, as a daily time series
  - Per-node "time in current stage" (flags nodes stuck too long)
  - Ticket SLA status (on track / at risk / breached)
  - Per-datacenter health summary
- **Dashboard** - a fleet-health view visualizing all of the above:
  node lifecycle pipeline counts, GPU-hours-lost trend, open tickets sorted
  by SLA risk, datacenter breakdown, and a sorted list of nodes needing
  attention.

## Running it

```bash
python3 fleet_db.py              # initialize the database schema
python3 generate_sample_data.py  # simulate 30 days of fleet activity
python3 analytics.py             # compute dashboard_data.json
```

## Files

- `dashboard.html` - standalone dashboard (the live demo above)
- `fleet_db.py` - schema, state machine, and CRUD operations
- `generate_sample_data.py` - 30-day fleet simulation
- `analytics.py` - metrics computation for the dashboard
- `fleet.db` - SQLite database (generated)
- `fleet_snapshot.json` - full export of nodes/transitions/tickets (generated)
- `dashboard_data.json` - computed metrics for the dashboard (generated)

## Why this design

The state machine is the core of the system: every other piece of
analytics (utilization loss, time-in-stage, SLA tracking) is derived
purely from the transition audit log, which makes the system auditable
and easy to extend (e.g. adding new lifecycle stages or alerting rules
doesn't require touching the analytics code).
