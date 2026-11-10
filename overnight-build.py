"""Overnight estimator build (2026-11-09, Matt: build the estimator next steps and the strategy-session items with
Qwen, as few Claude tokens as possible). Runs on Matt's PC against the home Qwen server; no Claude.
  Step 1 (Python, no LLM): company labor units and pricing facts from the budget store (vzw-budgets.sqlite) and
  the intel export: unit minutes per install category, hours per antenna/remote/BDA and per 1,000 ft of cable by
  job type, cost split, and win rates by price band. These are the RF labor units no public source has.
  Step 2 (Qwen): one draft per deliverable, from the source library, the strategy notes and the step-1 tables.
  Every prompt says: use only numbers given, never invent prices or rates, mark gaps as GAP.
Outputs: drafts\*.md, data\*.csv and build-log.txt in this folder. All drafts need review before use.
Usage: uv run --no-project python -I overnight-build.py
"""
import csv
import json
import os
import sqlite3
import statistics as st
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
AUTO = r"C:\Users\MattBusdiecker\Documents\VZW Intel Automation"
LIB = r"C:\Users\MattBusdiecker\Downloads\Estimator_Source_Library.md"
STRAT = r"C:\Users\MattBusdiecker\.claude\projects\C--windows-system32\memory\project-2027-strategy-sessions.md"
os.makedirs(os.path.join(HERE, "drafts"), exist_ok=True); os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
LOG = open(os.path.join(HERE, "build-log.txt"), "a", encoding="utf-8")
log = lambda m: (LOG.write(time.strftime("%Y-%m-%d %H:%M ") + m + "\n"), LOG.flush(), print(m))

# ---------- Step 1: company data ----------
db = sqlite3.connect(os.path.join(AUTO, "vzw-budgets.sqlite"))
rows = {r["Job Number"]: r for r in json.load(open(os.path.join(AUTO, "vzw-jobs.json"), encoding="utf-8-sig"))}
won = lambda j: (rows.get(j, {}).get("Outcome Group") or "").startswith("Won") and rows[j].get("PO Mismatch") != "Yes"
INSTALL = {"DAS Build / Expansion", "Passive DAS (antennas + cable)", "DRAN (passive DAS + Samsung radios)", "Signal Source / Plugin / Upgrade",
           "Small Cell / DOT / SpiderCloud", "Carrier Add / Integration (no new active gear)", "Install only (gear by others)", "Booster / Repeater", "Outdoor DAS"}


def q(vals):
    vals = sorted(v for v in vals if v is not None and v > 0)
    if not vals:
        return None
    pick = lambda p: vals[min(len(vals) - 1, int(p * (len(vals) - 1) + 0.5))]
    return {"n": len(vals), "p25": round(pick(0.25), 2), "median": round(st.median(vals), 2), "p75": round(pick(0.75), 2)}


