"""SetuHaul data audit.

Every finding is a SQL query run against a read-only copy of the snapshot.
Output: out/audit_results.json  (used by the Word documents)
        out/audit_queries.sql   (hand-in: every proof query in one file)

Run:  python3 -I scripts/audit.py data/setuhaul_freight_operations.db out/
"""
import json, os, shutil, sqlite3, sys, tempfile

DB, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)

# Each finding: id, category, severity, title, refs (Doc A / Doc B ids), sql, why (impact), fix (design response)
F = []
def f(id, cat, sev, title, refs, sql, why, fix):
    F.append(dict(id=id, cat=cat, sev=sev, title=title, refs=refs, sql=sql.strip(), why=why, fix=fix))

ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"

# ---------- 1. Capacity and compatibility ----------
f("F01","Capacity","Critical","Active bookings on docks rated below the load weight","A01, A07; B-D1, B-D2",f"""
SELECT a.appointment_id, a.appointment_status, s.shipment_id, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg
FROM appointments a JOIN shipments s USING(shipment_id)
JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
WHERE a.appointment_status IN {ACTIVE} AND s.load_weight_kg > d.max_vehicle_weight_kg""",
"Two promises that cannot be kept: the truck would be refused at the dock.",
"Feasibility rule F2 (weight) on every option and again inside the commit transaction; re-plan both trucks.")

f("F02","Capacity","Critical","Dock events are not reflected in slot status","A02; B-D5","""
SELECT e.dock_event_id, e.event_type, d.dock_code, e.event_start_ts, e.event_end_ts, sl.slot_id, sl.slot_start_ts, sl.slot_status
FROM dock_status_events e JOIN appointment_slots sl ON sl.dock_id=e.dock_id
JOIN docks d ON d.dock_id=e.dock_id
WHERE sl.slot_status='OPEN' AND sl.slot_start_ts < COALESCE(e.event_end_ts,'9999') AND sl.slot_end_ts > e.event_start_ts
ORDER BY e.dock_event_id, sl.slot_start_ts""",
"A slot on a broken dock still looks OPEN, so a naive availability check would offer it.",
"Derive slot usability from dock_status_events at the as-of time; never trust slot_status alone.")

f("F03","Capacity","Medium","docks.dock_status stays ACTIVE during a breakdown or maintenance","B-D6","""
SELECT d.dock_code, d.dock_status, e.event_type, e.event_start_ts, e.event_end_ts
FROM docks d JOIN dock_status_events e USING(dock_id)
WHERE e.event_type IN ('BREAKDOWN','MAINTENANCE') AND d.dock_status='ACTIVE'""",
"Any screen that reads dock_status shows a broken dock as working.",
"Treat dock_status as a static attribute; compute live status from events at as-of.")

f("F04","Capacity","High","Planned unload is longer than the booked slot","A03; B-D3",f"""
SELECT a.appointment_id, s.shipment_id, d.dock_code, sl.slot_start_ts, sl.slot_end_ts,
       CAST(ROUND((julianday(sl.slot_end_ts)-julianday(sl.slot_start_ts))*1440) AS INT) AS slot_min, s.expected_unload_min
FROM appointments a JOIN shipments s USING(shipment_id)
JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
WHERE a.appointment_status NOT IN ('CANCELLED','REJECTED')
  AND s.expected_unload_min > ROUND((julianday(sl.slot_end_ts)-julianday(sl.slot_start_ts))*1440)
ORDER BY sl.slot_start_ts""",
"Each of these spills into the next window on the same dock; delays cascade.",
"Model dock occupancy as time intervals (start + unload + changeover), not as one slot per truck.")

