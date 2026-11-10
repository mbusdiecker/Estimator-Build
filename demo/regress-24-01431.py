"""Run the project's phase0 blind-fill (24-01431) against a chosen tool version without editing the original script.
Usage: regress-24-01431.py <project dir> <v4|v5>"""
import os, sys
proj, ver = sys.argv[1], sys.argv[2]
p0 = os.path.join(proj, 'phase0', 'fill_24_01431.py')
src = open(p0, encoding='utf-8').read()
src = src.replace("IBW-Estimating-Tool-DRAFT-v4.xlsx", f"IBW-Estimating-Tool-DRAFT-{ver}.xlsx")
src = src.replace("'24-01431_filled.xlsx'", f"'24-01431_filled_{ver}.xlsx'")
src = src.replace("'calc_results.json'", f"'calc_results_{ver}.json'")
sys.path.insert(0, proj)
# validate_v3 validates sys.argv[1] at import time; point it at the tool file under test
sys.argv = [p0, os.path.join(proj, f'IBW-Estimating-Tool-DRAFT-{ver}.xlsx')]
exec(compile(src, p0, 'exec'), {'__file__': p0, '__name__': '__main__'})
