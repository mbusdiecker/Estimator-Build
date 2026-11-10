"""Survey (1-500) and design (1-520) hours on 2026 installation budgets, per 100k sq ft, plus travel lines."""
import sqlite3, sys, statistics as st
c = sqlite3.connect(sys.argv[1])
q = """select j.job, j.sq_ft, l.code, l.qty, l.rate from budget_lines l join jobs j on j.job=l.job
       where j.budget_date>='2025-07-01' and j.job not like '%-Q%' and j.job_type not like 'Engineering%' and l.qty>0
       and (l.code like '1-500%' or l.code like '1-520%' or l.code like '2-047%' or l.code like '2-046%')"""
d = {}
for job, sf, code, qty, rate in c.execute(q):
    k = code[:5]
    d.setdefault(k, []).append((job, sf, qty, rate))
for k, v in sorted(d.items()):
    hrs = [x[2] for x in v if (x[3] or 0) < 200]   # hour lines (rate < $200/h), not purchased services
    per = [x[2] / x[1] * 100000 for x in v if x[1] and (x[3] or 0) < 200]
    print(k, 'lines', len(v), 'hour lines', len(hrs),
          'median h', st.median(hrs) if hrs else None, 'p25/p75', (st.quantiles(hrs, n=4)[0], st.quantiles(hrs, n=4)[2]) if len(hrs) > 3 else None,
          'median h per 100k sf', round(st.median(per), 1) if per else None, 'n sf', len(per))