f("F05","Capacity","High","Booked work already collides on the same dock (cascade proof)","new",f"""
WITH b AS (
  SELECT a.appointment_id, sl.dock_id, d.dock_code, sl.slot_start_ts AS st,
         datetime(sl.slot_start_ts, '+'||s.expected_unload_min||' minutes') AS planned_end_utc
  FROM appointments a JOIN shipments s USING(shipment_id)
  JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
  WHERE a.appointment_status IN {ACTIVE})
SELECT x.dock_code, x.appointment_id AS first_booking, x.st AS first_start,
       y.appointment_id AS next_booking, y.st AS next_start,
       CAST(ROUND((julianday(x.planned_end_utc)-julianday(y.st))*1440) AS INT) AS overlap_min
FROM b x JOIN b y ON x.dock_id=y.dock_id AND y.st > x.st
WHERE julianday(x.planned_end_utc) > julianday(y.st)
  AND NOT EXISTS (SELECT 1 FROM b z WHERE z.dock_id=x.dock_id AND z.st > x.st AND z.st < y.st)""",
"The plan itself is infeasible before any driver is late: the next truck cannot start on time.",
"Interval check (no overlap per dock) in the feasibility layer and a DB trigger as the last guard.")

f("F06","Capacity","High","Current bookings that the latest ETA already misses (phantom capacity)","A04; B-D4",f"""
SELECT a.appointment_id, s.shipment_id, sl.slot_start_ts, sl.slot_end_ts, e.effective_eta_ts, e.eta_confidence
FROM appointments a JOIN shipments s USING(shipment_id)
JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN v_latest_eta e USING(shipment_id)
LEFT JOIN facility_checkins fc USING(shipment_id)
WHERE a.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION') AND a.is_current=1 AND fc.gate_in_ts IS NULL
  AND julianday(e.effective_eta_ts) > julianday(sl.slot_start_ts, '+30 minutes')""",
"These slots look taken but their trucks cannot check in within the 30-minute grace; capacity is locked for nothing.",
"Certain-miss rule: release the slot (with dispatcher notice) once even the optimistic ETA misses the grace window.")

f("F07","Capacity","Critical","SHP1015's original booking was infeasible from the day maintenance was planned","new","""
SELECT a.appointment_id, a.booked_at, sl.slot_start_ts, sl.slot_end_ts, s.original_eta_ts, s.expected_unload_min,
       strftime('%H:%M', s.original_eta_ts, '+'||(s.expected_unload_min+15+330)||' minutes') AS planned_unload_end_ist,
       e.dock_event_id, e.event_start_ts AS maintenance_start, e.created_at AS maintenance_planned_at
FROM appointments a JOIN shipments s USING(shipment_id)
JOIN appointment_slots sl ON sl.slot_id=a.slot_id
JOIN dock_status_events e ON e.dock_id=sl.dock_id AND e.event_type='MAINTENANCE'
WHERE a.appointment_id='APT1015A'""",
"Booked 1 Aug 12:00; maintenance planned 1 Aug 14:00 from 18:00. A 17:30 arrival + 15 min + 60 min unload ends 18:45. Nobody re-checked the booking for 3 days, so the reefer exception on 4 Aug was predictable on 1 Aug.",
"Event-driven re-validation: every new dock event, rule change or ETA update re-checks all affected bookings and raises an at-risk flag.")

f("F08","Capacity","Critical","A historic double booking: two CONFIRMED bookings on one slot for three days","new","""
SELECT a.slot_id, a.appointment_id, a.shipment_id, a.appointment_status, a.confirmed_at, a.cancelled_at, a.cancellation_reason
FROM appointments a WHERE a.slot_id IN (SELECT slot_id FROM appointments GROUP BY slot_id HAVING COUNT(*)>1)
ORDER BY a.slot_id, a.confirmed_at""",
"APT1007 (confirmed 1 Aug 11:05) and APT1012A (confirmed 1 Aug 11:55) both held SLOT-JAI-003 until 4 Aug 09:40. The unique index cannot see the past because the table overwrites status instead of logging transitions.",
"Append-only appointment_events (every status change with timestamp and actor); audit queries run on history, not just current rows.")

