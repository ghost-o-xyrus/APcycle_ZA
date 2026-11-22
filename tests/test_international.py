"""Our US and German companies, and imports into India: tax numbers, billed company, tax rules, currency."""
from __future__ import annotations

import copy
import json
import sqlite3
from datetime import date

import pytest

from apflow.checks import run_checks
from apflow.company import ENTITIES, Repo, regime
from apflow.config import SETTINGS
from apflow.decide import decide
from apflow.extract.parser import parse, parse_date
from apflow.generator import InvoiceSpec, SpecLine, truth
from apflow.money import fmt, to_inr
from apflow.scenarios import V, line
from apflow.taxid import country_of, de_check, fr_key, validate
from tests.conftest import AS_OF, ex_from_truth


# ---------------- tax numbers ----------------

@pytest.mark.parametrize("v", ["DE136695976", "DE811569869", "FR40303265045", "FR83404833048", "de 136 695 976"])
def test_published_valid_vat_ids_pass(v):
    assert validate(v)[0], validate(v)


@pytest.mark.parametrize("v,why", [("DE136695977", "check digit"), ("FR41303265045", "key"),
                                   ("DE12345678", "format"), ("00-1234567", "prefix"), ("XX123", "unrecognised")])
def test_bad_tax_numbers_fail_with_a_reason(v, why):
    ok, msg = validate(v)
    assert not ok and why in msg


def test_country_of_tax_numbers():
    assert country_of("84-3729106") == "US" and country_of("DE318456278") == "DE"
    assert country_of(SETTINGS.company_gstin) == "IN" and country_of("GB123456789") is None
    assert de_check("13669597" + "6") and fr_key("303265045") == "40"


def test_our_companies_and_foreign_vendors_have_valid_numbers():
    for e in ENTITIES.values():
        assert validate(e.tax_id)[0], e
    for vid in ("V101", "V102", "V103"):
        assert validate(V[vid].tax_id)[0], vid


# ---------------- which rules apply ----------------

@pytest.mark.parametrize("vc,ec,want", [
    ("IN", "IN", "gst"), ("DE", "IN", "import_india"), ("US", "IN", "import_india"), ("US", "US", "us_sales_tax"),
    ("DE", "DE", "vat_domestic"), ("FR", "DE", "eu_reverse_charge"), ("US", "DE", "import"), ("DE", "US", "import")])
def test_regimes(vc, ec, want):
    assert regime(vc, ec) == want


def us_truth(**kw):
    spec = InvoiceSpec(V["V101"], "LSM-1", "2026-10-28", "PO-2026-0201",
                       [line("PO-2026-0201", 1, 10), line("PO-2026-0201", 2, 2)], layout="X", entity="US")
    for k, v in kw.items():
        setattr(spec, k, v)
    return truth(spec)


def checks(t, conf=0.95):
    res, ctx = run_checks(ex_from_truth(t, conf=conf), Repo(), AS_OF)
    return {c.id: c for c in res}, ctx


def test_us_invoice_passes_and_resolves_company_and_currency():
    c, ctx = checks(us_truth())
    assert [k for k, v in c.items() if v.status == "fail"] == []
    assert ctx.entity.code == "US" and ctx.regime == "us_sales_tax" and ctx.cur == "USD"
    assert ctx.vendor_how == "exact EIN"
    assert "$474.38" in c["C06"].message


def test_billed_to_our_other_company_is_held():
    t = us_truth()
    t["buyer_tax_id"] = ENTITIES["DE"].tax_id  # US PO, but addressed to the German company
    c, _ = checks(t)
    assert c["C04"].status == "fail" and c["C04"].severity == "hold" and "Kaveri Instruments GmbH" in c["C04"].message


def test_billed_to_a_stranger_is_rejected_when_read_confidently():
    t = us_truth()
    t["buyer_tax_id"] = "46-1234567"
    assert checks(t)[0]["C04"].severity == "reject"
    assert checks(t, conf=0.7)[0]["C04"].severity == "hold"


def test_invalid_vendor_vat_id_is_rejected_unsupported_scheme_is_only_noted():
    t = truth(InvoiceSpec(V["V102"], "RL-1", "2026-11-12", "PO-2026-0204", [line("PO-2026-0204", 1, 5)],
                          layout="X", entity="DE"))
    bad = dict(t, vendor_tax_id="DE294750168")
    assert checks(bad)[0]["C03"].severity == "reject"
    odd = dict(t, vendor_tax_id="GB123456789")
    c3 = checks(odd)[0]["C03"]
    assert c3.status == "pass" and "not supported" in c3.message


def test_domestic_german_supply_without_vat_is_held():
    t = truth(InvoiceSpec(V["V102"], "RL-2", "2026-11-12", "PO-2026-0204",
                          [SpecLine("Laboratory Magnetic Stirrer", "", "pcs", 5, 420.00, 0)], layout="X", entity="DE"))
    c, ctx = checks(t)
    assert ctx.regime == "vat_domestic" and c["C06"].status == "fail" and "without VAT" in c["C06"].message


