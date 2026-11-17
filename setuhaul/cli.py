"""Talk to the agent from a terminal, as any driver, at any snapshot time.

    python -m setuhaul.cli --driver DRV006 --at 09:34
    python -m setuhaul.cli --driver DRV014 --at 09:44 --trace     # show spans + metrics

Commands inside the chat:  /time HH:MM   /board   /plan   /quit
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .db import connect, prepare_working_copy
from .obs import Telemetry
from .scheduler import compare
from .timeutil import hhmm, parse
from .workflow import DriverAgent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", default="DRV006")
    ap.add_argument("--at", default="09:34")
    ap.add_argument("--db", default=None, help="working db (default: fresh copy of the snapshot)")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()
    db = Path(a.db) if a.db else prepare_working_copy(Path(tempfile.mkdtemp()) / "cli.db")
    conn = connect(db)
    tel = Telemetry(echo=a.trace)
    agent = DriverAgent(conn, tel)
    now = parse(f"2026-08-04T{a.at}:00+05:30")
    thread = f"CLI-{a.driver}"
    print(f"SetuHaul driver chat as {a.driver} at {hhmm(now)} (db: {db}). /quit to exit.")
    while True:
        try:
            text = input(f"[{hhmm(now)}] {a.driver}> ").strip()
        except EOFError:
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text.startswith("/time"):
            now = parse(f"2026-08-04T{text.split()[1]}:00+05:30")
            continue
        if text == "/plan":
            for r in compare(conn, "FAC-JAI-01", now):
                print(f"  {r.policy:45s} {json.dumps({k: v for k, v in r.metrics.items() if k != 'swap_proposals'})}")
            continue
        if text == "/board":
            for r in conn.execute("""SELECT d.dock_code, substr(sl.slot_start_ts,12,5) s, a.shipment_id, a.appointment_status
                                     FROM appointments a JOIN appointment_slots sl USING(slot_id) JOIN docks d USING(dock_id)
                                     WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
                                     AND d.facility_id='FAC-JAI-01' ORDER BY d.dock_code, s"""):
                print(f"  {r['dock_code']} {r['s']} {r['shipment_id']} {r['appointment_status']}")
            continue
        t = agent.handle(a.driver, text, now, thread)
        print(f"AGENT> {t.reply}" if t.reply else "AGENT> (duplicate suppressed)")
        print(f"       state={t.state} intent={t.intent} actions={t.actions}")


if __name__ == "__main__":
    main()