f("F09","Capacity","Critical","The two supplied unique indexes leave seven bad bookings possible","A20; B-D17","-- proved by test inserts on a throw-away copy; see 'probe' rows",
"The indexes stop only 'two active bookings on one slot' and 'two current bookings per shipment'.",
"Booking service + feasibility re-check inside BEGIN IMMEDIATE + new triggers (interval overlap, is_current) + hold index.")

f("F10","Capacity","Medium","v_slot_availability has no notion of time, weight, events or holds","A12; B-D7","""
SELECT slot_id, dock_code, slot_start_ts, availability_status FROM v_slot_availability
WHERE facility_id='FAC-JAI-01' AND availability_status='AVAILABLE' AND slot_start_ts < '2026-08-04T09:45:00+05:30'
ORDER BY slot_start_ts LIMIT 8""",
"At 09:45 the view still offers slots that started hours earlier, and would offer D3 09:00 on a broken dock.",
"Replace with a feasibility function parameterised by shipment and as-of; keep the view for cross-checks only.")

# ---------- 2. Fleet and assignment ----------
f("F11","Fleet","High","One driver on two moving trucks","A05; B-D9","""
SELECT driver_id, COUNT(*) AS trips, GROUP_CONCAT(shipment_id||'/'||vehicle_id, ', ') AS trips_and_trucks
FROM shipments WHERE current_status='IN_TRANSIT' GROUP BY driver_id HAVING COUNT(DISTINCT vehicle_id)>1""",
"DRV007 cannot drive VEH008 from Ajmer and VEH007 from Mumbai at the same time; one ETA is fiction.",
"Flag to the dispatcher; when DRV007 chats, ask which truck he is in before using either ETA.")

f("F12","Fleet","High","One vehicle on several live shipments","A06; B-D10","""
SELECT vehicle_id, GROUP_CONCAT(shipment_id||' ('||current_status||', departs '||substr(planned_departure_ts,12,5)||')', '; ') AS shipments
FROM shipments WHERE current_status NOT IN ('COMPLETED','CANCELLED')
GROUP BY vehicle_id HAVING COUNT(*)>1""",
"The second trip of each truck depends on the first one finishing; the plan does not say so.",
"Store trip sequence (vehicle_trip_seq) and propagate delays to the next trip of the same truck.")

f("F13","Fleet","High","A truck leaves Neemrana while it is still inside Jaipur DC","new","""
SELECT a.shipment_id AS first_trip, a.vehicle_id, fc.gate_out_ts AS left_jaipur_at,
       b.shipment_id AS next_trip, b.origin_city, b.actual_departure_ts AS departed_origin_at
FROM shipments a JOIN facility_checkins fc ON fc.shipment_id=a.shipment_id
JOIN shipments b ON b.vehicle_id=a.vehicle_id AND b.shipment_id<>a.shipment_id
WHERE b.actual_departure_ts IS NOT NULL AND fc.gate_out_ts IS NOT NULL
  AND julianday(b.actual_departure_ts) < julianday(fc.gate_out_ts)""",
"VEH001/DRV001 gated out of Jaipur at 08:50 but 'departed' Neemrana (about 150 km away) at 08:15. Either the departure time or the vehicle on SHP1017 is wrong, so SHP1017's ETA cannot be trusted.",
"Physical-plausibility check on every ETA input (time-ordering of the same vehicle's events); a failed check downgrades ETA confidence to LOW.")

