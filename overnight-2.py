"""Overnight estimator build, part 2 (2026-11-09, Matt: "do it"). Five jobs on the home Qwen server, no Claude:
  A  self-review of the 12 drafts: numbers not in the company data or library, style breaks  -> reviews/*.review.md
  B  proposed lost reasons for the unverified losses, from the Smartsheet notes in lost-reason-review.xlsx
                                                                                               -> data/lost-reason-proposals.csv
  C  budget-line notes mined into inclusions, exclusions, assumptions, risks                   -> data/budget-notes-mined.csv, drafts/13-notes-mined.md
  D  equipment catalog: each distinct part normalized (category, OEM, family, bands), with our own unit-cost history
                                                                                               -> data/price-book.csv
  E  part to install category (labor unit) map: history first, Qwen only for parts with none   -> data/part-to-labor.csv
Each Qwen answer is cached in cache\ so a rerun picks up where it stopped. Nothing outside this folder changes.
Usage: uv run --no-project --with openpyxl python -I overnight-2.py
"""
import csv
import hashlib
import json
import os
import re
import sqlite3
import statistics as st
import sys
import time
import urllib.request
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
AUTO = r"C:\Users\MattBusdiecker\Documents\VZW Intel Automation"
for d in ("data", "drafts", "reviews", "cache"):
    os.makedirs(os.path.join(HERE, d), exist_ok=True)
LOG = open(os.path.join(HERE, "build-log.txt"), "a", encoding="utf-8")
log = lambda m: (LOG.write(time.strftime("%Y-%m-%d %H:%M ") + m + "\n"), LOG.flush(), print(m))
URL = "http://192.168.1.2:8081/v1/chat/completions"
KEY = os.environ.get("LLM_API_KEY")
if not KEY:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        KEY = winreg.QueryValueEx(k, "LLM_API_KEY")[0]


def ask(system, user, as_json=True, max_tokens=4000):
    key = hashlib.sha1((system + "\n" + user).encode()).hexdigest()
    cp = os.path.join(HERE, "cache", key + ".txt")
    if os.path.exists(cp):
        out = open(cp, encoding="utf-8").read()
    else:
        body = {"model": "qwen", "temperature": 0, "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if as_json:
            body["response_format"] = {"type": "json_object"}
        out = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=1800) as resp:
                    out = json.load(resp)["choices"][0]["message"]["content"]
                break
            except Exception as e:
                log(f"  qwen retry {attempt + 1}: {e}"); time.sleep(60)
        if out is None:
            return None
        open(cp, "w", encoding="utf-8").write(out)
    if not as_json:
        return out
    try:
        return json.loads(re.sub(r"^```(json)?|```$", "", out.strip()))
    except Exception:
        return None


