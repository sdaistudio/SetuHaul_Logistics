-- SetuHaul data audit (hand-in #1). Each block: -- @id | @title | @impact, then one query.
-- Run: python -m audit.run_audit   (writes audit/findings.md with every result)

-- @A01 | Active booking violates dock weight limit | SHP1005 (20.5 t) is CONFIRMED on D3 (max 20 t) and SHP1014 (21 t) is PENDING on D1 (max 20 t). The system of record already holds infeasible bookings.
SELECT a.appointment_id, a.shipment_id, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg, a.appointment_status
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id) JOIN docks d ON d.dock_id = sl.dock_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') AND s.load_weight_kg > d.max_vehicle_weight_kg;

-- @A02 | Dock event not reflected in slot status | DEVT001 breakdown starts 09:15 but SLOT-JAI-030 (09:00-10:00) stays OPEN and holds a CONFIRMED booking; DEVT003 blocks nothing.
SELECT e.dock_event_id, e.event_type, e.event_start_ts, e.event_end_ts, sl.slot_id, sl.slot_start_ts, sl.slot_end_ts, sl.slot_status
FROM dock_status_events e JOIN appointment_slots sl ON sl.dock_id = e.dock_id
WHERE sl.slot_start_ts < COALESCE(e.event_end_ts, '9999') AND sl.slot_end_ts > e.event_start_ts AND sl.slot_status = 'OPEN';

-- @A03 | Unload longer than the slot | 70- and 75-min unloads booked into 60-min slots spill into the next booking on the same dock. Minutes are rounded, not truncated: v1 used CAST(... AS INT) on julianday differences, which turned some 60-min slots into 59 and reported 7 rows; only 4 are real (review item A3).
SELECT a.appointment_id, a.shipment_id, s.expected_unload_min,
       CAST(ROUND((julianday(sl.slot_end_ts) - julianday(sl.slot_start_ts)) * 1440) AS INT) AS slot_min, a.appointment_status
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id)
WHERE s.expected_unload_min > ROUND((julianday(sl.slot_end_ts) - julianday(sl.slot_start_ts)) * 1440) AND a.appointment_status <> 'CANCELLED';

-- @A04 | Booked slot already missed by declared ETA | Confirmed appointments whose truck's latest ETA is after the window ends; capacity is held but will not be used.
SELECT a.appointment_id, a.shipment_id, sl.slot_start_ts, sl.slot_end_ts, le.effective_eta_ts, le.eta_confidence
FROM appointments a JOIN appointment_slots sl USING(slot_id) JOIN v_latest_eta le USING(shipment_id)
WHERE a.is_current = 1 AND a.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION') AND le.effective_eta_ts > sl.slot_end_ts;

-- @A05 | One driver driving two trucks at once | DRV007 is IN_TRANSIT on VEH008 (SHP1007) and VEH007 (SHP1016) at the same time.
SELECT driver_id, GROUP_CONCAT(shipment_id || ':' || vehicle_id, ', ') AS shipments, COUNT(*) n
FROM shipments WHERE current_status = 'IN_TRANSIT' GROUP BY driver_id HAVING COUNT(DISTINCT vehicle_id) > 1;

-- @A06 | Same vehicle on more than one live shipment | VEH004 (yard, SHP1004) also carries SHP1020 at 18:00; VEH015 Jaipur 11:20 and Gurugram 14:00.
SELECT vehicle_id, GROUP_CONCAT(shipment_id || ' ' || current_status || ' ' || destination_facility_id || ' ' || substr(original_eta_ts, 12, 5), ' | ') AS shipments
FROM shipments WHERE current_status NOT IN ('COMPLETED','CANCELLED') GROUP BY vehicle_id HAVING COUNT(*) > 1;

-- @A07 | Pending request on an incompatible dock | APT1014A asks D1 (20 t) for a 21 t CRITICAL load, and OM001 emails the warehouse to confirm it.
SELECT a.appointment_id, a.shipment_id, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg, a.appointment_status, om.operational_message_id, om.delivery_status
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id) JOIN docks d ON d.dock_id = sl.dock_id
LEFT JOIN operational_messages om ON om.appointment_id = a.appointment_id
WHERE a.appointment_status = 'PENDING_CONFIRMATION' AND s.load_weight_kg > d.max_vehicle_weight_kg;

-- @A08 | Snapshot mixes clock times | The "snapshot" contains rows stamped after 16:00 (and a 12:35 no-show) next to open 09:30 conversations; there is no single as-of time.
SELECT 'appointments' t, MAX(updated_at) latest FROM appointments UNION ALL
SELECT 'chat_messages', MAX(message_ts) FROM chat_messages UNION ALL
SELECT 'eta_updates', MAX(created_at) FROM eta_updates UNION ALL
SELECT 'facility_checkins', MAX(updated_at) FROM facility_checkins;

