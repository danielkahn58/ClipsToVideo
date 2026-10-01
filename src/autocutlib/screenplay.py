"""Read the dialogue (who says what, in order) from a screenplay PDF."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .report import AutocutError
from .text import tokenize

# Industry-standard screenplay indents, in points from the page's left edge
# (action 1.5", dialogue 2.5", parenthetical ~3.1", character cue ~3.5-3.7").
ACTION_X = 108
DIALOGUE_MIN_X = 160
CUE_MIN_X = 240
CUE_MAX_X = 345  # anything further right is a transition ("CUT TO:") or page number


@dataclass
class Line:
    character: str
    text: str
    page: int
    tokens: list = field(default_factory=list)


def parse_pages(spec):
    if not spec:
        return None
    pages = set()
    try:
        for part in str(spec).split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-")
                pages.update(range(int(a), int(b) + 1))
            elif part:
                pages.add(int(part))
    except ValueError:
        raise AutocutError(f"Bad page range {spec!r}: use something like 3-4 or 3,5-6") from None
    return pages


def pdf_rows(pdf_path, pages):
    """Return text rows with their x position and whether a blank line precedes them."""
    import pdfplumber
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            if pages and pno not in pages:
                continue
            words = page.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False)
            words.sort(key=lambda w: (w["top"], w["x0"]))
            page_rows = []
            for w in words:
                if page_rows and abs(w["top"] - page_rows[-1]["top"]) <= 2.5:
                    page_rows[-1]["words"].append(w)
                else:
                    page_rows.append({"top": w["top"], "words": [w]})
            diffs = [b["top"] - a["top"] for a, b in zip(page_rows, page_rows[1:]) if b["top"] > a["top"]]
            line_h = sorted(diffs)[len(diffs) // 2] if diffs else 12
            prev_top = None
            for r in page_rows:
                ws = sorted(r["words"], key=lambda w: w["x0"])
                rows.append({
                    "page": pno,
                    "x0": ws[0]["x0"],
                    "text": " ".join(w["text"] for w in ws),
                    "gap": prev_top is None or (r["top"] - prev_top) > 1.5 * line_h,
                })
                prev_top = r["top"]
    return rows


def clean_cue(text):
    name = re.sub(r"\(.*?\)", "", text)          # (V.O.), (O.S.), (CONT'D) ...
    name = name.replace("^", "").strip()          # Final Draft dual-dialogue marker
    return re.sub(r"\s+", " ", name).upper()


def is_cue(text):
    t = text.strip()
    letters = re.sub(r"[^A-Za-z]", "", re.sub(r"\(.*?\)", "", t))
    return (letters != "" and t == t.upper() and len(t) <= 45
            and not re.match(r"^(INT|EXT|I/E|INT\./EXT)[\. ]", t)
            and not t.endswith(":"))


def skip_row(text):
    t = text.strip()
    return (re.fullmatch(r"\d+\.?", t) is not None            # page numbers
            or re.fullmatch(r"\(?(MORE|CONT'?D|CONTINUED)\)?:?", t.replace("’", "'"), re.I) is not None)


def parse_screenplay(pdf_path, pages):
    rows = pdf_rows(pdf_path, pages)
    if not rows and pages:
        raise AutocutError("No text found on the selected pages - check the page range.")
    if not rows:
        raise AutocutError("No text found in the PDF (is it a scanned image? It needs selectable text).")

    # Calibrate horizontal offset using scene headings, which sit at the action margin.
    heading_x = [r["x0"] for r in rows if re.match(r"^\d*\s*(INT|EXT)[\. /]", r["text"])]
    offset = 0.0
    if heading_x:
        heading_x.sort()
        offset = heading_x[len(heading_x) // 2] - ACTION_X
        if abs(offset) < 4:
            offset = 0.0

    lines, cur, in_paren = [], None, False

    def finish():
        nonlocal cur
        if cur and cur["parts"]:
            text = re.sub(r"\(.*?\)", "", " ".join(cur["parts"]))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                lines.append(Line(cur["char"], text, cur["page"]))
        cur = None

    for r in rows:
        t, x = r["text"].strip(), r["x0"] - offset
        if skip_row(t):
            continue
        if r["gap"] and cur and cur["parts"]:
            finish()
        if CUE_MIN_X <= x < CUE_MAX_X and is_cue(t):
            finish()
            cur, in_paren = {"char": clean_cue(t), "parts": [], "page": r["page"]}, False
        elif cur is not None and DIALOGUE_MIN_X <= x < CUE_MAX_X:
            if t.startswith("(") or in_paren:          # parenthetical, possibly multi-line
                in_paren = not t.endswith(")")
                continue
            cur["parts"].append(t)
        else:
            finish()
    finish()
    return lines


def load_dialogue(pdf_path, pages=None):
    """Parse the screenplay and tokenize each line, dropping lines with no words."""
    if not Path(pdf_path).expanduser().is_file():
        raise AutocutError(f"Screenplay not found: {pdf_path}")
    pdf_path = Path(pdf_path).expanduser()
    if isinstance(pages, str) or pages is None:
        pages = parse_pages(pages)
    lines = parse_screenplay(pdf_path, pages)
    for ln in lines:
        ln.tokens = tokenize(ln.text)
    return [ln for ln in lines if ln.tokens]


def dump_text(lines):
    """The same listing `--dump` prints."""
    out = [f"Parsed {len(lines)} dialogue lines:\n"]
    for i, ln in enumerate(lines, 1):
        out.append(f"{i:3d}  p{ln.page:<3d} {ln.character:<14} {ln.text}")
    return "\n".join(out)


def characters(lines):
    """Speaking characters in order of first appearance, with line counts."""
    counts = {}
    for ln in lines:
        counts[ln.character] = counts.get(ln.character, 0) + 1
    return counts
