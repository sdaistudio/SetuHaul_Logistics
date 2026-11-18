-- SetuHaul data audit: one proof query per finding. Run against setuhaul_freight_operations.db

-- F01 [Critical] Active bookings on docks rated below the load weight  (refs: A01, A07; B-D1, B-D2)
SELECT a.appointment_id, a.appointment_status, s.shipment_id, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg
FROM appointments a JOIN shipments s USING(shipment_id)
JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') AND s.load_weight_kg > d.max_vehicle_weight_kg;

-- F02 [Critical] Dock events are not reflected in slot status  (refs: A02; B-D5)
SELECT e.dock_event_id, e.event_type, d.dock_code, e.event_start_ts, e.event_end_ts, sl.slot_id, sl.slot_start_ts, sl.slot_status
FROM dock_status_events e JOIN appointment_slots sl ON sl.dock_id=e.dock_id
JOIN docks d ON d.dock_id=e.dock_id
WHERE sl.slot_status='OPEN' AND sl.slot_start_ts < COALESCE(e.event_end_ts,'9999') AND sl.slot_end_ts > e.event_start_ts
ORDER BY e.dock_event_id, sl.slot_start_ts;

-- F03 [Medium] docks.dock_status stays ACTIVE during a breakdown or maintenance  (refs: B-D6)
SELECT d.dock_code, d.dock_status, e.event_type, e.event_start_ts, e.event_end_ts
FROM docks d JOIN dock_status_events e USING(dock_id)
WHERE e.event_type IN ('BREAKDOWN','MAINTENANCE') AND d.dock_status='ACTIVE';

-- F04 [High] Planned unload is longer than the booked slot  (refs: A03; B-D3)
SELECT a.appointment_id, s.shipment_id, d.dock_code, sl.slot_start_ts, sl.slot_end_ts,
       CAST(ROUND((julianday(sl.slot_end_ts)-julianday(sl.slot_start_ts))*1440) AS INT) AS slot_min, s.expected_unload_min
FROM appointments a JOIN shipments s USING(shipment_id)
JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
WHERE a.appointment_status NOT IN ('CANCELLED','REJECTED')
  AND s.expected_unload_min > ROUND((julianday(sl.slot_end_ts)-julianday(sl.slot_start_ts))*1440)
ORDER BY sl.slot_start_ts;

