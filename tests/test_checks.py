"""Checks at their boundaries, on extractions built straight from truth (no PDF in the way)."""
import copy

import pytest

from apflow.checks import dup_score, run_checks
from apflow.company import LedgerInvoice, Repo
from apflow.generator import InvoiceSpec, truth
from apflow.scenarios import V, line
from tests.conftest import AS_OF, ex_from_truth


def base_truth():
    # S01-like: PO-2026-0101 lines 1 and 2, intra-state
    return truth(InvoiceSpec(V["V001"], "DLS/T/1", "2026-09-22", "PO-2026-0101",
                             [line("PO-2026-0101", 1, 200), line("PO-2026-0101", 2, 50)]))


def recompute(t):
    for l in t["lines"]:
        l["amount"] = round(l["qty"] * l["unit_price"], 2)
    t["subtotal"] = round(sum(l["amount"] for l in t["lines"]), 2)
    tax = round(sum(l["amount"] * l["gst_rate"] / 100 for l in t["lines"]), 2)
    if t["igst"]:
        t["igst"] = tax
    else:
        t["cgst"] = round(tax / 2, 2)
        t["sgst"] = round(tax - t["cgst"], 2)
    t["total"] = round(t["subtotal"] + tax, 2)
    return t


def check(t, cid, repo=None, conf=0.95, route="text"):
    res, ctx = run_checks(ex_from_truth(t, route=route, conf=conf), repo or Repo(), AS_OF)
    return next(c for c in res if c.id == cid), res


def test_base_all_pass():
    _, res = check(base_truth(), "C01")
    assert [c.id for c in res if c.status == "fail"] == []


# --- C05 arithmetic: rupee tolerance boundary ---
@pytest.mark.parametrize("delta,status", [(0.99, "pass"), (1.00, "pass"), (1.02, "fail")])
def test_total_tolerance(delta, status):
    t = base_truth()
    t["total"] = round(t["total"] + delta, 2)
    assert check(t, "C05")[0].status == status


def test_wrong_tax_rate_caught_even_if_total_adds_up():
    t = base_truth()
    t["cgst"] += 100
    t["sgst"] += 100
    t["total"] += 200  # internally consistent, but not what the PO GST rates give
    c = check(t, "C05")[0]
    assert c.status == "fail" and "PO GST rates" in c.message


# --- C11 price: per-unit tolerance after tax normalisation ---
@pytest.mark.parametrize("bump,status", [(0.99, "pass"), (1.00, "pass"), (1.01, "fail")])
def test_price_tolerance_low_value(bump, status):
    t = base_truth()
    t["lines"][0]["unit_price"] = 185.00 + bump  # PO 185.00, tolerance max(1, 0.5%) = 1.00
    assert check(recompute(t), "C11")[0].status == status


def test_price_tolerance_relative_for_high_value():
    # 6800 PO price: tolerance = 0.5% = 34.00
    t = truth(InvoiceSpec(V["V001"], "DLS/T/2", "2026-09-22", "PO-2026-0101", [line("PO-2026-0101", 3, 4)]))
    t["lines"][0]["unit_price"] = 6834.00
    assert check(recompute(t), "C11")[0].status == "pass"
    t["lines"][0]["unit_price"] = 6834.01
    assert check(recompute(t), "C11")[0].status == "fail"


def test_billing_below_po_price_is_fine():
    t = base_truth()
    t["lines"][0]["unit_price"] = 150.00
    assert check(recompute(t), "C11")[0].status == "pass"


def test_tax_inclusive_po_normalised():
    # GeM-style PO: 11,800 incl. 18% -> 10,000 pre-tax; invoice at 10,000 must pass
    t = truth(InvoiceSpec(V["V002"], "N/1", "2026-10-03", "PO-2026-0102", [line("PO-2026-0102", 2, 2)]))
    assert t["lines"][0]["unit_price"] == 10000.00
    assert check(t, "C11")[0].status == "pass"
    t["lines"][0]["unit_price"] = 11800.00  # vendor billed the tax-inclusive figure as pre-tax
    assert check(recompute(t), "C11")[0].status == "fail"


def test_item_not_on_po():
    t = base_truth()
    t["lines"].append({"description": "Gold Plated Paperweight", "hsn": "7114", "uom": "pcs", "qty": 1,
                       "unit_price": 5000.0, "amount": 5000.0, "gst_rate": 3})
    c = check(recompute(t), "C11")[0]
    assert c.status == "fail" and "not on the PO" in c.message


