"""Builds the static website in site/ (deployed by GitHub Pages).

    pip install markdown
    python tools/build_site.py

Pages: hub, solution design, discussion prototype, data analysis, ER + data
browser, and every hand-in document rendered from Markdown.
"""
from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
REPO = "https://github.com/sdaistudio/SetuHaul_Logistics"

DOCS = [  # (output, source markdown, title, one line)
    ("design.html", "docs/00_solution_design.md", "Solution design", "Options, architecture on AWS, deterministic core, solver, guardrails, evals, results and open decisions."),
    ("system-design.html", "docs/07_system_design_L1_L2.md", "L1 / L2 system design", "Who decides what (model, tool, database, person); model.py / tool.py / database.py contracts; data fetched per situation; facility scheduling tool."),
    ("walkthrough.html", "docs/08_situation_walkthrough.md", "Situation walkthrough", "Every ladder level run through the workflow: model step, tool calls, tables read and written, reply."),
    ("problem.html", "docs/01_problem_definition.md", "Problem definition sheet", "Facts, estimates, assumptions and unknowns; the 20 data issues."),
    ("decisions.html", "docs/02_decision_register.md", "Decision register", "D1–D13: every decision the brief asks for, with reason and evidence."),
    ("solver.html", "docs/03_solver_design.md", "Solver design", "Branch and bound, look-ahead, CP-SAT proof, location on demand."),
    ("readout.html", "docs/06_client_readout.md", "Client readout", "One page for the head of operations and the planner."),
    ("audit.html", "audit/findings.md", "Data audit findings", "Twenty data issues, each with the SQL that proves it."),
    ("scenarios.html", "scenarios/transcript.md", "Scenario run transcript", "Every driver conversation in the Jaipur replay, with system state."),
    ("evals.html", "evals/report.md", "Evaluation report", "NLU, feasibility, guardrail, integrity and solver gates."),
]
APPS = [  # (output, source html, title, one line)
    ("prototype.html", "prototype/index.html", "Discussion prototype", "Conversation replay, dock board, solver look-ahead, scheduling comparison, evals, observability."),
    ("analysis.html", "analysis/report.html", "Data analysis", "Statistical profile: delays, ETA trust, capacity, unload cascades (Monte Carlo), data quality."),
    ("er.html", "analysis/er_browser.html", "ER diagram & data browser", "Clickable entity model; every table's rows, columns, keys and CREATE statement."),
]

CSS = """
:root{--bg:#f7f6f3;--card:#fff;--ink:#1c1917;--mu:#6b6560;--ln:#e5e2dc;--b:#1d5fa8;--g:#1f7a5a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 Inter,system-ui,sans-serif}
.top{background:#1c1917;color:#fff;padding:10px 22px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;font-size:13.5px}
.top a{color:#e7e5e4;text-decoration:none}.top a:hover{color:#fff;text-decoration:underline}.top b{color:#fff;margin-right:8px}
.wrap{max-width:1240px;margin:auto;padding:28px 22px 70px}
.doc{background:var(--card);border:1px solid var(--ln);border-radius:12px;padding:28px 34px}
.doc h1{font-size:26px;margin-top:0}.doc h2{font-size:19px;margin-top:30px;border-top:1px solid var(--ln);padding-top:18px}.doc h3{font-size:16px}
.doc table{border-collapse:collapse;width:100%;font-size:13.5px;margin:12px 0;display:block;overflow-x:auto}
.doc th,.doc td{border:1px solid var(--ln);padding:6px 9px;text-align:left;vertical-align:top}.doc th{background:#faf9f7}
.doc code{font-family:'JetBrains Mono',monospace;font-size:12.5px;background:#f1efeb;padding:1px 4px;border-radius:4px}
.doc pre{background:#1c1917;color:#e7e5e4;padding:12px;border-radius:8px;overflow:auto}.doc pre code{background:none;color:inherit}
.doc img{max-width:100%;border:1px solid var(--ln);border-radius:8px}.doc blockquote{border-left:3px solid var(--ln);margin:8px 0;padding:2px 14px;color:#44403c;background:#faf9f7}
.doc a{color:var(--b)}
.hero h1{font-size:30px;margin:0 0 6px}.hero p{color:var(--mu);max-width:820px;margin:0 0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;margin-top:14px}
.card{display:block;background:var(--card);border:1px solid var(--ln);border-radius:12px;padding:18px;text-decoration:none;color:var(--ink);transition:border-color .15s}
.card:hover{border-color:var(--g)}.card b{display:block;font-size:16px;margin-bottom:4px}.card span{color:var(--mu);font-size:13.5px}
.card.big{border-left:5px solid var(--g)}h2.sec{font-size:17px;margin:34px 0 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-top:16px}
.kpi{background:var(--card);border:1px solid var(--ln);border-radius:10px;padding:12px}.kpi .v{font-size:22px;font-weight:700}.kpi .l{font-size:12.5px;color:var(--mu)}
.arch{margin-top:14px;background:#fff;border:1px solid var(--ln);border-radius:12px;padding:10px}.arch img{width:100%;height:auto}
footer{color:var(--mu);font-size:12.5px;margin-top:30px}
"""
FONTS = '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono&display=swap" rel="stylesheet">'


def topbar() -> str:
    links = [("index.html", "Home"), ("design.html", "Design"), ("system-design.html", "L1/L2"), ("prototype.html", "Prototype"), ("demo2.html", "Demo 2"),
             ("analysis.html", "Analysis"), ("er.html", "ER & data"), ("decisions.html", "Decisions"),
             ("solver.html", "Solver"), ("readout.html", "Readout"), (REPO, "GitHub")]
    return '<nav class="top"><b>SetuHaul</b>' + "".join(f'<a href="{h}">{t}</a>' for h, t in links) + "</nav>"