f("F14","Fleet","High","Downstream trips are infeasible, so a 'no-show' is really a planning error","A17 (partly); new root cause","""
SELECT nxt.shipment_id AS later_trip, nxt.vehicle_id, nxt.origin_city, nxt.planned_departure_ts,
       cur.shipment_id AS truck_busy_with, cur.current_status AS busy_status,
       COALESCE(fc.gate_in_ts, cur.latest_eta_ts) AS busy_at_jaipur_from, a.appointment_status AS later_trip_appt
FROM shipments nxt JOIN shipments cur ON cur.vehicle_id=nxt.vehicle_id AND cur.shipment_id<>nxt.shipment_id
LEFT JOIN facility_checkins fc ON fc.shipment_id=cur.shipment_id
LEFT JOIN appointments a ON a.shipment_id=nxt.shipment_id
WHERE nxt.current_status='ASSIGNED' AND cur.current_status IN ('IN_TRANSIT','WAITING')
  AND julianday(COALESCE(fc.gate_in_ts, cur.latest_eta_ts)) > julianday(nxt.planned_departure_ts, '-6 hours')""",
"VEH003 is in Jaipur's yard from 08:20, so SHP1018 could never leave Bhiwadi at 08:00: its NO_SHOW is the plan's fault, not the driver's. VEH015 (SHP1021, Noida 10:00) and VEH004 (SHP1020, Kota 13:30) show the same pattern.",
"No-show classification must check whether the truck was physically able to come; proactive alert to dispatch for SHP1020/SHP1021 now.")

# ---------- 3. ETA and time ----------
f("F15","Time","High","No single snapshot time: rows run from 07:10 to 16:30","A08; B-D16","""
SELECT 'eta_updates' t, MIN(created_at) mn, MAX(created_at) mx FROM eta_updates
UNION ALL SELECT 'chat_messages', MIN(message_ts), MAX(message_ts) FROM chat_messages
UNION ALL SELECT 'appointments', MIN(updated_at), MAX(updated_at) FROM appointments
UNION ALL SELECT 'facility_checkins', MIN(updated_at), MAX(updated_at) FROM facility_checkins
UNION ALL SELECT 'shipments', MIN(updated_at), MAX(updated_at) FROM shipments""",
"There is no 'now'. A query run 'as of 09:45' still sees a 16:25 ETA update and a 12:35 no-show.",
"Every read takes an explicit as_of and ignores rows created after it (event-time filter).")

f("F16","Time","Medium","latest_eta_ts differs from the plan with no ETA record behind it","A10; B-D11","""
SELECT s.shipment_id, s.original_eta_ts, s.latest_eta_ts FROM shipments s
WHERE s.latest_eta_ts <> s.original_eta_ts AND NOT EXISTS (SELECT 1 FROM eta_updates e WHERE e.shipment_id=s.shipment_id)""",
"Nobody knows who changed these ETAs or how sure they are.",
"Treat an unexplained ETA as MEDIUM confidence and log a lineage gap; every ETA change must write eta_updates.")

f("F17","Time","Medium","The ETA view labels the original plan as HIGH confidence","A11","""
SELECT shipment_id, effective_eta_ts, eta_source, eta_confidence FROM v_latest_eta
WHERE eta_source='ORIGINAL_PLAN' AND eta_confidence='HIGH'""",
"A plan made three days earlier is reported as if the driver had just confirmed it.",
"Confidence of an unconfirmed plan = MEDIUM, decaying to LOW as the slot approaches without a driver ping.")

f("F18","Time","Medium","An ETA range cannot be stored; it is squeezed into one point","new","""
SELECT eta_update_id, shipment_id, declared_eta_ts, confidence_code, note FROM eta_updates WHERE note LIKE '%between%'""",
"'Between 12:30 and 13:00' became 12:45. The planner loses the late edge it should plan against.",
"Add eta_earliest_ts / eta_latest_ts; plan against the late edge for LOW confidence.")

