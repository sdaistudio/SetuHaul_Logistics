"""Integrity proof: competing requests cannot double-book capacity.

Run all:   python3 -I -m unittest discover -s tests -v
Report:    python3 -I run_integrity.py out/      (50 race repeats + stress, writes integrity_results.json)

Every test runs on its own fresh copy of the snapshot. Threads use separate
database connections, as separate web workers would.
"""
import os, random, shutil, sqlite3, sys, tempfile, threading, time, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul import db, capacity as K, timeline as T, workflow as W, world as Wd
from setuhaul.clock import ts, iso, add

AS_OF = ts("2026-08-04T09:45:00+05:30")
RACE_SLOT = "SLOT-JAI-052"                 # D4 17:00-18:00 (25 t standard dock)
RACERS = ["SHP1006", "SHP1007", "SHP1009", "SHP1012", "SHP1013", "SHP1014", "SHP1002", "SHP1003", "SHP1004", "SHP1005"]   # all able to use D4; none needs approval


def fresh(tag):
    d = tempfile.mkdtemp(prefix=f"sh-{tag}-")
    return db.fresh_copy(os.path.join(d, "t.db")), d


def race(path, slot=RACE_SLOT, racers=RACERS, as_of=AS_OF):
    """All racers commit the same slot at the same instant. Returns per-racer results."""
    barrier, out, errs = threading.Barrier(len(racers)), {}, {}

    def go(sid):
        c = db.connect(path)
        barrier.wait()
        try:
            out[sid] = K.commit(c, sid, slot, as_of, idem_key=f"race:{sid}")
        except Exception as ex:                       # anything here would be a leaked error
            errs[sid] = repr(ex)
        finally:
            c.close()
    th = [threading.Thread(target=go, args=(s,)) for s in racers]
    [t.start() for t in th]
    [t.join() for t in th]
    return out, errs


def active_on(con, slot):
    return con.execute("SELECT COUNT(*) FROM appointments WHERE slot_id=? AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')", (slot,)).fetchone()[0]


def invariants(con):
    q = lambda s: con.execute(s).fetchone()[0]
    return {
        "slots_with_two_active": q("""SELECT COUNT(*) FROM (SELECT slot_id FROM appointments WHERE appointment_status IN
                                      ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY slot_id HAVING COUNT(*)>1)"""),
        "shipments_with_two_current": q("""SELECT COUNT(*) FROM (SELECT shipment_id FROM appointments WHERE is_current=1 AND appointment_status IN
                                      ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') GROUP BY shipment_id HAVING COUNT(*)>1)"""),
        "overlapping_service_intervals": q("""SELECT COUNT(*) FROM dock_reservations a JOIN dock_reservations b ON a.dock_id=b.dock_id
                                      AND a.reservation_id<b.reservation_id AND a.live=1 AND b.live=1 AND a.kind='BOOKING' AND b.kind IN ('BOOKING','HOLD')
                                      AND julianday(a.start_ts)<julianday(b.end_ts) AND julianday(b.start_ts)<julianday(a.end_ts)"""),
        "incompatible_service_bookings": q("""SELECT COUNT(*) FROM appointments a JOIN shipments s USING(shipment_id) JOIN appointment_slots sl ON sl.slot_id=a.slot_id
                                      JOIN docks d ON d.dock_id=sl.dock_id WHERE a.appointment_id LIKE 'APT-N%' AND a.appointment_status IN ('CONFIRMED','IN_PROGRESS')
                                      AND (s.load_weight_kg>d.max_vehicle_weight_kg OR (s.required_dock_type IN ('REEFER','HEAVY') AND s.required_dock_type<>d.dock_type))"""),
    }


def naive_book(path, sid, slot, as_of, gap_s=0.05):
    """NEGATIVE CONTROL: the classic bug. Check, pause, insert, with no transaction around them."""
    c = sqlite3.connect(path, timeout=30, isolation_level=None)
    free = c.execute("SELECT COUNT(*) FROM appointments WHERE slot_id=? AND appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION','IN_PROGRESS')", (slot,)).fetchone()[0] == 0
    time.sleep(gap_s)                                  # the window in which another driver also sees 'free'
    try:
        if free:
            c.execute("UPDATE appointments SET is_current=0 WHERE shipment_id=?", (sid,))
            c.execute("INSERT INTO appointments VALUES (?,?,?, 'CONFIRMED','DRIVER_CHAT',1,?,?,NULL,NULL,NULL,NULL,?)",
                      (f"NAIVE-{sid}", sid, slot, iso(as_of), iso(as_of), iso(as_of)))
            return "BOOKED"
        return "SAW_TAKEN"
    except sqlite3.IntegrityError as ex:
        return "RAW_ERROR: " + str(ex)
    finally:
        c.close()


