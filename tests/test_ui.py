"""End-to-end in a real browser: the flows the demo depends on. Skipped if Chrome/Playwright is missing."""
import os
import shutil
import threading
from datetime import date

import pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium")
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome")


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    from werkzeug.serving import make_server
    from apflow.service import Service
    from apflow.web.app import create_app
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.pop("GROQ_API_KEY", None)
    svc = Service(str(tmp_path_factory.mktemp("ui")), as_of=date(2026, 11, 20), model_mode="auto")
    srv = make_server("127.0.0.1", 0, create_app(svc), threaded=True)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def page(base_url):
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1440, "height": 1000})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
        yield pg
        b.close()


def stamp_text(page):
    page.wait_for_selector("#stamp:not([hidden])", timeout=20000)
    return page.inner_text("#stamp-word")


def pick(page, sid):
    """Click a sample and wait until the desk has switched to the new run."""
    before = page.url
    page.click(f'[data-sample="{sid}"]')
    page.wait_for_function("u => location.href !== u", arg=before, timeout=10000)


def test_happy_path_stamps_approved(page, base_url):
    page.goto(f"{base_url}/#/process")
    pick(page, "S01")
    assert stamp_text(page) == "APPROVED"
    assert page.locator('#stages [data-status="ok"]').count() == 8
    assert "/#/process/" in page.url


def test_live_view_reveals_stages_progressively(page):
    pick(page, "S02")
    page.wait_for_selector('#stages [data-stage="read"][data-status="ok"]')
    # the decision is not on screen the moment the first stages are
    assert page.locator('#stages [data-stage="decide"][data-status="pending"], #stages [data-stage="decide"][data-status="working"]').count() == 1
    assert stamp_text(page) == "APPROVED"


def test_edge_case_confirm_po(page):
    pick(page, "E3")
    assert stamp_text(page) == "ON HOLD"
    page.click('[data-act="confirm_po"]')
    page.wait_for_function("document.querySelector('#stamp-word').textContent === 'APPROVED'")
    assert page.locator(".stage.human").count() == 1
    page.click('[data-tab="lines"]')
    assert "confirmed by a person" in page.inner_text("#tab-body")


def test_override_requires_reason(page):
    pick(page, "H01")
    assert stamp_text(page) == "ON HOLD"
    page.click('[data-act="approve"]')
    page.click("#do-act")
    page.wait_for_selector("#act-error:not([hidden])")
    assert "reason" in page.inner_text("#act-error")
    page.fill("#reason", "Vendor confirmed new bank account on master number")
    page.click("#do-act")
    page.wait_for_function("document.querySelector('#stamp-word').textContent === 'APPROVED'")


def test_reject_shows_vendor_message(page):
    pick(page, "R05")
    assert stamp_text(page) == "REJECTED"
    assert "quotation" in page.inner_text("#decision").lower()


def test_upload_error_is_shown(page, tmp_path):
    p = tmp_path / "notes.pdf"
    p.write_bytes(b"not really a pdf")
    page.set_input_files("#file", str(p))
    page.wait_for_selector("#upload-error:not([hidden])")
    assert "not a PDF" in page.inner_text("#upload-error")


def test_dashboard_reflects_runs_and_opens_a_run(page, base_url):
    page.goto(f"{base_url}/#/dashboard")
    page.wait_for_selector(".kpis")
    assert page.inner_text(".kpi .v") == "5"  # S01, S02, E3, H01, R05
    page.click('.chip[data-f="rejected"]')
    assert page.locator("#runs-table tbody tr").count() == 1
    page.click("#runs-table tbody tr")
    assert stamp_text(page) == "REJECTED"


def test_no_browser_errors(page):
    # 400s are the validation answers these tests provoke on purpose; anything else is a bug.
    assert [e for e in page.errors if "status of 400" not in e] == []


def test_model_check_without_key_explains_what_to_do(page, base_url):
    page.goto(f"{base_url}/#/dashboard")
    page.click("#model-check")
    page.wait_for_selector("#model-check-result .error")
    assert "GEMINI_API_KEY" in page.inner_text("#model-check-result")


def test_how_it_works_page_runs_an_edge_case(page, base_url):
    page.goto(f"{base_url}/#/how")
    page.wait_for_selector("#process-map svg")
    rules = page.inner_text("#rules")
    assert rules.index("Arithmetic tolerance") < rules.index("Price tolerance")  # order kept, not alphabetical
    assert "₹1.00" in rules and "0.85" in rules
    page.click('[data-run-edge="E4"]')
    assert stamp_text(page) == "ON HOLD"
    assert "12.0%" in page.inner_text("#decision")


def test_html_in_an_invoice_is_shown_as_text(page, base_url, tmp_path):
    from dataclasses import replace
    from apflow.generator import InvoiceSpec, build
    from apflow.scenarios import V, line
    evil = replace(V["V001"], name='<img src=x onerror="document.title=\'pwned\'">Evil')
    pdf, _ = build(InvoiceSpec(evil, "DLS/2026/7777", "2026-11-10", "PO-2026-0101", [line("PO-2026-0101", 1, 5)]),
                   str(tmp_path), "evil")
    page.goto(f"{base_url}/#/process")
    before = page.url
    page.set_input_files("#file", pdf)
    page.wait_for_function("u => location.href !== u", arg=before)
    stamp_text(page)
    page.click('[data-tab="fields"]')
    assert '<img src=x onerror=' in page.inner_text("#tab-body")
    assert page.title() != "pwned" and page.locator("#tab-body img").count() == 0