f("F19","Time","Medium","Trucks inside the grace period are labelled LATE","new","""
SELECT fc.shipment_id, fc.gate_in_ts, sl.slot_start_ts, fc.arrival_state,
       CAST(ROUND((julianday(fc.gate_in_ts)-julianday(sl.slot_start_ts))*1440) AS INT) AS minutes_after_start,
       (SELECT rule_value FROM facility_rules r WHERE r.facility_id=fc.facility_id AND r.rule_type='NO_SHOW_GRACE_MIN') AS grace_min
FROM facility_checkins fc JOIN appointments a ON a.shipment_id=fc.shipment_id AND a.is_current=1
JOIN appointment_slots sl ON sl.slot_id=a.slot_id
WHERE fc.arrival_state='LATE'""",
"SHP1005 (5 min) and SHP1004 (25 min) arrived inside the 30-minute grace, so their appointments are still valid. A system that reads LATE as 'missed' would wrongly push them back.",
"Define states precisely: ON_TIME_WITHIN_GRACE vs LATE_BEYOND_GRACE; only the second loses the appointment.")

f("F20","Time","Medium","The 21:00 last-start rule conflicts with slots that start at 21:00","A15","""
SELECT sl.slot_id, d.dock_code, substr(sl.slot_start_ts,12,5) AS starts, substr(sl.slot_end_ts,12,5) AS ends, sl.slot_status
FROM appointment_slots sl JOIN docks d USING(dock_id)
WHERE sl.facility_id='FAC-JAI-01' AND substr(sl.slot_start_ts,12,5) >= '21:00' ORDER BY sl.slot_start_ts""",
"Five slots are published that the rule says need manual approval. Is 21:00 itself allowed?",
"Treat a 21:00 start as needing approval (strict reading) until the client confirms; label these slots 'approval needed'.")

f("F21","Time","Medium","Rules are duplicated, missing or wider than the slot grid","new","""
SELECT f.facility_id, f.open_time, f.close_time, f.checkin_grace_min AS grace_in_facilities,
  (SELECT rule_value FROM facility_rules r WHERE r.facility_id=f.facility_id AND r.rule_type='NO_SHOW_GRACE_MIN') AS grace_in_rules,
  (SELECT COUNT(*) FROM facility_rules r WHERE r.facility_id=f.facility_id) AS rule_count,
  (SELECT MIN(substr(slot_start_ts,12,5)) FROM appointment_slots s WHERE s.facility_id=f.facility_id) AS first_slot,
  (SELECT MAX(substr(slot_end_ts,12,5)) FROM appointment_slots s WHERE s.facility_id=f.facility_id) AS last_slot_end
FROM facilities f""",
"Grace lives in two places (they agree today, but nothing keeps them in sync). Gurugram has no last-start, early-check-in or heavy rule. Jaipur opens at 06:00 but its first slot is 08:00.",
"One rule store with typed values and effective dates; the facility table keeps only identity.")

f("F22","Time","Low","The reefer rule names dock D5 in free text; rule values are untyped text","A16; new","""
SELECT rule_id, facility_id, rule_type, rule_value, typeof(rule_value) AS stored_as, description FROM facility_rules ORDER BY rule_id""",
"If a second reefer dock is added, the rule text is wrong; '60', 'TRUE' and '21:00' are all stored as text.",
"Rules reference dock capabilities (supports_refrigerated) rather than dock codes; add value_type.")

f("F23","Time","Medium","Slots exist only for 4 August: no next-day capacity","new","""
SELECT facility_id, MIN(substr(slot_start_ts,1,10)) AS first_day, MAX(substr(slot_end_ts,1,10)) AS last_day, COUNT(*) AS slots
FROM appointment_slots GROUP BY facility_id""",
"'First slot tomorrow morning' cannot be computed or offered for the reefer truck.",
"Generate a rolling 48-hour slot horizon from facility calendars; until then, escalation states the gap.")

f("F24","Time","Low","Slots have no updated_at: a 4 Aug breakdown appears on rows 'created' 1 Aug","new","""
SELECT slot_id, slot_status, block_reason, created_at FROM appointment_slots WHERE slot_status='BLOCKED' ORDER BY slot_id""",
"The data cannot say when a block was applied, so nobody can audit whether drivers were told in time.",
"Add updated_at and a slot_events history table.")