def write_csv(name, header, data):
    with open(os.path.join(HERE, "data", name), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(header); w.writerows(data)


# 1a. Unit minutes per install category (the template's own labor units, as entered on won jobs).
cat = {}
for job, category, entered, unit_min, hours in db.execute("select job, category, entered, unit_minutes, hours from labor_hours"):
    if won(job) and rows[job].get("Job Type") in INSTALL and entered and entered > 0:
        c = cat.setdefault(category, {"unit": [], "qty": [], "hours": [], "jobs": set()})
        c["unit"].append(unit_min); c["qty"].append(entered); c["hours"].append(hours); c["jobs"].add(job)
unit_rows = []
for k, c in sorted(cat.items(), key=lambda kv: -len(kv[1]["jobs"])):
    u = q(c["unit"])
    if u and len(c["jobs"]) >= 3:
        unit_rows.append([k, len(c["jobs"]), u["p25"], u["median"], u["p75"], round(sum(x or 0 for x in c["hours"]), 1)])
write_csv("labor-units-by-category.csv", ["Install category", "Won jobs", "Unit minutes p25", "Unit minutes median", "Unit minutes p75", "Total hours"], unit_rows)
log(f"labor units: {len(unit_rows)} install categories from won jobs")

# 1b. Productivity by job type: hours per antenna, per remote, per BDA, per 1,000 ft cable; cost split.
jt_rows = []
by_type = {}
for job, ant, rem, bda, cable, hrs, lab, mat, subs in db.execute(
        "select job, antennas, das_remotes, bda_repeaters, cable_ft, labor_hours, labor_cost, material_cost, subs_direct from jobs"):
    if not won(job) or rows[job].get("Job Type") not in INSTALL or not hrs:
        continue
    t = by_type.setdefault(rows[job]["Job Type"], {"h_ant": [], "h_rem": [], "h_bda": [], "h_kft": [], "lab": [], "mat": [], "subs": []})
    tot = (lab or 0) + (mat or 0) + (subs or 0)
    if ant: t["h_ant"].append(hrs / ant)
    if rem: t["h_rem"].append(hrs / rem)
    if bda: t["h_bda"].append(hrs / bda)
    if cable: t["h_kft"].append(hrs / cable * 1000)
    if tot: t["lab"].append((lab or 0) / tot); t["mat"].append((mat or 0) / tot); t["subs"].append((subs or 0) / tot)
for jt, t in sorted(by_type.items()):
    f = lambda k: (q(t[k]) or {}).get("median")
    jt_rows.append([jt, len(t["lab"]), f("h_ant"), f("h_rem"), f("h_bda"), f("h_kft"), f("lab"), f("mat"), f("subs")])
write_csv("productivity-by-job-type.csv", ["Job type", "Won jobs", "Hours per antenna", "Hours per remote", "Hours per BDA", "Hours per 1,000 ft cable",
                                           "Labor share", "Material share", "Subs share"], jt_rows)
log(f"productivity: {len(jt_rows)} job types")

# 1c. Win rates by price band (outliers.py) and the type fits.
bands = json.load(open(os.path.join(AUTO, "win-bands.json"), encoding="utf-8")) if os.path.exists(os.path.join(AUTO, "win-bands.json")) else []
fits = json.load(open(os.path.join(AUTO, "type-fits.json"), encoding="utf-8")) if os.path.exists(os.path.join(AUTO, "type-fits.json")) else {}
write_csv("win-rate-by-price-band.csv", ["Price / fit", "Bids", "Win rate"], [[b["label"], b["n"], b["win_rate"]] for b in bands])

facts = "\n".join(["COMPANY DATA (from ANS won-job budgets; the only numbers you may use):",
                   "Labor units by install category (unit minutes p25 / median / p75, won jobs):"] +
                  [f"- {r[0]}: {r[2]} / {r[3]} / {r[4]} min ({r[1]} jobs)" for r in unit_rows[:45]] +
                  ["Productivity by job type (median hours per antenna, per remote, per BDA, per 1,000 ft cable; labor/material/subs share):"] +
                  [f"- {r[0]} ({r[1]} wins): {r[2]} h/ant, {r[3]} h/remote, {r[4]} h/BDA, {r[5]} h/kft; shares {r[6]}/{r[7]}/{r[8]}" for r in jt_rows] +
                  ["Win rate by price position against the won-job fit (bids):"] + [f"- {b['label']}: {b['n']} bids, won {b['win_rate']:.0%}" for b in bands] +
                  ["Per-type fit drivers: " + "; ".join(f"{k}: {v.get('driver')} on {v.get('pool')}" for k, v in fits.items())])
open(os.path.join(HERE, "data", "company-facts.txt"), "w", encoding="utf-8").write(facts)

# ---------- Step 2: Qwen drafts ----------
URL = "http://192.168.1.2:8081/v1/chat/completions"
KEY = os.environ.get("LLM_API_KEY")
if not KEY:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        KEY = winreg.QueryValueEx(k, "LLM_API_KEY")[0]
library = open(LIB, encoding="utf-8").read()
strategy = open(STRAT, encoding="utf-8").read()
RULES = """You draft internal working documents for ANS (becoming Charge Digital about Jan 1 2027), an in-building wireless
integrator: cellular DAS, neutral host, public safety ERCES/BDA, small cells, structured cabling.
Hard rules:
- Never invent prices, dollar amounts, labor rates, hours or percentages. Use only numbers in COMPANY DATA or the source
  library text given. Where a number is needed and not given, write GAP: <what is needed and who supplies it>.
- ERCES: the company follows the Safer Buildings Coalition for practice; IFC 510, NFPA 1225 Ch. 18 and the local AHJ govern
  compliance and win where stricter. Use "per AHJ requirements" wording in customer-facing text.
- Plain professional English. No em dashes or en dashes; use periods, commas, colons or parentheses. No hype words
  (robust, seamless, leverage, crucial, comprehensive, streamline). No closing summaries or offers.
- Output GitHub markdown only: a title line, then the content. Use tables where the content is tabular.
- This is a draft for review by Matt Busdiecker; mark anything uncertain as VERIFY."""

TASKS = [
    ("01-scope-checklist-das.md", "Write a bid scope checklist for cellular DAS and neutral-host jobs, built from ANSI/BICSI 006, the Division 27 53 19 "
     "owner specs and the carrier-integration notes in the library. Group by phase: pre-bid, design, materials, installation, carrier integration, "
     "commissioning and ATP, closeout. Each line: item, why it moves cost, typical owner (ANS, customer, carrier, sub). End with a missed-scope list."),
    ("02-scope-checklist-erces.md", "Write a bid scope checklist for ERCES / public safety BDA jobs following Safer Buildings Coalition practice, with IFC 510 "
     "and NFPA 1225 Ch. 18 as the compliance basis and the AHJ governing. Cover the trigger, survey, design, UL 2524 gear, 2-hour pathway and enclosures, "
     "battery runtime, annunciator and fire alarm monitoring (NFPA 72), FCC 90.219 licensee consent and registration, grid testing, acceptance, annual "
     "recertification. Each line: item, cost driver, who decides (AHJ, owner, ANS)."),
    ("03-scope-checklist-cabling.md", "Write a structured cabling bid scope checklist from TIA-568/569/606/607, NECA/BICSI 568 and BICSI ITSIMM as named in the "
     "library: pathways, horizontal and backbone, terminations, labeling, grounding, testing and certification, warranty programs. Each line: item and cost driver."),
    ("04-labor-units.md", "Turn COMPANY DATA into a labor-unit reference for the estimator. Section 1: install-category unit minutes (table, keep the numbers "
     "exactly). Section 2: productivity by job type (table). Section 3: how to use them (published NECA/RSMeans units for cable, conduit and tray; "
     "these company units for RF tasks), which categories have too few jobs to trust (under 5 jobs), and GAPs."),
    ("05-condition-factors.md", "Draft a condition-factor table for the estimator: ceiling height, ceiling type, occupied building, after-hours, union or "
     "prevailing wage, floor count, building type, pathway availability, penetrations. Columns: factor, levels, what it applies to (labor categories), "
     "multiplier (GAP: calibrate from company jobs; do not invent values), source method (NECA, Mike Holt, CallMC as in the library)."),
    ("06-soft-costs.md", "List the soft-cost line items an IBW/ERCES estimate needs (design, survey, carrier and FCC paperwork, AHJ permits, commissioning, ATP, "
     "grid testing, annual recertification, project management, travel). For each: when it applies, how to price it (company history line code where "
     "obvious, otherwise GAP), and the T3 ranges from the library only as a cross-check with the source named."),
    ("07-ahj-rules-template.md", "Design a per-AHJ rules table for ERCES that the estimator reads automatically: columns for jurisdiction, adopted IFC and NFPA "
     "1225 editions, coverage % general and critical, 2-hour pathway rule, battery hours, donor antenna rules, annunciator, grid size, annual test, "
     "submittal and permit steps, source document, last verified date. Fill two example rows only from the library (San Francisco FD and Durham NC), "
     "marking every unverified cell VERIFY. Add how to maintain it from SBC updates."),
    ("08-assumptions-library.md", "Write a quote assumptions library in line with the strategy-session rules: assumptions written in the affirmative, only "
     "those that move margin, 'per AHJ requirements' for public safety, change orders for anything outside. Group by DAS, ERCES, cabling, carrier "
     "integration, site conditions, schedule. Mark each as Include in every quote or Use when it applies."),
    ("09-rom-workflow.md", "Align the ROM and quote workflow to the strategy-session rules: ROM about 5 days and full quote about 10 days from floor plans, "
     "final prices typically 10-15% below the ROM, flag competitive bids early, ROM first then paid design or survey, no free designs or BOMs before "
     "commitment, one free design refresh within 12 months then an automatic price adjustment, pre-construction site walks with operations. Write the "
     "step-by-step workflow with owners and a checklist for each gate, and note where the won-job fit check and price-band win rates in COMPANY DATA "
     "plug in (budget review)."),
    ("10-takeoff-rebuild-spec.md", "Write a build spec for rebuilding OpenTakeoff around DAS/ERCES takeoffs, from the library section 4a: device library, run "
     "types with drops and slack, riser and floor roll-up, ERCES grid overlay, labor hooks to the company units, BOM bridge to iBwave and the ANS budget "
     "format, shared storage, and the AGPL license decision (fork vs clean-room). Include an MVP cut and test plan on real plan sets."),
    ("11-estimator-concept-memo.md", "Draft a one-page memo from Matt Busdiecker to Lance and IBW leadership for the meeting the week of Nov 23: the budget "
     "review process and its review artifact (won-job fit check, price-band win rates, outlier reasons) are the seed of an estimator tool, as the "
     "alternative to deciding on a dedicated estimator hire first. Use only COMPANY DATA numbers. Sections: the problem (from the strategy notes), what "
     "exists today, the build plan (from the library build order), what it needs from others (GAP list), decision asked. Warm, direct tone."),
    ("12-nov23-topics.md", "Draft the topics Matt sends Lance before the Nov 23 week meeting, from the strategy notes' open items and Matt's ownership: ROM "
     "tool alignment, quote assumptions standardization, the estimator approach, backlog and win-rate evidence from COMPANY DATA. Each topic: one-line "
     "ask, owner, decision needed. Short."),
]


def ask(task):
    msgs = [{"role": "system", "content": RULES},
            {"role": "user", "content": f"SOURCE LIBRARY:\n{library}\n\nSTRATEGY NOTES:\n{strategy}\n\n{facts}\n\nTASK: {task}"}]
    body = json.dumps({"model": "qwen", "temperature": 0.3, "max_tokens": 6000, "chat_template_kwargs": {"enable_thinking": False}, "messages": msgs}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(URL, data=body, headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1800) as resp:
                return json.load(resp)["choices"][0]["message"]["content"]
        except Exception as e:
            log(f"  retry {attempt + 1}: {e}"); time.sleep(90)
    return None


for name, task in TASKS:
    path = os.path.join(HERE, "drafts", name)
    if os.path.exists(path) and "--redo" not in sys.argv:
        continue
    t0 = time.time()
    out = ask(task)
    if out:
        open(path, "w", encoding="utf-8").write(out.strip() + "\n")
        log(f"{name}: {len(out):,} chars in {time.time() - t0:.0f}s")
    else:
        log(f"{name}: FAILED")
log("done")
