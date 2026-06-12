"""
Sample data generator for the Fleet Node Lifecycle Tracker.

Simulates a small GPU fleet (40 nodes across 2 datacenters) over a
30-day window, with randomized failures, repair tickets, SLA breaches,
and re-integrations. This gives the dashboard realistic data to
visualize: current fleet state distribution, utilization loss over
time, time-in-stage per node, and open tickets approaching/breaching SLA.
"""

import random
from datetime import datetime, timedelta

from fleet_db import (
    init_db,
    add_node,
    transition_node,
    open_ticket,
    close_ticket,
    NodeState,
    export_snapshot,
)
import json
from pathlib import Path


random.seed(42)

DATACENTERS = ["DC-East-1", "DC-West-2"]
GPU_TYPES = ["H100", "A100", "H100"]  # weighted toward H100
PROVIDERS = ["CoreWeave", "Lambda", "Crusoe", "InternalDC"]

NUM_NODES = 40
SIM_DAYS = 30


def iso(dt):
    return dt.isoformat(timespec="seconds") + "Z"


def main():
    init_db(reset=True)

    start = datetime(2026, 5, 13, 0, 0, 0)  # 30 days before "today" (Jun 12, 2026)

    # 1. Provision all nodes as HEALTHY at simulation start
    for i in range(1, NUM_NODES + 1):
        node_id = f"node-{i:03d}"
        dc = DATACENTERS[i % len(DATACENTERS)]
        rack = f"R{(i % 8) + 1:02d}"
        gpu_type = GPU_TYPES[i % len(GPU_TYPES)]
        gpu_count = 8
        add_node(node_id, rack, dc, gpu_type, gpu_count,
                 initial_state=NodeState.HEALTHY, timestamp=iso(start))

    # 2. Simulate day-by-day events
    # Track which nodes are mid-lifecycle so we can advance them
    node_ids = [f"node-{i:03d}" for i in range(1, NUM_NODES + 1)]
    in_flight = {}  # node_id -> dict with ticket info / next-step timing
    ticket_counter = 1000

    for day in range(SIM_DAYS):
        current_day = start + timedelta(days=day)

        # --- Advance in-flight repairs ---
        for node_id in list(in_flight.keys()):
            info = in_flight[node_id]
            if current_day >= info["next_action_at"]:
                stage = info["stage"]

                if stage == "DOWN":
                    # open a ticket, move to TICKETED
                    ticket_id = f"TCK-{ticket_counter}"
                    ticket_counter += 1
                    provider = random.choice(PROVIDERS)
                    sla = random.choice([24, 48, 48, 72, 72])
                    open_ticket(
                        ticket_id, node_id, provider,
                        issue_summary=info["issue"],
                        sla_hours=sla,
                        timestamp=iso(current_day),
                    )
                    transition_node(node_id, NodeState.TICKETED,
                                     actor=provider,
                                     notes=f"Ticket {ticket_id} opened with {provider} (SLA {sla}h)",
                                     timestamp=iso(current_day))
                    info["ticket_id"] = ticket_id
                    info["stage"] = "TICKETED"
                    info["next_action_at"] = current_day + timedelta(
                        hours=random.randint(2, 18)
                    )

                elif stage == "TICKETED":
                    transition_node(node_id, NodeState.IN_REPAIR,
                                     actor=in_flight[node_id].get("provider", "vendor"),
                                     notes="Repair started on-site",
                                     timestamp=iso(current_day))
                    info["stage"] = "IN_REPAIR"
                    # repair duration: most within SLA, occasional breach (longer tail)
                    if random.random() < 0.15:
                        repair_hours = random.randint(50, 80)  # SLA breach
                    else:
                        repair_hours = random.randint(4, 24)
                    info["next_action_at"] = current_day + timedelta(hours=repair_hours)

                elif stage == "IN_REPAIR":
                    decommission = random.random() < 0.08
                    if decommission:
                        transition_node(node_id, NodeState.DECOMMISSIONED,
                                         actor="ops-team",
                                         notes="Hardware unrepairable - decommissioned",
                                         timestamp=iso(current_day))
                        if "ticket_id" in info:
                            close_ticket(info["ticket_id"], timestamp=iso(current_day))
                        del in_flight[node_id]
                        continue
                    else:
                        transition_node(node_id, NodeState.REPAIRED,
                                         actor="ops-team",
                                         notes="Repair completed, awaiting re-integration",
                                         timestamp=iso(current_day))
                        if "ticket_id" in info:
                            close_ticket(info["ticket_id"], timestamp=iso(current_day))
                        info["stage"] = "REPAIRED"
                        info["next_action_at"] = current_day + timedelta(
                            hours=random.randint(2, 12)
                        )

                elif stage == "REPAIRED":
                    transition_node(node_id, NodeState.REINTEGRATING,
                                     actor="ops-team",
                                     notes="Running validation + re-joining scheduler pool",
                                     timestamp=iso(current_day))
                    info["stage"] = "REINTEGRATING"
                    info["next_action_at"] = current_day + timedelta(
                        hours=random.randint(2, 8)
                    )

                elif stage == "REINTEGRATING":
                    transition_node(node_id, NodeState.HEALTHY,
                                     actor="ops-team",
                                     notes="Validation passed - node back in service",
                                     timestamp=iso(current_day))
                    del in_flight[node_id]

        # --- Randomly trigger new failures ---
        # Roughly 1-3 new failures per day across the fleet
        num_new_failures = random.choice([0, 1, 1, 2, 2, 3])
        decommissioned = {
            n["node_id"] for n in
            __import__("fleet_db").get_all_nodes()
            if n["current_state"] == "DECOMMISSIONED"
        }
        candidates = [
            n for n in node_ids
            if n not in in_flight and n not in decommissioned
        ]
        random.shuffle(candidates)

        for node_id in candidates[:num_new_failures]:
            # check current state - only fail HEALTHY nodes
            issue = random.choice([
                "GPU ECC error rate exceeding threshold",
                "Node unreachable - NIC failure suspected",
                "Thermal throttling detected on GPU 3",
                "NVLink degradation across 2 GPUs",
                "Power supply unit fault",
                "Disk I/O errors on local NVMe",
                "Memory ECC uncorrectable errors",
            ])

            if random.random() < 0.3:
                # degrade first, then go down
                transition_node(node_id, NodeState.DEGRADED,
                                 actor="monitoring",
                                 notes=f"Alert: {issue}",
                                 timestamp=iso(current_day))
                transition_node(node_id, NodeState.DOWN,
                                 actor="monitoring",
                                 notes="Escalated to DOWN - node pulled from pool",
                                 timestamp=iso(current_day + timedelta(hours=2)))
            else:
                transition_node(node_id, NodeState.DOWN,
                                 actor="monitoring",
                                 notes=f"Alert: {issue} - node pulled from pool",
                                 timestamp=iso(current_day))

            in_flight[node_id] = {
                "stage": "DOWN",
                "issue": issue,
                "next_action_at": current_day + timedelta(hours=random.randint(1, 6)),
            }

    # 3. Export snapshot for the dashboard
    snapshot = export_snapshot()
    out_path = Path(__file__).parent / "fleet_snapshot.json"
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)

    print(f"Simulated {SIM_DAYS} days across {NUM_NODES} nodes.")
    print(f"Total transitions: {len(snapshot['transitions'])}")
    print(f"Total tickets: {len(snapshot['tickets'])}")
    print(f"Open tickets: {sum(1 for t in snapshot['tickets'] if t['closed_at'] is None)}")
    print(f"Snapshot written to {out_path}")

    # Quick summary of current state distribution
    from collections import Counter
    state_counts = Counter(n["current_state"] for n in snapshot["nodes"])
    print("\nCurrent fleet state distribution:")
    for state, count in state_counts.most_common():
        print(f"  {state}: {count}")


if __name__ == "__main__":
    main()
