"""End to end: golden scenarios, changed-input tests, safety properties, determinism."""
import copy
import random

import pytest

from apflow.checks import run_checks
from apflow.company import Repo
from apflow.config import SETTINGS
from apflow.decide import decide
from apflow.generator import build
from apflow.pipeline import run_invoice
from apflow.scenarios import random_clean, scenarios
from tests.conftest import AS_OF, Oracle, ex_from_truth
from tests.test_checks import base_truth, recompute

SCEN = {s.id: s for s in scenarios()}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("scen"))
    return {sid: build(s.spec, d, sid) for sid, s in SCEN.items()}


@pytest.fixture(scope="module")
def results(built):
    out = {}
    for sid, (pdf, t) in built.items():
        s = SCEN[sid]
        out[sid] = run_invoice(pdf, Repo().with_ledger(s.extra_ledger),
                               llm=Oracle(t) if s.spec.scanned else None, as_of=AS_OF)
    return out


# ---------- golden scenarios: exact decision AND exact set of failed checks ----------
@pytest.mark.parametrize("sid", list(SCEN))
def test_golden(sid, results):
    s, rr = SCEN[sid], results[sid]
    fails = {c.id for c in rr.checks if c.status == "fail"}
    assert rr.decision.outcome == s.expect, rr.decision.summary
    assert fails == s.expect_fail


def test_every_hold_has_owner_and_question(results):
    for sid, rr in results.items():
        if rr.decision.outcome == "HOLD":
            assert rr.decision.owner and rr.decision.question, sid


def test_every_reject_has_vendor_message(results):
    for sid, rr in results.items():
        if rr.decision.outcome == "REJECT":
            assert rr.decision.vendor_message and "corrected invoice" in rr.decision.vendor_message, sid


def test_every_failed_check_has_evidence(results):
    for sid, rr in results.items():
        for c in rr.checks:
            if c.status == "fail":
                assert c.message and c.evidence, (sid, c.id)


def test_e1_part_approval(results):
    d = results["E1"].decision
    assert (d.approvable_amount, d.held_amount) == (50000.0, 50000.0)


def test_e2_question_names_both_invoices(results):
    q = results["E2"].decision.question
    assert "MOS-2026-0431" in q and "MOS-2026-0398" in q


def test_e3_candidate_is_the_right_po(results):
    c = next(c for c in results["E3"].checks if c.id == "C09")
    assert c.evidence["candidates"][0][1] == "PO-2026-0106"


def test_e4_impact_in_rupees(results):
    c = next(c for c in results["E4"].checks if c.id == "C11")
    assert c.evidence["over"][0]["impact"] == round((369.60 - 330.00) * 180, 2)


def test_high_value_needs_manager(results):
    assert results["S04"].decision.approval_level == "Manager sign-off"
    assert results["S01"].decision.approval_level.startswith("Auto")


def test_hold_owner_priority_fraud_first():
    t = base_truth()
    t["ifsc"] = "KKBK0000811"        # Treasury
    t["total"] += 50                 # AP
    res, _ = run_checks(ex_from_truth(t), Repo(), AS_OF)
    d = decide(res, ex_from_truth(t), SETTINGS)
    assert d.outcome == "HOLD" and d.owner == "Treasury" and len(d.reasons) == 2


def test_reject_beats_hold():
    t = base_truth()
    t["buyer_gstin"] = "07AAACN4089K1Z5"  # reject
    t["total"] += 50                      # hold
    res, _ = run_checks(ex_from_truth(t), Repo(), AS_OF)
    assert decide(res, ex_from_truth(t), SETTINGS).outcome == "REJECT"


# ---------- changed-input tests ----------
def _fails(t, repo=None):
    res, _ = run_checks(ex_from_truth(t), repo or Repo(), AS_OF)
    return {c.id for c in res if c.status == "fail"}


@pytest.mark.parametrize("i", [0, 1])
def test_price_up_5pct_flips_only_price_check(i):
    t = base_truth()
    t["lines"][i]["unit_price"] = round(t["lines"][i]["unit_price"] * 1.05, 2)
    assert _fails(recompute(t)) == {"C11"}


def test_reordering_lines_changes_nothing():
    t = base_truth()
    t2 = copy.deepcopy(t)
    t2["lines"].reverse()
    assert _fails(t) == _fails(t2) == set()


def test_vendor_name_case_and_spacing_irrelevant():
    t = base_truth()
    t["vendor_name"] = "  DECCAN   lab supplies "
    assert _fails(t) == set()


@pytest.mark.parametrize("field", ["invoice_number", "invoice_date", "total"])
def test_dropping_a_required_field_flips_c02(field):
    t = base_truth()
    t[field] = None
    assert "C02" in _fails(t)


def test_random_clean_invoices_never_fail_identity_or_arithmetic():
    # 40 random clean invoices: any failure must come from history-dependent checks
    # (quantity, PO balance, duplicate, date), never from arithmetic, tax, GSTIN, vendor or PO identity.
    for seed in range(40):
        spec = random_clean(seed, scanned=False)
        from apflow.generator import truth
        t = truth(spec)
        res, _ = run_checks(ex_from_truth(t), Repo(), AS_OF)
        fails = {c.id for c in res if c.status == "fail"}
        # C14 allowed: a random bill can coincide with a paid one (seed 20 re-bills the same
        # five-month housekeeping block 17 days after the last one; holding that is correct).
        assert fails <= {"C12", "C13", "C14", "C15"}, (seed, fails)


# ---------- safety ----------
def test_scans_without_model_are_never_auto_approved(built, tmp_path):
    pdfs = [built["S05"][0], built["E3"][0]]
    for seed in range(4):
        pdfs.append(build(random_clean(500 + seed, scanned=True), str(tmp_path), f"s{seed}")[0])
    for pdf in pdfs:
        rr = run_invoice(pdf, Repo(), llm=None, as_of=AS_OF)
        assert rr.decision.outcome != "APPROVE", pdf
        assert any(c.id == "X01" and c.status == "fail" for c in rr.checks) or rr.decision.outcome == "REJECT"


def test_garbage_input_does_not_crash(tmp_path):
    from reportlab.pdfgen import canvas
    p = str(tmp_path / "blank.pdf")
    c = canvas.Canvas(p)
    c.drawString(100, 700, "Minutes of the canteen committee meeting, nothing to pay here. " * 4)
    c.save()
    rr = run_invoice(p, Repo(), as_of=AS_OF)
    assert rr.decision.outcome in ("REJECT", "HOLD")


# ---------- determinism ----------
def test_same_input_same_output(built):
    pdf, _ = built["E4"]
    a = run_invoice(pdf, Repo(), as_of=AS_OF).to_dict()
    b = run_invoice(pdf, Repo(), as_of=AS_OF).to_dict()
    strip = lambda d: {k: v for k, v in d.items() if k not in ("run_id", "events")}
    assert strip(a) == strip(b)
    assert [e["stage"] for e in a["events"]] == ["intake", "read", "classify", "validate", "vendor", "po", "risk", "decide"]
