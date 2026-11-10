"""Build an outliers.py --draft detail.json from a filled v4 workbook (BUDGET tab) plus the job BOM.
Usage: v4-detail.py <filled v4 .xlsx> <bom.json> <site> <job type> <out detail.json>
Reads cached values only (the workbook was recalculated and saved by Excel)."""
import json, re, sys
import openpyxl

src, bom_path, site, job_type, out = sys.argv[1:6]
wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
bd = wb["BUDGET"]
cells = {(r, c): v for r, row in enumerate(bd.iter_rows(min_row=1, max_row=64, max_col=13, values_only=True), 1)
         for c, v in enumerate(row, 1)}
num = lambda x: float(x) if isinstance(x, (int, float)) else 0.0

lines, labor_cost, labor_hrs = [], 0.0, 0.0
for r in range(13, 44):
    code, typ = cells.get((r, 2)), cells.get((r, 1))
    total = num(cells.get((r, 5)))
    if not code or total == 0:
        continue
    qty = num(cells.get((r, 3)))
    lines.append({"row": r, "type": typ, "code": code, "qty": qty, "rate": num(cells.get((r, 4))),
                  "total": total, "price": num(cells.get((r, 8)))})
    if typ == "L":
        labor_cost += total
        labor_hrs += qty if num(cells.get((r, 4))) < 200 else 0   # purchased-service lines carry qty 1, not hours
material = sum(l["total"] for l in lines if l["type"] in ("M", "H"))
subs = sum(l["total"] for l in lines if l["type"] in ("S", "D", "E", "ER", "X"))
price, burdened, gp = num(cells.get((54, 5))), num(cells.get((49, 5))), num(cells.get((51, 5)))

bom = json.load(open(bom_path, encoding="utf-8"))
ant = sum(b["qty"] for b in bom if (b["type"] or "") == "Antenna" and "GPS" not in (b["install_type"] or ""))
radios = sum(b["qty"] for b in bom if (b["type"] or "") == "Radio Transceiver")
cable = sum(b["qty"] for b in bom if re.search(r'1/2" Coax|Fiber \(', b["install_type"] or ""))
equipment = [{"row": b["row"], "type": b["type"], "mfr": b["mfr"], "mpn": b["mpn"], "desc": b["description"],
              "qty": b["qty"], "unitCost": b["unit_cost"], "ext": b["ext_cost"], "suppliedBy": b["supplied_by"],
              "installType": b["install_type"], "countedAs": b["counted_as"]} for b in bom]
d = {"job": "DRAFT", "sourceName": src.split("\\")[-1], "source": "draft",
     "queue": {"job": "DRAFT", "site": site, "source": "draft"},
     "values": {"Job Number": "DRAFT", "Site": site, "Quoted Price": price, "GP After Burden": gp / price if price else None,
                "Burdened Cost": burdened, "Labor Cost": labor_cost, "ANS Material Cost": material,
                "Subs and Direct": subs, "Labor Hours": labor_hrs},
     "jobType": job_type,
     "counts": {"Antennas": ant, "Radios RRH RU": radios, "DAS Remotes": 0, "Small Cell Nodes": 0,
                "BDA / Repeater Units": 0, "FTTE Nodes": 0},
     "cableFt": cable, "budgetLines": lines, "equipment": equipment}
json.dump(d, open(out, "w", encoding="utf-8"), indent=1)
print(f"price {price:,.2f}  GP {gp / price:.1%}  labor ${labor_cost:,.2f} / {labor_hrs:g} h  material ${material:,.2f}  "
      f"subs ${subs:,.2f}  ant {ant:g}  radios {radios:g}  cable {cable:,.0f} ft")
