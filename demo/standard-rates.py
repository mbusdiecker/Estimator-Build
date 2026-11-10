"""Most common (mode) internal cost rate and bill rate per labor cost code on 2026 IBW budgets in the intel store."""
import sqlite3, sys
from collections import Counter, defaultdict
c = sqlite3.connect(sys.argv[1])
rows = c.execute("""select l.job, l.code, l.rate, l.bill_rate from budget_lines l join jobs j on j.job=l.job
                    where l.type='L' and l.qty>0 and j.budget_date>='2026-01-01' and l.job not like '%-Q%'""").fetchall()
by = defaultdict(lambda: (Counter(), Counter(), set()))
for job, code, rate, bill in rows:
    k = (code or '').strip()
    cr, br, jobs = by[k]
    if rate: cr[round(rate, 2)] += 1
    if bill: br[round(bill, 2)] += 1
    jobs.add(job)
for k in sorted(by):
    cr, br, jobs = by[k]
    print(f"{k:32} jobs={len(jobs):3}  cost {cr.most_common(3)}  bill {br.most_common(3)}")
