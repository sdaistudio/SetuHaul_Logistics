-- Production profile: Aurora PostgreSQL Serverless v2 (ap-south-1).
-- Only the capacity-critical objects are shown; the rest of the SQLite schema
-- ports 1:1 (TEXT timestamps -> timestamptz).

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Same rule the SQLite snapshot enforces: one active booking per slot.
CREATE UNIQUE INDEX ux_active_appointment_per_slot
  ON appointments(slot_id)
  WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');

CREATE UNIQUE INDEX ux_current_active_appointment_per_shipment
  ON appointments(shipment_id)
  WHERE is_current AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');

-- What SQLite cannot express: no two planned service intervals may overlap on
-- one dock, across slots. This closes gap A20 of the data audit.
CREATE TABLE appointment_plan (
  appointment_id text PRIMARY KEY REFERENCES appointments(appointment_id),
  dock_id        text NOT NULL REFERENCES docks(dock_id),
  service        tstzrange NOT NULL,
  active         boolean NOT NULL DEFAULT true,
  EXCLUDE USING gist (dock_id WITH =, service WITH &&) WHERE (active)
);

-- Holds: one live hold per slot and per shipment; expiry swept every minute.
CREATE TABLE slot_holds (
  hold_id      text PRIMARY KEY,
  slot_id      text NOT NULL REFERENCES appointment_slots(slot_id),
  shipment_id  text NOT NULL REFERENCES shipments(shipment_id),
  thread_id    text,
  hold_status  text NOT NULL CHECK (hold_status IN ('HELD','RELEASED','EXPIRED','CONVERTED','REJECTED')),
  service      tstzrange NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL
);
CREATE UNIQUE INDEX ux_live_hold_per_slot     ON slot_holds(slot_id)     WHERE hold_status = 'HELD';
CREATE UNIQUE INDEX ux_live_hold_per_shipment ON slot_holds(shipment_id) WHERE hold_status = 'HELD';

-- Dedupe that the snapshot lacks (audit A09).
CREATE UNIQUE INDEX ux_chat_external_id ON chat_messages(external_message_id) WHERE external_message_id IS NOT NULL;
CREATE UNIQUE INDEX ux_exception_dedupe ON driver_exceptions(dedupe_key) WHERE dedupe_key IS NOT NULL;

-- Commit pattern used by capacity.commit_hold() on Postgres:
--   BEGIN;
--   SELECT pg_advisory_xact_lock(hashtext(:facility_id));   -- serialise per facility
--   -- re-run feasibility on fresh rows
--   UPDATE appointments SET ... WHERE appointment_id = :old;   -- release old
--   INSERT INTO appointments ...;                             -- unique index guard
--   INSERT INTO appointment_plan ...;                         -- EXCLUDE guard
--   UPDATE slot_holds SET hold_status = 'CONVERTED' ...;
--   COMMIT;
