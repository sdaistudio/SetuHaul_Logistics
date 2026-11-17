"""Interactive ER diagram + data browser for the SetuHaul database -> er_browser.html"""
import json, re, sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
import os, sys
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "setuhaul_freight_operations.db")
SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "source", "setuhaul_schema_and_seed.sql")
c = sqlite3.connect(DB)
sql_text = Path(SQL).read_text()

DESC = {
    "carriers": "Transport companies that provide trucks and drivers.",
    "drivers": "Driver identity, phone and carrier.",
    "vehicle_types": "Reference list of vehicle classes and typical dock needs.",
    "vehicles": "Physical trucks assigned to shipments.",
    "facilities": "Warehouses: hours, check-in grace, default unload.",
    "docks": "Loading bays: type (standard / reefer / heavy), weight limit.",
    "facility_contacts": "Warehouse and gate contacts for escalation.",
    "facility_rules": "No-show grace, reefer, heavy-dock and last-start rules.",
    "shipments": "One freight movement: weight, dock need, priority, ETA, unload time, status.",
    "eta_updates": "Original, driver-declared or overridden ETAs with confidence.",
    "appointment_slots": "Time windows on one dock.",
    "appointments": "Bookings of a shipment into a slot, including history.",
    "dock_status_events": "Breakdowns, maintenance and capacity cuts on a dock.",
    "facility_checkins": "Actual gate-in, yard, dock and unload timestamps.",
    "chat_threads": "One driver conversation / request.",
    "chat_messages": "Individual driver, agent and ops messages.",
    "driver_exceptions": "Structured exception extracted from a conversation.",
    "operational_messages": "Emails / SMS to and from the warehouse.",
}
LANES = [
    ("People & fleet", ["carriers", "drivers", "vehicles", "vehicle_types"]),
    ("Shipment", ["shipments", "eta_updates", "facility_checkins"]),
    ("Facility", ["facilities", "facility_contacts", "facility_rules", "docks", "dock_status_events"]),
    ("Capacity & booking", ["appointment_slots", "appointments"]),
    ("Conversation & messaging", ["chat_threads", "chat_messages", "driver_exceptions", "operational_messages"]),
]
LANE_COLOR = ["#475569", "#1f7a5a", "#1d5fa8", "#b45309", "#6d4fd8"]

tables = {}
for _, names in LANES:
    for t in names:
        cols = [dict(name=r[1], type=r[2], notnull=bool(r[3]), pk=bool(r[5])) for r in c.execute(f'PRAGMA table_info("{t}")')]
        fks = {r[3]: f"{r[2]}.{r[4]}" for r in c.execute(f'PRAGMA foreign_key_list("{t}")')}
        for col in cols:
            col["fk"] = fks.get(col["name"])
        ddl = c.execute("select sql from sqlite_master where name=?", (t,)).fetchone()[0]
        # enumerated values from CHECK (col IN (...))
        enums = {m.group(1): re.findall(r"'([^']*)'", m.group(2))
                 for m in re.finditer(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)\)", ddl)}
        for col in cols:
            col["allowed"] = enums.get(col["name"])
        cur = c.execute(f'select * from "{t}"')
        rows = [list(r) for r in cur.fetchall()]
        idx = [dict(name=r[0], sql=r[1]) for r in c.execute("select name, sql from sqlite_master where type='index' and tbl_name=? and sql is not null", (t,))]
        tables[t] = dict(name=t, desc=DESC.get(t, ""), cols=cols, rows=rows, ddl=ddl, indexes=idx,
                         inserts=len(re.findall(rf'INSERT INTO "{t}"', sql_text)))

views = {}
for name, vsql in c.execute("select name, sql from sqlite_master where type='view'"):
    cur = c.execute(f'select * from "{name}"')
    views[name] = dict(name=name, sql=vsql, cols=[d[0] for d in cur.description], rows=[list(r) for r in cur.fetchall()])

# --------------------------------------------------------------- layout
BW, HDR, ROW, GAPY, LANEW, TOP = 210, 30, 17, 22, 262, 74
pos, y_by_lane = {}, []
for li, (_, names) in enumerate(LANES):
    y = TOP
    for t in names:
        keys = [cl for cl in tables[t]["cols"] if cl["pk"] or cl["fk"]]
        h = HDR + ROW * len(keys) + 22
        pos[t] = dict(x=24 + li * LANEW, y=y, w=BW, h=h, keys=[k["name"] for k in keys], lane=li)
        y += h + GAPY
    y_by_lane.append(y)
W, H = 24 + len(LANES) * LANEW, max(y_by_lane) + 20

def col_y(t, col):
    p = pos[t]
    return p["y"] + HDR + ROW * p["keys"].index(col) + ROW / 2 + 2

edges = []
for t, d in tables.items():
    for col in d["cols"]:
        if col["fk"]:
            pt, pc = col["fk"].split(".")
            if pt == t:
                continue
            edges.append(dict(child=t, ccol=col["name"], parent=pt, pcol=pc))