# ---------- 4. Conversation and messaging ----------
f("F25","Messaging","High","A messaging retry is stored as a new message, exception and thread","A09; B-D12","""
SELECT m.chat_message_id, m.thread_id, m.external_message_id, m.message_ts, m.is_duplicate, e.exception_id, e.dedupe_key, e.exception_status
FROM chat_messages m LEFT JOIN driver_exceptions e ON e.thread_id=m.thread_id
WHERE m.sender_type='DRIVER' AND m.message_text IN (SELECT message_text FROM chat_messages WHERE sender_type='DRIVER' GROUP BY message_text HAVING COUNT(*)>1)""",
"Without a unique key the retry could book twice; here only a manual is_duplicate flag caught it.",
"Inbound dedupe on (driver, normalised text, 10-min window) + idempotency key on every write.")

f("F26","Messaging","Medium","One driver, several threads: context is split","new","""
SELECT driver_id, COUNT(*) AS threads, GROUP_CONCAT(thread_id||' '||substr(opened_at,12,5)||' '||thread_intent, ' | ') AS detail
FROM chat_threads GROUP BY driver_id HAVING COUNT(*)>1""",
"DRV004 says 'I reached late and am in the yard' (THR008, 09:27), then '45 minutes late' (THR010, 09:31). In one thread the second message is easy to place: his morning load is already at the gate, so he must mean the evening one.",
"One open conversation per driver; threads are per exception, linked to one driver session.")

f("F27","Messaging","High","Three driver messages never got a reply","new","""
SELECT t.thread_id, t.driver_id, t.thread_status, m.message_ts, m.message_text
FROM chat_threads t JOIN chat_messages m ON m.thread_id=t.thread_id AND m.sender_type='DRIVER' AND m.is_duplicate=0
WHERE NOT EXISTS (SELECT 1 FROM chat_messages r WHERE r.thread_id=t.thread_id AND r.sender_type<>'DRIVER')""",
"The heavy-trailer driver (09:39), the late truck in the yard (09:27) and an ask-only driver (10:02) are left waiting.",
"Reply SLA monitor: any driver message unanswered after 60 seconds raises an alarm; auto-acknowledge at once.")

f("F28","Messaging","High","Pending confirmation requests that never reached the warehouse","B-D13","""
SELECT om.operational_message_id, om.appointment_id, om.delivery_status, om.sent_at, a.appointment_status, a.booked_at
FROM operational_messages om JOIN appointments a USING(appointment_id)
WHERE a.appointment_status='PENDING_CONFIRMATION'""",
"APT1013A has waited since 09:32 on an email still QUEUED; APT1014A's email was SENT but never delivered or answered. Both slots are locked.",
"Pending has a decision deadline tied to slot start; delivery is tracked; no reply by the deadline triggers a phone call or release.")

f("F29","Messaging","High","The escalation channel itself is broken","A13, A14; B-D14, B-D15","""
SELECT 'message' AS kind, om.operational_message_id AS id, om.recipient_address AS target, om.delivery_status AS status,
  CASE WHEN EXISTS (SELECT 1 FROM facility_contacts c WHERE c.email=om.recipient_address) THEN 'known contact' ELSE 'not in contacts' END AS note
FROM operational_messages om WHERE om.delivery_status='FAILED'
UNION ALL
SELECT 'contact', contact_id, contact_role||' / '||contact_name, COALESCE(email,'NO EMAIL'), 'phone '||COALESCE(phone,'-')
FROM facility_contacts WHERE email IS NULL OR contact_name LIKE 'Vacant%'""",
"The reefer escalation (OM004) went to an address nobody owns and failed; the Gurugram night role is vacant with no email.",
"Route by role and channel health; a FAILED hand-off is not a hand-off; fall back to phone and to the shift manager.")

