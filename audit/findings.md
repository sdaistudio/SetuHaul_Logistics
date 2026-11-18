# Data audit — evidence for the Problem Definition Sheet

Each finding is proved by the query under it, run read-only on the supplied snapshot.

## A01. Active booking violates dock weight limit

SHP1005 (20.5 t) is CONFIRMED on D3 (max 20 t) and SHP1014 (21 t) is PENDING on D1 (max 20 t). The system of record already holds infeasible bookings.

```sql
SELECT a.appointment_id, a.shipment_id, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg, a.appointment_status
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id) JOIN docks d ON d.dock_id = sl.dock_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') AND s.load_weight_kg > d.max_vehicle_weight_kg;
```

Rows returned: **2**

| appointment_id | shipment_id | load_weight_kg | dock_code | max_vehicle_weight_kg | appointment_status |
|---|---|---|---|---|---|
| APT1014A | SHP1014 | 21000 | D1 | 20000 | PENDING_CONFIRMATION |
| APT1005 | SHP1005 | 20500 | D3 | 20000 | CONFIRMED |

## A02. Dock event not reflected in slot status

DEVT001 breakdown starts 09:15 but SLOT-JAI-030 (09:00-10:00) stays OPEN and holds a CONFIRMED booking; DEVT003 blocks nothing.

```sql
SELECT e.dock_event_id, e.event_type, e.event_start_ts, e.event_end_ts, sl.slot_id, sl.slot_start_ts, sl.slot_end_ts, sl.slot_status
FROM dock_status_events e JOIN appointment_slots sl ON sl.dock_id = e.dock_id
WHERE sl.slot_start_ts < COALESCE(e.event_end_ts, '9999') AND sl.slot_end_ts > e.event_start_ts AND sl.slot_status = 'OPEN';
```

Rows returned: **3**

| dock_event_id | event_type | event_start_ts | event_end_ts | slot_id | slot_start_ts | slot_end_ts | slot_status |
|---|---|---|---|---|---|---|---|
| DEVT001 | BREAKDOWN | 2026-08-04T09:15:00+05:30 | 2026-08-04T13:00:00+05:30 | SLOT-JAI-030 | 2026-08-04T09:00:00+05:30 | 2026-08-04T10:00:00+05:30 | OPEN |
| DEVT003 | CAPACITY_REDUCTION | 2026-08-04T08:00:00+05:30 | 2026-08-04T09:20:00+05:30 | SLOT-JAI-015 | 2026-08-04T08:00:00+05:30 | 2026-08-04T09:00:00+05:30 | OPEN |
| DEVT003 | CAPACITY_REDUCTION | 2026-08-04T08:00:00+05:30 | 2026-08-04T09:20:00+05:30 | SLOT-JAI-016 | 2026-08-04T09:00:00+05:30 | 2026-08-04T10:00:00+05:30 | OPEN |

## A03. Unload longer than the slot

70- and 75-min unloads booked into 60-min slots spill into the next booking on the same dock. Minutes are rounded, not truncated: v1 used CAST(... AS INT) on julianday differences, which turned some 60-min slots into 59 and reported 7 rows; only 4 are real (review item A3).

```sql
SELECT a.appointment_id, a.shipment_id, s.expected_unload_min,
       CAST(ROUND((julianday(sl.slot_end_ts) - julianday(sl.slot_start_ts)) * 1440) AS INT) AS slot_min, a.appointment_status
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id)
WHERE s.expected_unload_min > ROUND((julianday(sl.slot_end_ts) - julianday(sl.slot_start_ts)) * 1440) AND a.appointment_status <> 'CANCELLED';
```

Rows returned: **4**

| appointment_id | shipment_id | expected_unload_min | slot_min | appointment_status |
|---|---|---|---|---|
| APT1002 | SHP1002 | 70 | 60 | IN_PROGRESS |
| APT1005 | SHP1005 | 75 | 60 | CONFIRMED |
| APT1009 | SHP1009 | 75 | 60 | CONFIRMED |
| APT1014A | SHP1014 | 75 | 60 | PENDING_CONFIRMATION |

## A04. Booked slot already missed by declared ETA

Confirmed appointments whose truck's latest ETA is after the window ends; capacity is held but will not be used.

```sql
SELECT a.appointment_id, a.shipment_id, sl.slot_start_ts, sl.slot_end_ts, le.effective_eta_ts, le.eta_confidence
FROM appointments a JOIN appointment_slots sl USING(slot_id) JOIN v_latest_eta le USING(shipment_id)
WHERE a.is_current = 1 AND a.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION') AND le.effective_eta_ts > sl.slot_end_ts;
```

