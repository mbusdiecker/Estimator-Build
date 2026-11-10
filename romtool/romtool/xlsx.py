"""Zero-dependency XLSX writer (stdlib zipfile + xml only).

Renders one or more sheets of rows into a minimal-but-valid Office Open XML
workbook: [Content_Types].xml, rels, workbook.xml, worksheets with inline
strings (no sharedStrings) and numeric cells. Mirrors the zero-dep PNG writer
convention in report.py -- no openpyxl, no image libs anywhere in ROMtool.
"""

import io
import zipfile

_XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _col_name(idx):
    """0-based column index -> spreadsheet column letters (0 -> A)."""
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell_xml(col, row, value):
    ref = "%s%d" % (_col_name(col), row)
    if value is None or value == "":
        return '<c r="%s"/>' % ref
    if isinstance(value, bool):
        return '<c r="%s" t="b"><v>%d</v></c>' % (ref, int(value))
    if isinstance(value, (int, float)):
        return '<c r="%s" t="n"><v>%r</v></c>' % (ref, value)
    return ('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
            % (ref, _esc(value)))


def _sheet_xml(rows):
    out = [_XML_HEADER,
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           "<sheetData>"]
    for r_i, row in enumerate(rows, start=1):
        cells = []
        for c_i, val in enumerate(row):
            cell = _cell_xml(c_i, r_i, val)
            if cell:
                cells.append(cell)
        out.append("<row r=\"%d\">%s</row>" % (r_i, "".join(cells)))
    out.append("</sheetData></worksheet>")
    return "".join(out)


def _workbook_xml(names):
    sheets = "".join(
        '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_esc(n), i + 1, i + 1)
        for i, n in enumerate(names))
    return "".join([
        _XML_HEADER,
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        "<sheets>", sheets, "</sheets></workbook>",
    ])


def _content_types(n):
    overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet%d.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.'
        'worksheet+xml"/>' % (i + 1) for i in range(n))
    return "".join([
        _XML_HEADER,
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package'
        '.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats'
        '-officedocument.spreadsheetml.sheet.main+xml"/>',
        overrides,
        "</Types>",
    ])


def encode_xlsx(sheets):
    """sheets: list of {"name": str, "rows": list[list[cell]]} -> xlsx bytes."""
    names = [s["name"] for s in sheets]
    parts = [
        ("[Content_Types].xml", _content_types(len(sheets))),
        ("_rels/.rels", "".join([
            _XML_HEADER,
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006'
            '/relationships">',
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            '/officeDocument" Target="xl/workbook.xml"/>',
            "</Relationships>",
        ])),
        ("xl/workbook.xml", _workbook_xml(names)),
        ("xl/_rels/workbook.xml.rels", "".join([
            _XML_HEADER,
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006'
            '/relationships">',
            "".join(
                '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/worksheet" Target="worksheets/'
                'sheet%d.xml"/>' % (i + 1, i + 1) for i in range(len(sheets))),
            "</Relationships>",
        ])),
    ]
    for i, sheet in enumerate(sheets):
        parts.append(("xl/worksheets/sheet%d.xml" % (i + 1), _sheet_xml(sheet["rows"])))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts:
            zf.writestr(name, data)
    return buf.getvalue()
