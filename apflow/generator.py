"""Invoice PDF generator with ground truth.

Every invoice is described by an `InvoiceSpec`; `truth(spec)` gives the exact
values a perfect extractor should return, and `render(spec, path)` draws it in
one of four layouts. Layout D is a held-out layout: the parser was not written
against its labels, so its accuracy measures how well rules generalise.
"""
from __future__ import annotations

import io
import json
import os
import random
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import date

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from PIL import Image, ImageFilter

from .company import Vendor
from .config import SETTINGS


@dataclass
class SpecLine:
    description: str
    hsn: str
    uom: str
    qty: float
    unit_price: float  # pre-tax
    gst_rate: float


@dataclass
class InvoiceSpec:
    vendor: Vendor
    invoice_number: str | None
    invoice_date: str  # ISO
    po_reference: str | None
    lines: list[SpecLine]
    buyer_gstin: str = SETTINGS.company_gstin
    buyer_name: str = SETTINGS.company_name
    layout: str = "A"
    scanned: bool = False
    title: str = "TAX INVOICE"
    tax_mode: str = "auto"  # auto | cgst_sgst | igst
    bank_account: str | None = None  # None -> vendor master value
    ifsc: str | None = None
    total_override: float | None = None  # inject an arithmetic error
    seed: int = 0


def money(x: float) -> float:
    return round(x + 1e-9, 2)


def truth(spec: InvoiceSpec) -> dict:
    lines = []
    for l in spec.lines:
        lines.append({"description": l.description, "hsn": l.hsn, "uom": l.uom, "qty": l.qty,
                      "unit_price": l.unit_price, "amount": money(l.qty * l.unit_price), "gst_rate": l.gst_rate})
    subtotal = money(sum(l["amount"] for l in lines))
    tax = money(sum(l["amount"] * l["gst_rate"] / 100 for l in lines))
    intra = spec.vendor.state == spec.buyer_gstin[:2]
    mode = spec.tax_mode if spec.tax_mode != "auto" else ("cgst_sgst" if intra else "igst")
    cgst = sgst = igst = 0.0
    if mode == "cgst_sgst":
        cgst = money(tax / 2)
        sgst = money(tax - cgst)
    else:
        igst = tax
    total = spec.total_override if spec.total_override is not None else money(subtotal + tax)
    return {
        "doc_type": "invoice" if "INVOICE" in spec.title.upper() else spec.title.lower(),
        "vendor_name": spec.vendor.name,
        "vendor_gstin": spec.vendor.gstin,
        "buyer_gstin": spec.buyer_gstin,
        "invoice_number": spec.invoice_number,
        "invoice_date": spec.invoice_date,
        "po_reference": spec.po_reference,
        "subtotal": subtotal, "cgst": cgst, "sgst": sgst, "igst": igst, "total": money(total),
        "bank_account": spec.bank_account or spec.vendor.bank_account,
        "ifsc": spec.ifsc or spec.vendor.ifsc,
        "lines": lines,
        "layout": spec.layout, "scanned": spec.scanned,
    }


# ---------- number and date formats per layout ----------

def inr(x: float) -> str:
    """Indian digit grouping: 1,47,500.00"""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(iso: str, layout: str) -> str:
    y, m, d = iso.split("-")
    if layout == "A":
        return f"{d}-{MONTHS[int(m) - 1]}-{y}"
    if layout == "B":
        return f"{d}/{m}/{y}"
    if layout == "C":
        return iso
    return f"{d}.{m}.{y}"


def fmt_qty(q: float) -> str:
    return str(int(q)) if float(q).is_integer() else f"{q:.2f}"


LABELS = {
    "A": dict(inv="Invoice No:", date="Invoice Date:", po="PO Number:", vg="GSTIN:", bg="Buyer GSTIN:",
              bill="Bill To:", head=["S.No", "Description", "HSN", "Qty", "Unit", "Rate", "Amount"],
              sub="Sub Total", cgst="CGST", sgst="SGST", igst="IGST", total="Grand Total",
              acct="Bank A/c No:", ifsc="IFSC:", num=inr),
    "B": dict(inv="Bill No.", date="Dated", po="Buyer's Order No.", vg="GSTIN/UIN:", bg="Buyer GSTIN/UIN:",
              bill="Buyer (Bill to)", head=["Sl", "Description of Goods", "HSN/SAC", "Quantity", "Rate", "per", "Amount"],
              sub="Taxable Value", cgst="Output CGST", sgst="Output SGST", igst="Output IGST", total="Total Amount",
              acct="A/c No.", ifsc="IFS Code", num=inr),
    "C": dict(inv="Invoice #", date="Date", po="Ref PO", vg="Supplier GSTIN", bg="Customer GSTIN",
              bill="Customer", head=["Item", "Qty", "Price", "Total", "HSN"],
              sub="Net Amount", cgst="CGST @", sgst="SGST @", igst="IGST @", total="Amount Payable",
              acct="Account Number", ifsc="IFSC Code", num=lambda x: f"{x:.2f}"),
    # Held-out layout: none of these labels were used to write the parser.
    "D": dict(inv="Document No.", date="Doc Date", po="Order Ref", vg="Registration No.", bg="Recipient Reg. No.",
              bill="Recipient", head=["Particulars", "Code", "Units", "Unit Cost", "Value"],
              sub="Assessable Value", cgst="Central Tax", sgst="State Tax", igst="Integrated Tax",
              total="Invoice Value", acct="Beneficiary A/c", ifsc="Branch Code", num=lambda x: "Rs." + inr(x)),
}


