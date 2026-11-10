"""Apply Matt's 2026-11-10 standards to build_v6.py and reference/labor/job_factors.csv.
- Rates & Markups: Form-IBWBudget template standard rates (bill blank where the template has none) and 15% markups,
  plus the template's 15% markup on 2-001 installation and 2-430 warehousing labor.
- Survey and design default to internal hours at the budget medians (8 h survey, 40 h design) plus survey mileage as
  2-046 travel hours (Lance's method, IRS $0.725/mi).
- Job factors: closeout 40 h, order materials 8 h (1-520), design revisions 2 x 4 h (1-520) when a design is required.
- Fiber priced from the BOM lines by default; module parts fall back to the BOM average unit cost of their install type.
- Review check: labor lines with hours but no bill rate or markup.
Every replacement must match exactly once."""
import csv, os, sys
proj = sys.argv[1]
p = os.path.join(proj, 'build_v6.py')
t = open(p, encoding='utf-8').read()
R = []
def rep(a, b): R.append((a, b))

# rates
rep("    c=wr.cell(row=r,column=3,value=125); c.font=input_f; c.fill=inputbg; c.number_format='$#,##0'; c.alignment=ctr\n"
    "    _cost=55 if code in ('2-001','1-570','1-580','1-590','2-046','2-430') else 70",
    "    # Form-IBWBudget standard rates (template copy 2026-11-10); None = no standard bill rate in the template (flagged)\n"
    "    _bill,_cost=STD_RATES.get(code,(None,55))\n"
    "    c=wr.cell(row=r,column=3,value=_bill); c.font=input_f; c.fill=inputbg; c.number_format='$#,##0'; c.alignment=ctr")
rep("markups=[('Material markup',0.25,'mat_mkup','Default 25% — override on BUDGET'),\n"
    "         ('Subcontractor markup',0.25,'sub_mkup','Default 25% — override on BUDGET')]",
    "markups=[('Material markup',0.15,'mat_mkup','Form-IBWBudget standard 15% — override on BUDGET'),\n"
    "         ('Subcontractor markup',0.15,'sub_mkup','Form-IBWBudget standard 15% — override on BUDGET'),\n"
    "         ('Installation labor markup (2-001)',0.15,'inst_mkup','Form-IBWBudget prices 2-001 at cost + 15%'),\n"
    "         ('Warehousing labor markup (2-430)',0.15,'wh_mkup','Form-IBWBudget prices 2-430 at cost + 15%')]")
rep("labor_first=6; r=6\n",
    "labor_first=6; r=6\n"
    "STD_RATES={'1-500':(120,70),'1-510':(120,70),'1-520':(120,70),'1-550':(120,70),'1-560':(110,70),'2-001':(None,55),\n"
    "           '1-570':(105,55),'1-580':(None,55),'1-590':(None,55),'2-046':(None,55),'2-430':(40,40)}\n")
# survey / design / travel defaults
rep("('Site survey basis','Purchased service','Purchased service / Internal hours','1-500: service $ OR hours x rate','Estimators often book internal hours',",
    "('Site survey basis','Internal hours','Purchased service / Internal hours','1-500: service $ OR hours x rate','Standard: internal hours (Matt 2026-11-10)',")
rep("('Site survey hours (if internal)',0,'hrs','1-500 quantity when basis = Internal hours','',None,'survey_hrs',0),",
    "('Site survey hours (if internal)',8,'hrs','1-500 quantity when basis = Internal hours','Standard 8 h: median of 2025-26 budgets (middle half 8-16 h)',None,'survey_hrs',0),")
rep("('iBwave design basis','Purchased service','Purchased service / Internal hours','1-520: service $ OR hours x rate','',",
    "('iBwave design basis','Internal hours','Purchased service / Internal hours','1-520: service $ OR hours x rate','Standard: internal hours (Matt 2026-11-10)',")
rep("('Design hours (if internal)',0,'hrs','1-520 quantity when basis = Internal hours','',None,'design_hrs',0),",
    "('Design hours (if internal)',40,'hrs','1-520 quantity when basis = Internal hours','Standard 40 h: median of 2025-26 budgets (middle half 30-64 h); revisions and order materials are job factors',None,'design_hrs',0),")
rep(" ('One-way travel days','1','days','Travel labor & expense','',None,'travel_days',0),",
    " ('One-way travel days','1','days','Travel labor & expense','',None,'travel_days',0),\n"
    " ('Survey / site visit round-trip miles',0,'miles','2-046 travel hours = miles x mileage rate / travel cost rate','Lance: mileage carried as travel labor hours; route the distance, do not guess. Default origin Watervliet',None,'survey_miles',0),")
rep(" ('Shipping % of material',0.05,'%','3-130 Shipping = material x this','Default 5%',None,'ship_pct',0),",
    " ('Shipping % of material',0.05,'%','3-130 Shipping = material x this','Default 5%',None,'ship_pct',0),\n"
    " ('Mileage rate ($/mile)',0.725,'$ / mile','2-046 survey travel hours','IRS 2026 business rate',None,'mile_rate',0),")
rep("'Use BOM lines when the design BOM already carries the fiber and 3a is not filled in','\"3a Fiber takeoff,BOM lines\"','fiber_src',0),",
    "'Default BOM lines (Matt 2026-11-10): 3a parts have no catalog prices and its slack/location defaults inflate quantities','\"3a Fiber takeoff,BOM lines\"','fiber_src',0),")
