"""Kaveri ERP: the system of record the desk reads POs and goods receipts from, and the tester journey."""
from __future__ import annotations

import io
import os
import threading
from datetime import date

import pytest
from werkzeug.serving import make_server

from apflow.company import PO, POLine, Repo, Vendor
from apflow.erp.api import api_token
from apflow.erp.db import ErpDB, ErpError
from apflow.erp.reader import read_po, read_receipt
from apflow.erp_client import ErpClient, ErpUnavailable
from apflow.generator import InvoiceSpec, SpecLine, render_pdf, render_po_pdf, render_receipt_pdf
from apflow.gstin import with_checksum
from apflow.service import Service
from apflow.web.app import create_app

AS_OF = date(2026, 11, 20)
TESTER_GSTIN = with_checksum("29AAECT7781K1Z")  # a supplier a tester brings, not in the demo master


@pytest.fixture
def svc(tmp_path):
    return Service(str(tmp_path), as_of=AS_OF, model_mode="none")


@pytest.fixture
def client(svc):
    c = create_app(svc).test_client()
    c.token = {"Authorization": "Bearer " + api_token(svc.erp_db)}
    return c


# ---------------- the record itself ----------------

def test_seed_holds_the_demo_company_with_documents(tmp_path):
    db = ErpDB(str(tmp_path / "erp"))
    # demo company 11 vendors, 16 POs, 20 receipts; GeM reference 1 + 1; real-invoice pack 3 vendors, 3 POs, 1 receipt
    assert db.counts() == {"vendors": 15, "purchase_orders": 20, "receipts": 21}
    assert [c["code"] for c in db.companies()] == ["T01", "T02", "T03"]
    for p in db.purchase_orders():
        assert p["doc_id"] and os.path.exists(db.document(p["doc_id"])["path"]), p["po_number"]
    assert all(r["doc_id"] for r in db.receipts())
    ref = db.purchase_order("GEMC-511687772029047")
    assert ref["source"] == "reference" and "Hyderabad" in ref["external_buyer"]


def test_desk_sees_exactly_the_old_company_data_through_the_erp(tmp_path):
    from apflow.erp.realpack import POS as RP_POS, RECEIPTS as RP_REC, VENDORS as RP_V
    r, base = ErpClient(ErpDB(str(tmp_path / "erp"))).repo(), Repo()
    key = lambda x: repr(x).replace(".0,", ",").replace(".0)", ")")  # noqa: E731  200 == 200.0
    rp_pos, rp_v = {p["po_number"] for p in RP_POS}, {v["vendor_id"] for v in RP_V}
    assert sorted(map(key, [v for v in r.vendors.values() if v.vendor_id not in rp_v])) == \
        sorted(map(key, base.vendors.values()))
    assert sorted(map(key, [g for g in r.grns if g.po_number not in rp_pos])) == sorted(map(key, base.grns))
    assert sorted(map(key, [l for ls in r.po_lines.values() for l in ls if l.po_number not in rp_pos])) == \
        sorted(map(key, [l for ls in base.po_lines.values() for l in ls]))
    assert set(r.entities) == {"IN", "US", "DE", "T01", "T02", "T03"}  # the group, plus the real buyers as test companies
    assert r.entities["T02"].name == "Americas PAC" and r.entities["T02"].currency == "USD"
    assert {g.grn_number for g in r.grns if g.po_number in rp_pos} == {x["receipt_no"] for x in RP_REC}
    assert "GEMC-511687772029047" not in r.pos  # another buyer's order never reaches the checks