def _row_cells(layout: str, i: int, l: dict, num) -> list[str]:
    q, p, a = fmt_qty(l["qty"]), num(l["unit_price"]), num(l["amount"])
    if layout == "A":
        return [str(i), l["description"], l["hsn"], q, l["uom"], p, a]
    if layout == "B":
        return [str(i), l["description"], l["hsn"], q, p, l["uom"], a]
    if layout == "C":
        return [l["description"], q, p, a, l["hsn"]]
    return [l["description"], l["hsn"], q, p, a]


COLS = {  # x positions (points) per column
    "A": [40, 70, 300, 350, 390, 430, 500],
    "B": [40, 62, 300, 355, 405, 465, 500],
    "C": [40, 300, 345, 420, 510],
    "D": [40, 300, 350, 400, 480],
}


def render_pdf(spec: InvoiceSpec, path: str) -> dict:
    t = truth(spec)
    L = LABELS[spec.layout]
    num = L["num"]
    W, H = A4
    c = canvas.Canvas(path, pagesize=A4)
    c.setTitle(f"{spec.title} {spec.invoice_number or ''}")
    y = H - 50
    # Seller block (vendor name is always the first line of the page)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(40, y, spec.vendor.name)
    c.setFont("Helvetica", 9)
    c.drawString(40, y - 14, f"{L['vg']} {spec.vendor.gstin}")
    c.drawString(40, y - 26, f"State Code: {spec.vendor.state}")
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(W - 40, y, spec.title)
    c.setFont("Helvetica", 9)
    y -= 56
    # Header fields
    hdr = []
    if spec.invoice_number is not None:
        hdr.append(f"{L['inv']} {spec.invoice_number}")
    hdr.append(f"{L['date']} {fmt_date(spec.invoice_date, spec.layout)}")
    if spec.po_reference:
        hdr.append(f"{L['po']} {spec.po_reference}")
    for i, h in enumerate(hdr):
        c.drawString(330, y - 12 * i, h)
    c.drawString(40, y, f"{L['bill']} {spec.buyer_name}")
    c.drawString(40, y - 12, "Plot 14, KIADB Industrial Area, Bengaluru 560058")
    c.drawString(40, y - 24, f"{L['bg']} {spec.buyer_gstin}")
    y -= 60
    # Line table
    cols = COLS[spec.layout]
    c.setFont("Helvetica-Bold", 8.5)
    for x, h in zip(cols, L["head"]):
        c.drawString(x, y, h)
    c.line(40, y - 4, W - 40, y - 4)
    c.setFont("Helvetica", 8.5)
    y -= 18
    for i, l in enumerate(t["lines"], 1):
        for x, cell in zip(cols, _row_cells(spec.layout, i, l, num)):
            c.drawString(x, y, cell)
        y -= 14
    c.line(40, y + 6, W - 40, y + 6)
    y -= 14
    # Totals
    rows = [(L["sub"], t["subtotal"])]
    rates = sorted({l["gst_rate"] for l in t["lines"]})
    rate_txt = "/".join(f"{r:g}" for r in rates)
    if t["igst"]:
        rows.append((f"{L['igst']} {rate_txt}%", t["igst"]))
    else:
        half = "/".join(f"{r / 2:g}" for r in rates)
        rows.append((f"{L['cgst']} {half}%", t["cgst"]))
        rows.append((f"{L['sgst']} {half}%", t["sgst"]))
    rows.append((L["total"], t["total"]))
    for label, val in rows:
        bold = label == L["total"]
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.5 if bold else 9)
        c.drawString(330, y, label)
        c.drawRightString(W - 40, y, num(val))
        y -= 14
    # Bank details and footer
    y -= 20
    c.setFont("Helvetica", 9)
    c.drawString(40, y, f"{L['acct']} {t['bank_account']}")
    c.drawString(40, y - 12, f"{L['ifsc']} {t['ifsc']}")
    c.drawString(40, y - 40, "Certified that the particulars given above are true and correct.")
    c.drawString(W - 200, y - 70, f"For {spec.vendor.name}")
    c.save()
    return t


def to_scan(pdf_path: str, out_path: str, seed: int = 0) -> None:
    """Rasterise, skew, blur and add noise, then save as an image-only PDF."""
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", "150", "-png", "-singlefile", pdf_path, os.path.join(td, "p")], check=True)
        im = Image.open(os.path.join(td, "p.png")).convert("L")
    im = im.rotate(rng.uniform(-1.2, 1.2), expand=True, fillcolor=255)
    im = im.filter(ImageFilter.GaussianBlur(radius=0.6))
    px = im.load()
    w, h = im.size
    for _ in range(int(w * h * 0.004)):  # salt-and-pepper speckle
        x, y = rng.randrange(w), rng.randrange(h)
        px[x, y] = 0 if rng.random() < 0.5 else 255
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=55)
    Image.open(buf).convert("RGB").save(out_path, "PDF", resolution=150)


def build(spec: InvoiceSpec, out_dir: str, name: str) -> tuple[str, dict]:
    os.makedirs(out_dir, exist_ok=True)
    pdf = os.path.join(out_dir, f"{name}.pdf")
    if spec.scanned:
        with tempfile.TemporaryDirectory() as td:
            clean = os.path.join(td, "clean.pdf")
            t = render_pdf(spec, clean)
            to_scan(clean, pdf, spec.seed)
    else:
        t = render_pdf(spec, pdf)
    with open(os.path.join(out_dir, f"{name}.truth.json"), "w") as f:
        json.dump(t, f, indent=2)
    return pdf, t
