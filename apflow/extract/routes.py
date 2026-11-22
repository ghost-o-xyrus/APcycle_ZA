"""Extraction routes and the router that picks between them.

text  - PDF has a text layer: pdfplumber text -> parser.
ocr   - image-only PDF: rasterise, Tesseract (with orientation fix) -> parser,
        confidence scaled by Tesseract's own word confidence.
llm   - vision model (Gemini / Claude) returning JSON in the same schema.
        The client is injected, so tests use a recorded fake and the app uses a
        real one. Escalation happens only when the cheap route is not good enough.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Protocol

import pdfplumber
import pytesseract
from PIL import Image

from ..config import SETTINGS
from ..schema import Extraction, Field, Line, HEADER_FIELDS
from .parser import parse


MAX_TEXT_PAGES = 5  # invoices are short; a 40-page register took 14 s locally (minutes on a small host)
MAX_OCR_PAGES = 3


def page_count(pdf_path: str) -> int:
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def unreadable_reason(pdf_path: str) -> str | None:
    """Why a PDF cannot be processed at all (None if it opens and has pages)."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
    except Exception as e:  # pdfminer wraps the real cause; look at its text
        cause = f"{type(e).__name__} {e} {getattr(e, '__cause__', '')!r} {e.args!r}".lower()
        if "password" in cause or "encrypt" in cause:
            return "The PDF is password-protected"
        return "The PDF is damaged and cannot be opened"
    if n == 0:
        return "The PDF has no pages"
    return None


def text_layer(pdf_path: str, max_pages: int = MAX_TEXT_PAGES) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        # dedupe_chars: some PDFs fake bold by printing each letter twice ("AAmmeerriiccaa").
        return "\n".join((p.dedupe_chars().extract_text() or "") for p in pdf.pages[:max_pages])


def page_images(pdf_path: str, dpi: int = 300, last_page: int | None = None) -> list[Image.Image]:
    with tempfile.TemporaryDirectory() as td:
        cmd = ["pdftoppm", "-r", str(dpi), "-png"] + (["-l", str(last_page)] if last_page else [])
        subprocess.run(cmd + [pdf_path, os.path.join(td, "p")], check=True, capture_output=True, timeout=120)
        files = sorted(f for f in os.listdir(td) if f.endswith(".png"))
        return [Image.open(os.path.join(td, f)).copy() for f in files]