def naive_race(path, keep_index):
    c = sqlite3.connect(path, isolation_level=None)
    if not keep_index:
        c.execute("DROP INDEX ux_active_appointment_per_slot")
        c.execute("DROP INDEX ux_current_active_appointment_per_shipment")
    c.close()
    barrier, out = threading.Barrier(len(RACERS)), {}

    def go(sid):
        barrier.wait()
        out[sid] = naive_book(path, sid, RACE_SLOT, AS_OF)
    th = [threading.Thread(target=go, args=(s,)) for s in RACERS]
    [t.start() for t in th]
    [t.join() for t in th]
    return out


class Integrity(unittest.TestCase):

    def test_01_ten_drivers_one_slot(self):
        path, d = fresh("race")
        out, errs = race(path)
        con = db.connect(path)
        winners = [s for s, r in out.items() if r["ok"]]
        self.assertEqual(errs, {}, "no raw errors may leak to drivers")
        self.assertEqual(len(winners), 1)
        self.assertTrue(all(r["code"] == "OPTION_GONE" for s, r in out.items() if s not in winners))
        self.assertEqual(active_on(con, RACE_SLOT), 1)
        self.assertEqual(sum(invariants(con).values()), 0)
        shutil.rmtree(d)

    def test_02_negative_control_without_guards_double_books(self):
        path, d = fresh("naive")
        out = naive_race(path, keep_index=False)
        booked = sum(1 for v in out.values() if v == "BOOKED")
        self.assertGreater(booked, 1, "the naive code MUST double-book, or the race test proves nothing")
        shutil.rmtree(d)

    def test_03_index_alone_blocks_but_leaks_raw_errors(self):
        path, d = fresh("idx")
        out = naive_race(path, keep_index=True)
        self.assertEqual(sum(1 for v in out.values() if v == "BOOKED"), 1)
        self.assertGreater(sum(1 for v in out.values() if v.startswith("RAW_ERROR")), 0)
        shutil.rmtree(d)

    def test_04_random_stress_keeps_invariants(self):
        path, d = fresh("stress")
        con = db.connect(path)
        slots = [r["slot_id"] for r in db.rows(con, """SELECT slot_id FROM appointment_slots WHERE facility_id='FAC-JAI-01'
                                                         AND slot_status='OPEN' AND slot_start_ts>='2026-08-04T11:00:00+05:30'""")]
        ships = [r["shipment_id"] for r in db.rows(con, """SELECT shipment_id FROM shipments WHERE destination_facility_id='FAC-JAI-01'
                                                           AND current_status NOT IN ('CANCELLED','COMPLETED')""")]
        stats = {"CONFIRMED": 0, "OPTION_GONE": 0, "NEEDS_APPROVAL": 0, "errors": 0}
        lock = threading.Lock()

        def worker(seed):
            rnd = random.Random(seed)
            c = db.connect(path)
            for _ in range(25):
                try:
                    r = K.commit(c, rnd.choice(ships), rnd.choice(slots), AS_OF)
                    with lock:
                        stats[r["code"]] = stats.get(r["code"], 0) + 1
                except Exception:
                    with lock:
                        stats["errors"] += 1
            c.close()
        th = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in th]
        [t.join() for t in th]
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(sum(invariants(con).values()), 0, invariants(con))
        self.__class__.stress_stats = dict(stats, invariants=invariants(con))
        shutil.rmtree(d)

    def test_05_retry_is_idempotent(self):
        path, d = fresh("idem")
        con = db.connect(path)
        r1 = K.commit(con, "SHP1012", "SLOT-JAI-022", AS_OF, idem_key="wa-9002-r1")
        r2 = K.commit(con, "SHP1012", "SLOT-JAI-022", AS_OF, idem_key="wa-9002-r1")
        self.assertTrue(r1["ok"])
        self.assertTrue(r2.get("replayed"))
        self.assertEqual(r1["appointment_id"], r2["appointment_id"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM appointments WHERE shipment_id='SHP1012' AND is_current=1").fetchone()[0], 1)
        shutil.rmtree(d)

    def test_06_duplicate_message_is_suppressed(self):
        path, d = fresh("dup")
        con = db.connect(path)
        m = "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"
        a = W.handle(con, "+91-9000010006", m, ts("2026-08-04T09:34:00+05:30"), "wa-9001")
        b = W.handle(con, "+91-9000010006", m, ts("2026-08-04T09:35:02+05:30"), "wa-9009")
        self.assertIsNotNone(a["reply"])
        self.assertIsNone(b["reply"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM eta_updates WHERE eta_update_id LIKE 'ETA-N%'").fetchone()[0], 1)
        shutil.rmtree(d)

    def test_07_stale_option_is_rechecked_at_commit(self):
        path, d = fresh("stale")
        con = db.connect(path)
        opts, _ = T.options(con, "SHP1012", AS_OF)
        o = opts[0]
        Wd.dock_event(con, "DEVT-TEST", o["dock_id"], "BREAKDOWN", add(AS_OF, 1), add(AS_OF, 600), "test breakdown", add(AS_OF, 1))
        r = K.commit(con, "SHP1012", o["slot_id"], add(AS_OF, 2))
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "OPTION_GONE")
        shutil.rmtree(d)

    def test_08_hold_expires_and_slot_returns(self):
        path, d = fresh("hold")
        con = db.connect(path)
        opts, _ = T.options(con, "SHP1020", AS_OF, limit=None)
        o = [x for x in opts if x["needs_approval"]][0]
        h = K.hold_for_approval(con, "SHP1020", o["slot_id"], AS_OF, "DRV004", o["needs_approval"])
        self.assertEqual(h["code"], "HELD_FOR_APPROVAL")
        others, _ = T.options(con, "SHP1013", AS_OF, limit=None)
        self.assertNotIn(o["slot_id"], [x["slot_id"] for x in others], "a held slot is not offered to anyone else")
        gone = K.sweep(con, add(ts(h["deadline"]), 1))
        self.assertEqual([g["hold_id"] for g in gone], [h["hold_id"]])
        later, _ = T.options(con, "SHP1013", add(ts(h["deadline"]), 1), limit=None)
        self.assertIn(o["slot_id"], [x["slot_id"] for x in later], "after expiry the slot is free again")
        shutil.rmtree(d)

    def test_09_coordinator_approval_is_rechecked(self):
        path, d = fresh("appr")
        con = db.connect(path)
        opts, _ = T.options(con, "SHP1020", AS_OF, limit=None)
        o = [x for x in opts if x["needs_approval"]][0]
        h = K.hold_for_approval(con, "SHP1020", o["slot_id"], AS_OF, "DRV004", o["needs_approval"])
        Wd.dock_event(con, "DEVT-T2", o["dock_id"], "MAINTENANCE", add(AS_OF, 1), add(AS_OF, 900), "unplanned", add(AS_OF, 1))
        r = K.decide(con, h["hold_id"], True, "CON001", add(AS_OF, 3))
        self.assertFalse(r["ok"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM appointments WHERE appointment_id LIKE 'APT-N%'").fetchone()[0], 0)
        shutil.rmtree(d)

    def test_10_database_guards_reject_the_audit_gaps(self):
        path, d = fresh("guards")
        con = db.connect(path)
        bad = {
            "blocked slot": ("SHP1012", "SLOT-JAI-031", 1, "SLOT_NOT_OPEN"),
            "wrong facility": ("SHP1012", "SLOT-GGN-084", 1, "WRONG_FACILITY"),
            "heavy load on standard dock": ("SHP1016", "SLOT-JAI-006", 1, "DOCK_WEIGHT_EXCEEDED"),
            "cancelled shipment": ("SHP1019", "SLOT-JAI-007", 1, "SHIPMENT_NOT_BOOKABLE"),
            "active booking with is_current=0": ("SHP1009", "SLOT-JAI-048", 0, "ACTIVE_APPOINTMENT_MUST_BE_CURRENT"),
        }
        self.__class__.guard_results = {}
        for label, (sid, slot, cur, expect) in bad.items():
            with self.assertRaises(sqlite3.IntegrityError) as cm:
                con.execute("INSERT INTO appointments VALUES ('X-'||?,?,?, 'CONFIRMED','SCHEDULING_TOOL',?,?,?,NULL,NULL,NULL,NULL,?)",
                            (label, sid, slot, cur, iso(AS_OF), iso(AS_OF), iso(AS_OF)))
            self.assertIn(expect, str(cm.exception))
            self.__class__.guard_results[label] = str(cm.exception)
        shutil.rmtree(d)

    def test_11_overlap_trigger_catches_a_feasibility_bug(self):
        """Simulate a bug: the feasibility code forgets busy intervals. The DB trigger must still refuse."""
        path, d = fresh("bug")
        con = db.connect(path)
        real = T.earliest_fit
        try:
            T.earliest_fit = lambda intervals, s0, dur, latest_start=None: real([], s0, dur, latest_start)
            with self.assertRaises(K.GuardTripped) as cm:
                K.commit(con, "SHP1012", "SLOT-JAI-047", AS_OF)       # D4 12:00 window; SHP1009's 75-min unload still runs then
            self.assertIn("DOCK_INTERVAL_OVERLAP", str(cm.exception))
        finally:
            T.earliest_fit = real
        self.assertEqual(con.execute("SELECT COUNT(*) FROM appointments WHERE appointment_id LIKE 'APT-N%'").fetchone()[0], 0)
        shutil.rmtree(d)

    def test_12_swap_is_all_or_nothing(self):
        path, d = fresh("swap")
        con = db.connect(path)
        before = db.rows(con, "SELECT appointment_id, appointment_status, is_current FROM appointments ORDER BY appointment_id")
        with self.assertRaises(RuntimeError):
            K.swap(con, "SHP1016", "SHP1007", AS_OF, "test")        # heavy truck cannot take a standard slot
        after = db.rows(con, "SELECT appointment_id, appointment_status, is_current FROM appointments ORDER BY appointment_id")
        self.assertEqual(before, after, "a failed swap changes nothing")
        shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
