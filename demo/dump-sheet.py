"""Print non-empty cached values from a sheet range of an Excel-saved .xlsx. Usage: dump-sheet.py <xlsx> <sheet> <min_row> <max_row> [max_col]"""
import sys, openpyxl
p, sh, r0, r1 = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
mc = int(sys.argv[5]) if len(sys.argv) > 5 else 14
ws = openpyxl.load_workbook(p, data_only=True, read_only=True)[sh]
for i, row in enumerate(ws.iter_rows(min_row=r0, max_row=r1, max_col=mc, values_only=True), r0):
    vals = [f'{openpyxl.utils.get_column_letter(j+1)}={str(v)[:38]}' for j, v in enumerate(row) if v not in (None, '')]
    if vals: print(i, ' | '.join(vals))
