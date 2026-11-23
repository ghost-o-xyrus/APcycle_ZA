"""The real-invoice test pack: buyer-side records made up to match three real, public invoices.

The invoices are real documents from the public DocILE dataset (US business invoices, 2000-2019).
Nothing on our side existed, so the vendor, the buyer (as a test company), the purchase order and,
for goods, the goods receipt are entered here as that buyer's AP team would have them. Each is
set up for one designed outcome, so the desk can be tested on documents nobody here wrote:

  Arista Laboratories #913 -> Philip Morris USA      PO 8500005968 printed; lab tests     approve (sign-off)
  NRG Media IN-11912105668 -> Americas PAC           radio spots billed $26, PO says $24  hold: price
  JKS MotorSports #11590   -> Sports Marketing Ent.  signs; Stores accepted 3 of 4 vinyls  hold: quantity

The five documents are public records (UCSF Industry Documents Library, FCC political files), as
collected in the DocILE dataset; they ship in realpack_docs/ and appear on the desk as samples D1-D5.
D4 and D5 are real documents that are not invoices: a purchase requisition and a credit request.
The stand-in reader's answers for D1-D3 are our transcriptions of the printed pages; with a real
reader (Gemini, or a model on your computer) the desk reads them itself.
"""
from __future__ import annotations

import os
import time

COMPANIES = [
    {"code": "T01", "name": "Philip Morris USA", "country": "US", "currency": "USD",
     "address": "615 Maury Street, Richmond, VA 23224"},
    {"code": "T02", "name": "Americas PAC", "country": "US", "currency": "USD",
     "address": "2560 Plymouth Street, Marion, IA 52302"},
    {"code": "T03", "name": "Sports Marketing Enterprise", "country": "US", "currency": "USD",
     "address": "P.O. Box 2959, Winston-Salem, NC 27102"},
]

VENDORS = [
    {"vendor_id": "RP01", "name": "Arista Laboratories", "country": "US", "currency": "USD", "terms_days": 30},
    {"vendor_id": "RP02", "name": "NRG Media LLC", "country": "US", "currency": "USD", "terms_days": 30},
    {"vendor_id": "RP03", "name": "JKS MotorSports", "country": "US", "currency": "USD", "terms_days": 10},
]

_TESTS = [("Metals I", 521.0), ("Mercury", 179.0), ("Nitric Oxide", 225.0), ("Ammonia", 246.0),
          ("Hydrogen Cyanide", 230.0), ("Aromatic Amines I", 434.0)]

POS = [
    {"po_number": "8500005968", "vendor_id": "RP01", "entity": "T01", "po_date": "2004-03-15", "kind": "services",
     "currency": "USD",
     "lines": [{"description": d, "qty_ordered": 50, "unit_price": r, "uom": "tests"} for d, r in _TESTS]
     + [{"description": d, "qty_ordered": 85, "unit_price": r, "uom": "tests"} for d, r in _TESTS]
     + [{"description": "Massachusetts Conditions", "qty_ordered": 1, "unit_price": 7798.75, "uom": "lot"},
        {"description": "Report", "qty_ordered": 2, "unit_price": 300.0, "uom": "each"}]},
    {"po_number": "8843566", "vendor_id": "RP02", "entity": "T02", "po_date": "2019-11-15", "kind": "services",
     "currency": "USD",
     "lines": [{"description": "WMQA-FM spots", "qty_ordered": 200, "unit_price": 24.0, "uom": "spots"}]},
    {"po_number": "SME-0712", "vendor_id": "RP03", "entity": "T03", "po_date": "2000-07-12", "kind": "goods",
     "currency": "USD",
     "lines": [
         {"description": "8' x 16' NWCS Concrete Grandstand", "qty_ordered": 3, "unit_price": 384.0},
         {"description": "12' x 36' NWCS Billboards", "qty_ordered": 3, "unit_price": 1296.0},
         {"description": "10' x 72' NWCS End of Backstretch", "qty_ordered": 1, "unit_price": 2160.0},
         {"description": "9' x 96' NWCS Pedestrian Bridge", "qty_ordered": 2, "unit_price": 2592.0},
         {"description": "20' x 20' NWCS Pedestrian Bridge", "qty_ordered": 4, "unit_price": 1200.0},
         {"description": "6' x 120' NWCS Roof of GT Center", "qty_ordered": 2, "unit_price": 2016.0},
         {"description": "4' x 36' NWCS Scoreboards", "qty_ordered": 4, "unit_price": 403.20},
         {"description": "6' x 16' NWCS Scoreboards", "qty_ordered": 4, "unit_price": 288.0},
         {"description": "4' x 28' NWCS Pit Terrace Grandstand", "qty_ordered": 2, "unit_price": 313.60},
         {"description": "12' x 20' NWCS Glen Club", "qty_ordered": 2, "unit_price": 720.0},
         {"description": "7' x 16' NWCS Gate 2 Entrance", "qty_ordered": 2, "unit_price": 336.0},
         {"description": "36' x 72' NWCS Paddock Sign", "qty_ordered": 2, "unit_price": 7257.60},
         {"description": "4' x 8' NWCS Victory Lane Vinyl with Silver Keyline", "qty_ordered": 4, "unit_price": 256.0},
         {"description": "4' x 24' NWCS, Global, Watkins Glen, Victory Lane", "qty_ordered": 1, "unit_price": 432.0},
         {"description": "4' x 24' NWCS, Repaint & Vinyl", "qty_ordered": 2, "unit_price": 432.0},
         {"description": "1' x 12' NWCS Inside Tunnel", "qty_ordered": 1, "unit_price": 36.0},
         {"description": "25'6\" x 25'6\" x 23'6\" Flex Face NWCS Souvenirs Sold Here Bldg", "qty_ordered": 4,
          "unit_price": 1406.25},
         {"description": "Materials for install", "qty_ordered": 1, "unit_price": 163.99, "requires_receipt": False},
         {"description": "Install labor", "qty_ordered": 1, "unit_price": 480.0, "requires_receipt": False},
         {"description": "Paint materials", "qty_ordered": 1, "unit_price": 2743.03, "requires_receipt": False},
         {"description": "Lift rental", "qty_ordered": 1, "unit_price": 1620.0, "requires_receipt": False},
     ]},
]

