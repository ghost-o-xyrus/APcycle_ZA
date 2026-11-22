"""Label-anchored invoice parser shared by the text-layer and OCR routes.

Header fields: find a known label, read the value that follows.
Line items: on each row of the item table, choose the (qty, rate, amount)
triple whose product reconciles (qty x rate = amount), which survives column
re-ordering and numbers inside descriptions ("500 ml", "32A").
"""
from __future__ import annotations

import re
from datetime import date
from itertools import permutations

from ..schema import Extraction, Field, Line

# ---------- value patterns ----------
# Loose on purpose: OCR reads 0 as O and Z as 2; the checksum repair fixes them later.
GSTIN_TOKEN = re.compile(r"\b[0-9O]{2}[A-Z0-9]{10}[0-9A-Z][Z2][0-9A-Z]\b")
CUR = r"(?:Rs\.?|INR|₹|US\$|\$|USD|€|EUR|£|GBP|¥|JPY)"
MONEY = re.compile(CUR + r"?\s*(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{2})")
NUMBER = re.compile(r"^" + CUR + r"?(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)$")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
def _y2(y: str) -> int:
    y = int(y)
    return y if y > 99 else (2000 + y if y < 70 else 1900 + y)


# Each pattern gives candidate (year, month, day) readings, best first. Day-first is the Indian
# norm; month-first is tried only when day-first is impossible (9/20/2020 on a US invoice).
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: [(int(m[1]), int(m[2]), int(m[3]))]),
    (re.compile(r"\b(\d{1,2})[-\s]([A-Za-z]{3})[a-z]*[-\s,]+(\d{4})\b"),
     lambda m: [(int(m[3]), MONTHS.get(m[2].lower()[:3], 0), int(m[1]))]),
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"),
     lambda m: [(int(m[3]), MONTHS.get(m[1].lower()[:3], 0), int(m[2]))]),
    (re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b"),
     lambda m: [(int(m[3]), int(m[2]), int(m[1])), (int(m[3]), int(m[1]), int(m[2]))]),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b(?![/\d])"),
     lambda m: [(_y2(m[3]), int(m[2]), int(m[1])), (_y2(m[3]), int(m[1]), int(m[2]))]),
]
DOCNO = r"([A-Z0-9][A-Z0-9/\-\.]{1,30}[A-Z0-9])"

# ---------- label synonyms (written against layouts A-C and common Indian usage) ----------
LABELS = {
    "invoice_number": [r"invoice\s*(?:no\.?|number|#)\s*[:\-]?", r"bill\s*no\.?\s*[:\-]?", r"inv\.?\s*no\.?\s*[:\-]?",
                       r"(?<![a-z])inv\.?\s*#\s*[:\-]?"],
    "invoice_date": [r"invoice\s*date\s*[:\-]?", r"\bdated\b\s*[:\-]?", r"(?<![a-z])date\s*[:\-]?"],
    "po_reference": [r"p\.?\s*o\.?\s*(?:no\.?|number|#|ref\.?)\s*[:\-]?", r"buyer'?s\s*order\s*no\.?\s*[:\-]?",
                     r"ref\.?\s*po\s*[:\-]?", r"purchase\s*order\s*(?:no\.?|number)?\s*[:\-]?", r"order\s*no\.?\s*[:\-]?"],
    "buyer_gstin": [r"(?:buyer|customer|bill\s*to|consignee)\s*gstin(?:/uin)?\s*[:\-]?"],
    "vendor_gstin": [r"(?:supplier|seller|vendor)?\s*gstin(?:/uin)?\s*[:\-]?"],
    "subtotal": [r"sub\s*-?\s*total", r"taxable\s*value", r"net\s*amount", r"total\s*before\s*tax"],
    "cgst": [r"cgst"],
    "sgst": [r"sgst|utgst"],
    "igst": [r"igst"],
    "total": [r"grand\s*total", r"total\s*amount(?!\s*before)", r"amount\s*payable", r"invoice\s*total", r"balance\s*due",
              r"total\s*amount\s*due", r"amount\s*due", r"total\s*due", r"total\s*invoice(?:\s*amount)?"],
    "bank_account": [r"(?:bank\s*)?a/?c\.?\s*(?:no\.?|number)\s*[:\-]?", r"account\s*(?:no\.?|number)\s*[:\-]?"],
    "ifsc": [r"ifs(?:c)?\s*code\s*[:\-]?", r"ifsc\s*[:\-]?"],
}
TABLE_HEAD = re.compile(r"\b(hsn|qty|quantity|description|item)\b", re.I)
# A document is known by its title, not by words used as field labels: real invoices carry
# "Estimate #", "Quotation No." or "PO Number" fields that refer to other documents.
# Order matters within a line: the more specific name wins ("Proforma Invoice" is not an invoice).
DOC_TYPES = [
    ("credit_note", r"credit\s*note|credit\s*memo|credit\s*request"),
    ("debit_note", r"debit\s*note|debit\s*memo"),
    ("quotation", r"quotation|quote|estimate|pro\s*-?\s*forma(?:\s*invoice)?"),
    ("purchase_order", r"purchase\s*order|sales\s*order|order\s*confirmation|purchase\s*requisition|work\s*order"
                       r"|confirmation"),
    ("receipt", r"(?:payment\s*|cash\s*|sales\s*)?receipt"),
    ("delivery_challan", r"delivery\s*challan|delivery\s*note|packing\s*(?:list|slip)"),
    ("invoice", r"(?:tax\s*|gst\s*|commercial\s*|sales\s*|service\s*)?invoice|bill\s*of\s*supply"),
]
_LABEL_AFTER = re.compile(r"^\s*(?:#|no\b|no\.|nos?\b|number|num\b|n°|date|dt\b|id\b|ref|code|amount|total|"
                          r"period|terms|due|[:/(])", re.I)
