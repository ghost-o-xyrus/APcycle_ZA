"""Part 4: the run store, the feedback loop into the checks, human actions, uploads."""
import os
from datetime import date

import pytest

from apflow.service import ActionError, Service
from apflow.store import Store

AS_OF = date(2026, 11, 20)


@pytest.fixture()
def svc(tmp_path_factory):
    # A fresh store per test; the sample PDFs are built once and shared.
    shared = tmp_path_factory.getbasetemp() / "svc_samples"
    shared.mkdir(exist_ok=True)
    d = tmp_path_factory.mktemp("svc")
    os.symlink(shared, d / "samples")
    return Service(str(d), db_path=str(d / "t.db"), as_of=AS_OF)


def run(svc, sid):
    r = svc.wait(svc.start_sample(sid))
    assert r["state"] == "done", r.get("error")
    return r


# ---------------- store ----------------

def test_run_record_roundtrip(svc):
    r = run(svc, "S01")
    assert r["outcome"] == "APPROVE" and r["final_status"] == "approved"
    assert [e["stage"] for e in r["events"]] == ["intake", "read", "classify", "validate", "vendor", "po", "risk", "decide"]
    assert [e["seq"] for e in r["events"]] == list(range(1, 9))
    assert {c["id"] for c in r["checks"]} >= {"C01", "C05", "C11", "C14", "X01"}
    assert r["extraction"]["route"] == "text" and r["decision"]["approval_level"].startswith("Auto")
    assert [(m["line"], m["po_line"]) for m in r["matches"]] == [(0, 1), (1, 2)]
    m = r["matches"][0]  # the three-way match: invoice line, PO line from the ERP, goods receipts from the ERP
    assert m["po"]["po_number"] == "PO-2026-0101" and m["po"]["doc"].startswith("/erp/docs/")
    assert [g["grn"] for g in m["receipts"]] == ["GRN-5001"] and m["price_ok"] and m["qty_ok"]
    assert r["total_ms"] == sum(e["ms"] for e in r["events"])


def test_events_resume_after_seq(svc):
    r = run(svc, "S02")
    assert [e["seq"] for e in svc.store.events(r["run_id"], after_seq=5)] == [6, 7, 8]


def test_store_survives_reopen(tmp_path):
    db = str(tmp_path / "x.db")
    s = Store(db)
    s.start_run("r1", "a.pdf", "/tmp/a.pdf", "abc")
    s.add_event("r1", {"stage": "intake", "status": "ok", "ms": 1, "detail": "x"})
    s2 = Store(db)
    assert s2.get_run("r1")["events"][0]["detail"] == "x"


# ---------------- feedback loop: approved runs become history ----------------

def test_resubmitting_an_approved_invoice_is_rejected(svc):
    assert run(svc, "S01")["outcome"] == "APPROVE"
    again = run(svc, "S01")
    assert again["outcome"] == "REJECT"
    assert any(c["id"] == "C14" and c["status"] == "fail" for c in again["checks"])
    assert "processed before" in again["events"][0]["detail"]


def test_rejected_and_held_runs_do_not_become_history(svc):
    run(svc, "R02")     # rejected
    run(svc, "H01")     # held
    assert svc.store.ledger_entries() == []


def test_part_approval_books_only_the_approved_share(svc):
    e1 = run(svc, "E1")
    assert (e1["approvable"], e1["held"]) == (50000.0, 50000.0)
    svc.act(e1["run_id"], "approve_part", "", "Asha (AP)")
    led = svc.store.ledger_entries()
    assert len(led) == 1 and led[0].subtotal == 50000.0 and led[0].lines[0]["qty"] == pytest.approx(2.0)
    # The contract is now fully billed: the same bill again has nothing left to approve.
    again = svc.wait(svc.start_sample("E1"))
    c13 = next(c for c in again["checks"] if c["id"] == "C13")
    assert c13["status"] == "fail" and c13["evidence"]["approvable"] == 0.0


# ---------------- human actions ----------------

def test_confirm_po_reruns_checks_and_approves(svc):
    e3 = run(svc, "E3")
    assert e3["outcome"] == "HOLD" and e3["po_how"] == "inferred"
    r = svc.act(e3["run_id"], "confirm_po", "", "Asha (AP)", po="PO-2026-0106")
    assert r["evaluation"] == 2 and r["final_status"] == "approved" and r["po_how"] == "confirmed"
    c09 = next(c for c in r["checks"] if c["id"] == "C09")
    assert c09["status"] == "pass" and "confirmed by Asha (AP)" in c09["message"]
    stages = [e["stage"] for e in r["events"]]
    assert stages.count("decide") == 2 and "human" in stages
    assert [a["action"] for a in r["actions"]] == ["confirm_po"]