def test_gst_charged_on_an_import_into_india_is_held():
    t = truth(InvoiceSpec(V["V102"], "RL-3", "2026-11-02", "PO-2026-0202", [line("PO-2026-0202", 1, 2)],
                          layout="X", entity="IN"))
    t["igst"] = 1134.0
    t["total"] += 1134.0
    c, ctx = checks(t)
    assert ctx.regime == "import_india" and c["C06"].status == "fail" and "import into India" in c["C06"].message


def test_unplaced_foreign_currency_invoice_is_held():
    t = us_truth()
    for k in ("po_reference", "vendor_tax_id", "buyer_tax_id", "buyer_name"):
        t[k] = None
    t["vendor_name"] = "Somebody Else Inc"
    c, ctx = checks(t)
    assert ctx.entity is None and c["C16"].status == "fail" and "no PO or vendor record" in c["C16"].message


def test_indian_invoices_keep_gst_rules():
    t = truth(InvoiceSpec(V["V001"], "DLS/T/9", "2026-09-22", "PO-2026-0101", [line("PO-2026-0101", 1, 200)]))
    c, ctx = checks(t)
    assert ctx.regime == "gst" and c["C03"].name == "GSTIN valid" and c["C16"].message == "Amounts in INR, as on PO-2026-0101"


# ---------------- money ----------------

def test_one_approval_limit_in_rupees():
    t = truth(InvoiceSpec(V["V102"], "RL-4", "2026-11-02", "PO-2026-0202",
                          [line("PO-2026-0202", 1, 2), line("PO-2026-0202", 2, 2)], layout="X", entity="IN"))
    res, ctx = run_checks(ex_from_truth(t), Repo(), AS_OF)
    d = decide(res, ex_from_truth(t), SETTINGS, ctx.cur)
    assert d.outcome == "APPROVE" and d.approval_level == "Manager sign-off"  # 7,800 EUR = 7.1 lakh
    assert "€7,800.00" in d.summary and "₹7,09,800.00" in d.summary
    small = decide(res, ex_from_truth(t), SETTINGS, "INR")  # the same figure in rupees is under the limit
    assert small.approval_level.startswith("Auto")


def test_money_formats():
    assert fmt(147500, "INR") == "₹1,47,500.00" and fmt(5750, "USD") == "$5,750.00"
    assert fmt(1234.5, "EUR") == "€1,234.50" and fmt(54223, "JPY") == "¥54,223" and to_inr(100, "USD") == 8400


# ---------------- reading ----------------

def test_us_dates_read_month_first_eu_day_first():
    assert parse_date("10/11/2026", month_first=True) == "2026-10-11"
    assert parse_date("10/11/2026") == "2026-11-10"
    us = "Acme Corp\nEIN: 74-2918365\nInvoice No: A-1\nInvoice Date: 10/11/2026\nTotal Due (USD) $10.00"
    eu = "Acme GmbH\nVAT ID: DE294750167\nInvoice No: A-1\nInvoice Date: 10.11.2026\nTotal Due (EUR) €10.00"
    assert parse(us, "text", 0.95, SETTINGS.company_gstin).get("invoice_date") == "2026-10-11"
    assert parse(eu, "text", 0.95, SETTINGS.company_gstin).get("invoice_date") == "2026-11-10"


def test_buyer_and_vendor_tax_numbers_are_told_apart():
    text = "Acme GmbH\nVAT ID: DE294750167\nBill To: Kaveri Instruments GmbH\nCustomer VAT ID: DE318456278\nVAT 19% 79.80"
    ex = parse(text, "text", 0.95, SETTINGS.company_gstin)
    assert ex.get("vendor_tax_id") == "DE294750167" and ex.get("buyer_tax_id") == "DE318456278"
    assert ex.get("buyer_name") == "Kaveri Instruments GmbH" and ex.get("tax") == 79.80


# ---------------- store ----------------

def test_old_store_gains_currency_columns(tmp_path):
    from apflow.store import Store
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, created_at REAL, file_name TEXT, file_path TEXT, "
                "sha256 TEXT, sample_id TEXT, state TEXT, error TEXT, route TEXT, vendor_id TEXT, vendor_name TEXT, "
                "invoice_number TEXT, invoice_date TEXT, po_number TEXT, po_how TEXT, subtotal REAL, total REAL, "
                "outcome TEXT, final_status TEXT, owner TEXT, question TEXT, summary TEXT, approvable REAL, held REAL, "
                "approval_level TEXT, total_ms INTEGER, evaluation INTEGER DEFAULT 1, extraction_json TEXT, "
                "decision_json TEXT, matches_json TEXT, route_log_json TEXT)")
    con.commit()
    con.close()
    Store(str(db))
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(runs)")}
    assert {"currency", "entity"} <= cols