-- F05 [High] Booked work already collides on the same dock (cascade proof)  (refs: new)
WITH b AS (
  SELECT a.appointment_id, sl.dock_id, d.dock_code, sl.slot_start_ts AS st,
         datetime(sl.slot_start_ts, '+'||s.expected_unload_min||' minutes') AS planned_end_utc
  FROM appointments a JOIN shipments s USING(shipment_id)
  JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
  WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'))
SELECT x.dock_code, x.appointment_id AS first_booking, x.st AS first_start,
       y.appointment_id AS next_booking, y.st AS next_start,
       CAST(ROUND((julianday(x.planned_end_utc)-julianday(y.st))*1440) AS INT) AS overlap_min
FROM b x JOIN b y ON x.dock_id=y.dock_id AND y.st > x.st
WHERE julianday(x.planned_end_utc) > julianday(y.st)
  AND NOT EXISTS (SELECT 1 FROM b z WHERE z.dock_id=x.dock_id AND z.st > x.st AND z.st < y.st);

-- F06 [High] Current bookings that the latest ETA already misses (phantom capacity)  (refs: A04; B-D4)
SELECT a.appointment_id, s.shipment_id, sl.slot_start_ts, sl.slot_end_ts, e.effective_eta_ts, e.eta_confidence
FROM appointments a JOIN shipments s USING(shipment_id)
JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN v_latest_eta e USING(shipment_id)
LEFT JOIN facility_checkins fc USING(shipment_id)
WHERE a.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION') AND a.is_current=1 AND fc.gate_in_ts IS NULL
  AND julianday(e.effective_eta_ts) > julianday(sl.slot_start_ts, '+30 minutes');

-- F07 [Critical] SHP1015's original booking was infeasible from the day maintenance was planned  (refs: new)
SELECT a.appointment_id, a.booked_at, sl.slot_start_ts, sl.slot_end_ts, s.original_eta_ts, s.expected_unload_min,
       strftime('%H:%M', s.original_eta_ts, '+'||(s.expected_unload_min+15+330)||' minutes') AS planned_unload_end_ist,
       e.dock_event_id, e.event_start_ts AS maintenance_start, e.created_at AS maintenance_planned_at
FROM appointments a JOIN shipments s USING(shipment_id)
JOIN appointment_slots sl ON sl.slot_id=a.slot_id
JOIN dock_status_events e ON e.dock_id=sl.dock_id AND e.event_type='MAINTENANCE'
WHERE a.appointment_id='APT1015A';

-- F08 [Critical] A historic double booking: two CONFIRMED bookings on one slot for three days  (refs: new)
SELECT a.slot_id, a.appointment_id, a.shipment_id, a.appointment_status, a.confirmed_at, a.cancelled_at, a.cancellation_reason
FROM appointments a WHERE a.slot_id IN (SELECT slot_id FROM appointments GROUP BY slot_id HAVING COUNT(*)>1)
ORDER BY a.slot_id, a.confirmed_at;

-- F09 [Critical] The two supplied unique indexes leave seven bad bookings possible  (refs: A20; B-D17)
-- proved by test inserts on a throw-away copy; see 'probe' rows;

-- F10 [Medium] v_slot_availability has no notion of time, weight, events or holds  (refs: A12; B-D7)
SELECT slot_id, dock_code, slot_start_ts, availability_status FROM v_slot_availability
WHERE facility_id='FAC-JAI-01' AND availability_status='AVAILABLE' AND slot_start_ts < '2026-08-04T09:45:00+05:30'
ORDER BY slot_start_ts LIMIT 8;

-- F11 [High] One driver on two moving trucks  (refs: A05; B-D9)
SELECT driver_id, COUNT(*) AS trips, GROUP_CONCAT(shipment_id||'/'||vehicle_id, ', ') AS trips_and_trucks
FROM shipments WHERE current_status='IN_TRANSIT' GROUP BY driver_id HAVING COUNT(DISTINCT vehicle_id)>1;

-- F12 [High] One vehicle on several live shipments  (refs: A06; B-D10)
SELECT vehicle_id, GROUP_CONCAT(shipment_id||' ('||current_status||', departs '||substr(planned_departure_ts,12,5)||')', '; ') AS shipments
FROM shipments WHERE current_status NOT IN ('COMPLETED','CANCELLED')
GROUP BY vehicle_id HAVING COUNT(*)>1;

-- F13 [High] A truck leaves Neemrana while it is still inside Jaipur DC  (refs: new)
SELECT a.shipment_id AS first_trip, a.vehicle_id, fc.gate_out_ts AS left_jaipur_at,
       b.shipment_id AS next_trip, b.origin_city, b.actual_departure_ts AS departed_origin_at
FROM shipments a JOIN facility_checkins fc ON fc.shipment_id=a.shipment_id
JOIN shipments b ON b.vehicle_id=a.vehicle_id AND b.shipment_id<>a.shipment_id
WHERE b.actual_departure_ts IS NOT NULL AND fc.gate_out_ts IS NOT NULL
  AND julianday(b.actual_departure_ts) < julianday(fc.gate_out_ts);

-- F14 [High] Downstream trips are infeasible, so a 'no-show' is really a planning error  (refs: A17 (partly); new root cause)
SELECT nxt.shipment_id AS later_trip, nxt.vehicle_id, nxt.origin_city, nxt.planned_departure_ts,
       cur.shipment_id AS truck_busy_with, cur.current_status AS busy_status,
       COALESCE(fc.gate_in_ts, cur.latest_eta_ts) AS busy_at_jaipur_from, a.appointment_status AS later_trip_appt
FROM shipments nxt JOIN shipments cur ON cur.vehicle_id=nxt.vehicle_id AND cur.shipment_id<>nxt.shipment_id
LEFT JOIN facility_checkins fc ON fc.shipment_id=cur.shipment_id
LEFT JOIN appointments a ON a.shipment_id=nxt.shipment_id
WHERE nxt.current_status='ASSIGNED' AND cur.current_status IN ('IN_TRANSIT','WAITING')
  AND julianday(COALESCE(fc.gate_in_ts, cur.latest_eta_ts)) > julianday(nxt.planned_departure_ts, '-6 hours');

-- F15 [High] No single snapshot time: rows run from 07:10 to 16:30  (refs: A08; B-D16)
SELECT 'eta_updates' t, MIN(created_at) mn, MAX(created_at) mx FROM eta_updates
UNION ALL SELECT 'chat_messages', MIN(message_ts), MAX(message_ts) FROM chat_messages
UNION ALL SELECT 'appointments', MIN(updated_at), MAX(updated_at) FROM appointments
UNION ALL SELECT 'facility_checkins', MIN(updated_at), MAX(updated_at) FROM facility_checkins
UNION ALL SELECT 'shipments', MIN(updated_at), MAX(updated_at) FROM shipments;

-- F16 [Medium] latest_eta_ts differs from the plan with no ETA record behind it  (refs: A10; B-D11)
SELECT s.shipment_id, s.original_eta_ts, s.latest_eta_ts FROM shipments s
WHERE s.latest_eta_ts <> s.original_eta_ts AND NOT EXISTS (SELECT 1 FROM eta_updates e WHERE e.shipment_id=s.shipment_id);

-- F17 [Medium] The ETA view labels the original plan as HIGH confidence  (refs: A11)
SELECT shipment_id, effective_eta_ts, eta_source, eta_confidence FROM v_latest_eta
WHERE eta_source='ORIGINAL_PLAN' AND eta_confidence='HIGH';

-- F18 [Medium] An ETA range cannot be stored; it is squeezed into one point  (refs: new)
SELECT eta_update_id, shipment_id, declared_eta_ts, confidence_code, note FROM eta_updates WHERE note LIKE '%between%';

-- F19 [Medium] Trucks inside the grace period are labelled LATE  (refs: new)
SELECT fc.shipment_id, fc.gate_in_ts, sl.slot_start_ts, fc.arrival_state,
       CAST(ROUND((julianday(fc.gate_in_ts)-julianday(sl.slot_start_ts))*1440) AS INT) AS minutes_after_start,
       (SELECT rule_value FROM facility_rules r WHERE r.facility_id=fc.facility_id AND r.rule_type='NO_SHOW_GRACE_MIN') AS grace_min
FROM facility_checkins fc JOIN appointments a ON a.shipment_id=fc.shipment_id AND a.is_current=1
JOIN appointment_slots sl ON sl.slot_id=a.slot_id
WHERE fc.arrival_state='LATE';

-- F20 [Medium] The 21:00 last-start rule conflicts with slots that start at 21:00  (refs: A15)
SELECT sl.slot_id, d.dock_code, substr(sl.slot_start_ts,12,5) AS starts, substr(sl.slot_end_ts,12,5) AS ends, sl.slot_status
FROM appointment_slots sl JOIN docks d USING(dock_id)
WHERE sl.facility_id='FAC-JAI-01' AND substr(sl.slot_start_ts,12,5) >= '21:00' ORDER BY sl.slot_start_ts;

-- F21 [Medium] Rules are duplicated, missing or wider than the slot grid  (refs: new)
SELECT f.facility_id, f.open_time, f.close_time, f.checkin_grace_min AS grace_in_facilities,
  (SELECT rule_value FROM facility_rules r WHERE r.facility_id=f.facility_id AND r.rule_type='NO_SHOW_GRACE_MIN') AS grace_in_rules,
  (SELECT COUNT(*) FROM facility_rules r WHERE r.facility_id=f.facility_id) AS rule_count,
  (SELECT MIN(substr(slot_start_ts,12,5)) FROM appointment_slots s WHERE s.facility_id=f.facility_id) AS first_slot,
  (SELECT MAX(substr(slot_end_ts,12,5)) FROM appointment_slots s WHERE s.facility_id=f.facility_id) AS last_slot_end
FROM facilities f;

-- F22 [Low] The reefer rule names dock D5 in free text; rule values are untyped text  (refs: A16; new)
SELECT rule_id, facility_id, rule_type, rule_value, typeof(rule_value) AS stored_as, description FROM facility_rules ORDER BY rule_id;

-- F23 [Medium] Slots exist only for 4 August: no next-day capacity  (refs: new)
SELECT facility_id, MIN(substr(slot_start_ts,1,10)) AS first_day, MAX(substr(slot_end_ts,1,10)) AS last_day, COUNT(*) AS slots
FROM appointment_slots GROUP BY facility_id;

-- F24 [Low] Slots have no updated_at: a 4 Aug breakdown appears on rows 'created' 1 Aug  (refs: new)
SELECT slot_id, slot_status, block_reason, created_at FROM appointment_slots WHERE slot_status='BLOCKED' ORDER BY slot_id;

-- F25 [High] A messaging retry is stored as a new message, exception and thread  (refs: A09; B-D12)
SELECT m.chat_message_id, m.thread_id, m.external_message_id, m.message_ts, m.is_duplicate, e.exception_id, e.dedupe_key, e.exception_status
FROM chat_messages m LEFT JOIN driver_exceptions e ON e.thread_id=m.thread_id
WHERE m.sender_type='DRIVER' AND m.message_text IN (SELECT message_text FROM chat_messages WHERE sender_type='DRIVER' GROUP BY message_text HAVING COUNT(*)>1);

-- F26 [Medium] One driver, several threads: context is split  (refs: new)
SELECT driver_id, COUNT(*) AS threads, GROUP_CONCAT(thread_id||' '||substr(opened_at,12,5)||' '||thread_intent, ' | ') AS detail
FROM chat_threads GROUP BY driver_id HAVING COUNT(*)>1;

-- F27 [High] Three driver messages never got a reply  (refs: new)
SELECT t.thread_id, t.driver_id, t.thread_status, m.message_ts, m.message_text
FROM chat_threads t JOIN chat_messages m ON m.thread_id=t.thread_id AND m.sender_type='DRIVER' AND m.is_duplicate=0
WHERE NOT EXISTS (SELECT 1 FROM chat_messages r WHERE r.thread_id=t.thread_id AND r.sender_type<>'DRIVER');

-- F28 [High] Pending confirmation requests that never reached the warehouse  (refs: B-D13)
SELECT om.operational_message_id, om.appointment_id, om.delivery_status, om.sent_at, a.appointment_status, a.booked_at
FROM operational_messages om JOIN appointments a USING(appointment_id)
WHERE a.appointment_status='PENDING_CONFIRMATION';

-- F29 [High] The escalation channel itself is broken  (refs: A13, A14; B-D14, B-D15)
SELECT 'message' AS kind, om.operational_message_id AS id, om.recipient_address AS target, om.delivery_status AS status,
  CASE WHEN EXISTS (SELECT 1 FROM facility_contacts c WHERE c.email=om.recipient_address) THEN 'known contact' ELSE 'not in contacts' END AS note
FROM operational_messages om WHERE om.delivery_status='FAILED'
UNION ALL
SELECT 'contact', contact_id, contact_role||' / '||contact_name, COALESCE(email,'NO EMAIL'), 'phone '||COALESCE(phone,'-')
FROM facility_contacts WHERE email IS NULL OR contact_name LIKE 'Vacant%';

-- F30 [High] The existing chat booking path made an incompatible booking  (refs: A07)
SELECT a.appointment_id, a.booking_source, s.shipment_id, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl ON sl.slot_id=a.slot_id JOIN docks d ON d.dock_id=sl.dock_id
WHERE a.booking_source='DRIVER_CHAT' AND s.load_weight_kg > d.max_vehicle_weight_kg;

-- F31 [Medium] Driver time limits are stored without saying what they limit  (refs: new)
SELECT e.exception_id, e.latest_acceptable_ts, m.message_text AS agent_question
FROM driver_exceptions e JOIN chat_messages m ON m.thread_id=e.thread_id AND m.sender_type='AGENT'
WHERE e.latest_acceptable_ts IS NOT NULL;

-- F32 [High] 'max_vehicle_weight_kg' is compared with cargo weight  (refs: new)
SELECT v.vehicle_type_code, vt.description, MAX(s.load_weight_kg) AS max_payload_kg,
       (SELECT MIN(max_vehicle_weight_kg) FROM docks WHERE dock_type='STANDARD') AS smallest_standard_dock_kg
FROM shipments s JOIN vehicles v USING(vehicle_id) JOIN vehicle_types vt USING(vehicle_type_code)
GROUP BY v.vehicle_type_code ORDER BY max_payload_kg DESC;

-- F33 [Low] Only one check-in per shipment is allowed  (refs: new)
SELECT name, sql FROM sqlite_master WHERE name='facility_checkins';

-- F34 [Medium] An unload overrun is recorded as a 'capacity reduction' and in free-text notes  (refs: A19)
SELECT fc.shipment_id, d.dock_code, fc.unload_start_ts, s.expected_unload_min, fc.unload_end_ts, fc.notes,
       e.event_type, e.event_start_ts, e.event_end_ts
FROM facility_checkins fc JOIN shipments s USING(shipment_id) JOIN docks d ON d.dock_id=fc.actual_dock_id
LEFT JOIN dock_status_events e ON e.dock_id=fc.actual_dock_id
WHERE fc.queue_state='IN_DOCK';

-- F35 [Medium] No-show booking is closed but its shipment is still ASSIGNED  (refs: A17)
SELECT a.appointment_id, a.appointment_status, a.is_current, s.shipment_id, s.current_status, s.vehicle_id
FROM appointments a JOIN shipments s USING(shipment_id) WHERE a.appointment_status='NO_SHOW';

-- F36 [High] Moving shipments with no current appointment  (refs: A18)
SELECT s.shipment_id, s.priority_code, s.required_dock_type, s.current_status, e.effective_eta_ts
FROM shipments s JOIN v_latest_eta e USING(shipment_id)
WHERE s.current_status IN ('IN_TRANSIT','WAITING','AT_GATE')
  AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id=s.shipment_id AND a.is_current=1 AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'));

