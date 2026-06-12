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
import re
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from pathlib import Path

from fleet_db import DOWNTIME_STATES

SNAPSHOT_PATH = Path(__file__).parent / "fleet_snapshot.json"
DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard.html"


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", ""))


def load_snapshot():
    with open(SNAPSHOT_PATH) as f:
        return json.load(f)


def as_of_from_snapshot(snapshot):
    """Reference time for metrics — must match snapshot generated_at."""
    return parse_ts(snapshot["generated_at"])


def compute_state_distribution(snapshot):
    counts = defaultdict(int)
    for node in snapshot["nodes"]:
        counts[node["current_state"]] += 1
    return dict(counts)


def compute_time_in_stage(snapshot, as_of):
    """For each node, how long (hours) it's been in its current state."""
    result = []
    for node in snapshot["nodes"]:
        since = parse_ts(node["state_since"])
        hours = (as_of - since).total_seconds() / 3600
        result.append({
            "node_id": node["node_id"],
            "datacenter": node["datacenter"],
            "rack": node["rack"],
            "gpu_type": node["gpu_type"],
            "current_state": node["current_state"],
            "hours_in_stage": round(hours, 1),
        })
    return sorted(result, key=lambda r: -r["hours_in_stage"])


def compute_utilization_loss(snapshot, as_of):
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

    window_start = as_of - timedelta(days=30)
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
                else as_of
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


def compute_ticket_status(snapshot, as_of):
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
            elapsed_hours = round((as_of - opened).total_seconds() / 3600, 1)
            remaining = (sla_deadline - as_of).total_seconds() / 3600
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
    as_of = as_of_from_snapshot(snapshot)
    data = {
        "generated_at": snapshot["generated_at"],
        "state_distribution": compute_state_distribution(snapshot),
        "time_in_stage": compute_time_in_stage(snapshot, as_of),
        "utilization_loss": compute_utilization_loss(snapshot, as_of),
        "tickets": compute_ticket_status(snapshot, as_of),
        "datacenter_summary": compute_datacenter_summary(snapshot),
        "nodes": snapshot["nodes"],
    }
    return data


def format_for_dashboard(snapshot, data):
    """Shape analytics output for dashboard.html."""
    tickets_by_id = {t["ticket_id"]: t for t in snapshot["tickets"]}
    open_tickets = [t for t in data["tickets"] if not t["closed"]]
    closed_tickets = [t for t in data["tickets"] if t["closed"]]
    recent_closed = sorted(
        closed_tickets,
        key=lambda t: tickets_by_id[t["ticket_id"]]["closed_at"],
        reverse=True,
    )
    return {
        "generated_at": data["generated_at"],
        "state_distribution": data["state_distribution"],
        "active_nodes": [
            n for n in data["time_in_stage"] if n["current_state"] != "HEALTHY"
        ],
        "utilization_loss": data["utilization_loss"],
        "open_tickets": open_tickets,
        "recent_closed_tickets": recent_closed,
        "ticket_status_counts": dict(Counter(t["status"] for t in data["tickets"])),
        "datacenter_summary": data["datacenter_summary"],
    }


def update_dashboard_html(view_data):
    """Embed fresh dashboard data so the HTML timestamp matches the metrics."""
    with open(DASHBOARD_HTML_PATH) as f:
        content = f.read()
    json_block = json.dumps(view_data, indent=2)
    new_content, n = re.subn(
        r"(<script type=\"application/json\" id=\"dashboard-data\">\n).*?(  </script>)",
        rf"\1{json_block}\n\2",
        content,
        count=1,
        flags=re.DOTALL,
    )
    if n != 1:
        raise RuntimeError("Could not find embedded dashboard data in dashboard.html")
    with open(DASHBOARD_HTML_PATH, "w") as f:
        f.write(new_content)


if __name__ == "__main__":
    snapshot = load_snapshot()
    data = build_dashboard_data()
    view_data = format_for_dashboard(snapshot, data)

    out_path = Path(__file__).parent / "dashboard_data.json"
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)

    update_dashboard_html(view_data)

    print("State distribution:", data["state_distribution"])
    print("Data as of:", data["generated_at"])
    print("\nTotal GPU-hours lost (30 days):", data["utilization_loss"]["total_gpu_hours_lost"])
    print("\nTicket status breakdown:")
    print(Counter(t["status"] for t in data["tickets"]))
    print("\nNodes stuck longest in current stage:")
    for n in data["time_in_stage"][:5]:
        print(f"  {n['node_id']}: {n['current_state']} for {n['hours_in_stage']}h")
    print(f"\nWritten to {out_path}")
    print(f"Updated embedded data in {DASHBOARD_HTML_PATH}")
