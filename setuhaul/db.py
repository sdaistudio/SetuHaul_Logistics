"""SQLite access + the extra tables the prototype needs.

The supplied snapshot is left untouched; `prepare_working_copy` copies it and
applies migrations. In production the same DDL is expressed for Aurora
PostgreSQL in infra/postgres_schema.sql (with an EXCLUDE constraint for
interval overlap, which SQLite cannot express).
"""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "data" / "setuhaul_freight_operations.db"

MIGRATIONS = """
CREATE TABLE IF NOT EXISTS slot_holds (
    hold_id      TEXT PRIMARY KEY,
    slot_id      TEXT NOT NULL REFERENCES appointment_slots(slot_id),
    shipment_id  TEXT NOT NULL REFERENCES shipments(shipment_id),
    thread_id    TEXT,
    hold_status  TEXT NOT NULL CHECK (hold_status IN ('HELD','RELEASED','EXPIRED','CONVERTED','REJECTED')),
    planned_start_ts TEXT NOT NULL,
    planned_end_ts   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    closed_at    TEXT
);
-- One live hold per slot and one live hold per shipment.
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_hold_per_slot
    ON slot_holds(slot_id) WHERE hold_status = 'HELD';
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_hold_per_shipment
    ON slot_holds(shipment_id) WHERE hold_status = 'HELD';

-- Planned service interval for every appointment written by this system,
-- so commits can check interval overlap on a dock, not only slot identity.
CREATE TABLE IF NOT EXISTS appointment_plan (
    appointment_id   TEXT PRIMARY KEY REFERENCES appointments(appointment_id),
    dock_id          TEXT NOT NULL,
    planned_start_ts TEXT NOT NULL,
    planned_end_ts   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_dedupe (
    dedupe_key   TEXT PRIMARY KEY,
    external_message_id TEXT,
    first_seen_at TEXT NOT NULL,
    hit_count    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key     TEXT PRIMARY KEY,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_conversations (
    thread_id    TEXT PRIMARY KEY,
    driver_id    TEXT NOT NULL,
    state        TEXT NOT NULL,
    lang         TEXT NOT NULL DEFAULT 'en',
    data_json    TEXT NOT NULL DEFAULT '{}',
    version      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    escalation_id TEXT PRIMARY KEY,
    thread_id    TEXT,
    shipment_id  TEXT,
    reason_code  TEXT NOT NULL,
    route_to     TEXT NOT NULL,
    handoff_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'OPEN',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    entity_id    TEXT,
    detail_json  TEXT
);
"""


def prepare_working_copy(dest: str | Path, src: str | Path = SNAPSHOT) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    conn = connect(dest)
    conn.executescript(MIGRATIONS)
    conn.close()
    return dest


def connect(path: str | Path) -> sqlite3.Connection:
    # isolation_level=None -> we issue BEGIN IMMEDIATE ourselves for writes.
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up-front: two commits for the same
    facility cannot interleave their read-check-write sequences."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def audit(conn, ts: str, actor: str, action: str, entity_id: str | None, detail: str = "{}"):
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, entity_id, detail_json) VALUES (?,?,?,?,?)",
        (ts, actor, action, entity_id, detail),
    )