Rows returned: **2**

| appointment_id | shipment_id | slot_start_ts | slot_end_ts | effective_eta_ts | eta_confidence |
|---|---|---|---|---|---|
| APT1006 | SHP1006 | 2026-08-04T10:00:00+05:30 | 2026-08-04T11:00:00+05:30 | 2026-08-04T11:20:00+05:30 | HIGH |
| APT1015A | SHP1015 | 2026-08-04T17:00:00+05:30 | 2026-08-04T18:00:00+05:30 | 2026-08-04T18:30:00+05:30 | HIGH |

## A05. One driver driving two trucks at once

DRV007 is IN_TRANSIT on VEH008 (SHP1007) and VEH007 (SHP1016) at the same time.

```sql
SELECT driver_id, GROUP_CONCAT(shipment_id || ':' || vehicle_id, ', ') AS shipments, COUNT(*) n
FROM shipments WHERE current_status = 'IN_TRANSIT' GROUP BY driver_id HAVING COUNT(DISTINCT vehicle_id) > 1;
```

Rows returned: **1**

| driver_id | shipments | n |
|---|---|---|
| DRV007 | SHP1007:VEH008, SHP1016:VEH007 | 2 |

## A06. Same vehicle on more than one live shipment

VEH004 (yard, SHP1004) also carries SHP1020 at 18:00; VEH015 Jaipur 11:20 and Gurugram 14:00.

```sql
SELECT vehicle_id, GROUP_CONCAT(shipment_id || ' ' || current_status || ' ' || destination_facility_id || ' ' || substr(original_eta_ts, 12, 5), ' | ') AS shipments
FROM shipments WHERE current_status NOT IN ('COMPLETED','CANCELLED') GROUP BY vehicle_id HAVING COUNT(*) > 1;
```

Rows returned: **3**

| vehicle_id | shipments |
|---|---|
| VEH003 | SHP1003 WAITING FAC-JAI-01 08:50 / SHP1018 ASSIGNED FAC-JAI-01 11:50 |
| VEH004 | SHP1004 WAITING FAC-JAI-01 08:45 / SHP1020 ASSIGNED FAC-JAI-01 18:00 |
| VEH015 | SHP1006 IN_TRANSIT FAC-JAI-01 10:20 / SHP1021 ASSIGNED FAC-GGN-01 14:00 |

## A07. Pending request on an incompatible dock

APT1014A asks D1 (20 t) for a 21 t CRITICAL load, and OM001 emails the warehouse to confirm it.

```sql
SELECT a.appointment_id, a.shipment_id, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg, a.appointment_status, om.operational_message_id, om.delivery_status
FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl USING(slot_id) JOIN docks d ON d.dock_id = sl.dock_id
LEFT JOIN operational_messages om ON om.appointment_id = a.appointment_id
WHERE a.appointment_status = 'PENDING_CONFIRMATION' AND s.load_weight_kg > d.max_vehicle_weight_kg;
```

Rows returned: **1**

| appointment_id | shipment_id | load_weight_kg | dock_code | max_vehicle_weight_kg | appointment_status | operational_message_id | delivery_status |
|---|---|---|---|---|---|---|---|
| APT1014A | SHP1014 | 21000 | D1 | 20000 | PENDING_CONFIRMATION | OM001 | SENT |

## A08. Snapshot mixes clock times

The "snapshot" contains rows stamped after 16:00 (and a 12:35 no-show) next to open 09:30 conversations; there is no single as-of time.

```sql
SELECT 'appointments' t, MAX(updated_at) latest FROM appointments UNION ALL
SELECT 'chat_messages', MAX(message_ts) FROM chat_messages UNION ALL
SELECT 'eta_updates', MAX(created_at) FROM eta_updates UNION ALL
SELECT 'facility_checkins', MAX(updated_at) FROM facility_checkins;
```

Rows returned: **4**

| t | latest |
|---|---|
| appointments | 2026-08-04T16:30:00+05:30 |
| chat_messages | 2026-08-04T16:24:08+05:30 |
| eta_updates | 2026-08-04T16:25:00+05:30 |
| facility_checkins | 2026-08-04T09:30:00+05:30 |

## A09. Duplicates not enforced by the schema

external_message_id and driver_exceptions.dedupe_key have no UNIQUE constraint; the retry in THR009 got a new external id (wa-9009) so id-based dedupe alone fails.