@pytest.mark.parametrize("bad,msg", [
    ({"po_number": "PO-2026-0101", "lines": [{"line_no": 1, "received": 1, "accepted": 2}]}, "accepted cannot be more"),
    ({"po_number": "PO-2026-0101", "lines": [{"line_no": 1, "received": 30, "accepted": 30}]}, "more than 10% over"),
    ({"po_number": "PO-2026-0101", "lines": [{"line_no": 9, "received": 1, "accepted": 1}]}, "no line 9"),
    ({"po_number": "PO-2026-0108", "lines": [{"line_no": 1, "received": 1, "accepted": 1}]}, "cancelled"),
    ({"po_number": "GEMC-511687772029047", "lines": [{"line_no": 1, "received": 1, "accepted": 1}]}, "closed"),
    ({"po_number": "PO-2026-0101", "lines": [{"line_no": 1, "received": 0, "accepted": 0}]}, "at least one line"),
    ({"po_number": "PO-NOPE", "lines": []}, "Choose the PO"),
])
def test_receipts_are_validated_like_an_erp(tmp_path, bad, msg):
    db = ErpDB(str(tmp_path / "erp"))
    with pytest.raises(ErpError, match=msg):
        db.create_receipt(bad)


def test_vendors_and_pos_are_validated(tmp_path):
    db = ErpDB(str(tmp_path / "erp"))
    with pytest.raises(ErpError, match="valid GSTIN"):
        db.create_vendor({"name": "X", "country": "IN", "gstin": "29AAECT7781K1Z0"})
    with pytest.raises(ErpError, match="not valid"):
        db.create_vendor({"name": "X", "country": "DE", "tax_id": "DE136695977"})
    with pytest.raises(ErpError, match="already exists"):
        db.create_vendor({"name": "Copy", "country": "IN", "gstin": db.vendor("V001")["gstin"]})
    with pytest.raises(ErpError, match="exists in the vendor master"):
        db.create_po({"vendor_id": "V999", "lines": [{"description": "a", "qty_ordered": 1, "unit_price": 1}]})
    with pytest.raises(ErpError, match="tax rate between"):
        db.create_po({"vendor_id": "V001", "lines": [{"description": "a", "qty_ordered": 1, "unit_price": 1, "tax_rate": 99}]})
    with pytest.raises(ErpError, match="already exists"):
        db.create_po({"vendor_id": "V001", "po_number": "PO-2026-0101",
                      "lines": [{"description": "a", "qty_ordered": 1, "unit_price": 1}]})


# ---------------- the API ----------------