def test_confirming_the_wrong_po_does_not_approve(svc):
    e3 = run(svc, "E3")
    r = svc.act(e3["run_id"], "confirm_po", "", "AP", po="PO-2026-0101")  # V001's other PO, different items
    assert r["final_status"] == "on_hold"
    assert any(c["id"] == "C11" and c["status"] == "fail" for c in r["checks"])


def test_override_needs_a_reason_and_is_recorded(svc):
    h = run(svc, "H01")
    with pytest.raises(ActionError, match="reason"):
        svc.act(h["run_id"], "approve", "   ")
    r = svc.act(h["run_id"], "approve", "Called vendor on master number; bank change confirmed", "Ravi (Treasury)")
    assert r["final_status"] == "approved" and r["outcome"] == "HOLD"  # system view kept, human view added
    assert r["actions"][0]["detail"] == {"overrode": "HOLD"}


@pytest.mark.parametrize("sid,action,kw,msg", [
    ("S01", "approve", {"reason": "x"}, "Only invoices on hold"),
    ("H01", "approve_part", {}, "no part that can be approved"),
    ("E3", "confirm_po", {}, "Choose the PO"),
    ("E3", "confirm_po", {"po": "PO-9999"}, "not a known PO"),
    ("H01", "reject", {}, "reason the vendor will see"),
    ("H01", "delete", {}, "Unknown action"),
])
def test_invalid_actions_are_refused(svc, sid, action, kw, msg):
    r = run(svc, sid)
    with pytest.raises(ActionError, match=msg):
        svc.act(r["run_id"], action, **kw)


def test_a_decided_run_cannot_be_acted_on_twice(svc):
    h = run(svc, "H01")
    svc.act(h["run_id"], "reject", "Bank change not confirmed")
    with pytest.raises(ActionError, match="rejected"):
        svc.act(h["run_id"], "approve", "changed my mind")


# ---------------- uploads ----------------

def test_upload_validation(svc):
    with pytest.raises(ActionError, match="not a PDF"):
        svc.start_upload(b"GIF89a....", "x.pdf")
    with pytest.raises(ActionError, match="over 15 MB"):
        svc.start_upload(b"%PDF" + b"0" * (15 * 1024 * 1024 + 1), "big.pdf")


def test_upload_name_cannot_escape_the_uploads_folder(svc):
    data = open(svc._samples["S02"]["file"], "rb").read()
    rid = svc.start_upload(data, "../../../etc/passwd.pdf")
    r = svc.wait(rid)
    assert os.path.dirname(r["file_path"]) == svc.uploads_dir
    assert ".." not in r["file_name"]


def test_uploaded_scan_never_gets_the_standin_model(svc, tmp_path):
    # A scan the stand-in has no answer key for is read by OCR only and must go to a person.
    from apflow.generator import build
    from apflow.scenarios import random_clean
    pdf, _ = build(random_clean(4242, layout="A", scanned=True), str(tmp_path), "u")
    r = svc.wait(svc.start_upload(open(pdf, "rb").read(), "scan.pdf"))
    assert r["route"] == "ocr" and r["final_status"] != "approved"
    assert any(l.get("skipped") for l in r["route_log"])


def test_broken_pdf_is_rejected_with_a_reason_the_vendor_can_act_on(svc):
    r = svc.wait(svc.start_upload(b"%PDF-1.4\n% truncated garbage", "broken.pdf"))
    assert r["state"] == "done" and r["outcome"] == "REJECT"
    assert [c["id"] for c in r["checks"]] == ["C00"] and "damaged" in r["checks"][0]["message"]
    assert "send an unprotected, undamaged PDF" in r["decision"]["vendor_message"]
    assert [e["stage"] for e in r["events"]] == ["intake", "read", "classify", "validate", "vendor", "po", "risk", "decide"]


