"""Kaveri ERP's own screens: where Purchasing enters POs and Stores record goods receipts.

Open to anyone with the link, for testers of this demo; the integration API (api.py) is the part
that needs a token. Uploaded PO and receipt PDFs are read into drafts that a person reviews.
"""
from __future__ import annotations

import os
import tempfile

from flask import Blueprint, jsonify, request, send_file, send_from_directory

from .api import api_token
from .db import ErpError
from .reader import read_po, read_receipt

HERE = os.path.dirname(__file__)


def make_ui(svc) -> Blueprint:
    db = svc.erp_db
    bp = Blueprint("erp_ui", __name__, url_prefix="/erp", static_folder=os.path.join(HERE, "static"),
                   static_url_path="/static")

    @bp.errorhandler(ErpError)
    def refused(e):
        return jsonify(error=str(e)), 400

    @bp.get("/")
    def page():
        return send_from_directory(bp.static_folder, "erp.html")

    @bp.get("/docs/<doc_id>")
    def doc(doc_id):
        d = db.document(doc_id)
        if not d or not os.path.exists(d["path"]):
            return jsonify(error="No such document"), 404
        return send_file(d["path"], mimetype="application/pdf", download_name=d["file_name"])

    def _pdf_upload(kind):
        f = request.files.get("file")
        if not f:
            raise ErpError("Choose a PDF to upload")
        data = f.read()
        if not data.startswith(b"%PDF"):
            raise ErpError("That is not a PDF")
        if len(data) > 15 * 1024 * 1024:
            raise ErpError("That file is over 15 MB")
        doc_id = db.add_document(kind, os.path.basename(f.filename or f"{kind}.pdf")[:120], data)
        return doc_id, db.document(doc_id)["path"]

    @bp.get("/ui/overview")
    def overview():
        tok = api_token(db)
        s = svc.erp.status()
        return jsonify(counts=db.counts(), audit=db.audit(15),
                       integration={"base": request.host_url.rstrip("/") + "/erp/api/record/v1",
                                    "token_hint": f"{tok[:4]}…{tok[-4:]}", "desk_mode": s["mode"],
                                    "desk_url": s["url"], "desk_last_sync": s["last_sync"], "desk_calls": s["calls"]})

    @bp.get("/ui/companies")
    def companies():
        from .api import subsidiaries
        return jsonify(subsidiaries(db))

    @bp.post("/ui/companies")
    def new_company():
        return jsonify(db.create_company(request.get_json(silent=True) or {})), 201

    @bp.get("/ui/vendors")
    def vendors():
        return jsonify(db.vendors())

    @bp.post("/ui/vendors")
    def new_vendor():
        return jsonify(db.create_vendor(request.get_json(silent=True) or {})), 201

    @bp.get("/ui/pos")
    def pos():
        out = []
        for p in db.purchase_orders():
            got = db.received_so_far(p["po_number"])
            for l in p["lines"]:
                g = got.get(l["line_no"], {})
                l["received"], l["accepted"] = g.get("received", 0.0), g.get("accepted", 0.0)
            p["value"] = round(sum(l["qty_ordered"] * l["unit_price"] for l in p["lines"]), 2)
            p["receipts"] = [r["receipt_no"] for r in db.receipts() if r["po_number"] == p["po_number"]]
            out.append(p)
        return jsonify(out)

    @bp.get("/ui/receipts")
    def receipts():
        pos = {p["po_number"]: p for p in db.purchase_orders()}
        out = []
        for r in db.receipts():
            p = pos.get(r["po_number"], {})
            names = {l["line_no"]: l["description"] for l in p.get("lines", [])}
            for l in r["lines"]:
                l["description"] = names.get(l["line_no"], "")
            r["vendor_name"] = p.get("vendor_name")
            out.append(r)
        return jsonify(out[::-1])

    @bp.post("/ui/read/po")
    def read_po_doc():
        doc_id, path = _pdf_upload("po")
        return jsonify(doc_id=doc_id, draft=read_po(path, db.vendors(), svc.real_model, db.companies()))

    @bp.post("/ui/pos")
    def new_po():
        b = request.get_json(silent=True) or {}
        if not b.get("vendor_id") and b.get("new_vendor"):
            b["vendor_id"] = db.create_vendor(b["new_vendor"])["vendor_id"]
        return jsonify(db.create_po(b, doc_id=b.get("doc_id"))), 201

    @bp.post("/ui/read/receipt")
    def read_receipt_doc():
        doc_id, path = _pdf_upload("receipt")
        return jsonify(doc_id=doc_id, draft=read_receipt(path, svc.real_model))

    @bp.post("/ui/receipts")
    def new_receipt():
        b = request.get_json(silent=True) or {}
        return jsonify(db.create_receipt(b, doc_id=b.get("doc_id"))), 201

    @bp.post("/ui/reset")
    def reset():
        db.reset_tester_records()
        return jsonify(ok=True, counts=db.counts())

    return bp