# Stores counted everything for the JKS order; one Victory Lane vinyl arrived torn and was not accepted.
RECEIPTS = [{"receipt_no": "RR-2000-0806", "po_number": "SME-0712", "receipt_date": "2000-08-06",
             "received_by": "Stores, Watkins Glen",
             "short": {13: 3}}]  # line_no -> accepted (received = ordered)


def seed_realpack(db) -> None:
    from ..company import PO, Entity, POLine, Vendor
    from ..generator import render_po_pdf, render_receipt_pdf
    with db._lock:
        for c in COMPANIES:
            db._conn.execute("INSERT OR IGNORE INTO companies (code, name, country, tax_id, currency, address, source, "
                             "created_at) VALUES (?,?,?,?,?,?,?,?)",
                             (c["code"], c["name"], c["country"], "", c["currency"], c["address"], "realpack", time.time()))
        for v in VENDORS:
            db._conn.execute("INSERT OR IGNORE INTO vendors (vendor_id, name, gstin, state, bank_account, bank_code, "
                             "status, terms_days, country, tax_id, currency, source, created_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (v["vendor_id"], v["name"], "", "", "", "", "approved", v["terms_days"], v["country"], "",
                              v["currency"], "realpack", time.time()))
    ents = {c["code"]: Entity(c["code"], c["name"], c["country"], "", c["currency"], c["address"], "EIN")
            for c in COMPANIES}
    vend = {v["vendor_id"]: Vendor(v["vendor_id"], v["name"], "", "", "", "", "approved", v["terms_days"],
                                   v["country"], "", v["currency"]) for v in VENDORS}
    for p in POS:
        if db.purchase_order(p["po_number"]):
            continue
        po = PO(p["po_number"], p["vendor_id"], p["po_date"], "open", p["kind"], p["entity"], p["currency"])
        lines = [POLine(p["po_number"], i, l["description"], "", l.get("uom", "ea"), l["qty_ordered"], l["unit_price"],
                        "excl_tax", 0.0, l.get("requires_receipt", True) and p["kind"] == "goods")
                 for i, l in enumerate(p["lines"], 1)]
        path = os.path.join(db.docs_dir, f"rp-{p['po_number']}.pdf")
        render_po_pdf(po, lines, vend[p["vendor_id"]], path, ent=ents[p["entity"]])
        doc = db.add_document("po", f"{p['po_number']}.pdf", open(path, "rb").read())
        os.remove(path)
        db.create_po({**p, "lines": [{**l, "line_no": i, "uom": l.get("uom", "ea")} for i, l in enumerate(p["lines"], 1)]},
                     actor="system", source="realpack", doc_id=doc)
    for r in RECEIPTS:
        if db.receipt(r["receipt_no"]):
            continue
        pod = db.purchase_order(r["po_number"])
        po = PO(pod["po_number"], pod["vendor_id"], pod["po_date"], "open", pod["kind"], pod["entity"], pod["currency"])
        rows, lines = [], []
        for l in pod["lines"]:
            if not l["requires_receipt"]:
                continue
            acc = r["short"].get(l["line_no"], l["qty_ordered"])
            pl = POLine(pod["po_number"], l["line_no"], l["description"], "", l["uom"], l["qty_ordered"], l["unit_price"],
                        "excl_tax", 0.0, True)
            rows.append((pl, l["qty_ordered"], acc))
            lines.append({"line_no": l["line_no"], "received": l["qty_ordered"], "accepted": acc})
        path = os.path.join(db.docs_dir, f"rp-{r['receipt_no']}.pdf")
        render_receipt_pdf(r["receipt_no"], po, rows, vend[pod["vendor_id"]], r["receipt_date"], r["received_by"], path,
                           ent=ents[pod["entity"]])
        doc = db.add_document("receipt", f"{r['receipt_no']}.pdf", open(path, "rb").read())
        os.remove(path)
        db.create_receipt({"po_number": r["po_number"], "receipt_no": r["receipt_no"], "receipt_date": r["receipt_date"],
                           "received_by": r["received_by"], "lines": lines}, actor="system", source="realpack", doc_id=doc)
    db._commit("system", "load real-invoice test pack", "*", {"pos": len(POS)})


