"""Run every audit query against the untouched snapshot and write audit/findings.md."""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "setuhaul_freight_operations.db"


def blocks(sql: str):
    for m in re.finditer(r"-- @(A\d+) \| (.+?) \| (.+?)\n(.*?;)", sql, re.S):
        yield m.group(1), m.group(2).strip(), m.group(3).strip(), m.group(4).strip()


def main():
    sql = (ROOT / "audit" / "data_audit.sql").read_text()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out = ["# Data audit — evidence for the Problem Definition Sheet", "",
           "Each finding is proved by the query under it, run read-only on the supplied snapshot.", ""]
    for fid, title, impact, q in blocks(sql):
        cur = conn.execute(q)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        out += [f"## {fid}. {title}", "", impact, "", "```sql", q, "```", "",
                f"Rows returned: **{len(rows)}**", ""]
        if rows:
            out += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            out += ["| " + " | ".join("" if v is None else str(v).replace("|", "/") for v in r) + " |" for r in rows[:12]]
            out.append("")
    (ROOT / "audit" / "findings.md").write_text("\n".join(out))
    print("\n".join(out[:3]))
    print(f"{len(list(blocks(sql)))} findings written to audit/findings.md")


if __name__ == "__main__":
    sys.exit(main())
