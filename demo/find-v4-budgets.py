"""List intel-store budgets that look like v4-tool output (file name or v4-only tab/labels)."""
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
r = c.execute("select job, budget_file from jobs where budget_file like '%stimat%' or budget_file like '%DRAFT%'").fetchall()
print('by file name:', len(r)); [print(' ', x) for x in r]
r = c.execute("select distinct job from budget_summary where label like '%Job Setup%' or label like '%Extension%' or label like '%BOM Import%'").fetchall()
print('by v4 labels in summary:', len(r)); [print(' ', x) for x in r]
r = c.execute("select distinct job from budget_lines where note like '%4. Extension%' or note like '%Job Setup%'").fetchall()
print('by v4 line notes:', len(r)); [print(' ', x) for x in r]
print('jobs in store:', c.execute('select count(*) from jobs').fetchone()[0])