def write_csv(name, header, rows):
    with open(os.path.join(HERE, "data", name), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(header); w.writerows(rows)


db = sqlite3.connect(os.path.join(AUTO, "vzw-budgets.sqlite"))
jobs = {r["Job Number"]: r for r in json.load(open(os.path.join(AUTO, "vzw-jobs.json"), encoding="utf-8-sig"))}
STYLE = "No em or en dashes. No hype words (robust, seamless, leverage, crucial, comprehensive). Plain professional English."

# ---------- A: self-review of the drafts ----------
facts = open(os.path.join(HERE, "data", "company-facts.txt"), encoding="utf-8").read()
library = open(r"C:\Users\MattBusdiecker\Downloads\Estimator_Source_Library.md", encoding="utf-8").read()
for f in sorted(os.listdir(os.path.join(HERE, "drafts"))):
    if not f.endswith(".md") or f.startswith("13-"):
        continue
    out_p = os.path.join(HERE, "reviews", f.replace(".md", ".review.md"))
    if os.path.exists(out_p):
        continue
    text = open(os.path.join(HERE, "drafts", f), encoding="utf-8").read()
    r = ask("You review a draft for an in-building wireless estimator. Report problems only, as a markdown list under three headings: "
            "'Numbers not in the sources' (every dollar amount, rate, hour, percentage or count in the draft that does not appear in COMPANY DATA "
            "or the SOURCE LIBRARY; quote the draft text), 'Factual or scope concerns', 'Style' (" + STYLE + "). If a heading has nothing, write None.",
            f"COMPANY DATA:\n{facts}\n\nSOURCE LIBRARY:\n{library}\n\nDRAFT ({f}):\n{text}", as_json=False)
    if r:
        open(out_p, "w", encoding="utf-8").write(f"# Review of {f}\n\n{r.strip()}\n")
log("A: draft reviews written")

# ---------- B: lost-reason proposals ----------
try:
    from openpyxl import load_workbook
    ws = load_workbook(os.path.join(AUTO, "lost-reason-review.xlsx"), read_only=True)["Lost reasons"]
    hdr = None; items = []
    for row in ws.iter_rows(values_only=True):
        if hdr is None:
            hdr = list(row); continue
        o = dict(zip(hdr, row))
        if o.get("Smartsheet Notes"):
            items.append(o)
    REASONS = ["Price", "Lost to competitor (not price)", "Customer cancelled / project shelved", "Never awarded (no decision)",
               "Scope changed / requoted under another job", "Carrier dropped the site", "Won (PO missing from Smartsheet)", "Unclear from notes"]
    out_rows = []
    for i in range(0, len(items), 15):
        batch = items[i:i + 15]
        text = "\n".join(f"[{k}] {o['Job']} | {o['Site']} | {o['Customer']} | notes: {str(o['Smartsheet Notes'])[:700]}" for k, o in enumerate(batch))
        r = ask("Classify why each quoted job was not won, from its project-tracker notes only. Answer JSON: {\"items\": [{\"i\": <index>, "
                f"\"reason\": one of {json.dumps(REASONS)}, \"confidence\": \"high\"|\"medium\"|\"low\", \"evidence\": short quote from the notes}}]}}. "
                "Use 'Unclear from notes' when the notes do not say.", text)
        for a in (r or {}).get("items", []):
            try:
                o = batch[int(a["i"])]
            except Exception:
                continue
            out_rows.append([o["Job"], o["Site"], o["Salesperson"], a.get("reason"), a.get("confidence"), a.get("evidence")])
    write_csv("lost-reason-proposals.csv", ["Job", "Site", "Salesperson", "Proposed reason", "Confidence", "Evidence"], out_rows)
    log(f"B: lost-reason proposals for {len(out_rows)} of {len(items)} jobs with notes")
except Exception as e:
    log(f"B: failed: {e}")

# ---------- C: budget-line notes ----------
notes = Counter()
for (n,) in db.execute("select trim(note) from budget_lines where note is not null and length(trim(note)) > 8"):
    notes[n] += 1
uniq = list(notes)
mined = []
for i in range(0, len(uniq), 40):
    batch = uniq[i:i + 40]
    r = ask("Each line is a note an estimator wrote next to a budget line on an in-building wireless job. Classify each. Answer JSON: "
            "{\"items\": [{\"i\": <index>, \"kind\": \"inclusion\"|\"exclusion\"|\"assumption\"|\"risk\"|\"pricing basis\"|\"other\", "
            "\"topic\": short topic such as lifts, after-hours, travel, testing, permits, carrier, design, materials, "
            "\"statement\": the note rewritten as one plain sentence}]}.", "\n".join(f"[{k}] {t[:300]}" for k, t in enumerate(batch)))
    for a in (r or {}).get("items", []):
        try:
            t = batch[int(a["i"])]
        except Exception:
            continue
        mined.append([a.get("kind"), a.get("topic"), a.get("statement"), notes[t], t])
mined.sort(key=lambda x: (str(x[0]), -x[3]))
write_csv("budget-notes-mined.csv", ["Kind", "Topic", "Statement", "Times used", "Original note"], mined)
by = defaultdict(list)
for k, t, s, n, _ in mined:
    if k in ("inclusion", "exclusion", "assumption", "risk"):
        by[(k, str(t).lower())].append((n, s))
md = ["# What our budgets actually say (mined from budget-line notes)", "",
      "Each statement was written by an estimator on a past budget; counts are how many budget lines carry it. Draft for review.", ""]
for kind in ("inclusion", "exclusion", "assumption", "risk"):
    md += [f"## {kind.title()}s", ""]
    for (k, t), lst in sorted(by.items(), key=lambda kv: -sum(n for n, _ in kv[1])):
        if k != kind:
            continue
        md.append(f"- **{t}** ({sum(n for n, _ in lst)} lines): " + "; ".join(s for _, s in sorted(lst, reverse=True)[:3]))
    md.append("")
open(os.path.join(HERE, "drafts", "13-notes-mined.md"), "w", encoding="utf-8").write("\n".join(md))
log(f"C: {len(uniq)} distinct notes mined")

# ---------- D: equipment catalog and price book ----------
parts = defaultdict(list)
for job, typ, mfr, mpn, desc, qty, unit, unit_cost, sup, inst, ca in db.execute(
        "select job, coalesce(type,''), coalesce(mfr,''), coalesce(mpn,''), coalesce(description,''), qty, coalesce(units,''), unit_cost, "
        "coalesce(supplied_by,''), coalesce(install_type,''), coalesce(counted_as,'') from equipment where qty > 0"):
    key = (mfr.strip().lower(), re.sub(r"\s+", "", mpn).upper()) if mpn.strip() else ("", desc.strip().lower()[:60])
    if not key[1]:
        continue
    parts[key].append({"job": job, "type": typ, "mfr": mfr.strip(), "mpn": mpn.strip(), "desc": desc.strip(), "qty": qty, "units": unit,
                       "cost": unit_cost, "sup": sup, "inst": inst, "ca": ca, "date": str(jobs.get(job, {}).get("Quote Submitted") or "")})
keys = sorted(parts, key=lambda k: -len(parts[k]))
CATS = ["antenna", "das remote", "das head end / POI / hub", "radio / RRH", "small cell", "BDA / repeater", "FTTE node", "coax cable", "fiber cable",
        "copper / Cat cable", "connector / jumper", "splitter / coupler / combiner / filter", "fiber panel / cassette / patch", "rack / enclosure / power",
        "battery / UPS", "annunciator / monitoring", "mounting / support / hardware", "labeling / firestop / consumables", "test / tools / rental",
        "network / router / switch", "license / software", "service / labor placeholder", "other"]
info = {}
for i in range(0, len(keys), 40):
    batch = keys[i:i + 40]
    text = "\n".join(f"[{k}] {parts[key][0]['mfr']} | {parts[key][0]['mpn']} | {parts[key][0]['desc'][:110]} | typed: {parts[key][0]['type']}"
                     for k, key in enumerate(batch))
    r = ask("Normalize each in-building wireless equipment line. Answer JSON: {\"items\": [{\"i\": <index>, \"category\": one of " + json.dumps(CATS) +
            ", \"oem\": manufacturer name normalized or \"\", \"family\": product family or model line, \"bands\": frequency bands or \"\", "
            "\"unit\": \"each\"|\"foot\"|\"other\"}]}. Use only what the line says; do not guess prices.", text)
    for a in (r or {}).get("items", []):
        try:
            info[batch[int(a["i"])]] = a
        except Exception:
            pass
    if i % 400 == 0:
        log(f"D: {i}/{len(keys)} parts normalized")
pb = []
for key in keys:
    L = parts[key]; a = info.get(key, {})
    costs = [(x["date"], x["cost"]) for x in L if x["cost"] and x["cost"] > 0 and x["sup"] in ("", "ANS")]
    last = max(costs) if costs else ("", None)
    vals = [c for _, c in costs]
    pb.append([L[0]["mfr"], L[0]["mpn"], L[0]["desc"][:120], a.get("category"), a.get("oem"), a.get("family"), a.get("bands"), a.get("unit"),
               len({x["job"] for x in L}), len(vals), round(st.median(vals), 2) if vals else None, min(vals) if vals else None,
               max(vals) if vals else None, last[1], last[0]])
write_csv("price-book.csv", ["Mfr", "MPN", "Description", "Category", "OEM", "Family", "Bands", "Unit", "Jobs", "Priced lines",
                             "Median unit cost", "Min", "Max", "Last unit cost", "Last quote date"], pb)
log(f"D: price book with {len(pb)} parts ({sum(1 for r in pb if r[10])} with our own cost history)")

# ---------- E: part to install category (labor unit) ----------
labor_cats = [r[0] for r in csv.reader(open(os.path.join(HERE, "data", "labor-units-by-category.csv"), encoding="utf-8"))][1:]
pm, need = [], []
for key in keys:
    insts = Counter(x["inst"] for x in parts[key] if x["inst"])
    if insts:
        top, n = insts.most_common(1)[0]
        pm.append([parts[key][0]["mfr"], parts[key][0]["mpn"], parts[key][0]["desc"][:100], top, "history", f"{n} of {len(parts[key])} lines"])
    else:
        need.append(key)
for i in range(0, len(need), 40):
    batch = need[i:i + 40]
    r = ask("Pick the install labor category for each equipment line, from this list only: " + json.dumps(labor_cats) +
            ". Use \"none\" for items with no install labor (licenses, spares, shipping). Answer JSON: {\"items\": [{\"i\": <index>, \"category\": <name>}]}.",
            "\n".join(f"[{k}] {parts[key][0]['mfr']} | {parts[key][0]['mpn']} | {parts[key][0]['desc'][:110]} | category: {info.get(key, {}).get('category', '')}"
                      for k, key in enumerate(batch)))
    for a in (r or {}).get("items", []):
        try:
            key = batch[int(a["i"])]
        except Exception:
            continue
        pm.append([parts[key][0]["mfr"], parts[key][0]["mpn"], parts[key][0]["desc"][:100], a.get("category"), "qwen", "no history"])
write_csv("part-to-labor.csv", ["Mfr", "MPN", "Description", "Install category", "Source", "Basis"], pm)
log(f"E: {len(pm)} parts mapped to labor categories ({len(need)} by Qwen)")
log("part 2 done")