```sql
SELECT m1.chat_message_id, m1.external_message_id, m2.chat_message_id dup_id, m2.external_message_id dup_ext, m1.message_text
FROM chat_messages m1 JOIN chat_messages m2 ON m1.message_text = m2.message_text AND m1.chat_message_id < m2.chat_message_id AND m1.sender_type = 'DRIVER';
```

Rows returned: **1**

| chat_message_id | external_message_id | dup_id | dup_ext | message_text |
|---|---|---|---|---|
| MSG001 | wa-9001 | MSG015 | wa-9009 | Traffic after Shahpura. Reaching around 11:20. Any slot after 12? |

## A10. ETA without provenance

shipments.latest_eta_ts differs from the original plan for shipments that have no eta_updates row (who changed it, how sure?).

```sql
SELECT s.shipment_id, s.original_eta_ts, s.latest_eta_ts
FROM shipments s WHERE s.latest_eta_ts <> s.original_eta_ts AND NOT EXISTS (SELECT 1 FROM eta_updates e WHERE e.shipment_id = s.shipment_id);
```

Rows returned: **4**

| shipment_id | original_eta_ts | latest_eta_ts |
|---|---|---|
| SHP1002 | 2026-08-04T07:55:00+05:30 | 2026-08-04T07:58:00+05:30 |
| SHP1009 | 2026-08-04T10:45:00+05:30 | 2026-08-04T10:50:00+05:30 |
| SHP1010 | 2026-08-04T10:50:00+05:30 | 2026-08-04T10:55:00+05:30 |
| SHP1011 | 2026-08-04T10:40:00+05:30 | 2026-08-04T10:45:00+05:30 |

## A11. View overstates confidence

v_latest_eta reports eta_confidence = HIGH for shipments that only have the original plan.

```sql
SELECT shipment_id, eta_source, eta_confidence FROM v_latest_eta WHERE eta_source = 'ORIGINAL_PLAN' AND eta_confidence = 'HIGH';
```

Rows returned: **10**

| shipment_id | eta_source | eta_confidence |
|---|---|---|
| SHP1002 | ORIGINAL_PLAN | HIGH |
| SHP1007 | ORIGINAL_PLAN | HIGH |
| SHP1008 | ORIGINAL_PLAN | HIGH |
| SHP1009 | ORIGINAL_PLAN | HIGH |
| SHP1010 | ORIGINAL_PLAN | HIGH |
| SHP1011 | ORIGINAL_PLAN | HIGH |
| SHP1018 | ORIGINAL_PLAN | HIGH |
| SHP1019 | ORIGINAL_PLAN | HIGH |
| SHP1020 | ORIGINAL_PLAN | HIGH |
| SHP1021 | ORIGINAL_PLAN | HIGH |

## A12. View ignores dock events, weight and holds

v_slot_availability calls SLOT-JAI-030 OCCUPIED/AVAILABLE purely from slot_status + appointments; it cannot answer "available for THIS truck".

```sql
SELECT slot_id, dock_code, slot_start_ts, availability_status FROM v_slot_availability
WHERE slot_id IN ('SLOT-JAI-030','SLOT-JAI-016','SLOT-JAI-004');
```

Rows returned: **3**

| slot_id | dock_code | slot_start_ts | availability_status |
|---|---|---|---|
| SLOT-JAI-004 | D1 | 2026-08-04T11:00:00+05:30 | OCCUPIED |
| SLOT-JAI-016 | D2 | 2026-08-04T09:00:00+05:30 | OCCUPIED |
| SLOT-JAI-030 | D3 | 2026-08-04T09:00:00+05:30 | OCCUPIED |

## A13. Escalation contact unusable

CON005 (Gurugram night shift) is a vacant position with no email; OM004 went to night.shift@ which is not in facility_contacts and FAILED.

```sql
SELECT contact_id, facility_id, contact_role, contact_name, email, phone FROM facility_contacts WHERE email IS NULL OR contact_name LIKE '%Vacant%';
```

Rows returned: **1**

| contact_id | facility_id | contact_role | contact_name | email | phone |
|---|---|---|---|---|---|
| CON005 | FAC-GGN-01 | NIGHT_SHIFT_COORDINATOR | Vacant Position |  | +91-124-5001209 |

## A14. Failed message not treated as delivered

OM004 FAILED; the related thread is ESCALATED but no successful hand-off exists.

```sql
SELECT om.operational_message_id, om.shipment_id, om.recipient_address, om.delivery_status, t.thread_id, t.thread_status
FROM operational_messages om LEFT JOIN chat_threads t ON t.shipment_id = om.shipment_id WHERE om.delivery_status = 'FAILED';
```

Rows returned: **1**