_TYPE_RES = [(k, re.compile(r"(?<![a-z])(?:" + p + r")(?![a-z])", re.I)) for k, p in DOC_TYPES]
_STRONG = re.compile(r"(?<![a-z])(credit\s*note|credit\s*memo|credit\s*request|debit\s*note)(?![a-z])", re.I)
# Wording that says the money has already moved: paying this again would be a double payment.
_PAID = re.compile(r"thank\s*you\s*for\s*your\s*payment|payment\s*received|paid\s*in\s*full|payment\s*receipt"
                   r"|amount\s*received", re.I)
_INVOICE_LABEL = re.compile(r"(?<![a-z])(?:invoice|inv)\.?\s*(?:#|no\b|no\.|number|date)", re.I)


_NEGATED = re.compile(r"\bnot\s+(?:an?\s+)?$", re.I)


def _title_in(line: str):
    """The document type a line names as a title, ignoring uses as a field label ('Estimate #: 12')."""
    for kind, rx in _TYPE_RES:
        for m in rx.finditer(line):
            if not _LABEL_AFTER.match(line[m.end():]) and not _NEGATED.search(line[:m.start()]):
                return kind, m.group(0)
    return None


def to_float(s: str) -> float | None:
    s = re.sub(r"^" + CUR, "", s.strip()).strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(s: str) -> str | None:
    for pat, fn in DATE_PATTERNS:
        for m in pat.finditer(s):
            for y, mo, d in fn(m):
                try:
                    return date(y, mo, d).isoformat()
                except ValueError:
                    continue
    return None


_CURRENCY_MARKS = [("INR", re.compile(r"₹|\bRs\.?\s?\d|\bINR\b|\brupees\b", re.I)),
                   ("USD", re.compile(r"US\$|\$\s?\d|\bUSD\b")),
                   ("EUR", re.compile(r"€|\bEUR\b")), ("GBP", re.compile(r"£|\bGBP\b")),
                   ("JPY", re.compile(r"¥|\bJPY\b|\byen\b", re.I))]


def detect_currency(text: str) -> tuple[str | None, str]:
    """The invoice currency when the document shows one: rupees win whenever they appear."""
    counts = {}
    for code, rx in _CURRENCY_MARKS:
        n = len(rx.findall(text))
        if n:
            counts[code] = n
    if not counts or "INR" in counts:
        return ("INR" if counts else None), ""
    code = max(counts, key=counts.get)
    return code, f"{counts[code]} amounts marked {code}"