def _deskew(im: Image.Image) -> Image.Image:
    """Pick the small rotation that maximises horizontal text-line contrast."""
    import numpy as np
    g = im.convert("L").resize((im.width // 3, im.height // 3))
    best, best_angle = -1.0, 0.0
    for a10 in range(-20, 21, 2):
        a = a10 / 10
        arr = np.asarray(g.rotate(a, expand=False, fillcolor=255)) < 128
        rows = arr.sum(axis=1).astype(float)
        score = float(np.sum(np.diff(rows) ** 2))
        if score > best:
            best, best_angle = score, a
    return im.rotate(best_angle, expand=True, fillcolor=255) if best_angle else im


def postprocess(ex: Extraction, settings=SETTINGS) -> Extraction:
    """Repair what can be proven, and raise confidence where fields verify each other.

    - A GSTIN with a valid checksum verifies itself; a GSTIN one OCR swap away
      from exactly one valid value is repaired (and the repair is recorded).
    - Amounts that reconcile (lines -> subtotal, subtotal + tax -> total) are
      very unlikely to be misread together, so they earn confidence 0.9.
    """
    from ..gstin import repair, is_valid
    for k in ("vendor_gstin", "buyer_gstin"):
        f = ex.fields.get(k)
        if f and f.present:
            fixed, did = repair(f.value)
            if did:
                ex.notes.append(f"{k} repaired {f.value} -> {fixed} (checksum)")
                f.value, f.evidence = fixed, f.evidence + " [repaired]"
            if is_valid(f.value):
                f.confidence = max(f.confidence, 0.9)
    sub, tot = ex.get("subtotal"), ex.get("total")
    tax = sum(ex.get(k, 0.0) or 0.0 for k in ("cgst", "sgst", "igst", "tax"))
    lines_ok = bool(ex.lines) and sub is not None and abs(sum(l.amount for l in ex.lines) - sub) <= settings.money_abs_tol
    totals_ok = sub is not None and tot is not None and abs(sub + tax - tot) <= settings.money_abs_tol
    if totals_ok:
        for k in ("subtotal", "cgst", "sgst", "igst", "total"):
            if ex.fields.get(k) and ex.fields[k].present:
                ex.fields[k].confidence = max(ex.fields[k].confidence, 0.9)
    if lines_ok:
        for l in ex.lines:
            l.confidence = max(l.confidence, 0.9)
    return ex


def extract_text_route(pdf_path: str) -> Extraction:
    return postprocess(parse(text_layer(pdf_path), "text", base_conf=0.95, own_gstin=SETTINGS.company_gstin))


def _ocr_page(im: Image.Image) -> tuple[str, list[float]]:
    from PIL import ImageFilter
    d = pytesseract.image_to_data(im, config="--psm 6", output_type=pytesseract.Output.DICT)
    confs = [float(c) for w, c in zip(d["text"], d["conf"]) if w.strip() and float(c) >= 0]
    return pytesseract.image_to_string(im, config="--psm 6"), confs


def extract_ocr_route(pdf_path: str) -> Extraction:
    from PIL import ImageFilter
    texts, confs = [], []
    for im in page_images(pdf_path, last_page=MAX_OCR_PAGES):
        im = _deskew(im).convert("L").filter(ImageFilter.MedianFilter(3))  # removes scanner speckle
        t, c = _ocr_page(im)
        texts.append(t)
        confs += c
    text = "\n".join(texts)
    ocr_conf = (sum(confs) / len(confs) / 100) if confs else 0.0
    ex = parse(text, "ocr", base_conf=round(min(0.85, ocr_conf), 3), own_gstin=SETTINGS.company_gstin)
    ex.notes.append(f"tesseract mean word confidence {ocr_conf:.2f}")
    return postprocess(ex)


# ---------------- LLM vision route ----------------

EXTRA_FIELDS = ("currency", "tax", "vendor_tax_id", "buyer_tax_id", "buyer_name")
NON_INVOICE = ("quotation", "credit_note", "purchase_order", "delivery_challan", "receipt")

LLM_PROMPT = """You are reading one supplier invoice for an accounts payable team (India, US or Europe).
Return ONLY a JSON object with these keys (null when absent, never guess):
doc_type (invoice|credit_note|debit_note|quotation|purchase_order|receipt|delivery_challan|unknown); judge by the document's title, not by fields such as \"Estimate #\" or \"PO Number\",
vendor_name, vendor_gstin, buyer_gstin, invoice_number, invoice_date (YYYY-MM-DD; a numeric date like 06/11/2026 is day/month/year on Indian and European invoices, month/day/year only on US ones),
po_reference, subtotal, cgst, sgst, igst, total, bank_account, ifsc,
currency (ISO code of the amounts as printed, e.g. INR, USD, EUR),
tax (VAT or sales tax amount when the invoice is not under Indian GST, else 0),
vendor_tax_id and buyer_tax_id (EIN or VAT ID when not an Indian GSTIN), buyer_name (the bill-to company),
lines: [{description, hsn, qty, uom, unit_price, amount}],
confidence: {<field>: 0..1 for each header field}.
Amounts are numbers without currency symbols or commas, pre-tax unless the key is total.
bank_account is the account number or IBAN; ifsc is the IFSC, BIC or US routing number."""


class VisionClient(Protocol):
    def complete_json(self, prompt: str, images: list[Image.Image]) -> str: ...


def extraction_from_llm_json(raw: str) -> Extraction:
    """Parse the model's JSON into the shared schema (then postprocess like any route)."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    conf = data.get("confidence") or {}
    ex = Extraction(route="llm")
    for k in HEADER_FIELDS:
        v = data.get(k)
        c = float(conf.get(k, 0.85 if v not in (None, "") else 0.0))
        ex.fields[k] = Field(v, min(c, 0.9), "vision model")  # self-reported confidence is capped
    for k in EXTRA_FIELDS:  # outside the confidence average: absent on most Indian invoices
        v = data.get(k)
        if k == "currency" and v:
            v = str(v).upper()[:3]
        if k == "tax":
            try:
                v = float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                v = None
        ex.fields[k] = Field(v, 0.85 if v not in (None, "") else 0.0, "vision model")
    for l in data.get("lines") or []:
        try:
            ex.lines.append(Line(description=str(l.get("description", "")), qty=float(l["qty"]),
                                 unit_price=float(l["unit_price"]), amount=float(l["amount"]),
                                 hsn=(str(l["hsn"]) if l.get("hsn") else None), uom=l.get("uom"), confidence=0.85))
        except (KeyError, TypeError, ValueError):
            ex.notes.append(f"dropped unreadable line from model: {l}")
    return postprocess(ex)


class LazyPages:
    """Page images rendered only when first used; `key` identifies the file so a cache hit skips rendering."""

    def __init__(self, pdf_path: str, dpi: int = 200, max_pages: int = 2):
        import hashlib
        self.pdf_path, self.dpi, self.max_pages = pdf_path, dpi, max_pages
        with open(pdf_path, "rb") as f:
            self.key = f"{hashlib.sha256(f.read()).hexdigest()}:{dpi}:{max_pages}"
        self._pages = None

    def _load(self):
        if self._pages is None:
            self._pages = page_images(self.pdf_path, dpi=self.dpi, last_page=self.max_pages)
        return self._pages

    def __getitem__(self, i):
        return self._load()[i]

    def __iter__(self):
        return iter(self._load())

    def __len__(self):
        return len(self._load())


def extract_llm_route(pdf_path: str, client: VisionClient) -> Extraction:
    return extraction_from_llm_json(client.complete_json(LLM_PROMPT, LazyPages(pdf_path)))


# ---------------- router ----------------

def needs_escalation(ex: Extraction, settings=SETTINGS) -> list[str]:
    why = []
    missing = [f for f in settings.required_fields if not (ex.fields.get(f) and ex.fields[f].present)]
    if missing:
        why.append("missing " + ", ".join(missing))
    if not ex.lines:
        why.append("no line items")
    sub, tot = ex.get("subtotal"), ex.get("total")
    tax = sum(ex.get(k, 0.0) or 0.0 for k in ("cgst", "sgst", "igst", "tax"))
    if sub is not None and tot is not None and abs(sub + tax - tot) > settings.money_abs_tol:
        why.append("totals do not reconcile")
    if ex.lines and sub is not None and abs(sum(l.amount for l in ex.lines) - sub) > settings.money_abs_tol:
        why.append("line amounts do not sum to subtotal")
    if ex.mean_confidence() < settings.escalate_below_confidence:
        why.append(f"mean confidence {ex.mean_confidence():.2f}")
    return why


def extract(pdf_path: str, llm: VisionClient | None = None, settings=SETTINGS) -> tuple[Extraction, list[dict]]:
    """Returns the chosen extraction and a log of the routes tried."""
    log = []
    pages = page_count(pdf_path)
    txt = text_layer(pdf_path)
    if len(txt.strip()) >= settings.text_layer_min_chars:
        ex = postprocess(parse(txt, "text", base_conf=0.95, own_gstin=settings.company_gstin))
        log.append({"route": "text", "chars": len(txt), "pages": pages})
        if pages > MAX_TEXT_PAGES:
            ex.notes.append(f"read the first {MAX_TEXT_PAGES} of {pages} pages")
    else:
        # Scans: when a model is configured, ask it first. Our evaluation sent 100% of
        # OCR-read scans on to the model anyway, and OCR is slow on small hosts
        # (0.1 CPU). OCR stays as the fallback when the model fails.
        if llm is not None and settings.scan_model_first:
            try:
                ex = extract_llm_route(pdf_path, llm)
                log.append({"route": "llm", "chars": len(txt), "issues": needs_escalation(ex, settings)})
                return ex, log
            except Exception as e:  # model down or bad answer: fall back to OCR, say so
                log.append({"route": "llm", "error": str(e)[:600]})
            ex = extract_ocr_route(pdf_path)
            log.append({"route": "ocr", "chars": len(txt), "note": ex.notes[-1],
                        "issues": needs_escalation(ex, settings)})
            return ex, log
        ex = extract_ocr_route(pdf_path)
        log.append({"route": "ocr", "chars": len(txt), "note": ex.notes[-1], "pages": pages})
        if pages > MAX_OCR_PAGES:
            ex.notes.append(f"OCR read the first {MAX_OCR_PAGES} of {pages} pages")
    why = needs_escalation(ex, settings)
    log[-1]["issues"] = why
    settled_non_invoice = (ex.get("doc_type") in NON_INVOICE and ex.conf("doc_type") >= 0.9 and ex.route != "ocr")
    if why and not settled_non_invoice:  # a shaky OCR guess that it is not an invoice still goes to the model
        if llm is not None:
            try:
                ex2 = extract_llm_route(pdf_path, llm)
                why2 = needs_escalation(ex2, settings)
                log.append({"route": "llm", "issues": why2})
                # A text layer is more reliable than any model reading of it: on
                # text PDFs the model's answer wins only if strictly better.
                if len(why2) < len(why) or (ex.route != "text" and len(why2) <= len(why)):
                    ex = ex2
            except Exception as e:  # model down or bad JSON: keep the cheap result, say so
                log.append({"route": "llm", "error": str(e)[:600]})
        else:
            log.append({"route": "llm", "skipped": "no model configured"})
    return ex, log