| operational_message_id | shipment_id | recipient_address | delivery_status | thread_id | thread_status |
|---|---|---|---|---|---|
| OM004 | SHP1015 | night.shift@setuhaul.example | FAILED | THR005 | ESCALATED |

## A15. Operating-hour rule is ambiguous

RULE005 says no new start after 21:00, yet 21:00-22:00 slots exist and D6 20:00 slot ends 21:30; close_time is 22:00.

```sql
SELECT sl.slot_id, d.dock_code, substr(sl.slot_start_ts, 12, 5) start, substr(sl.slot_end_ts, 12, 5) "end", f.close_time
FROM appointment_slots sl JOIN docks d USING(dock_id) JOIN facilities f ON f.facility_id = sl.facility_id
WHERE substr(sl.slot_start_ts, 12, 5) >= '21:00' OR substr(sl.slot_end_ts, 12, 5) > '21:00';
```

Rows returned: **6**

| slot_id | dock_code | start | end | close_time |
|---|---|---|---|---|
| SLOT-JAI-014 | D1 | 21:00 | 22:00 | 22:00 |
| SLOT-JAI-028 | D2 | 21:00 | 22:00 | 22:00 |
| SLOT-JAI-042 | D3 | 21:00 | 22:00 | 22:00 |
| SLOT-JAI-056 | D4 | 21:00 | 22:00 | 22:00 |
| SLOT-JAI-070 | D5 | 21:00 | 22:00 | 22:00 |
| SLOT-JAI-079 | D6 | 20:00 | 21:30 | 22:00 |

## A16. Reefer rule hard-codes a dock code

RULE003 names "D5" in text; rules should reference dock capability, not a code.

```sql
SELECT rule_id, rule_type, rule_value, description FROM facility_rules WHERE description LIKE '%D5%';
```

Rows returned: **1**

| rule_id | rule_type | rule_value | description |
|---|---|---|---|
| RULE003 | REEFER_DOCK_REQUIRED | TRUE | Temperature-controlled loads must use dock D5. |

## A17. No-show recorded but shipment still ASSIGNED

APT1018 is NO_SHOW while SHP1018 status is ASSIGNED and its truck (VEH003) is in the yard for SHP1003.

```sql
SELECT a.appointment_id, a.appointment_status, s.shipment_id, s.current_status, s.vehicle_id
FROM appointments a JOIN shipments s USING(shipment_id) WHERE a.appointment_status = 'NO_SHOW';
```

Rows returned: **1**

| appointment_id | appointment_status | shipment_id | current_status | vehicle_id |
|---|---|---|---|---|
| APT1018 | NO_SHOW | SHP1018 | ASSIGNED | VEH003 |

## A18. In-transit shipments with no current booking

SHP1012 and SHP1016 lost their appointments; nothing re-books them unless a driver chats.

```sql
SELECT s.shipment_id, s.priority_code, s.current_status FROM shipments s
WHERE s.current_status IN ('IN_TRANSIT','WAITING') AND NOT EXISTS (
  SELECT 1 FROM appointments a WHERE a.shipment_id = s.shipment_id AND a.is_current = 1
  AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'));
```

Rows returned: **2**

| shipment_id | priority_code | current_status |
|---|---|---|
| SHP1012 | NORMAL | IN_TRANSIT |
| SHP1016 | HIGH | IN_TRANSIT |

## A19. Unload overrun visible only in notes

SHP1002 started 08:08 with a 70-min unload, still IN_DOCK at 09:10; the next D2 booking (SHP1004, 09:00) is already late.

```sql
SELECT fc.shipment_id, fc.unload_start_ts, s.expected_unload_min, fc.unload_end_ts, fc.queue_state, fc.notes
FROM facility_checkins fc JOIN shipments s USING(shipment_id) WHERE fc.queue_state = 'IN_DOCK';
```

Rows returned: **1**

| shipment_id | unload_start_ts | expected_unload_min | unload_end_ts | queue_state | notes |
|---|---|---|---|---|---|
| SHP1002 | 2026-08-04T08:08:00+05:30 | 70 |  | IN_DOCK | Unload running longer than planned. |

## A20. What the two unique indexes do NOT cover

They stop two active bookings on one slot and two current bookings per shipment. They do not stop: overlapping service intervals on a dock across slots, wrong dock type/weight, bookings inside dock events, bookings for cancelled shipments, or holds. Proof: this query returns 0 rows today, yet A01-A03 are violations.

```sql
SELECT slot_id, COUNT(*) FROM appointments WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY slot_id HAVING COUNT(*) > 1;
```

Rows returned: **0**
