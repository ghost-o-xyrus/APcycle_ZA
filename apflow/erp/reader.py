"""Reading a purchase order or a goods receipt document into a draft ERP record.

The same reading stack as invoices: the PDF's text when it has one, OCR for scans, and the
vision model when one is configured. The result is always a *draft*: a person reviews and
corrects it before the ERP stores it, as when Stores or Purchasing key a document into SAP.
"""
from __future__ import annotations

import difflib
import json
import re

from ..company import ENTITIES
from ..extract.parser import GSTIN_TOKEN, detect_currency, parse_date, to_float
from ..extract.routes import LazyPages, _deskew, _ocr_page, page_images, text_layer
from ..gstin import is_valid
from ..taxid import TOKEN as TAXID, clean, country_of

PO_PROMPT = """You are reading one PURCHASE ORDER (issued by a buyer to a supplier).
Return ONLY a JSON object (null when absent, never guess):
po_number, po_date (YYYY-MM-DD), buyer_name, vendor_name, vendor_gstin, vendor_tax_id (EIN or VAT ID),
currency (ISO code), kind (goods|services), price_basis (excl_tax|incl_tax: are rates inclusive of tax?),
lines: [{line_no, description, hsn, uom, qty, unit_price, tax_rate (percent number)}]."""

RECEIPT_PROMPT = """You are reading one GOODS RECEIPT (goods receipt note, receiving report, delivery acceptance).
Return ONLY a JSON object (null when absent, never guess):
receipt_no, receipt_date (YYYY-MM-DD), po_number, received_by,
lines: [{line_no (the PO line number if shown), description, ordered, received, accepted}]."""

OURS = {clean(e.tax_id) for e in ENTITIES.values()}


def document_text(pdf_path: str) -> tuple[str, str]:
    """(text, how it was read)."""
    t = text_layer(pdf_path, max_pages=3)
    if len(t.strip()) >= 80:
        return t, "PDF text"
    out = []
    for im in page_images(pdf_path, last_page=2):
        txt, _ = _ocr_page(_deskew(im).convert("L"))
        out.append(txt)
    return "\n".join(out), "OCR"


def _after(text: str, labels, value=r"([A-Z0-9][A-Za-z0-9/\-]{3,})"):
    for ln in text.splitlines():
        for lab in labels:
            m = re.search(lab + r"\s*" + value, ln, re.I)
            if m:
                return m.group(1).strip()
    return None


def _num(s):
    v = to_float(s) if s is not None else None
    return v


def _from_model(llm, pdf_path, prompt):
    if llm is None:
        return None
    try:
        raw = llm.complete_json(prompt, LazyPages(pdf_path))
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except Exception:  # a model failure falls back to reading the text
        return None


def _match_vendor(vendors, name, gstin, tax_id):
    for v in vendors:
        if gstin and v["gstin"] == gstin:
            return v["vendor_id"]
        if tax_id and clean(v["tax_id"]) == clean(tax_id):
            return v["vendor_id"]
    if name:
        best = max(vendors, key=lambda v: difflib.SequenceMatcher(None, v["name"].lower(), name.lower()).ratio(),
                   default=None)
        if best and difflib.SequenceMatcher(None, best["name"].lower(), name.lower()).ratio() >= 0.85:
            return best["vendor_id"]
    return None


def _entity_from(text):
    for e in ENTITIES.values():
        if clean(e.tax_id) in clean(text) or e.name.lower() in text.lower():
            return e.code
    return None


# ---------------- purchase orders ----------------

PO_ROW = re.compile(r"^\s*(\d{1,3})\s+(.+?)\s+(?:(\d{4,8})\s+)?([\d,]+(?:\.\d+)?)\s+([A-Za-z]{1,6})\s+"
                    r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})(?:\s+(\d{1,2}(?:\.\d+)?)\s*%)?\s*$")


