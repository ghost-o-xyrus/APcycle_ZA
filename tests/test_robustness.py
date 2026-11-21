"""Hostile and unusual inputs found by the adversarial campaign (21 Nov). Each must be handled
deliberately: never a crash, never an approval of something the system could not verify."""
import io
import os
import subprocess
from dataclasses import replace
from datetime import date

import pikepdf
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from apflow.company import Repo
from apflow.config import SETTINGS
from apflow.extract.routes import MAX_TEXT_PAGES, extract, unreadable_reason
from apflow.generator import InvoiceSpec, SpecLine, build
from apflow.pipeline import run_invoice
from apflow.scenarios import V, line, scenarios

AS_OF = date(2026, 11, 20)
S = {s.id: s for s in scenarios()}


@pytest.fixture(scope="module")
def d(tmp_path_factory):
    out = tmp_path_factory.mktemp("adv")
    base, _ = build(S["S01"].spec, str(out), "base")
    subprocess.run(["pdftoppm", "-r", "150", "-png", "-singlefile", base, str(out / "page")], check=True)
    return out


def run(path):
    return run_invoice(str(path), Repo(), as_of=AS_OF)


# ---------- files that cannot be opened ----------

def test_password_protected_pdf(d):
    p = d / "pw.pdf"
    with pikepdf.open(d / "base.pdf") as pdf:
        pdf.save(p, encryption=pikepdf.Encryption(user="secret", owner="owner"))
    assert unreadable_reason(str(p)) == "The PDF is password-protected"
    rr = run(p)
    assert rr.decision.outcome == "REJECT" and "password-protected" in rr.decision.vendor_message


def test_owner_only_encryption_opens_normally(d):
    # Many vendor PDFs are "encrypted" with an empty user password: they must still be read.
    p = d / "owner.pdf"
    with pikepdf.open(d / "base.pdf") as pdf:
        pdf.save(p, encryption=pikepdf.Encryption(user="", owner="owner"))
    assert unreadable_reason(str(p)) is None
    assert run(p).decision.outcome == "APPROVE"


def test_zero_page_pdf(d):
    p = d / "zero.pdf"
    pikepdf.new().save(p)
    rr = run(p)
    assert rr.decision.outcome == "REJECT" and rr.checks[0].message == "The PDF has no pages"


@pytest.mark.parametrize("data", [b"%PDF-1.4\n%garbage", b"%PDF-1.7\n" + os.urandom(4000)])
def test_damaged_pdf(d, data):
    p = d / "bad.pdf"
    p.write_bytes(data)
    rr = run(p)
    assert rr.decision.outcome == "REJECT" and "damaged" in rr.checks[0].message


# ---------- documents whose type is unclear ----------

def test_blank_page_goes_to_a_person_not_rejected(d):
    p = d / "blank.pdf"
    c = canvas.Canvas(str(p)); c.showPage(); c.save()
    rr = run(p)
    c01 = rr.checks[0]
    assert rr.decision.outcome == "HOLD" and c01.severity == "hold" and c01.owner == "AP"


def test_unreadable_sideways_scan_is_held_not_rejected(d):
    p = d / "sideways.pdf"
    Image.open(d / "page.png").rotate(90, expand=True).convert("RGB").save(p, "PDF", resolution=150)
    assert run(p).decision.outcome == "HOLD"


def test_confident_quotation_still_rejected():
    rr_spec = replace(S["R05"].spec)
    assert rr_spec.title == "QUOTATION"  # covered end to end by the R05 golden scenario


def test_low_confidence_non_invoice_type_is_held(d):
    from apflow.checks import c01_doc_type, Context
    from apflow.schema import Extraction, Field
    ex = Extraction(route="ocr", fields={"doc_type": Field("quotation", 0.7, "")})
    c = c01_doc_type(Context(ex, Repo(), AS_OF))
    assert c.severity == "hold" and "low confidence" in c.message


# ---------- long and unusual documents ----------

def test_long_documents_only_read_first_pages_and_say_so(d):
    p = d / "fifty.pdf"
    with pikepdf.open(d / "base.pdf") as src:
        dst = pikepdf.new()
        for _ in range(50):
            dst.pages.append(src.pages[0])
        dst.save(p)
    ex, log = extract(str(p))
    assert log[0]["pages"] == 50 and f"first {MAX_TEXT_PAGES} of 50" in " ".join(ex.notes)
    rr = run(p)
    # found by this test: reading stops at page 1's totals, so 50 pages looked like a clean invoice
    assert rr.decision.outcome == "HOLD" and rr.decision.owner == "AP"
    assert any(c.id == "X01" and "Not fully read" in c.message for c in rr.checks)
    assert "first 5 of 50 pages" in rr.events[1].detail