def classify_ex(text: str) -> tuple[str, float, str]:
    """(kind, confidence, evidence). Titles near the top are strong; label use is not evidence."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = lines[:15]
    for ln in head:
        m = _STRONG.search(ln)
        if m and not _NEGATED.search(ln[:m.start()]):  # "THIS IS NOT A CREDIT MEMO"
            return ("debit_note" if "debit" in m.group(1).lower() else "credit_note"), 0.95, ln[:80]
    for ln in head:
        if _PAID.search(ln):
            return "receipt", 0.95, ln[:80]
    for ln in head:
        t = _title_in(ln)
        if t:
            return t[0], 0.95, ln[:80]
    # No title near the top: an invoice-number or invoice-date label is good evidence of an invoice.
    for ln in lines:
        if _INVOICE_LABEL.search(ln):
            return "invoice", 0.7, ln[:80]
    for ln in lines[15:]:
        t = _title_in(ln) if len(ln) <= 40 else None  # a title further down stands alone, not mid-sentence
        if t:
            return t[0], 0.7, ln[:80]
    return "unknown", 0.3, ""


def classify(text: str) -> tuple[str, float]:
    kind, conf, _ = classify_ex(text)
    return kind, conf


_NOT_NAME = re.compile(r"\bpage\s*\d+\s*of\b|\bremit|\bbill\s*to\b|\bship\s*to\b|\bdate\b|\breport\b|\bform\b"
                       r"|#|\bphone\b|\bfax\b|\bstation\s*:|^[^a-z]*$", re.I)


def _looks_like_name(s: str) -> bool:
    if not s or len(s) > 70 or _NOT_NAME.search(s):
        return False
    if re.search(r":\s*\S", s) or _title_in(s) or _PAID.search(s):
        return False
    letters = sum(ch.isalpha() for ch in s)
    return letters >= 3 and letters >= 0.5 * len(s.replace(" ", ""))


def _find_after(lines, patterns, value_re, skip=None):
    """First value matching value_re that follows any label pattern on the same line."""
    for li, ln in enumerate(lines):
        for p in patterns:
            for m in re.finditer(p, ln, re.I):
                if skip and skip(ln, m):
                    continue
                rest = ln[m.end():]
                v = re.search(value_re, rest)
                if v:
                    return v.group(1) if v.groups() else v.group(0), ln.strip()
    return None, ""


def _money_after(lines, patterns):
    for ln in lines:
        for p in patterns:
            m = re.search(p, ln, re.I)
            if m:
                vals = [to_float(x) for x in MONEY.findall(ln[m.end():])]
                vals = [v for v in vals if v is not None]
                if vals:
                    return vals[-1], ln.strip()
    return None, ""


_PUNCT = ":;,.|~'\"‘’“”_-—=«»"


def _clean_tok(t: str) -> str:
    """OCR leaves punctuation stuck to numbers ('4,200.00:', '42,000.00.')."""
    c = t.strip(_PUNCT)
    return c if c and (c[0].isdigit() or c.startswith(("Rs", "INR", "₹"))) else t


def _parse_line_row(row: str) -> Line | None:
    toks = [_clean_tok(t) for t in row.split()]
    toks = [t for t in toks if t.strip(_PUNCT)]
    nums = []  # (index, value, raw)
    for i, t in enumerate(toks):
        m = NUMBER.match(t)
        if m:
            nums.append((i, to_float(t), t))
    if len(nums) < 3:
        return None
    # amount = last money-looking token
    money_idx = [k for k, (_, _, raw) in enumerate(nums) if "." in raw]
    if not money_idx:
        return None
    best = None
    for ka in reversed(money_idx):
        ia, amount, _ = nums[ka]
        for (kq, kr) in permutations(range(len(nums)), 2):
            if ka in (kq, kr):
                continue
            iq, q, rq = nums[kq]
            ir, r, rr = nums[kr]
            if q <= 0 or r <= 0 or abs(q * r - amount) > max(0.011, amount * 1e-6):
                continue
            score = (1 if "." in rr else 0) + (1 if iq > 0 else 0) + (0.5 if iq < ir else 0) + ia / 100
            if best is None or score > best[0]:
                best = (score, iq, q, ir, r, ia, amount)
        if best:
            break
    if not best:
        return None
    _, iq, q, ir, r, ia, amount = best
    used = {iq, ir, ia}
    # HSN: a 4-8 digit integer in the numeric columns. Prefer one right of the
    # quantity (a trailing HSN column); else the one nearest the quantity on its
    # left, so a number inside the description ("1000 ul") is not taken.
    hc = [i for i, v, raw in nums if i not in used and re.fullmatch(r"\d{4,8}", raw)]
    right = [i for i in hc if i > iq]
    left = [i for i in hc if i < iq]
    hsn_i = right[0] if right else (left[-1] if left else None)
    hsn = toks[hsn_i] if hsn_i is not None else None
    first_num = min(i for i in used | ({hsn_i} if hsn_i is not None and hsn_i < iq else set()))
    desc_toks = toks[:first_num]
    if desc_toks and re.fullmatch(r"\d{1,3}", desc_toks[0]):
        desc_toks = desc_toks[1:]  # serial number
    uom = next((toks[i] for i in range(min(used), len(toks))
                if i not in used and i != hsn_i and re.fullmatch(r"[A-Za-z]{2,6}", toks[i])), None)
    desc = " ".join(desc_toks).strip()
    if not desc:
        return None
    return Line(description=desc, qty=q, unit_price=r, amount=amount, hsn=hsn, uom=uom)


def parse(text: str, route: str, base_conf: float = 0.95, own_gstin: str | None = None) -> Extraction:
    lines = [l for l in text.splitlines() if l.strip()]
    ex = Extraction(route=route, raw_text=text)
    F = ex.fields

    kind, kc, kev = classify_ex(text)
    F["doc_type"] = Field(kind, kc, kev)
    cur, cev = detect_currency(text)
    F["currency"] = Field(cur, base_conf if cur else 0.0, cev)
    # Vendor name: the first line near the top that reads like a name, minus a document title
    # on the same line. Page markers, remit-to lines, labels and titles are not names.
    pick, conf = None, 0.0
    for i, ln in enumerate(lines[:6]):
        name = re.sub(r"\b(tax\s*invoice|invoice|quotation|credit\s*note|proforma)\b.*$", "", ln, flags=re.I).strip()
        if _looks_like_name(name):
            pick, conf = name, base_conf * (0.9 if i == 0 else 0.75)
            break
    if lines:
        F["vendor_name"] = Field(pick, conf, lines[0].strip())

    v, ev = _find_after(lines, LABELS["invoice_number"], DOCNO,
                        skip=lambda ln, m: bool(re.match(r"\s*date", ln[m.end():], re.I)))
    F["invoice_number"] = Field(v, base_conf if v else 0.0, ev)
    # Several different invoice numbers in one file = invoices bundled together.
    nums = set()
    for ln in lines:
        for pat in LABELS["invoice_number"]:
            for m in re.finditer(pat, ln, re.I):
                if re.match(r"\s*date", ln[m.end():], re.I):
                    continue
                vm = re.search(DOCNO, ln[m.end():])
                if vm:
                    nums.add(vm.group(1))
    if len(nums) > 1:
        ex.notes.append(f"contains {len(nums)} different invoice numbers: {', '.join(sorted(nums)[:5])}")
    v, ev = _find_after(lines, LABELS["invoice_date"], r"(.+)")
    F["invoice_date"] = Field(parse_date(v) if v else None, base_conf if v and parse_date(v) else 0.0, ev)
    v, ev = _find_after(lines, LABELS["po_reference"], DOCNO)
    F["po_reference"] = Field(v, base_conf if v else 0.0, ev)

    # GSTINs: by label first, then by position (first on the page = seller).
    all_g = []
    for ln in lines:
        all_g += GSTIN_TOKEN.findall(ln.upper())
    bg, evb = _find_after(lines, LABELS["buyer_gstin"], GSTIN_TOKEN.pattern)
    buyer_ctx = re.compile(r"(buyer|customer|bill\s*to|consignee)", re.I)
    vg, evv = _find_after(lines, LABELS["vendor_gstin"], GSTIN_TOKEN.pattern,
                          skip=lambda ln, m: bool(buyer_ctx.search(ln[max(0, m.start() - 16):m.end()])))
    # Fallbacks by position. The buyer is always us, so our own GSTIN is never the vendor's.
    from ..gstin import repair
    norm = lambda g: repair(g)[0]
    if not vg:
        cand = [g for g in all_g if g != bg and norm(g) != own_gstin]
        if cand:
            vg, evv = cand[0], "first GSTIN on page that is not ours"
    if not bg:
        rest = [g for g in all_g if g != vg]
        ours = [g for g in rest if norm(g) == own_gstin]
        bg, evb = ((ours or rest)[0], "GSTIN on page matching ours" if ours else "second GSTIN on page") if rest else (None, "")
    F["vendor_gstin"] = Field(vg, base_conf if vg else 0.0, evv)
    F["buyer_gstin"] = Field(bg, base_conf if bg else 0.0, evb)

    for k in ("subtotal", "cgst", "sgst", "igst", "total"):
        v, ev = _money_after(lines, LABELS[k])
        F[k] = Field(v, base_conf if v is not None else 0.0, ev)

    v, ev = _find_after(lines, LABELS["bank_account"], r"(\d[\d ]{7,20}\d)")
    v = v.replace(" ", "") if v else v
    F["bank_account"] = Field(v if v and 9 <= len(v) <= 18 else None, base_conf if v else 0.0, ev)
    v, ev = _find_after(lines, LABELS["ifsc"], r"([A-Z]{4}0[A-Z0-9]{6})")
    F["ifsc"] = Field(v, base_conf if v else 0.0, ev)

    # Line items: rows between the table header and the first totals label.
    # The header row is a hint only (OCR often mangles it); a row is accepted
    # only if its own numbers reconcile, which keeps address lines out.
    start = next((i for i, l in enumerate(lines) if len(TABLE_HEAD.findall(l)) >= 2), -1)
    end_pats = re.compile("|".join(LABELS["subtotal"] + LABELS["total"]), re.I)
    for l in lines[start + 1:]:
        if end_pats.search(l):
            break
        li = _parse_line_row(l)
        if li:
            li.confidence = base_conf
            ex.lines.append(li)
    if not ex.lines:
        ex.notes.append("no line items recognised")
    return ex