-- @A09 | Duplicates not enforced by the schema | external_message_id and driver_exceptions.dedupe_key have no UNIQUE constraint; the retry in THR009 got a new external id (wa-9009) so id-based dedupe alone fails.
SELECT m1.chat_message_id, m1.external_message_id, m2.chat_message_id dup_id, m2.external_message_id dup_ext, m1.message_text
FROM chat_messages m1 JOIN chat_messages m2 ON m1.message_text = m2.message_text AND m1.chat_message_id < m2.chat_message_id AND m1.sender_type = 'DRIVER';

-- @A10 | ETA without provenance | shipments.latest_eta_ts differs from the original plan for shipments that have no eta_updates row (who changed it, how sure?).
SELECT s.shipment_id, s.original_eta_ts, s.latest_eta_ts
FROM shipments s WHERE s.latest_eta_ts <> s.original_eta_ts AND NOT EXISTS (SELECT 1 FROM eta_updates e WHERE e.shipment_id = s.shipment_id);

-- @A11 | View overstates confidence | v_latest_eta reports eta_confidence = HIGH for shipments that only have the original plan.
SELECT shipment_id, eta_source, eta_confidence FROM v_latest_eta WHERE eta_source = 'ORIGINAL_PLAN' AND eta_confidence = 'HIGH';

-- @A12 | View ignores dock events, weight and holds | v_slot_availability calls SLOT-JAI-030 OCCUPIED/AVAILABLE purely from slot_status + appointments; it cannot answer "available for THIS truck".
SELECT slot_id, dock_code, slot_start_ts, availability_status FROM v_slot_availability
WHERE slot_id IN ('SLOT-JAI-030','SLOT-JAI-016','SLOT-JAI-004');

-- @A13 | Escalation contact unusable | CON005 (Gurugram night shift) is a vacant position with no email; OM004 went to night.shift@ which is not in facility_contacts and FAILED.
SELECT contact_id, facility_id, contact_role, contact_name, email, phone FROM facility_contacts WHERE email IS NULL OR contact_name LIKE '%Vacant%';

-- @A14 | Failed message not treated as delivered | OM004 FAILED; the related thread is ESCALATED but no successful hand-off exists.
SELECT om.operational_message_id, om.shipment_id, om.recipient_address, om.delivery_status, t.thread_id, t.thread_status
FROM operational_messages om LEFT JOIN chat_threads t ON t.shipment_id = om.shipment_id WHERE om.delivery_status = 'FAILED';

-- @A15 | Operating-hour rule is ambiguous | RULE005 says no new start after 21:00, yet 21:00-22:00 slots exist and D6 20:00 slot ends 21:30; close_time is 22:00.
SELECT sl.slot_id, d.dock_code, substr(sl.slot_start_ts, 12, 5) start, substr(sl.slot_end_ts, 12, 5) "end", f.close_time
FROM appointment_slots sl JOIN docks d USING(dock_id) JOIN facilities f ON f.facility_id = sl.facility_id
WHERE substr(sl.slot_start_ts, 12, 5) >= '21:00' OR substr(sl.slot_end_ts, 12, 5) > '21:00';

-- @A16 | Reefer rule hard-codes a dock code | RULE003 names "D5" in text; rules should reference dock capability, not a code.
SELECT rule_id, rule_type, rule_value, description FROM facility_rules WHERE description LIKE '%D5%';

-- @A17 | No-show recorded but shipment still ASSIGNED | APT1018 is NO_SHOW while SHP1018 status is ASSIGNED and its truck (VEH003) is in the yard for SHP1003.
SELECT a.appointment_id, a.appointment_status, s.shipment_id, s.current_status, s.vehicle_id
FROM appointments a JOIN shipments s USING(shipment_id) WHERE a.appointment_status = 'NO_SHOW';

-- @A18 | In-transit shipments with no current booking | SHP1012 and SHP1016 lost their appointments; nothing re-books them unless a driver chats.
SELECT s.shipment_id, s.priority_code, s.current_status FROM shipments s
WHERE s.current_status IN ('IN_TRANSIT','WAITING') AND NOT EXISTS (
  SELECT 1 FROM appointments a WHERE a.shipment_id = s.shipment_id AND a.is_current = 1
  AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'));

-- @A19 | Unload overrun visible only in notes | SHP1002 started 08:08 with a 70-min unload, still IN_DOCK at 09:10; the next D2 booking (SHP1004, 09:00) is already late.
SELECT fc.shipment_id, fc.unload_start_ts, s.expected_unload_min, fc.unload_end_ts, fc.queue_state, fc.notes
FROM facility_checkins fc JOIN shipments s USING(shipment_id) WHERE fc.queue_state = 'IN_DOCK';

-- @A20 | What the two unique indexes do NOT cover | They stop two active bookings on one slot and two current bookings per shipment. They do not stop: overlapping service intervals on a dock across slots, wrong dock type/weight, bookings inside dock events, bookings for cancelled shipments, or holds. Proof: this query returns 0 rows today, yet A01-A03 are violations.
SELECT slot_id, COUNT(*) FROM appointments WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY slot_id HAVING COUNT(*) > 1;