rep(" ('Fiber priced from','3a Fiber takeoff',", " ('Fiber priced from','BOM lines',")
# BUDGET lines
rep("   f\"=IF({ANS_INSTALLS},{ADDR['travelers']}*{ADDR['travel_days']}*{ADDR['workday']},0)\",\n"
    "   f\"={costrate('Travel Labor')}\", f\"={billrate('Travel Labor')}\", None, None,\n"
    "   'Crew travel — zero on sub-performed jobs'),",
    "   f\"=IF({ANS_INSTALLS},{ADDR['travelers']}*{ADDR['travel_days']}*{ADDR['workday']},0)+ROUND(IFERROR({ADDR['survey_miles']}*{ADDR['mile_rate']}/{costrate('Travel Labor')},0),1)\",\n"
    "   f\"={costrate('Travel Labor')}\", f\"={billrate('Travel Labor')}\", None, None,\n"
    "   'Crew travel (zero on sub-performed jobs) + survey mileage as hours (miles x rate / cost rate)'),")
rep("   f\"={ADDR['lab_cost_2001']}\", f\"={ADDR['lab_bill_2001']}\", None, None,\n"
    "   'From 4. Extension. ZERO when Job Setup says installation is performed by a Subcontractor'),",
    "   f\"={ADDR['lab_cost_2001']}\", f\"={ADDR['lab_bill_2001']}\", f\"={ADDR['inst_mkup']}\", None,\n"
    "   'From 4. Extension + job factors; cost + installation markup (Form-IBWBudget). ZERO when a sub installs'),")
rep("   f\"={costrate('Warehousing')}\", f\"={billrate('Warehousing')}\", None, None,\n"
    "   'Independent of who installs",
    "   f\"={costrate('Warehousing')}\", f\"={billrate('Warehousing')}\", f\"={ADDR['wh_mkup']}\", None,\n"
    "   'Independent of who installs")
rep("f\"=IF({ADDR['design_basis']}={q}Internal hours{q},{ADDR['design_hrs']},IF({ADDR['design_req']}={q}Yes{q},1,0))\"",
    "f\"=IF({ADDR['design_basis']}={q}Internal hours{q},{ADDR['design_hrs']}+ROUND({ADDR['lab_jf_1520']},1),IF({ADDR['design_req']}={q}Yes{q},1,0))\"")
rep("    ADDR[f'lab_jf_{key}']=f'SUMIFS(JOBF[Hours],JOBF[CostCode],\"{code}\")'\n",
    "    ADDR[f'lab_jf_{key}']=f'SUMIFS(JOBF[Hours],JOBF[CostCode],\"{code}\")'\n"
    "ADDR['lab_jf_1520']='SUMIFS(JOBF[Hours],JOBF[CostCode],\"1-520\")'\n")
# job factor quantity rule for design revisions
rep("    else: qv,qf,qfill=_num(j['DefaultQty']) or 0,input_f,inputbg",
    "    elif j['DefaultQty']=='design': qv,qf,qfill=f'=IF({ADDR[\"design_req\"]}=\"Yes\",1,0)',link_f,None\n"
    "    else: qv,qf,qfill=_num(j['DefaultQty']) or 0,input_f,inputbg")
# module price fallback
rep("        put(w,f'J{r}','=IF([@MPN]=\"\",\"\",IFERROR(INDEX(MATDATA[Price used],MATCH([@MPN],MATDATA[MPN],0)),\"\"))',calc_f,nf='$#,##0.00',al=ctr)",
    "        # catalog price by MPN, else the BOM's average unit cost for this install type (v6: keeps quoted fiber cost)\n"
    "        put(w,f'J{r}','=IFERROR(INDEX(MATDATA[Price used],MATCH([@MPN],MATDATA[MPN],0)),IFERROR(AVERAGEIFS(BOMImport[Cost used],BOMImport[Type used (calc)],[@[Install Type]],BOMImport[Cost used],\">0\"),\"\"))',calc_f,nf='$#,##0.00',al=ctr)")
# review check
rep("      ('Pathway factors set for areas in use',f\"={ADDR['lab_path_missing']}=0\")]",
    "      ('Pathway factors set for areas in use',f\"={ADDR['lab_path_missing']}=0\"),\n"
    "      ('Labor lines with hours have a bill rate or markup','=SUMPRODUCT((A13:A43=\"L\")*(N(+C13:C43)>0)*(N(+F13:F43)=0)*(G13:G43=\"\"))=0')]")

for a, b in R:
    n = t.count(a)
    if n != 1:
        sys.exit(f'expected 1 match, found {n}: {a[:90]!r}')
    t = t.replace(a, b)
open(p, 'w', encoding='utf-8', newline='').write(t)
print('build_v6.py: applied', len(R))

jf = os.path.join(proj, 'reference', 'labor', 'job_factors.csv')
rows = list(csv.DictReader(open(jf, encoding='utf-8')))
cols = list(rows[0].keys())
for r in rows:
    if r['JobFactor'] == 'Closeout package':
        r.update(Value='2400', DefaultQty='1', Grade='C', Reference='Matt standard: closeout package 40 h (26-00863 review 2026-10-26)')
names = {r['JobFactor'] for r in rows}
if 'Order materials' not in names:
    rows.append(dict(JobFactor='Order materials', Basis='per job', Value='480', Unit='min', CostCode='1-520', LaborClass='Engineering',
                     DefaultQty='1', Grade='C', Reference='Matt standard: 8 h on every ERCES and cellular DAS budget (Form-IBWBudget DAS Roundup row 11)'))
if 'Design revisions (2 x 4 h)' not in names:
    rows.append(dict(JobFactor='Design revisions (2 x 4 h)', Basis='per job', Value='480', Unit='min', CostCode='1-520', LaborClass='Engineering',
                     DefaultQty='design', Grade='C', Reference='Matt standard: up to two iBwave revisions at 4 h each, when a design is required'))
with open(jf, 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
print('job_factors.csv:', len(rows), 'rows')
