"""Router: cheap route first, model only when needed, and failure of the model is survivable."""
import pytest

from apflow.extract.routes import extract, extraction_from_llm_json, needs_escalation
from apflow.generator import build
from apflow.scenarios import random_clean
from tests.conftest import Broken, Oracle, ex_from_truth


@pytest.fixture(scope="module")
def docs(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("router"))
    out = {}
    for name, lay, sc in [("text", "A", False), ("held", "D", False), ("scan", "B", True)]:
        out[name] = build(random_clean(77, layout=lay, scanned=sc), d, name)
    return out


def test_text_layer_never_calls_model(docs):
    pdf, t = docs["text"]
    o = Oracle(t)
    ex, log = extract(pdf, llm=o)
    assert ex.route == "text" and o.calls == 0 and log[0]["issues"] == []


def test_unseen_layout_escalates_to_model(docs):
    pdf, t = docs["held"]
    o = Oracle(t)
    ex, log = extract(pdf, llm=o)
    assert o.calls == 1 and ex.route == "llm"
    assert ex.get("invoice_number") == t["invoice_number"]


def test_scan_goes_to_model_first(docs):
    pdf, t = docs["scan"]
    ex, log = extract(pdf, llm=Oracle(t, fence=True))  # fenced JSON must still parse
    assert [l["route"] for l in log] == ["llm"]  # no OCR spent when the model answers
    assert ex.route == "llm" and ex.get("total") == t["total"]


def test_scan_ocr_first_when_configured(docs):
    from dataclasses import replace
    from apflow.config import SETTINGS
    pdf, t = docs["scan"]
    ex, log = extract(pdf, llm=Oracle(t), settings=replace(SETTINGS, scan_model_first=False))
    assert [l["route"] for l in log] == ["ocr", "llm"] and ex.route == "llm"


def test_model_failure_falls_back_to_ocr_and_logs_it(docs):
    pdf, t = docs["scan"]
    ex, log = extract(pdf, llm=Broken())
    assert ex.route == "ocr"
    assert [l["route"] for l in log] == ["llm", "ocr"]
    assert "timed out" in log[0]["error"] and log[1]["issues"]


def test_no_model_configured_is_logged(docs):
    pdf, _ = docs["scan"]
    _, log = extract(pdf, llm=None)
    assert log[-1] == {"route": "llm", "skipped": "no model configured"}


def test_model_confidence_is_capped():
    ex = extraction_from_llm_json('{"invoice_number":"A1","total":10,"confidence":{"invoice_number":1.0}}')
    assert ex.conf("invoice_number") <= 0.9


def test_model_bad_line_dropped_not_crashing():
    ex = extraction_from_llm_json('{"lines":[{"description":"x","qty":"two","unit_price":1,"amount":2},'
                                  '{"description":"y","qty":2,"unit_price":1,"amount":2}]}')
    assert len(ex.lines) == 1 and any("dropped" in n for n in ex.notes)


def test_escalation_reasons():
    t = {"invoice_number": "A", "invoice_date": "2026-11-01", "subtotal": 100.0, "cgst": 9.0, "sgst": 9.0,
         "igst": 0.0, "total": 150.0, "lines": [{"description": "x", "qty": 1, "unit_price": 100, "amount": 100}]}
    why = needs_escalation(ex_from_truth(t))
    assert "totals do not reconcile" in why
    t["total"] = 118.0
    assert needs_escalation(ex_from_truth(t)) == []
    t["lines"] = []
    assert "no line items" in needs_escalation(ex_from_truth(t))
