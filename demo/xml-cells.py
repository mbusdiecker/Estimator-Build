"""Print the raw stored XML of cells on one sheet of an .xlsx. Usage: xml-cells.py <xlsx> <sheet name> <cell> [<cell> ...]"""
import re, sys, zipfile
path, sheet, cells = sys.argv[1], sys.argv[2], sys.argv[3:]
z = zipfile.ZipFile(path)
wbx = z.read('xl/workbook.xml').decode('utf-8')
rels = z.read('xl/_rels/workbook.xml.rels').decode('utf-8')
rid = re.search(r'<sheet [^>]*name="' + re.escape(sheet.replace('&', '&amp;')) + r'"[^>]*r:id="(rId\d+)"', wbx).group(1)
rel = re.search(r'<Relationship [^>]*Id="' + rid + r'"[^>]*/>', rels).group(0)
target = re.search(r'Target="([^"]+)"', rel).group(1)
part = target.lstrip('/') if target.startswith('/') else 'xl/' + target
x = z.read(part).decode('utf-8')
for c in cells:
    m = re.search(r'<c r="' + c + r'"[^>]*?(?:/>|>.*?</c>)', x)
    print(c, m.group(0) if m else '(not found)')