f("F30","Messaging","High","The existing chat booking path made an incompatible booking","A07","""
SELECT a.appointment_id, a.booking_source, s.shipment_id, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
WHERE a.booking_source='DRIVER_CHAT' AND s.load_weight_kg > d.max_vehicle_weight_kg""",
"Proof that today's chat flow books without a compatibility check.",
"Only the booking service writes appointments, and it re-runs every feasibility rule.")

f("F31","Messaging","Medium","Driver time limits are stored without saying what they limit","new","""
SELECT e.exception_id, e.latest_acceptable_ts, m.message_text AS agent_question
FROM driver_exceptions e JOIN chat_messages m ON m.thread_id=e.thread_id AND m.sender_type='AGENT'
WHERE e.latest_acceptable_ts IS NOT NULL""",
"13:30 is stored as latest_acceptable_ts, but the agent itself asked whether it is gate-out or unload start. The two readings differ by unload time plus exit time (about 60 minutes here).",
"Store the constraint type explicitly: LATEST_GATE_OUT, LATEST_UNLOAD_END or LATEST_START.")

# ---------- 5. Meaning of fields ----------
f("F32","Semantics","High","'max_vehicle_weight_kg' is compared with cargo weight","new","""
SELECT v.vehicle_type_code, vt.description, MAX(s.load_weight_kg) AS max_payload_kg,
       (SELECT MIN(max_vehicle_weight_kg) FROM docks WHERE dock_type='STANDARD') AS smallest_standard_dock_kg
FROM shipments s JOIN vehicles v USING(vehicle_id) JOIN vehicle_types vt USING(vehicle_type_code)
GROUP BY v.vehicle_type_code ORDER BY max_payload_kg DESC""",
"The column says vehicle weight, the rule (RULE004) and both designs compare it with payload. A loaded 32-ft multi-axle truck weighs far more than its payload, so under the literal meaning every standard dock would be too weak. The data only makes sense if the limit is a payload limit.",
"Grey-area question for the client; until answered, treat the limit as a payload limit and say so.")

f("F33","Semantics","Low","Only one check-in per shipment is allowed","new","""
SELECT name, sql FROM sqlite_master WHERE name='facility_checkins'""",
"A truck that is turned away and comes back (re-delivery, rejected load) cannot be recorded.",
"Allow multiple visits per shipment (visit_seq).")

f("F34","Semantics","Medium","An unload overrun is recorded as a 'capacity reduction' and in free-text notes","A19","""
SELECT fc.shipment_id, d.dock_code, fc.unload_start_ts, s.expected_unload_min, fc.unload_end_ts, fc.notes,
       e.event_type, e.event_start_ts, e.event_end_ts
FROM facility_checkins fc JOIN shipments s USING(shipment_id) JOIN docks d ON d.dock_id=fc.actual_dock_id
LEFT JOIN dock_status_events e ON e.dock_id=fc.actual_dock_id
WHERE fc.queue_state='IN_DOCK'""",
"The scheduler has to guess when D2 frees up.",
"Compute expected_free_at = unload_start + expected_unload (+ overrun buffer once exceeded) and treat it as a fixed block.")

f("F35","Semantics","Medium","No-show booking is closed but its shipment is still ASSIGNED","A17","""
SELECT a.appointment_id, a.appointment_status, a.is_current, s.shipment_id, s.current_status, s.vehicle_id
FROM appointments a JOIN shipments s USING(shipment_id) WHERE a.appointment_status='NO_SHOW'""",
"The shipment has no live appointment and no next step.",
"A no-show opens an exception for dispatch automatically.")

f("F36","Semantics","High","Moving shipments with no current appointment","A18",f"""
SELECT s.shipment_id, s.priority_code, s.required_dock_type, s.current_status, e.effective_eta_ts
FROM shipments s JOIN v_latest_eta e USING(shipment_id)
WHERE s.current_status IN ('IN_TRANSIT','WAITING','AT_GATE')
  AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id=s.shipment_id AND a.is_current=1 AND a.appointment_status IN {ACTIVE})""",
"SHP1012 and SHP1016 are on the road with nothing booked; they must be in every re-plan.",
"The facility planner's job list = every inbound shipment, booked or not.")