# --- C12 quantity: accepted minus already billed ---
@pytest.mark.parametrize("qty,status", [(196, "pass"), (197, "fail")])
def test_qty_vs_goods_accepted(qty, status):
    t = truth(InvoiceSpec(V["V004"], "CE/T", "2026-10-15", "PO-2026-0104", [line("PO-2026-0104", 1, qty)]))
    c = check(t, "C12")[0]
    assert c.status == status
    if status == "fail":
        assert c.owner == "Stores"


def test_qty_counts_prior_billing():
    repo = Repo().with_ledger([LedgerInvoice("V004", "CE/OLD", "2026-10-10", "PO-2026-0104", 33000.0, 36960.0, "paid",
                                             [{"line_no": 1, "qty": 100, "amount": 33000.0, "description": "LED"}])])
    t = truth(InvoiceSpec(V["V004"], "CE/T", "2026-10-15", "PO-2026-0104", [line("PO-2026-0104", 1, 97)]))
    assert check(t, "C12", repo)[0].status == "fail"  # 196 accepted - 100 billed = 96
    t = truth(InvoiceSpec(V["V004"], "CE/T", "2026-10-15", "PO-2026-0104", [line("PO-2026-0104", 1, 96)]))
    assert check(t, "C12", repo)[0].status == "pass"


# --- C13 PO balance: E1 part-approval amounts ---
def test_po_balance_part_approval():
    t = truth(InvoiceSpec(V["V003"], "SFS/T", "2026-11-15", "PO-2026-0103", [line("PO-2026-0103", 1, 4)]))
    c = check(t, "C13")[0]
    assert c.status == "fail"
    assert c.evidence["approvable"] == 50000.0 and c.evidence["excess"] == 50000.0
    t = truth(InvoiceSpec(V["V003"], "SFS/T", "2026-11-15", "PO-2026-0103", [line("PO-2026-0103", 1, 2)]))
    assert check(t, "C13")[0].status == "pass"


# --- C06 tax type ---
def test_tax_type_inter_vs_intra():
    t = base_truth()  # same state (29 -> 29)
    assert check(t, "C06")[0].status == "pass"
    t["igst"], t["cgst"], t["sgst"] = t["cgst"] + t["sgst"], 0.0, 0.0
    assert check(t, "C06")[0].status == "fail"


# --- C03 / C04 confidence-aware severity ---
def test_invalid_gstin_rejects_when_read_confidently_holds_when_not():
    t = base_truth()
    t["vendor_gstin"] = t["vendor_gstin"][:-1] + ("A" if t["vendor_gstin"][-1] != "A" else "B")
    assert check(t, "C03", conf=0.95)[0].severity == "reject"
    assert check(t, "C03", conf=0.6, route="ocr")[0].severity == "hold"


def test_low_confidence_buyer_gstin_holds_not_rejects():
    # A misread on a scan must reach a person, not bounce the vendor's invoice.
    t = base_truth()
    t["buyer_gstin"] = "07AAACN4089K1Z5"
    c = check(t, "C04", conf=0.6, route="ocr")[0]
    assert c.status == "fail" and c.severity == "hold" and c.owner == "AP"


def test_wrong_buyer():
    t = base_truth()
    t["buyer_gstin"] = "07AAACN4089K1Z5"
    c = check(t, "C04")[0]
    assert c.status == "fail" and c.severity == "reject"


# --- C07 / C08 vendor ---
def test_unambiguous_ocr_gstin_repaired_at_extraction():
    t = base_truth()
    t["vendor_gstin"] = "29AADCD44I7K1ZI"  # 1 -> I inside the PAN digits: one valid repair
    c = check(t, "C07")[0]
    assert c.status == "pass" and c.evidence["matched_by"] == "exact GSTIN"


def test_ambiguous_ocr_gstin_resolved_against_vendor_master():
    t = base_truth()
    t["vendor_gstin"] = "29AADCD4417K1Z1"  # checksum alone can't choose between 1/I twice
    c = check(t, "C07")[0]
    assert c.status == "pass" and "OCR correction" in c.evidence["matched_by"]


def test_monthly_recurring_service_bill_is_not_a_duplicate():
    led = LedgerInvoice("V003", "SFS/26-27/0519", "2026-10-31", "PO-2026-0103", 25000.0, 29500.0, "posted",
                        [{"line_no": 1, "qty": 1, "amount": 25000.0, "description": "Housekeeping Services per month"}])
    repo = Repo(ledger=[led])
    t = truth(InvoiceSpec(V["V003"], "SFS/26-27/0520", "2026-11-30", "PO-2026-0103", [line("PO-2026-0103", 1, 1)]))
    from datetime import date
    res, _ = run_checks(ex_from_truth(t), repo, date(2026, 12, 1))
    assert next(c for c in res if c.id == "C14").status == "pass"
    # ...but the same bill again 10 days later is held
    t["invoice_date"] = "2026-11-10"
    res, _ = run_checks(ex_from_truth(t), repo, date(2026, 12, 1))
    assert next(c for c in res if c.id == "C14").status == "fail"


