"""Zero-dependency PDF writer (stdlib only).

Mirrors the zero-dep convention of romtool/xlsx.py (spreadsheets) and
report.py (PNG): no reportlab, no external libs anywhere in ROMtool.
Produces a minimal-but-valid PDF 1.4 document: catalog + page tree +
Helvetica/Helvetica-Bold base-14 fonts + uncompressed content streams,
so the rendered text stays greppable (tests assert on it directly).

Input is a small op list per page rather than a layout engine:
  pages = [
      [("text", x, y, size, "string", bold), ("rule", x, y, width), ...]
  ]
Coordinates are PDF points, origin bottom-left, Letter page (612 x 792).
Text wrapping / page flow is the caller's job; the wrap_text() helper
approximates Helvetica widths at ~0.55 em per character (intentional
over-estimate so lines never clip) - fine for draft commercial sheets.
"""

AVG_CHAR_EM = 0.55          # conservative Helvetica average glyph width
PAGE_W, PAGE_H = 612.0, 792.0


def _esc(text):
    out = []
    for ch in str(text):
        if ch in ("\\", "(", ")"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def text_width(text, size):
    return len(str(text)) * size * AVG_CHAR_EM


def wrap_text(text, size, max_width):
    """Greedy word wrap using the ~0.55 em/char Helvetica approximation."""
    words = str(text).split()
    if not words:
        return [""]
    lines, cur = [], words[0]
    for w in words[1:]:
        trial = cur + " " + w
        if text_width(trial, size) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _content_stream(ops):
    out = []
    for op in ops:
        kind = op[0]
        if kind == "text":
            _, x, y, size, text, bold = op
            font = "F2" if bold else "F1"
            out.append("BT /%s %.2f Tf 1 0 0 1 %.2f %.2f Tm (%s) Tj ET"
                       % (font, size, x, y, _esc(text)))
        elif kind == "rule":
            _, x, y, w = op
            out.append("0.6 w 0.45 0.45 0.45 RG %.2f %.2f m %.2f %.2f l S"
                       % (x, y, x + w, y))
    return "\n".join(out).encode("latin-1", "replace")


def encode_pdf(pages, page_size=(PAGE_W, PAGE_H)):
    """pages: list of page op-lists -> minimal valid PDF 1.4 bytes."""
    objects = []           # (obj_num, bytes); numbering starts at 1

    def add(body_bytes):
        objects.append(body_bytes)
        return len(objects)

    # 1 catalog, 2 page tree, 3/4 fonts -- order fixed below
    npages = max(len(pages), 1)
    page_w, page_h = page_size
    kids = []

    # reserve 1..4 first so /Contents numbers land after them
    num_catalog, num_pages_obj, num_f1, num_f2 = 1, 2, 3, 4
    objects.extend([None, None, None, None])   # placeholders, replaced below
    for ops in pages or [[("text", 50, 720, 12, "(empty document)", False)]]:
        stream = _content_stream(ops)
        body = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        num_stream = add(body)
        page_dict = ("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] "
                     "/Contents %d 0 R /Resources << /Font << /F1 %d 0 R "
                     "/F2 %d 0 R >> >> >>"
                     % (num_pages_obj, _fmt(page_w), _fmt(page_h),
                        num_stream, num_f1, num_f2))
        kids.append("%d 0 R" % add(page_dict.encode("latin-1")))
    objects[num_catalog - 1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[num_pages_obj - 1] = ("<< /Type /Pages /Count %d /Kids [%s] >>"
                                  % (len(kids), " ".join(kids))
                                  ).encode("latin-1")
    objects[num_f1 - 1] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objects[num_f2 - 1] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"

    out = [b"%PDF-1.4\n%\xc7\xec\x8f\xa2\n"]
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(sum(len(o) for o in out))
        out.append(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref_off = sum(len(o) for o in out)
    n = len(objects) + 1
    xref = ["xref", "0 %d" % n, "0000000000 65535 f "]
    for off in offsets[1:]:
        xref.append("%010d 00000 n " % off)
    out.append("\n".join(xref).encode("latin-1") + b"\n")
    out.append(("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (n, xref_off)).encode("latin-1"))
    buf = b"".join(out)
    assert buf.count(b"startxref") == 1
    return buf


def _fmt(v):
    return ("%g" % float(v))


def extract_pdf_text(data):
    """Tiny helper (tests + smoke): pull Tj strings out of content streams."""
    import re

    texts = []
    for stream in re.findall(rb"stream\r?\n(.*?)endstream", data, re.S):
        for raw in re.findall(rb"\((?:[^()\\]|\\.)*\) Tj", stream, re.S):
            body = raw[1:-4]
            body = re.sub(rb"\\([()\\])", rb"\1", body)
            texts.append(body.decode("latin-1"))
    return texts