def page(title: str, body: str) -> str:
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)} — SetuHaul</title>{FONTS}<style>{CSS}</style></head><body>{topbar()}"
            f'<div class="wrap">{body}<footer>SetuHaul Logistics · FDE Academy challenge · source: <a href="{REPO}">{REPO}</a></footer></div></body></html>')


def fix_links(h: str) -> str:
    # repo-relative references in the Markdown -> site pages or GitHub source
    mapping = {src: out for out, src, _, _ in DOCS}
    def repl(m):
        target = m.group(1)
        if target in mapping:
            return f'<a href="{mapping[target]}"><code>{target}</code></a>'
        if re.match(r"^[\w./-]+\.(py|sql|yaml|yml|json|md|csv)$", target) or target.endswith("/"):
            return f'<a href="{REPO}/blob/main/{target}"><code>{target}</code></a>' if not target.endswith("/") else \
                f'<a href="{REPO}/tree/main/{target}"><code>{target}</code></a>'
        return m.group(0)
    return re.sub(r"<code>([^<]+)</code>", repl, h)


def inject_bar(raw: str) -> str:
    bar = ('<div style="position:sticky;top:0;z-index:50;background:#1c1917;padding:8px 18px;font:13.5px Inter,system-ui,sans-serif">'
           '<a href="index.html" style="color:#fff;text-decoration:none">← SetuHaul home</a></div>')
    return re.sub(r"(<body[^>]*>)", r"\1" + bar.replace("\\", "\\\\"), raw, count=1)


def main():
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()
    shutil.copy(ROOT / "docs" / "architecture.png", SITE / "architecture.png")
    shutil.copy(ROOT / "docs" / "architecture.svg", SITE / "architecture.svg")
    shutil.copytree(ROOT / "docs" / "design", SITE / "design")
    (SITE / ".nojekyll").write_text("")
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists"])
    for out, src, title, _ in DOCS:
        text = (ROOT / src).read_text(encoding="utf-8")
        body = fix_links(md.reset().convert(text))
        (SITE / out).write_text(page(title, f'<div class="doc">{body}</div>'), encoding="utf-8")
    for out, src, _, _ in APPS:
        (SITE / out).write_text(inject_bar((ROOT / src).read_text(encoding="utf-8")), encoding="utf-8")

    import build_demo2
    build_demo2.main()

    import json
    m = json.loads((ROOT / "scenarios" / "scenario_metrics.json").read_text())
    ev = json.loads((ROOT / "evals" / "report.json").read_text())
    kpis = [("0", "double bookings (10-driver race + replay)"),
            (str(m["time from first message to usable outcome (median min)"]) + " min", "median time to a usable answer"),
            (f'{ev["nlu"]["case_accuracy"]:.0%} / {ev["nlu_holdout"]["case_accuracy"]:.0%}', "NLU tuned / held-out (EN·HI·Hinglish)"),
            ("8 / 8", "solver optimum = OR-Tools CP-SAT"),
            (m["share resolved without manual takeover"], "conversations resolved without takeover"),
            ("20", "data issues proved by SQL")]
    card = lambda out, title, line, big=False: f'<a class="card{" big" if big else ""}" href="{out}"><b>{html.escape(title)}</b><span>{html.escape(line)}</span></a>'
    body = f"""<div class="hero"><h1>SetuHaul Logistics — driver exception service</h1>
<p>When many drivers report delays at once, SetuHaul answers each one in English, Hindi or Hinglish with a dock slot the truck can really make, and never promises the same capacity twice. Language at the edges; a deterministic solver decides every slot; a person decides when no safe answer exists.</p>
<p>Built on the 4 August 2026 Jaipur snapshot for the FDE Academy challenge. Everything below is generated from the code in <a href="{REPO}">the repository</a>.</p></div>
<div class="kpis">{"".join(f'<div class="kpi"><div class="v">{html.escape(v)}</div><div class="l">{html.escape(l)}</div></div>' for v, l in kpis)}</div>
<h2 class="sec">Start here</h2><div class="grid">
{card("design.html", "Solution design", DOCS[0][3], True)}
{card("system-design.html", "L1 / L2 system design", DOCS[1][3], True)}
{"".join(card(o, t, l, True) for o, _, t, l in APPS[:1])}
{card("demo2.html", "Demo 2 · Coordinator control tower (MVP 2)", "Truck GPS on a map, planned vs driver vs GPS ETA, green/amber/red per driver, one-click WhatsApp, and a dashboard of every driver conversation.", True)}
{"".join(card(o, t, l, True) for o, _, t, l in APPS[1:])}
</div>
<h2 class="sec">Architecture</h2><div class="arch"><a href="architecture.svg"><img src="architecture.png" alt="SetuHaul architecture on AWS"></a></div>
<h2 class="sec">Hand-in documents</h2><div class="grid">{"".join(card(o, t, l) for o, _, t, l in DOCS[2:])}
{card(REPO, "Source code on GitHub", "Workflow, NLU, solver, rules.yaml, tests, evals, AWS SAM template, analysis scripts.")}</div>"""
    (SITE / "index.html").write_text(page("Home", body), encoding="utf-8")
    print("site built:", sorted(p.name for p in SITE.iterdir()))


if __name__ == "__main__":
    main()