def test_unknown_vendor_holds_for_procurement():
    t = base_truth()
    t["vendor_gstin"], t["vendor_name"] = "24AAACZ9999Q1ZS", "Zenith Unknown Traders"
    c = check(t, "C07")[0]
    assert c.status == "fail" and c.owner == "Procurement"


def test_bank_details_missing_is_not_a_failure():
    t = base_truth()
    t["bank_account"] = t["ifsc"] = None
    assert check(t, "C08")[0].status == "pass"


def test_bank_change_is_fraud_flag_for_treasury():
    t = base_truth()
    t["ifsc"] = "KKBK0000811"
    c = check(t, "C08")[0]
    assert c.status == "fail" and c.owner == "Treasury" and c.evidence["fraud_flag"]


# --- C09 / C10 PO resolution ---
def test_po_inferred_when_missing():
    t = base_truth()
    t["po_reference"] = None
    c = check(t, "C09")[0]
    assert c.status == "fail" and "PO-2026-0101" in c.message


def test_po_inference_refuses_when_items_fit_no_po():
    t = base_truth()
    t["po_reference"] = None
    for l in t["lines"]:
        l["description"], l["hsn"], l["unit_price"] = "Unrelated Widget Assembly", "0000", 999.0
    c = check(recompute(t), "C09")[0]
    assert c.status == "fail" and "none of the vendor's open POs fit" in c.message


def test_po_of_another_vendor_rejected():
    t = base_truth()
    t["po_reference"] = "PO-2026-0105"  # belongs to V005
    c = check(t, "C10")[0]
    assert c.status == "fail" and c.severity == "reject"


def test_po_reference_formatting_tolerated():
    t = base_truth()
    t["po_reference"] = "po 2026 0101"
    assert check(t, "C09")[0].status == "pass"


# --- C14 duplicates ---
def test_duplicate_score_threshold_behaviour():
    led = LedgerInvoice("V001", "DLS/T/0", "2026-09-20", "PO-2026-0101", 58000.0, 64960.0, "paid",
                        [{"line_no": 1, "qty": 200, "amount": 37000.0, "description": "Borosilicate Beaker 500 ml"},
                         {"line_no": 2, "qty": 50, "amount": 21000.0, "description": "Nitrile Gloves Box of 100"}])
    t = base_truth()
    sc, parts = dup_score(ex_from_truth(t), led, AS_OF, 60)
    assert sc >= 0.85
    t2 = copy.deepcopy(t)
    t2["lines"][1]["qty"] = 10
    sc2, _ = dup_score(ex_from_truth(recompute(t2)), led, AS_OF, 60)
    assert sc2 < 0.85  # a genuinely different bill to the same vendor is not flagged


def test_exact_duplicate_number_normalised():
    repo = Repo().with_ledger([LedgerInvoice("V001", "dls-t-1", "2026-09-01", "PO-2026-0101", 1.0, 1.18, "paid", [])])
    c = check(base_truth(), "C14", repo)[0]  # 'DLS/T/1' vs 'dls-t-1'
    assert c.status == "fail" and c.severity == "reject"


# --- C15 dates ---
def test_future_and_pre_po_dates():
    t = base_truth()
    t["invoice_date"] = "2026-12-01"
    assert check(t, "C15")[0].status == "fail"
    t["invoice_date"] = "2026-08-01"  # PO raised 2026-09-02
    c = check(t, "C15")[0]
    assert c.status == "fail" and "predates" in c.message


# --- C01 short-circuit ---
def test_not_an_invoice_short_circuits():
    t = base_truth()
    t["doc_type"] = "quotation"
    _, res = check(t, "C01")
    assert [c.id for c in res] == ["C01"] and res[0].severity == "reject"


def test_credit_note_routed_not_rejected():
    t = base_truth()
    t["doc_type"] = "credit_note"
    c = check(t, "C01")[0]
    assert c.severity == "hold" and c.owner == "AP"


def test_c16_foreign_currency_is_held():
    from apflow.checks import c16_currency
    from apflow.schema import Extraction, Field

    class C:  # minimal context
        def __init__(self, cur):
            self.ex = Extraction(route="text", fields={"currency": Field(cur, 0.95, "")})
    assert c16_currency(C(None)).status == "pass" and c16_currency(C("INR")).status == "pass"
    r = c16_currency(C("USD"))
    assert r.status == "fail" and r.severity == "hold" and r.owner == "AP" and "Treasury" in r.question and "USD" in r.message
