"""Generator invariants: the answer key must be internally consistent and match what is printed."""
import os

import pytest

from apflow.extract.routes import text_layer
from apflow.generator import build, fmt_date, inr, truth
from apflow.scenarios import random_clean, scenarios


@pytest.mark.parametrize("seed", range(60))
def test_truth_reconciles(seed):
    t = truth(random_clean(seed))
    assert abs(sum(l["amount"] for l in t["lines"]) - t["subtotal"]) < 0.011
    for l in t["lines"]:
        assert abs(l["qty"] * l["unit_price"] - l["amount"]) < 0.006
    assert abs(t["subtotal"] + t["cgst"] + t["sgst"] + t["igst"] - t["total"]) < 0.011
    assert (t["igst"] == 0) or (t["cgst"] == t["sgst"] == 0)
    exp_tax = sum(l["amount"] * l["gst_rate"] / 100 for l in t["lines"])
    assert abs(exp_tax - (t["cgst"] + t["sgst"] + t["igst"])) < 0.011


def test_deterministic():
    assert truth(random_clean(7)) == truth(random_clean(7))
    assert truth(random_clean(7)) != truth(random_clean(8))


@pytest.mark.parametrize("layout", list("ABCD"))
def test_printed_text_contains_truth(layout, tmp_out):
    spec = random_clean(42, layout=layout, scanned=False)
    pdf, t = build(spec, tmp_out, f"g_{layout}")
    txt = text_layer(pdf)
    for v in (t["invoice_number"], t["vendor_gstin"], t["buyer_gstin"], t["po_reference"], t["ifsc"]):
        assert v in txt
    assert fmt_date(t["invoice_date"], layout) in txt
    assert (inr(t["total"]) if layout in "ABD" else f"{t['total']:.2f}") in txt


def test_scanned_pdf_has_no_text_layer(tmp_out):
    pdf, _ = build(random_clean(3, layout="A", scanned=True), tmp_out, "g_scan")
    assert len(text_layer(pdf).strip()) == 0


@pytest.mark.parametrize("x,s", [(0, "0.00"), (999.5, "999.50"), (1000, "1,000.00"), (147500, "1,47,500.00"),
                                 (12345678.9, "1,23,45,678.90")])
def test_indian_grouping(x, s):
    assert inr(x) == s


def test_scenario_ids_unique_and_expectations_valid():
    ss = scenarios()
    assert len({s.id for s in ss}) == len(ss)
    for s in ss:
        assert s.expect in ("APPROVE", "HOLD", "REJECT")
        assert (s.expect == "APPROVE") == (not s.expect_fail)
