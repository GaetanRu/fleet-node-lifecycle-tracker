"""
Fleet analytics: computes the metrics surfaced on the dashboard.

- Current state distribution (fleet health at a glance)
- GPU utilization loss (how many GPU-hours were lost to downtime,
  per day, over the simulation window)
- Per-node time-in-current-stage (flags nodes stuck too long)
- Ticket SLA status (on track / at risk / breached)
- Datacenter-level breakdown
"""

import json
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

from fleet_db import DOWNTIME_STATES

SNAPSHOT_PATH = Path(__file__).parent / "fleet_snapshot.json"
NOW = datetime(2026, 6, 12, 0, 0, 0)  # simulation "current time"


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", ""))


def load_snapshot():
    with open(SNAPSHOT_PATH) as f:
        return json.load(f)


def compute_state_distribution(snapshot):
    counts = defaultdict(int)
    for node in snapshot["nodes"]:
        counts[node["current_state"]] += 1
    return dict(counts)


def compute_time_in_stage(snapshot):
    """For each node, how long (hours) it's been in its current state."""
    result = []
    for node in snapshot["nodes"]:
        since = parse_ts(node["state_since"])
        hours = (NOW - since).total_seconds() / 3600
        result.append({
            "node_id": node["node_id"],
            "datacenter": node["datacenter"],
            "rack": node["rack"],
            "gpu_type": node["gpu_type"],
            "current_state": node["current_state"],
            "hours_in_stage": round(hours, 1),
        })
    return sorted(result, key=lambda r: -r["hours_in_stage"])


def compute_utilization_loss(snapshot):
    """
    For each node, walk its transition history and compute total
    GPU-hours lost to non-HEALTHY states over the 30-day window.
    Also bucket lost GPU-hours per day for a time series.
    """
    # Build per-node transition timelines
    by_node = defaultdict(list)
    for t in snapshot["transitions"]:
        by_node[t["node_id"]].append(t)

    node_meta = {n["node_id"]: n for n in snapshot["nodes"]}

    window_start = NOW - timedelta(days=30)
    daily_loss = defaultdict(float)  # date string -> lost GPU-hours
    total_loss_by_node = {}

    for node_id, transitions in by_node.items():
        transitions = sorted(transitions, key=lambda t: t["timestamp"])
        gpu_count = node_meta[node_id]["gpu_count"]
        node_total = 0.0

        for i, t in enumerate(transitions):
            state = t["to_state"]
            start = parse_ts(t["timestamp"])
            end = (
                parse_ts(transitions[i + 1]["timestamp"])
                if i + 1 < len(transitions)
                else NOW
            )
            if start < window_start:
                start = window_start
            if end <= start:
                continue
            if state in DOWNTIME_STATES:
                hours = (end - start).total_seconds() / 3600
                node_total += hours * gpu_count

                # bucket into daily loss for time series
                cur = start
                while cur < end:
                    day_str = cur.strftime("%Y-%m-%d")
                    next_day = (cur + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    segment_end = min(end, next_day)
                    segment_hours = (segment_end - cur).total_seconds() / 3600
                    daily_loss[day_str] += segment_hours * gpu_count
                    cur = segment_end

        total_loss_by_node[node_id] = round(node_total, 1)

    daily_series = [
        {"date": d, "gpu_hours_lost": round(v, 1)}
        for d, v in sorted(daily_loss.items())
    ]

    return {
        "daily_series": daily_series,
        "total_gpu_hours_lost": round(sum(total_loss_by_node.values()), 1),
        "by_node": total_loss_by_node,
    }


def compute_ticket_status(snapshot):
    """Classify each ticket as on_track / at_risk / breached / closed."""
    results = []
    for t in snapshot["tickets"]:
        opened = parse_ts(t["opened_at"])
        sla_deadline = opened + timedelta(hours=t["sla_hours"])

        if t["closed_at"]:
            closed = parse_ts(t["closed_at"])
            status = "closed_within_sla" if closed <= sla_deadline else "closed_breached_sla"
            elapsed_hours = round((closed - opened).total_seconds() / 3600, 1)
        else:
            elapsed_hours = round((NOW - opened).total_seconds() / 3600, 1)
            remaining = (sla_deadline - NOW).total_seconds() / 3600
            if remaining < 0:
                status = "breached"
            elif remaining < 6:
                status = "at_risk"
            else:
                status = "on_track"

        results.append({
            "ticket_id": t["ticket_id"],
            "node_id": t["node_id"],
            "provider": t["provider"],
            "issue_summary": t["issue_summary"],
            "sla_hours": t["sla_hours"],
            "elapsed_hours": elapsed_hours,
            "status": status,
            "closed": t["closed_at"] is not None,
        })
    return results


def compute_datacenter_summary(snapshot):
    by_dc = defaultdict(lambda: defaultdict(int))
    for node in snapshot["nodes"]:
        by_dc[node["datacenter"]][node["current_state"]] += 1
        by_dc[node["datacenter"]]["_total"] += 1
    return {dc: dict(states) for dc, states in by_dc.items()}


def build_dashboard_data():
    snapshot = load_snapshot()
    data = {
        "generated_at": snapshot["generated_at"],
        "state_distribution": compute_state_distribution(snapshot),
        "time_in_stage": compute_time_in_stage(snapshot),
        "utilization_loss": compute_utilization_loss(snapshot),
        "tickets": compute_ticket_status(snapshot),
        "datacenter_summary": compute_datacenter_summary(snapshot),
        "nodes": snapshot["nodes"],
    }
    return data


if __name__ == "__main__":
    data = build_dashboard_data()
    out_path = Path(__file__).parent / "dashboard_data.json"
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)

    print("State distribution:", data["state_distribution"])
    print("\nTotal GPU-hours lost (30 days):", data["utilization_loss"]["total_gpu_hours_lost"])
    print("\nTicket status breakdown:")
    from collections import Counter
    print(Counter(t["status"] for t in data["tickets"]))
    print("\nNodes stuck longest in current stage:")
    for n in data["time_in_stage"][:5]:
        print(f"  {n['node_id']}: {n['current_state']} for {n['hours_in_stage']}h")
    print(f"\nWritten to {out_path}")
