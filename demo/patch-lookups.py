"""Replace bare-table-name VLOOKUPs in build_v6.py with forms Excel resolves (A1 range or Table[column])."""
import sys
p = sys.argv[1]
t = open(p, encoding='utf-8').read()
reps = [
    ("IFERROR(VLOOKUP([@[Install Type]],InstallTypes,2,FALSE),\"?\"))',",
     "IFERROR(VLOOKUP([@[Install Type]],'+ADDR['it_full']+',2,FALSE),\"?\"))',"),
    ("IFERROR(VLOOKUP([@[Type used (calc)]],InstallTypes,2,FALSE),\"\"))',",
     "IFERROR(VLOOKUP([@[Type used (calc)]],'+ADDR['it_full']+',2,FALSE),\"\"))',"),
    ("IFERROR(VLOOKUP([@[Install Type]],InstallTypes,2,FALSE),\"\"))',",
     "IFERROR(VLOOKUP([@[Install Type]],'+ADDR['it_full']+',2,FALSE),\"\"))',"),
    ("IFERROR(VLOOKUP([@[iBwave Type]],TYPESUG,2,FALSE),\"\")",
     "IFERROR(INDEX(TYPESUG[Suggested assembly],MATCH([@[iBwave Type]],TYPESUG[iBwave Type],0)),\"\")"),
    ("for _col,_k in (('AG',5),('AH',6),('AI',7)):\n        put(wbm,f'{_col}{r}',f'=IF([@MPN]=\"\",1,IFERROR(VLOOKUP({_akey},AREAS,{_k},FALSE),1))'",
     "for _col,_k in (('AG','Height factor'),('AH','Pathway factor'),('AI','Condition factor')):\n        put(wbm,f'{_col}{r}',f'=IF([@MPN]=\"\",1,IFERROR(INDEX(AREAS[{_k}],MATCH({_akey},AREAS[Area],0)),1))'"),
    ("IFERROR(VLOOKUP([@[Height band]],HEIGHTF,3,FALSE),1)", "IFERROR(INDEX(HEIGHTF[Used],MATCH([@[Height band]],HEIGHTF[Height band],0)),1)"),
    ("IFERROR(VLOOKUP([@Pathway],PATHF,3,FALSE),1)", "IFERROR(INDEX(PATHF[Used],MATCH([@Pathway],PATHF[Pathway],0)),1)"),
    ("IFERROR(VLOOKUP([@Pathway],PATHF,4,FALSE),FALSE)", "IFERROR(INDEX(PATHF[Set],MATCH([@Pathway],PATHF[Pathway],0)),FALSE)"),
]
for a, b in reps:
    n = t.count(a)
    if n != 1:
        sys.exit(f'expected 1 match, found {n}: {a[:70]}')
    t = t.replace(a, b)
open(p, 'w', encoding='utf-8', newline='').write(t)
print('patched', len(reps))