def read_po(pdf_path: str, vendors: list, llm=None) -> dict:
    d = _from_model(llm, pdf_path, PO_PROMPT)
    if d:
        how = f"vision model ({getattr(llm, 'name', 'model')})"
        lines = [{"line_no": l.get("line_no") or i, "description": l.get("description") or "", "hsn": l.get("hsn") or "",
                  "uom": l.get("uom") or "pcs", "qty_ordered": l.get("qty"), "unit_price": l.get("unit_price"),
                  "tax_rate": l.get("tax_rate") or 0} for i, l in enumerate(d.get("lines") or [], 1)]
        text = json.dumps(d)
        vg, vt, name = d.get("vendor_gstin"), d.get("vendor_tax_id"), d.get("vendor_name")
        draft = {"po_number": d.get("po_number"), "po_date": d.get("po_date"), "currency": d.get("currency"),
                 "kind": d.get("kind") or "goods", "price_basis": d.get("price_basis") or "excl_tax", "lines": lines}
    else:
        text, how = document_text(pdf_path)
        draft = {"po_number": _after(text, [r"p\.?\s?o\.?\s*(?:no\.?|number|#)\s*[:\-]?", r"purchase\s*order\s*(?:no\.?|number|#)\s*[:\-]?",
                                            r"order\s*(?:no\.?|number)\s*[:\-]?", r"contract\s*no\.?\s*[:\-]?"]),
                 "po_date": None, "currency": None, "kind": "goods", "price_basis": "excl_tax", "lines": []}
        for ln in text.splitlines():
            if re.search(r"(po|order)\s*date|date\s*:", ln, re.I):
                draft["po_date"] = parse_date(ln)
                if draft["po_date"]:
                    break
        m = re.search(r"currency\s*:\s*([A-Z]{3})", text, re.I)
        draft["currency"] = m.group(1).upper() if m else (detect_currency(text)[0] or None)
        if re.search(r"inclusive\s+of\s+(tax|gst|all)", text, re.I):
            draft["price_basis"] = "incl_tax"
        if re.search(r"kind:\s*services", text, re.I):
            draft["kind"] = "services"
        for ln in text.splitlines():
            m = PO_ROW.match(ln)
            if m:
                q, r, amt = _num(m.group(4)), _num(m.group(6)), _num(m.group(7))
                if q and r is not None and amt is not None and abs(q * r - amt) <= max(1.0, 0.005 * amt):
                    draft["lines"].append({"line_no": int(m.group(1)), "description": m.group(2).strip(),
                                           "hsn": m.group(3) or "", "uom": m.group(5), "qty_ordered": q, "unit_price": r,
                                           "tax_rate": float(m.group(8)) if m.group(8) else None})
        sup = re.search(r"(?:supplier|vendor|seller)\s*(?:name)?\s*:\s*(.+)", text, re.I)
        name = re.split(r"\s{2,}|\(", sup.group(1))[0].strip() if sup else None
        gst = [g for g in GSTIN_TOKEN.findall(text.upper()) if is_valid(g) and clean(g) not in OURS]
        vg = gst[0] if gst else None
        tids = [t.replace(" ", "") for t in TAXID.findall(text) if clean(t) not in OURS and country_of(t) != "IN"]
        vt = tids[0] if tids else None
    draft["entity"] = _entity_from(text) or "IN"
    if not draft.get("currency"):
        draft["currency"] = ENTITIES[draft["entity"]].currency
    draft["vendor"] = {"match": _match_vendor(vendors, name, vg, vt), "name": name, "gstin": vg, "tax_id": vt,
                       "country": "IN" if vg else (country_of(vt) or None)}
    draft["read_by"] = how
    draft["notes"] = [] if draft["lines"] else ["No order lines could be read; add them by hand."]
    return draft


# ---------------- goods receipts ----------------

RECEIPT_ROW = re.compile(r"^\s*(\d{1,3})\s+(.+?)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)"
                         r"(?:\s+([\d,]+(?:\.\d+)?))?\s*$")


def read_receipt(pdf_path: str, llm=None) -> dict:
    d = _from_model(llm, pdf_path, RECEIPT_PROMPT)
    if d:
        how = f"vision model ({getattr(llm, 'name', 'model')})"
        lines = [{"line_no": l.get("line_no"), "description": l.get("description"), "received": l.get("received"),
                  "accepted": l.get("accepted", l.get("received"))} for l in d.get("lines") or []]
        return {"receipt_no": d.get("receipt_no"), "receipt_date": d.get("receipt_date"), "po_number": d.get("po_number"),
                "received_by": d.get("received_by"), "lines": lines, "read_by": how, "notes": []}
    text, how = document_text(pdf_path)
    draft = {"receipt_no": _after(text, [r"grn\s*(?:no\.?|number|#)\s*[:\-]?", r"receipt\s*(?:no\.?|number|#)\s*[:\-]?",
                                         r"receiving\s*report\s*(?:no\.?|#)\s*[:\-]?", r"delivery\s*note\s*no\.?\s*[:\-]?"]),
             "po_number": _after(text, [r"(?:against\s*)?p\.?\s?o\.?\s*(?:no\.?|number|#)\s*[:\-]?",
                                        r"order\s*(?:no\.?|number)\s*[:\-]?"]),
             "receipt_date": None, "received_by": None, "lines": [], "read_by": how, "notes": []}
    for ln in text.splitlines():
        if re.search(r"(receipt|received|grn)\s*date|date\s*:", ln, re.I):
            draft["receipt_date"] = parse_date(ln)
            if draft["receipt_date"]:
                break
    m = re.search(r"(?:received\s*(?:and\s*inspected\s*)?by|inspected\s*by)\s*:\s*(.+)", text, re.I)
    if m:
        draft["received_by"] = m.group(1).strip()[:60]
    for ln in text.splitlines():
        m = RECEIPT_ROW.match(ln)
        if m:
            ordered, rec, acc = _num(m.group(3)), _num(m.group(4)), _num(m.group(5))
            if rec is not None and acc is not None and acc <= rec:
                draft["lines"].append({"line_no": int(m.group(1)), "description": m.group(2).strip(),
                                       "ordered": ordered, "received": rec, "accepted": acc})
    if not draft["lines"]:
        draft["notes"].append("No received quantities could be read; enter them against the PO lines.")
    return draft