def test_api_needs_the_token(client):
    assert client.get("/erp/api/record/v1/purchaseOrder").status_code == 401
    assert client.get("/erp/api/record/v1/purchaseOrder", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/erp/api/record/v1/purchaseOrder", headers=client.token).get_json()
    first = next(i for i in r["items"] if i["id"] == "PO-2026-0101")
    assert r["count"] == 19 and first["item"]["items"][0]["quantity"] == 200  # 16 demo + 3 real-invoice pack
    assert client.get("/erp/api/record/v1/purchaseOrder?includeReference=true", headers=client.token).get_json()["count"] == 20
    assert client.get("/erp/api/record/v1/purchaseOrder/NOPE", headers=client.token).status_code == 404


def test_api_creates_records_the_netsuite_way(client):
    H = client.token
    v = client.post("/erp/api/record/v1/vendor", headers=H, json={"companyName": "Tester Labs", "country": "IN",
                                                                   "gstin": TESTER_GSTIN}).get_json()
    po = client.post("/erp/api/record/v1/purchaseOrder", headers=H, json={
        "entity": {"id": v["id"]}, "subsidiary": "IN", "currency": "INR", "tranDate": "2026-11-01",
        "item": {"items": [{"line": 1, "description": "Lab Stand", "quantity": 10, "rate": 500, "taxRate": 18}]}})
    assert po.status_code == 201
    pid = po.get_json()["id"]
    r = client.post(f"/erp/api/record/v1/purchaseOrder/{pid}/!transform/itemReceipt", headers=H,
                    json={"item": {"items": [{"orderLine": 1, "quantityReceived": 10, "quantityAccepted": 9}]}})
    assert r.status_code == 201 and r.get_json()["createdFrom"]["id"] == pid
    bad = client.post(f"/erp/api/record/v1/purchaseOrder/{pid}/!transform/itemReceipt", headers=H,
                      json={"item": {"items": [{"orderLine": 1, "quantityReceived": 5}]}})
    assert bad.status_code == 400 and "over" in bad.get_json()["o:errorDetails"][0]["detail"]


# ---------------- reading documents ----------------

def test_every_demo_po_and_receipt_reads_back_exactly(tmp_path):
    db = ErpDB(str(tmp_path / "erp"))
    for p in db.purchase_orders():
        if p["source"] == "reference":
            continue
        d = read_po(db.document(p["doc_id"])["path"], db.vendors(), companies=db.companies())
        assert d["po_number"] == p["po_number"] and d["po_date"] == p["po_date"], p["po_number"]
        assert d["vendor"]["match"] == p["vendor_id"] and d["entity"] == p["entity"] and d["currency"] == p["currency"]
        assert d["kind"] == p["kind"] and (d["price_basis"] == "incl_tax") == any(l["price_basis"] == "incl_tax" for l in p["lines"])
        got = [(l["line_no"], l["description"], l["qty_ordered"], l["unit_price"], l["tax_rate"]) for l in d["lines"]]
        assert got == [(l["line_no"], l["description"], l["qty_ordered"], l["unit_price"], l["tax_rate"]) for l in p["lines"]]
    for r in db.receipts():
        d = read_receipt(db.document(r["doc_id"])["path"])
        assert (d["receipt_no"], d["po_number"], d["receipt_date"]) == (r["receipt_no"], r["po_number"], r["receipt_date"])
        assert [(l["line_no"], l["received"], l["accepted"]) for l in d["lines"]] == \
            [(l["line_no"], l["received"], l["accepted"]) for l in r["lines"]]


def test_a_real_gem_contract_is_not_mistaken_for_our_po(tmp_path):
    db = ErpDB(str(tmp_path / "erp"))
    d = read_po(db.document(db.purchase_order("GEMC-511687772029047")["doc_id"])["path"], db.vendors())
    assert d["po_number"] and d["vendor"]["match"] is None  # read, but not one of our vendors


# ---------------- the desk reading the ERP over HTTP ----------------

def test_desk_over_http_with_token_matches_local(svc):
    app = create_app(svc)
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        over_http = ErpClient(None, url, api_token(svc.erp_db))
        assert over_http.snapshot() == ErpClient(svc.erp_db).snapshot() and over_http.calls == 4
        with pytest.raises(ErpUnavailable, match="401"):
            ErpClient(None, url, "wrong").snapshot()
        svc.erp = over_http  # the desk now reads only through the API
        run = svc.wait(svc.start_sample("S01"))
        assert run["outcome"] == "APPROVE" and over_http.calls >= 3
    finally:
        srv.shutdown()


def test_erp_down_fails_the_run_with_a_clear_reason(svc):
    svc.erp = ErpClient(None, "http://127.0.0.1:9", "t", timeout=1)
    run = svc.wait(svc.start_sample("S01"))
    assert run["state"] == "error" and "ERP not reachable" in run["error"]


# ---------------- the tester's journey: vendor, PO, receipt in the ERP; invoice at the desk ----------------

def _tester_docs(tmp_path):
    v = Vendor("V901", "Tester Labs Pvt Ltd", TESTER_GSTIN, "29", "50100012345678", "HDFC0001234", "approved")
    po = PO("PO-T-1", "V901", "2026-11-01", "open", "goods")
    lines = [POLine("PO-T-1", 1, "Lab Retort Stand", "9020", "pcs", 10, 500.0, "excl_tax", 18, True)]
    render_po_pdf(po, lines, v, str(tmp_path / "po.pdf"))
    render_receipt_pdf("GRN-T-1", po, [(lines[0], 6, 6)], v, "2026-11-05", "Stores-A", str(tmp_path / "grn1.pdf"))
    render_receipt_pdf("GRN-T-2", po, [(lines[0], 4, 4)], v, "2026-11-08", "Stores-A", str(tmp_path / "grn2.pdf"))
    render_pdf(InvoiceSpec(v, "TL/2026/77", "2026-11-10", "PO-T-1",
                           [SpecLine("Lab Retort Stand", "9020", "pcs", 10, 500.0, 18)]), str(tmp_path / "inv.pdf"))
    return v


def _upload(client, path, kind):
    data = open(path, "rb").read()
    return client.post(f"/erp/ui/read/{kind}", data={"file": (io.BytesIO(data), os.path.basename(path))},
                       content_type="multipart/form-data").get_json()


def test_tester_journey_partial_receipt_then_full(svc, client, tmp_path):
    _tester_docs(tmp_path)
    # 1-2. Upload the PO: the ERP reads it; the tester saves it with the new vendor.
    r = _upload(client, tmp_path / "po.pdf", "po")
    d = r["draft"]
    assert d["po_number"] == "PO-T-1" and d["vendor"]["match"] is None and d["vendor"]["gstin"] == TESTER_GSTIN
    saved = client.post("/erp/ui/pos", json={**d, "vendor_id": None, "doc_id": r["doc_id"], "new_vendor": {
        "name": d["vendor"]["name"], "country": "IN", "gstin": TESTER_GSTIN, "bank_account": "50100012345678",
        "bank_code": "HDFC0001234"}})
    assert saved.status_code == 201, saved.get_json()
    # 3. First delivery: 6 of 10 accepted.
    g = _upload(client, tmp_path / "grn1.pdf", "receipt")["draft"]
    assert g["po_number"] == "PO-T-1" and g["lines"][0]["accepted"] == 6
    assert client.post("/erp/ui/receipts", json=g).status_code == 201
    # 4. The invoice bills all 10: held for Stores, with 6 payable now.
    run = svc.wait(svc.start_upload(open(tmp_path / "inv.pdf", "rb").read(), "inv.pdf"))
    assert run["outcome"] == "HOLD" and run["owner"] == "Stores"
    m = run["matches"][0]
    assert [x["grn"] for x in m["receipts"]] == ["GRN-T-1"] and not m["qty_ok"] and m["available"] == 6
    # The rest arrives; Stores records it; AP checks again and it is approved.
    g2 = _upload(client, tmp_path / "grn2.pdf", "receipt")["draft"]
    assert client.post("/erp/ui/receipts", json=g2).status_code == 201
    after = svc.act(run["run_id"], "recheck")
    assert after["final_status"] == "approved" and after["outcome"] == "APPROVE"
    assert [x["grn"] for x in after["matches"][0]["receipts"]] == ["GRN-T-1", "GRN-T-2"]
    assert after["actions"][-1]["action"] == "recheck"


def test_invoice_before_its_po_is_held_then_passes_once_the_erp_has_it(svc, client, tmp_path):
    _tester_docs(tmp_path)
    run = svc.wait(svc.start_upload(open(tmp_path / "inv.pdf", "rb").read(), "inv.pdf"))
    assert run["outcome"] == "HOLD" and {"C07"} <= {c["id"] for c in run["checks"] if c["status"] == "fail"}
    with pytest.raises(Exception, match="not in the vendor master"):
        svc.act(run["run_id"], "approve", "trust me")
    r = _upload(client, tmp_path / "po.pdf", "po")
    client.post("/erp/ui/pos", json={**r["draft"], "doc_id": r["doc_id"], "new_vendor": {
        "name": "Tester Labs Pvt Ltd", "country": "IN", "gstin": TESTER_GSTIN, "bank_account": "50100012345678",
        "bank_code": "HDFC0001234"}})
    client.post("/erp/ui/receipts", json={"po_number": "PO-T-1", "lines": [{"line_no": 1, "received": 10, "accepted": 10}]})
    assert svc.act(run["run_id"], "recheck")["final_status"] == "approved"


def test_tester_records_can_be_removed(svc, client, tmp_path):
    client.post("/erp/ui/vendors", json={"name": "Temp", "country": "IN", "gstin": TESTER_GSTIN})
    assert svc.erp_db.counts()["vendors"] == 16
    client.post("/erp/ui/reset", json={})
    assert svc.erp_db.counts() == {"vendors": 15, "purchase_orders": 20, "receipts": 21}


def test_erp_pages_and_documents_are_served(client, svc):
    assert b"Kaveri ERP" in client.get("/erp/").data
    doc = svc.erp_db.purchase_order("PO-2026-0101")["doc_id"]
    r = client.get(f"/erp/docs/{doc}")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")
    assert client.get("/erp/docs/nope").status_code == 404
    assert client.post("/erp/ui/read/po", data={"file": (io.BytesIO(b"hello"), "x.pdf")},
                       content_type="multipart/form-data").status_code == 400
    st = client.get("/api/erp/status").get_json()
    assert st["error"] is None and st["records"]["purchaseOrders"] == 19


def test_deployed_mode_desk_reads_its_erp_over_http(tmp_path):
    """As on Render: one process, APFLOW_ERP_URL=self, the desk calls the ERP's API with the token."""
    import json
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "APFLOW_DATA": str(tmp_path / "data"), "APFLOW_MODEL": "none", "PORT": str(port),
           "APFLOW_ERP_URL": "self", "HTTPS_PROXY": "", "HTTP_PROXY": "", "https_proxy": "", "http_proxy": ""}
    proc = subprocess.Popen([sys.executable, "-m", "apflow.web.app"], env=env, cwd=os.path.dirname(os.path.dirname(__file__)),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    get = lambda p: json.loads(opener.open(f"http://127.0.0.1:{port}{p}", timeout=30).read())  # noqa: E731
    try:
        for _ in range(120):
            try:
                get("/api/config")
                break
            except OSError:
                time.sleep(0.5)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/runs", data=json.dumps({"sample_id": "S01"}).encode(),
                                     headers={"Content-Type": "application/json"})
        rid = json.loads(opener.open(req, timeout=30).read())["run_id"]
        for _ in range(120):
            run = get(f"/api/runs/{rid}")
            if run["state"] != "running":
                break
            time.sleep(0.5)
        assert run["outcome"] == "APPROVE", run.get("error")
        st = get("/api/erp/status")
        assert st["mode"] == "api" and st["url"] == f"http://127.0.0.1:{port}" and st["calls"] >= 3 and not st["error"]
    finally:
        proc.terminate()
        proc.wait(10)


