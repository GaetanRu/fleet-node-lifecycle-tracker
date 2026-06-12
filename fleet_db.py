"""
Fleet Node Lifecycle Tracker - Database & State Machine
==========================================================

Models the lifecycle of GPU compute nodes through failure, repair,
and re-integration, similar to how an infrastructure operations team
tracks fleet health and remediation workflows.

State machine stages:
    HEALTHY -> DEGRADED -> DOWN -> TICKETED -> IN_REPAIR -> REPAIRED -> REINTEGRATING -> HEALTHY
                                       |                                       |
                                       +-------------> DECOMMISSIONED <--------+
                                                        (if unrepairable)

Each transition is logged with a timestamp, actor, and notes, giving
a full audit trail per node - which is the basis for the dashboard's
"time in stage" and "utilization loss" calculations.
"""

import sqlite3
import json
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path


DB_PATH = Path(__file__).parent / "fleet.db"


class NodeState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    TICKETED = "TICKETED"
    IN_REPAIR = "IN_REPAIR"
    REPAIRED = "REPAIRED"
    REINTEGRATING = "REINTEGRATING"
    DECOMMISSIONED = "DECOMMISSIONED"


# Valid transitions: current_state -> set of allowed next states
VALID_TRANSITIONS = {
    NodeState.HEALTHY: {NodeState.DEGRADED, NodeState.DOWN},
    NodeState.DEGRADED: {NodeState.HEALTHY, NodeState.DOWN},
    NodeState.DOWN: {NodeState.TICKETED},
    NodeState.TICKETED: {NodeState.IN_REPAIR, NodeState.DECOMMISSIONED},
    NodeState.IN_REPAIR: {NodeState.REPAIRED, NodeState.DECOMMISSIONED},
    NodeState.REPAIRED: {NodeState.REINTEGRATING},
    NodeState.REINTEGRATING: {NodeState.HEALTHY, NodeState.DOWN},
    NodeState.DECOMMISSIONED: set(),  # terminal state
}

# States that count as "node is NOT contributing to usable fleet capacity"
DOWNTIME_STATES = {
    NodeState.DEGRADED,
    NodeState.DOWN,
    NodeState.TICKETED,
    NodeState.IN_REPAIR,
    NodeState.REPAIRED,
    NodeState.REINTEGRATING,
}


class InvalidTransitionError(Exception):
    pass


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset: bool = False):
    """Create the schema. If reset=True, drop existing tables first."""
    conn = get_connection()
    cur = conn.cursor()

    if reset:
        cur.executescript(
            """
            DROP TABLE IF EXISTS transitions;
            DROP TABLE IF EXISTS tickets;
            DROP TABLE IF EXISTS nodes;
            """
        )

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            rack TEXT NOT NULL,
            datacenter TEXT NOT NULL,
            gpu_type TEXT NOT NULL,
            gpu_count INTEGER NOT NULL,
            current_state TEXT NOT NULL,
            state_since TEXT NOT NULL,   -- ISO timestamp of last transition
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            actor TEXT,
            notes TEXT,
            FOREIGN KEY (node_id) REFERENCES nodes(node_id)
        );

        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            issue_summary TEXT NOT NULL,
            sla_hours INTEGER NOT NULL,
            FOREIGN KEY (node_id) REFERENCES nodes(node_id)
        );

        CREATE INDEX IF NOT EXISTS idx_transitions_node
            ON transitions(node_id);
        CREATE INDEX IF NOT EXISTS idx_tickets_node
            ON tickets(node_id);
        """
    )
    conn.commit()
    conn.close()


def _now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def add_node(node_id, rack, datacenter, gpu_type, gpu_count,
              initial_state=NodeState.HEALTHY, timestamp=None):
    conn = get_connection()
    ts = timestamp or _now_iso()
    conn.execute(
        """INSERT INTO nodes
           (node_id, rack, datacenter, gpu_type, gpu_count,
            current_state, state_since, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (node_id, rack, datacenter, gpu_type, gpu_count,
         initial_state.value if isinstance(initial_state, NodeState) else initial_state,
         ts, ts),
    )
    conn.execute(
        """INSERT INTO transitions (node_id, from_state, to_state, timestamp, actor, notes)
           VALUES (?, NULL, ?, ?, ?, ?)""",
        (node_id,
         initial_state.value if isinstance(initial_state, NodeState) else initial_state,
         ts, "system", "Node provisioned"),
    )
    conn.commit()
    conn.close()


def transition_node(node_id, to_state, actor="system", notes=None, timestamp=None):
    """Apply a validated state transition and record it in the audit log."""
    if isinstance(to_state, str):
        to_state = NodeState(to_state)

    conn = get_connection()
    row = conn.execute(
        "SELECT current_state FROM nodes WHERE node_id = ?", (node_id,)
    ).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"Unknown node_id: {node_id}")

    from_state = NodeState(row["current_state"])

    if to_state not in VALID_TRANSITIONS.get(from_state, set()):
        conn.close()
        raise InvalidTransitionError(
            f"Cannot transition node {node_id} from {from_state.value} "
            f"to {to_state.value}. Valid next states: "
            f"{[s.value for s in VALID_TRANSITIONS.get(from_state, set())]}"
        )

    ts = timestamp or _now_iso()
    conn.execute(
        "UPDATE nodes SET current_state = ?, state_since = ? WHERE node_id = ?",
        (to_state.value, ts, node_id),
    )
    conn.execute(
        """INSERT INTO transitions (node_id, from_state, to_state, timestamp, actor, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (node_id, from_state.value, to_state.value, ts, actor, notes),
    )
    conn.commit()
    conn.close()
    return from_state, to_state


def open_ticket(ticket_id, node_id, provider, issue_summary, sla_hours, timestamp=None):
    conn = get_connection()
    ts = timestamp or _now_iso()
    conn.execute(
        """INSERT INTO tickets (ticket_id, node_id, provider, opened_at, closed_at,
                                  issue_summary, sla_hours)
           VALUES (?, ?, ?, ?, NULL, ?, ?)""",
        (ticket_id, node_id, provider, ts, issue_summary, sla_hours),
    )
    conn.commit()
    conn.close()


def close_ticket(ticket_id, timestamp=None):
    conn = get_connection()
    ts = timestamp or _now_iso()
    conn.execute("UPDATE tickets SET closed_at = ? WHERE ticket_id = ?", (ts, ticket_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Read / aggregation helpers used by the dashboard
# ---------------------------------------------------------------------------

def get_all_nodes():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_node_transitions(node_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM transitions WHERE node_id = ? ORDER BY timestamp",
        (node_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_transitions():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM transitions ORDER BY timestamp").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_open_tickets():
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM tickets WHERE closed_at IS NULL ORDER BY opened_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_tickets():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM tickets ORDER BY opened_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def export_snapshot():
    """Export a full JSON snapshot of nodes, transitions, and tickets -
    this is what the dashboard front-end consumes."""
    return {
        "generated_at": _now_iso(),
        "nodes": get_all_nodes(),
        "transitions": get_all_transitions(),
        "tickets": get_all_tickets(),
    }


if __name__ == "__main__":
    init_db(reset=True)
    print(f"Initialized empty database at {DB_PATH}")