def test_a_crash_inside_the_engine_still_fails_visibly(svc, monkeypatch):
    import apflow.service as service_mod
    def boom(*a, **k):
        raise RuntimeError("simulated engine bug")
    monkeypatch.setattr(service_mod, "run_invoice", boom)
    r = svc.wait(svc.start_sample("S01"))
    assert r["state"] == "error" and "simulated engine bug" in r["error"]
    assert r["events"][-1]["stage"] == "error"


# ---------------- dashboard metrics ----------------

def test_metrics_reconcile_with_runs(svc):
    s01, r02, h01, e1 = run(svc, "S01"), run(svc, "R02"), run(svc, "H01"), run(svc, "E1")
    m = svc.store.metrics()
    assert m["processed"] == 4 and m["outcomes"] == {"APPROVE": 1, "HOLD": 2, "REJECT": 1}
    assert m["no_touch_rate"] == 0.25 and m["open_holds"] == 2
    assert m["amount_waiting"] == round(h01["total"] + e1["total"], 2)
    assert m["queue_by_owner"]["Treasury"]["count"] == 1 and m["queue_by_owner"]["Procurement"]["count"] == 1
    assert {r["check_id"] for r in m["hold_reasons"]} == {"C08", "C12", "C13"}
    svc.act(h01["run_id"], "approve", "Bank change confirmed by phone")
    svc.act(e1["run_id"], "approve_part", "")
    m = svc.store.metrics()
    assert m["open_holds"] == 0 and m["amount_waiting"] == 0
    assert m["no_touch_rate"] == 0.25  # overrides never count as no-touch
    assert m["amount_approved"] == round(s01["total"] + h01["total"] + e1["total"] * 0.5, 2)


def test_reset_clears_everything(svc):
    run(svc, "S01")
    svc.reset()
    assert svc.store.metrics()["processed"] == 0 and svc.store.ledger_entries() == []


# ---------------- found on real invoices (DocILE) ----------------

def test_an_exact_copy_of_an_approved_file_is_rejected_even_when_nothing_can_be_read(svc, monkeypatch):
    """The file itself proves the repeat; reading the invoice number is not needed."""
    first = run(svc, "S01")
    assert first["final_status"] == "approved"
    from apflow import pipeline
    from apflow.schema import Extraction, Field
    blank = Extraction(route="ocr", fields={"doc_type": Field("invoice", 0.95, "")})
    monkeypatch.setattr(pipeline, "extract", lambda *a, **k: (blank, [{"route": "ocr"}]))
    again = run(svc, "S01")
    c14 = next(c for c in again["checks"] if c["id"] == "C14")
    assert again["outcome"] == "REJECT" and c14["severity"] == "reject"
    assert f"run {first['run_id']}" in c14["message"]


def test_cannot_approve_anyway_for_an_unknown_vendor(svc, tmp_path):
    h = run(svc, "H05")  # missing invoice number: approving would leave nothing to catch a repeat
    with pytest.raises(ActionError, match="could not be read"):
        svc.act(h["run_id"], "approve", "Paid on the phone")
    assert svc.store.get_run(h["run_id"])["final_status"] == "on_hold"


def test_cannot_approve_anyway_when_the_vendor_is_not_in_the_master(svc, monkeypatch):
    from apflow import checks
    real = checks.resolve_vendor
    monkeypatch.setattr(checks, "resolve_vendor", lambda ctx: None)  # vendor stays unknown
    h = run(svc, "S01")
    monkeypatch.setattr(checks, "resolve_vendor", real)
    assert h["final_status"] == "on_hold"
    with pytest.raises(ActionError, match="not in the vendor master"):
        svc.act(h["run_id"], "approve", "Looks fine to me")


def test_real_public_invoices_reach_their_designed_outcomes(tmp_path):
    """D1-D5 are real documents; their buyers, POs and receipts are the ERP's real-invoice test pack."""
    from datetime import date
    svc = Service(str(tmp_path / "real"), as_of=date(2026, 11, 23), model_mode="standin")
    got = {}
    for sid in ("D1", "D2", "D3", "D4", "D5"):
        r = svc.wait(svc.start_sample(sid))
        got[sid] = (r["outcome"], r.get("owner"))
        assert r["outcome"] == svc._samples[sid]["expect"], (sid, r.get("summary"))
    assert got["D2"][1] == "Procurement" and got["D3"][1] == "Stores" and got["D5"][1] == "AP"
    d3 = svc.wait(svc.start_sample("D3"))  # the same file again: a duplicate only if the first was approved
    assert d3["outcome"] == "HOLD"