def test_reader_panel_finds_switches_and_restores(page, base_url, tmp_path, monkeypatch):
    """Find a private model on this machine, switch to it, see the header change, switch back."""
    import json as _json
    import socket
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(__file__))
    from fake_reader_server import serve_in_thread
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = serve_in_thread(port, "numind/NuExtract3", {})
    monkeypatch.setenv("APFLOW_READERS_DIR", str(tmp_path))
    (tmp_path / "r.json").write_text(_json.dumps({"url": f"http://127.0.0.1:{port}", "model": "numind/NuExtract3"}))
    try:
        page.goto(f"{base_url}/#/dashboard")
        page.wait_for_selector("#reader-options input")
        assert page.is_checked('input[name="reader"][value="standin"]')
        page.click("#reader-scan")
        page.wait_for_selector(f'.reader-opt[data-kind="private"]', timeout=15000)
        assert "Found 1 vision model" in page.inner_text("#reader-status")
        page.click('.reader-opt[data-kind="private"]')
        page.wait_for_function("() => document.querySelector('#model-note').textContent.includes('NuExtract3')")
        assert f"127.0.0.1:{port}" in page.inner_text("#model-note")
        page.fill("#reader-url", "http://example.com:80")
        page.click("#reader-connect")
        page.wait_for_function("() => document.querySelector('#reader-status').textContent.includes('not this machine')")
        page.click('.reader-opt[data-kind="standin"]')
        page.wait_for_function("() => document.querySelector('#model-note').textContent.includes('stand-in')")
        page.goto(f"{base_url}/#/process")
        assert page.is_hidden("#upload-privacy")  # only shown while a free cloud model reads scans
        page.errors[:] = [e for e in page.errors if "400" not in e]  # the refused address is an expected 400
    finally:
        srv.shutdown()


def test_us_invoice_shows_dollars_and_tax_numbers(page, base_url):
    page.goto(f"{base_url}/#/process")
    pick(page, "I1")
    assert stamp_text(page) == "APPROVED"
    assert "$6,224.38" in page.inner_text("#decision")
    page.click('[data-tab="fields"]')
    body = page.inner_text("#tab-body")
    assert "Vendor tax number" in body and "74-2918365" in body and "Kaveri Instruments Inc." in body
    assert "Vendor GSTIN" not in body  # the Indian fields are hidden when empty on a US invoice
    page.click('[data-tab="checks"]')
    assert "US sales tax charged ($474.38)" in page.inner_text("#tab-body")


def test_tester_journey_across_erp_and_desk(page, base_url, tmp_path):
    """PO and first delivery into the ERP, invoice held at the desk, second delivery, Check again: approved."""
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(__file__))
    from test_erp import _tester_docs
    _tester_docs(tmp_path)
    page.goto(f"{base_url}/erp/#/pos/new")
    page.wait_for_selector("#upload input[type=file]", state="attached")
    page.set_input_files("#upload input[type=file]", str(tmp_path / "po.pdf"))
    page.wait_for_selector("#read-by:not([hidden])")
    assert page.input_value('[name="po_number"]') == "PO-T-1" and page.input_value('[name="v_name"]') == "Tester Labs Pvt Ltd"
    page.fill('[name="v_bank"]', "50100012345678")
    page.fill('[name="v_code"]', "HDFC0001234")
    page.click("text=Save purchase order")
    page.wait_for_url("**/#/pos/PO-T-1")
    page.click("text=Record a goods receipt")
    page.wait_for_selector("#upload input[type=file]", state="attached")
    page.set_input_files("#upload input[type=file]", str(tmp_path / "grn1.pdf"))
    page.wait_for_function("document.querySelector('[name=\"acc\"]') && document.querySelector('[name=\"acc\"]').value === '6'")
    page.click("text=Save goods receipt")
    page.wait_for_url("**/#/pos/PO-T-1")
    # the invoice at the desk
    page.goto(f"{base_url}/#/process")
    page.wait_for_selector("#file", state="attached")
    assert "add the PO" in page.inner_text("#own-invoice")
    before = page.url
    page.set_input_files("#file", str(tmp_path / "inv.pdf"))
    page.wait_for_function("u => location.href !== u", arg=before, timeout=10000)
    assert stamp_text(page) == "ON HOLD"
    page.click('[data-tab="lines"]')
    body = page.inner_text("#tab-body")
    assert "GRN-T-1" in body and "10 billed, 6 available" in body
    assert page.locator(".erp-fix a").get_attribute("href").startswith("/erp/#/receipts/new?po=PO-T-1")
    # the rest arrives and is recorded in the ERP; AP checks again
    run_url = page.url
    page.goto(f"{base_url}/erp/#/receipts/new?po=PO-T-1")
    page.wait_for_selector('[name="rec"]')
    page.fill('[name="rec"]', "4")
    page.click("text=Save goods receipt")
    page.wait_for_url("**/#/pos/PO-T-1")
    page.goto(run_url)
    page.wait_for_selector('[data-act="recheck"]')
    page.click('[data-act="recheck"]')
    page.wait_for_function("document.querySelector('#stamp-word').textContent === 'APPROVED'", timeout=15000)
    assert "ERP connected" in page.inner_text("#erp-pill")