def run():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    results = []
    for x in F:
        if x["id"] == "F09":
            x["rows"] = probe_indexes()
            x["cols"] = ["attempt", "result"]
        else:
            cur = con.execute(x["sql"])
            x["cols"] = [d[0] for d in cur.description]
            x["rows"] = [list(r) for r in cur.fetchall()]
        x["row_count"] = len(x["rows"])
        results.append(x)
    json.dump(results, open(os.path.join(OUT, "audit_results.json"), "w"), indent=1, default=str)
    with open(os.path.join(OUT, "audit_queries.sql"), "w") as fh:
        fh.write("-- SetuHaul data audit: one proof query per finding. Run against setuhaul_freight_operations.db\n\n")
        for x in results:
            fh.write(f"-- {x['id']} [{x['sev']}] {x['title']}  (refs: {x['refs']})\n{x['sql']};\n\n")
    for x in results:
        print(f"{x['id']} {x['sev']:8} rows={x['row_count']:3}  {x['title']}")
        for r in x["rows"][:6]:
            print("      ", r)


def probe_indexes():
    """Try seven bad bookings on a throw-away copy; record whether SQLite accepts them."""
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "probe.db")
    shutil.copy(DB, p)
    c = sqlite3.connect(p, isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    now = "2026-08-04T09:45:00+05:30"
    tests = [
        ("Book a BLOCKED slot (D3 10:00, leveller breakdown) for SHP1012", "SHP1012", "SLOT-JAI-031", "CONFIRMED", 1),
        ("Book a Gurugram slot for a Jaipur shipment (SHP1012)", "SHP1012", "SLOT-GGN-084", "CONFIRMED", 1),
        ("Book 31 t heavy SHP1016 on 20 t standard D1 13:00", "SHP1016", "SLOT-JAI-006", "CONFIRMED", 1),
        ("Book the CANCELLED shipment SHP1019", "SHP1019", "SLOT-JAI-007", "CONFIRMED", 1),
        ("Book a slot that ended before as-of (D4 08:00)", "SHP1012", "SLOT-JAI-043", "CONFIRMED", 1),
        ("Active booking with is_current=0 (escapes per-shipment index): SHP1009 second booking", "SHP1009", "SLOT-JAI-048", "CONFIRMED", 0),
        ("Overlap across slots: 75-min SHP1014 at D4 10:00 runs into SHP1009's D4 11:00", "SHP1014", "SLOT-JAI-045", "CONFIRMED", 1),
        ("Second active booking on an occupied slot (D1 10:00, APT1007)", "SHP1012", "SLOT-JAI-003", "CONFIRMED", 1),
    ]
    out = []
    for i, (label, shp, slot, st, cur) in enumerate(tests):
        c.execute("BEGIN")
        try:
            if label.startswith("Overlap"):
                # SLOT-JAI-045 belongs to APT1006 (certain miss). Cancel it first, as a re-planner would.
                c.execute("UPDATE appointments SET appointment_status='CANCELLED', is_current=0 WHERE appointment_id='APT1006'")
                c.execute("UPDATE appointments SET appointment_status='CANCELLED', is_current=0 WHERE appointment_id='APT1014A'")
            c.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
                      (f"PROBE{i}", shp, slot, st, "SCHEDULING_TOOL", cur, now, now, now))
            out.append([label, "ACCEPTED (gap)"])
        except sqlite3.IntegrityError as e:
            out.append([label, f"REJECTED: {e}"])
        c.execute("ROLLBACK")
    c.close()
    shutil.rmtree(tmp)
    return out


if __name__ == "__main__":
    run()