def test_goods_cannot_be_received_against_another_buyers_order_even_if_open(tmp_path):
    db = ErpDB(str(tmp_path / "erp"))
    db._x("UPDATE purchase_orders SET status='open' WHERE po_number='GEMC-511687772029047'")
    with pytest.raises(ErpError, match="another buyer"):
        db.create_receipt({"po_number": "GEMC-511687772029047", "lines": [{"line_no": 1, "received": 1, "accepted": 1}]})


def test_test_companies_let_a_real_invoice_be_tested(svc, client, tmp_path):
    """A tester adds the buyer printed on a real invoice, then a PO ordered by it."""
    db = svc.erp_db
    with pytest.raises(ErpError, match="name"):
        db.create_company({"country": "US"})
    with pytest.raises(ErpError, match="Country"):
        db.create_company({"name": "X", "country": "JP"})
    with pytest.raises(ErpError, match="GSTIN"):
        db.create_company({"name": "Y", "country": "IN", "tax_id": "29ABCDE1234F1Z5"})
    c = client.post("/erp/ui/companies", json={"name": "Leo Burnett USA", "country": "US"}).get_json()
    assert c["code"] == "T04" and c["currency"] == "USD" and c["source"] == "tester"
    v = db.create_vendor({"name": "Eller Media Company", "country": "US"})
    p = db.create_po({"vendor_id": v["vendor_id"], "entity": "T04", "currency": "USD", "kind": "services",
                      "lines": [{"description": "Bulletin space", "qty_ordered": 1, "unit_price": 1750}]})
    assert p["entity"] == "T04"
    names = {x["id"]: x["name"] for x in client.get("/erp/ui/companies").get_json()}
    assert names["T04"] == "Leo Burnett USA" and names["IN"].startswith("Kaveri")
    db.reset_tester_records()
    assert db.company("T04") is None and db.company("T01")  # the real-invoice pack stays
