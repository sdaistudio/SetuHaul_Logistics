"""Builds report.html (self-contained) from results.json."""
import json, math
from pathlib import Path
HERE = Path(__file__).resolve().parent

R = json.loads((HERE / "results.json").read_text(), parse_constant=lambda x: None)

def clean(o):
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o

R = clean(R)
PURPOSE = {
    "carriers": "Transport companies", "drivers": "Driver identity, phone, carrier", "vehicle_types": "Vehicle classes",
    "vehicles": "Physical trucks", "facilities": "Warehouses, hours, grace, default unload", "docks": "Bays: type, weight limit",
    "facility_contacts": "Escalation contacts", "facility_rules": "No-show, reefer, heavy, last-start rules",
    "shipments": "One freight movement: weight, dock need, priority, ETA, unload", "eta_updates": "Driver / ops ETA revisions + confidence",
    "appointment_slots": "Time windows per dock", "appointments": "Bookings incl. history", "dock_status_events": "Breakdown, maintenance, capacity cuts",
    "facility_checkins": "Gate, yard, dock, unload timestamps", "chat_threads": "One conversation per request",
    "chat_messages": "Driver / agent messages + parsed intent", "driver_exceptions": "Structured exception from a chat",
    "operational_messages": "Emails / SMS to warehouse",
}
for r in R["inventory"]:
    r["purpose"] = PURPOSE.get(r["table"], "")

html = (HERE / "report_template.html").read_text().replace("/*DATA*/null", json.dumps(R, default=str).replace("</", "<\\/"))
(HERE / "report.html").write_text(html)
print("ok", len(html))
