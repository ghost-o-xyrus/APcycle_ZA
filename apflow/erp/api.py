"""Kaveri ERP's integration API, shaped like NetSuite's REST record API.

  GET  /erp/api/record/v1/vendor                     list (add ?includeReference=true for reference records)
  GET  /erp/api/record/v1/vendor/<id>
  POST /erp/api/record/v1/vendor
  GET  /erp/api/record/v1/purchaseOrder              list, lines under item.items
  GET  /erp/api/record/v1/purchaseOrder/<id>
  POST /erp/api/record/v1/purchaseOrder
  POST /erp/api/record/v1/purchaseOrder/<id>/!transform/itemReceipt   receive goods against a PO
  GET  /erp/api/record/v1/itemReceipt                list
  GET  /erp/api/record/v1/itemReceipt/<id>

Every call needs `Authorization: Bearer <token>`: an integration token issued to the AP product,
as a NetSuite administrator issues one to an integration record. Wrong or missing token: 401.
"""
from __future__ import annotations

import hmac
import os
import secrets

from flask import Blueprint, jsonify, request

from .db import ErpDB, ErpError


def api_token(db: ErpDB, env=os.environ) -> str:
    """The integration token: APFLOW_ERP_TOKEN if set, else one generated once and kept with the ERP's data."""
    if env.get("APFLOW_ERP_TOKEN"):
        return env["APFLOW_ERP_TOKEN"]
    path = os.path.join(db.folder, "integration_token")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_urlsafe(24))
        os.chmod(path, 0o600)
    return open(path).read().strip()


# ---------------- records as the API shows them ----------------

def vendor_json(v: dict) -> dict:
    return {"id": v["vendor_id"], "companyName": v["name"], "country": v["country"], "gstin": v["gstin"] or None,
            "taxId": v["tax_id"] or None, "state": v["state"] or None, "currency": v["currency"],
            "bankAccount": v["bank_account"] or None, "bankCode": v["bank_code"] or None, "status": v["status"],
            "terms": v["terms_days"], "source": v["source"]}


def po_json(p: dict) -> dict:
    return {"id": p["po_number"], "tranId": p["po_number"], "tranDate": p["po_date"], "status": p["status"],
            "entity": {"id": p["vendor_id"], "refName": p.get("vendor_name")}, "subsidiary": p["entity"],
            "currency": p["currency"], "kind": p["kind"], "externalBuyer": p.get("external_buyer"),
            "source": p["source"], "document": f"/erp/docs/{p['doc_id']}" if p.get("doc_id") else None,
            "item": {"items": [{"line": l["line_no"], "description": l["description"], "hsn": l["hsn"] or None,
                                "units": l["uom"], "quantity": l["qty_ordered"], "rate": l["unit_price"],
                                "priceBasis": l["price_basis"], "taxRate": l["tax_rate"],
                                "requiresReceipt": bool(l["requires_receipt"])} for l in p["lines"]]}}


def receipt_json(r: dict) -> dict:
    return {"id": r["receipt_no"], "tranId": r["receipt_no"], "tranDate": r["receipt_date"],
            "createdFrom": {"id": r["po_number"]}, "receivedBy": r["received_by"], "source": r["source"],
            "document": f"/erp/docs/{r['doc_id']}" if r.get("doc_id") else None,
            "item": {"items": [{"orderLine": l["line_no"], "quantityReceived": l["received"],
                                "quantityAccepted": l["accepted"]} for l in r["lines"]]}}


def listing(items):
    return {"links": [], "count": len(items), "hasMore": False, "offset": 0, "totalResults": len(items), "items": items}


def make_api(db: ErpDB, on_call=None) -> Blueprint:
    bp = Blueprint("erp_api", __name__, url_prefix="/erp/api/record/v1")

    @bp.before_request
    def auth():
        got = request.headers.get("Authorization", "")
        want = "Bearer " + api_token(db)
        if not hmac.compare_digest(got.encode(), want.encode()):
            return jsonify({"title": "Unauthorized", "status": 401,
                            "o:errorDetails": [{"detail": "Invalid login attempt: missing or wrong integration token."}]}), 401
        if on_call:
            on_call(request.method, request.path)

    @bp.errorhandler(ErpError)
    def refused(e):
        return jsonify({"title": "Bad Request", "status": 400, "o:errorDetails": [{"detail": str(e)}]}), 400

    def ref_ok():
        return request.args.get("includeReference") == "true"

    @bp.get("/vendor")
    def vendors():
        return jsonify(listing([vendor_json(v) for v in db.vendors() if ref_ok() or v["source"] != "reference"]))

    @bp.get("/vendor/<vid>")
    def vendor(vid):
        v = db.vendor(vid)
        return (jsonify(vendor_json(v)), 200) if v else (jsonify({"title": "Not Found", "status": 404}), 404)

    @bp.post("/vendor")
    def new_vendor():
        b = request.get_json(silent=True) or {}
        v = db.create_vendor({"name": b.get("companyName"), "country": b.get("country"), "gstin": b.get("gstin"),
                              "tax_id": b.get("taxId"), "currency": b.get("currency"), "bank_account": b.get("bankAccount"),
                              "bank_code": b.get("bankCode"), "terms_days": b.get("terms")}, actor="api")
        return jsonify(vendor_json(v)), 201

    @bp.get("/purchaseOrder")
    def pos():
        return jsonify(listing([po_json(p) for p in db.purchase_orders() if ref_ok() or p["source"] != "reference"]))

    @bp.get("/purchaseOrder/<po>")
    def po(po):
        p = db.purchase_order(po)
        return (jsonify(po_json(p)), 200) if p else (jsonify({"title": "Not Found", "status": 404}), 404)

    @bp.post("/purchaseOrder")
    def new_po():
        b = request.get_json(silent=True) or {}
        p = db.create_po({"po_number": b.get("tranId"), "vendor_id": (b.get("entity") or {}).get("id"),
                          "po_date": b.get("tranDate"), "entity": b.get("subsidiary") or "IN", "currency": b.get("currency"),
                          "kind": b.get("kind") or "goods",
                          "lines": [{"line_no": i.get("line"), "description": i.get("description"), "hsn": i.get("hsn"),
                                     "uom": i.get("units"), "qty_ordered": i.get("quantity"), "unit_price": i.get("rate"),
                                     "price_basis": i.get("priceBasis"), "tax_rate": i.get("taxRate"),
                                     "requires_receipt": i.get("requiresReceipt", True)}
                                    for i in (b.get("item") or {}).get("items", [])]}, actor="api")
        return jsonify(po_json(p)), 201

    @bp.post("/purchaseOrder/<po>/!transform/itemReceipt")
    def receive(po):
        b = request.get_json(silent=True) or {}
        r = db.create_receipt({"po_number": po, "receipt_no": b.get("tranId"), "receipt_date": b.get("tranDate"),
                               "received_by": b.get("receivedBy"),
                               "lines": [{"line_no": i.get("orderLine"), "received": i.get("quantityReceived"),
                                          "accepted": i.get("quantityAccepted", i.get("quantityReceived"))}
                                         for i in (b.get("item") or {}).get("items", [])]}, actor="api")
        return jsonify(receipt_json(r)), 201

    @bp.get("/itemReceipt")
    def receipts():
        return jsonify(listing([receipt_json(r) for r in db.receipts()]))

    @bp.get("/itemReceipt/<rid>")
    def receipt(rid):
        r = db.receipt(rid)
        return (jsonify(receipt_json(r)), 200) if r else (jsonify({"title": "Not Found", "status": 404}), 404)

    return bp