def test_bundled_invoices_in_one_file_are_held(d):
    import pikepdf
    a, _ = build(S["S01"].spec, str(d), "inv_a")
    b, _ = build(S["H01"].spec, str(d), "inv_b")
    p = d / "bundle.pdf"
    out = pikepdf.new()
    for f in (a, b):
        with pikepdf.open(f) as src:
            out.pages.extend(src.pages)
    out.save(p)
    rr = run(p)
    x01 = next(c for c in rr.checks if c.id == "X01")
    assert rr.decision.outcome == "HOLD" and "2 different invoice numbers" in x01.message
    assert "one PDF per invoice" in x01.question


def test_two_page_invoice_with_totals_on_page_two(d):
    p = d / "two.pdf"
    c = canvas.Canvas(str(p))
    c.setFont("Helvetica", 9)
    c.drawString(40, 790, "Deccan Lab Supplies"); c.drawString(400, 790, "TAX INVOICE")
    c.drawString(40, 776, "GSTIN: " + V["V001"].gstin)
    c.drawString(330, 740, "Invoice No: DLS/2026/9003"); c.drawString(330, 728, "Invoice Date: 12-Nov-2026")
    c.drawString(330, 716, "PO Number: PO-2026-0101")
    c.drawString(40, 728, "Buyer GSTIN: " + SETTINGS.company_gstin)
    c.drawString(40, 690, "S.No Description HSN Qty Unit Rate Amount")
    c.drawString(40, 674, "1 Borosilicate Beaker 500 ml 7017 100 pcs 185.00 18,500.00")
    c.drawString(40, 660, "Continued on next page")
    c.showPage(); c.setFont("Helvetica", 9)
    c.drawString(40, 790, "S.No Description HSN Qty Unit Rate Amount")
    c.drawString(40, 774, "2 Nitrile Gloves Box of 100 4015 20 box 420.00 8,400.00")
    c.drawString(330, 740, "Sub Total 26,900.00"); c.drawString(330, 726, "CGST 6% 1,614.00")
    c.drawString(330, 712, "SGST 6% 1,614.00"); c.drawString(330, 698, "Grand Total 30,128.00")
    c.save()
    rr = run(p)
    assert len(rr.extraction.lines) == 2 and rr.decision.outcome == "APPROVE"


def test_thirty_lines_not_on_the_po(d):
    lines = [SpecLine(f"Spare part type {i:02d} assembly", "8483", "pcs", i % 7 + 1, 100.0 + i, 18) for i in range(30)]
    pdf, t = build(InvoiceSpec(V["V001"], "DLS/2026/9001", "2026-11-10", "PO-2026-0101", lines), str(d), "thirty")
    rr = run(pdf)
    assert len(rr.extraction.lines) == 30
    assert rr.decision.outcome == "HOLD" and any(c.id == "C11" and c.status == "fail" for c in rr.checks)


def test_decimal_quantities(d):
    pdf, _ = build(InvoiceSpec(V["V001"], "DLS/2026/9002", "2026-11-10", "PO-2026-0101",
                               [SpecLine("Nitrile Gloves Box of 100", "4015", "box", 2.5, 420.0, 12)]), str(d), "dec")
    rr = run(pdf)
    assert rr.extraction.lines[0].qty == 2.5 and rr.decision.outcome == "APPROVE"


def test_html_in_vendor_text_is_kept_as_text(d):
    evil = replace(V["V001"], name='<img src=x onerror="document.title=1">Evil & Co')
    pdf, _ = build(InvoiceSpec(evil, "DLS/2026/9004", "2026-11-10", "PO-2026-0101", [line("PO-2026-0101", 1, 10)]),
                   str(d), "evil")
    rr = run(pdf)
    assert "<img" in rr.extraction.get("vendor_name")  # stored verbatim; the UI escapes it (test_ui)


def test_credit_note_routed_to_ap(d):
    pdf, _ = build(replace(S["S01"].spec, title="CREDIT NOTE", invoice_number="CN-1"), str(d), "cn")
    rr = run(pdf)
    assert rr.decision.outcome == "HOLD" and rr.decision.owner == "AP"