svg = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="Inter,system-ui,sans-serif" role="img" aria-label="SetuHaul entity relationship diagram">',
       '<defs><marker id="crow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#8a847c"/></marker></defs>']
for li, (lname, _) in enumerate(LANES):
    x = 24 + li * LANEW
    svg.append(f'<text x="{x}" y="40" font-size="13" font-weight="700" fill="{LANE_COLOR[li]}">{lname}</text>')
    svg.append(f'<line x1="{x}" x2="{x + BW}" y1="50" y2="50" stroke="{LANE_COLOR[li]}" stroke-width="2"/>')
for i, e in enumerate(edges):
    a, b = pos[e["child"]], pos[e["parent"]]
    y1, y2 = col_y(e["child"], e["ccol"]), col_y(e["parent"], e["pcol"])
    if a["lane"] == b["lane"]:
        x1 = a["x"] + a["w"]; x2 = b["x"] + b["w"]; bend = 28 + (i % 4) * 7
        d = f"M{x1} {y1} C{x1 + bend} {y1}, {x2 + bend} {y2}, {x2} {y2}"
    elif a["lane"] > b["lane"]:
        x1, x2 = a["x"], b["x"] + b["w"]; mid = (x1 + x2) / 2
        d = f"M{x1} {y1} C{mid} {y1}, {mid} {y2}, {x2} {y2}"
    else:
        x1, x2 = a["x"] + a["w"], b["x"]; mid = (x1 + x2) / 2
        d = f"M{x1} {y1} C{mid} {y1}, {mid} {y2}, {x2} {y2}"
    svg.append(f'<path class="edge" data-a="{e["child"]}" data-b="{e["parent"]}" d="{d}" fill="none" stroke="#b9b3aa" stroke-width="1.2" marker-end="url(#crow)"><title>{e["child"]}.{e["ccol"]} → {e["parent"]}.{e["pcol"]} (many-to-one)</title></path>')
for t, p in pos.items():
    col = LANE_COLOR[p["lane"]]
    n = len(tables[t]["rows"])
    g = [f'<g class="tbl" data-t="{t}" tabindex="0" role="link" aria-label="Open {t} data">',
         f'<rect x="{p["x"]}" y="{p["y"]}" width="{p["w"]}" height="{p["h"]}" rx="8" fill="#fff" stroke="{col}" stroke-width="1.4"/>',
         f'<path d="M{p["x"]} {p["y"] + 8}a8 8 0 0 1 8-8h{p["w"] - 16}a8 8 0 0 1 8 8v{HDR - 8}h-{p["w"]}z" fill="{col}"/>',
         f'<text x="{p["x"] + 10}" y="{p["y"] + 20}" font-size="13" font-weight="700" fill="#fff">{t}</text>',
         f'<text x="{p["x"] + p["w"] - 10}" y="{p["y"] + 20}" font-size="11" fill="#fff" text-anchor="end">{n} rows</text>']
    for k, cname in enumerate(p["keys"]):
        cl = next(x for x in tables[t]["cols"] if x["name"] == cname)
        tag = "PK" if cl["pk"] else "FK"
        yy = p["y"] + HDR + ROW * k + 13
        g.append(f'<text x="{p["x"] + 10}" y="{yy}" font-size="10" font-weight="700" fill="{"#b45309" if tag == "PK" else "#1d5fa8"}">{tag}</text>')
        g.append(f'<text x="{p["x"] + 32}" y="{yy}" font-size="11.5" fill="#1c1917">{cname}</text>')
    others = len(tables[t]["cols"]) - len(p["keys"])
    g.append(f'<text x="{p["x"] + 10}" y="{p["y"] + p["h"] - 8}" font-size="11" fill="#1d5fa8" text-decoration="underline">+{others} more columns · view data →</text>')
    g.append("</g>")
    svg += g
svg.append("</svg>")

sql_summary = {
    "bytes": len(sql_text), "lines": sql_text.count("\n"),
    "create_table": len(re.findall(r"CREATE TABLE", sql_text)),
    "unique_indexes": len(re.findall(r"CREATE UNIQUE INDEX", sql_text)),
    "indexes": len(re.findall(r"CREATE INDEX", sql_text)),
    "views": len(re.findall(r"CREATE VIEW", sql_text)),
    "inserts": len(re.findall(r"INSERT INTO", sql_text)),
    "checks": len(re.findall(r"CHECK", sql_text)),
    "fks": len(re.findall(r"FOREIGN KEY", sql_text)),
    "pragma_fk": "PRAGMA foreign_keys" in sql_text,
    "header": "\n".join(sql_text.splitlines()[:5]),
    "all_indexes": re.findall(r"CREATE (?:UNIQUE )?INDEX[^;]*;", sql_text),
}

data = dict(tables=tables, views=views, lanes=LANES, laneColor=LANE_COLOR, edges=edges, sql=sql_summary)
html = (HERE / "er_template.html").read_text().replace("<!--SVG-->", "\n".join(svg)) \
    .replace("/*DATA*/null", json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/"))
(HERE / "er_browser.html").write_text(html)
print("ok", len(html), "edges", len(edges), "size", W, H)