DOCS = os.path.join(os.path.dirname(__file__), "realpack_docs")
_T = lambda d, q, p: {"description": d, "qty": q, "unit_price": p, "amount": round(q * p, 2), "uom": None, "hsn": None}
_JKS = [("8' x 16' NWCS CONCRETE GRANDSTAND", 3, 384), ("12' x 36' NWCS BILLBOARDS", 3, 1296),
        ("10' x 72' NWCS-END OF BACKSTRETCH", 1, 2160), ("9' x 96' NWCS PEDESTRIAN BRIDGE", 2, 2592),
        ("20\" x 20' NWCS PEDESTRIAN BRIDGE", 4, 1200), ("6' x 120' NWCS ROOF OF GT CENTER", 2, 2016),
        ("4' x 36' NWCS SCOREBOARDS", 4, 403.2), ("6' x 16' NWCS SCOREBOARDS", 4, 288),
        ("4' x 28' NWCS PIT TERRACE GRANDSTAND", 2, 313.6), ("12' x 20' NWCS GLEN CLUB", 2, 720),
        ("7' x 16' NWCS GATE 2 ENTRANCE", 2, 336), ("36' x 72' NWCS PADDOCK SIGN", 2, 7257.6),
        ("4' x 8' NWCS VICTORY LANE VINYL WITH SILVER KEYLINE", 4, 256),
        ("4' x 24' NWCS, GLOBAL, WATKINS GLEN, VICTORY LANE", 1, 432), ("4' x 24' NWCS, REPAINT & VINYL", 2, 432),
        ("1' x 12' NWCS INSIDE TUNNEL", 1, 36), ("25'6\" x 25'6\" x 23'6\" FLEX FACE NWCS SOUVENIRS SOLD HERE BLDG", 4, 1406.25),
        ("MATERIALS FOR INSTALL", 1, 163.99), ("INSTALL LABOR", 1, 480), ("PAINT MATERIALS", 1, 2743.03),
        ("LIFT RENTAL", 1, 1620)]

# id, file, title, vendor, designed outcome, what a careful reader of the page returns
REAL_SAMPLES = [
    ("D1", "D1-arista-laboratories-913.pdf", "Real: lab tests for Philip Morris USA, PO printed (2004)",
     "Arista Laboratories", "APPROVE",
     {"doc_type": "invoice", "vendor_name": "Arista Laboratories", "buyer_name": "Philip Morris USA",
      "invoice_number": "913", "invoice_date": "2004-04-30", "po_reference": "8500005968", "currency": "USD",
      "subtotal": 256123.75, "tax": 0.0, "total": 256123.75,
      "lines": [_T(d, 50, p) for d, p in _TESTS] + [_T(d, 85, p) for d, p in _TESTS]
      + [_T("Massachusetts Conditions", 1, 7798.75), _T("Report", 2, 300.0)]}),
    ("D2", "D2-nrg-media-IN-11912105668.pdf", "Real: radio spots billed above the agreed rate (2019)",
     "NRG Media LLC", "HOLD",
     {"doc_type": "invoice", "vendor_name": "NRG Media LLC", "buyer_name": "Americas PAC",
      "invoice_number": "IN-11912105668", "invoice_date": "2019-12-29", "po_reference": None, "currency": "USD",
      "subtotal": 5200.0, "tax": 0.0, "total": 5200.0, "lines": [_T("WMQA-FM Spots", 200, 26.0)]}),
    ("D3", "D3-jks-motorsports-11590.pdf", "Real: track signs, one not accepted at goods receipt (2000)",
     "JKS MotorSports", "HOLD",
     {"doc_type": "invoice", "vendor_name": "JKS MotorSports", "buyer_name": "Sports Marketing Enterprise",
      "invoice_number": "11590", "invoice_date": "2000-08-08", "po_reference": None, "currency": "USD",
      "subtotal": 54223.22, "tax": 0.0, "total": 54223.22, "lines": [_T(d, q, p) for d, q, p in _JKS]}),
    ("D4", "D4-philip-morris-purchase-requisition.pdf", "Real: a purchase requisition, not an invoice (2001)",
     "Philip Morris U.S.A.", "REJECT", {"doc_type": "purchase_order"}),
    ("D5", "D5-wtov-credit-request.pdf", "Real: a billing credit request, not an invoice (2020)",
     "WTOV", "HOLD", {"doc_type": "credit_note"}),
]
