"""Kaveri ERP: the system of record for vendors, purchase orders and goods receipts.

It stands in for what an AP automation product connects to at a client (NetSuite, SAP S/4HANA):
buying and Stores enter POs and goods receipts here; the invoice desk only reads them, through
the API in api.py, with a token. It has its own database file and its own document store.

Records carry a `source`: "seed" (the demo company's own history, with generated documents),
"tester" (added through the ERP screens), or "reference" (real public documents kept for
comparison, such as a GeM contract placed by another buyer).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
  vendor_id TEXT PRIMARY KEY, name TEXT NOT NULL, gstin TEXT DEFAULT '', state TEXT DEFAULT '',
  bank_account TEXT DEFAULT '', bank_code TEXT DEFAULT '', status TEXT DEFAULT 'approved', terms_days INTEGER DEFAULT 30,
  country TEXT DEFAULT 'IN', tax_id TEXT DEFAULT '', currency TEXT DEFAULT 'INR', source TEXT DEFAULT 'seed',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS purchase_orders (
  po_number TEXT PRIMARY KEY, vendor_id TEXT NOT NULL, po_date TEXT, status TEXT DEFAULT 'open', kind TEXT DEFAULT 'goods',
  entity TEXT DEFAULT 'IN', currency TEXT DEFAULT 'INR', external_buyer TEXT, source TEXT DEFAULT 'seed',
  doc_id TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS po_lines (
  po_number TEXT NOT NULL, line_no INTEGER NOT NULL, description TEXT, hsn TEXT DEFAULT '', uom TEXT DEFAULT 'pcs',
  qty_ordered REAL, unit_price REAL, price_basis TEXT DEFAULT 'excl_tax', tax_rate REAL DEFAULT 0,
  requires_receipt INTEGER DEFAULT 1, PRIMARY KEY (po_number, line_no)
);
CREATE TABLE IF NOT EXISTS receipts (
  receipt_no TEXT PRIMARY KEY, po_number TEXT NOT NULL, receipt_date TEXT, received_by TEXT, source TEXT DEFAULT 'seed',
  doc_id TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS receipt_lines (
  receipt_no TEXT NOT NULL, po_number TEXT NOT NULL, line_no INTEGER NOT NULL, received REAL, accepted REAL,
  PRIMARY KEY (receipt_no, line_no)
);
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY, kind TEXT, file_name TEXT, path TEXT, sha256 TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, actor TEXT, action TEXT, record TEXT, detail TEXT
);
"""

REFERENCE_DIR = os.path.join(os.path.dirname(__file__), "reference")


class ErpError(ValueError):
    """A record the ERP refuses, with a message for the person entering it."""


class ErpDB:
    def __init__(self, folder: str, seed: bool = True):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.docs_dir = os.path.join(folder, "docs")
        os.makedirs(self.docs_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(os.path.join(folder, "erp.db"), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self.changed_at = time.time()
        if seed and not self._q("SELECT 1 FROM purchase_orders LIMIT 1"):
            self.seed()

    # ---------- low level ----------
    def _q(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _x(self, sql, args=()):
        with self._lock:
            self._conn.execute(sql, args)

    def _commit(self, actor, action, record, detail=None):
        with self._lock:
            self._conn.execute("INSERT INTO audit (at, actor, action, record, detail) VALUES (?,?,?,?,?)",
                               (time.time(), actor, action, record, json.dumps(detail or {})))
            self._conn.commit()
            self.changed_at = time.time()

    # ---------- documents ----------
    def add_document(self, kind: str, file_name: str, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        doc_id = f"{kind}-{sha[:12]}"
        path = os.path.join(self.docs_dir, doc_id + ".pdf")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
        self._x("INSERT OR IGNORE INTO documents (doc_id, kind, file_name, path, sha256, created_at) VALUES (?,?,?,?,?,?)",
                (doc_id, kind, file_name, path, sha, time.time()))
        return doc_id

    def document(self, doc_id: str) -> dict | None:
        r = self._q("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
        return r[0] if r else None

    # ---------- reads ----------
    def vendors(self):
        return self._q("SELECT * FROM vendors ORDER BY vendor_id")

    def vendor(self, vendor_id):
        r = self._q("SELECT * FROM vendors WHERE vendor_id=?", (vendor_id,))
        return r[0] if r else None

    def purchase_orders(self):
        pos = self._q("SELECT p.*, v.name AS vendor_name FROM purchase_orders p LEFT JOIN vendors v USING (vendor_id) "
                      "ORDER BY (p.source='reference'), p.po_number")
        lines = self._q("SELECT * FROM po_lines ORDER BY po_number, line_no")
        by = {}
        for l in lines:
            by.setdefault(l["po_number"], []).append(l)
        for p in pos:
            p["lines"] = by.get(p["po_number"], [])
        return pos

    def purchase_order(self, po_number):
        return next((p for p in self.purchase_orders() if p["po_number"] == po_number), None)

    def receipts(self):
        recs = self._q("SELECT * FROM receipts ORDER BY receipt_date, receipt_no")
        lines = self._q("SELECT * FROM receipt_lines ORDER BY receipt_no, line_no")
        by = {}
        for l in lines:
            by.setdefault(l["receipt_no"], []).append(l)
        for r in recs:
            r["lines"] = by.get(r["receipt_no"], [])
        return recs

    def receipt(self, receipt_no):
        return next((r for r in self.receipts() if r["receipt_no"] == receipt_no), None)

    def received_so_far(self, po_number) -> dict:
        rows = self._q("SELECT line_no, SUM(received) r, SUM(accepted) a FROM receipt_lines WHERE po_number=? "
                       "GROUP BY line_no", (po_number,))
        return {r["line_no"]: {"received": r["r"] or 0.0, "accepted": r["a"] or 0.0} for r in rows}

    def audit(self, limit=50):
        return self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    def counts(self):
        return {k: self._q(f"SELECT COUNT(*) n FROM {t}")[0]["n"] for k, t in
                (("vendors", "vendors"), ("purchase_orders", "purchase_orders"), ("receipts", "receipts"))}

    # ---------- writes (validated like an ERP would) ----------
    def next_id(self, kind: str) -> str:
        if kind == "vendor":
            n = self._q("SELECT COUNT(*) n FROM vendors WHERE source='tester'")[0]["n"] + 1
            return f"V9{n:02d}"
        if kind == "po":
            n = self._q("SELECT COUNT(*) n FROM purchase_orders WHERE source='tester'")[0]["n"] + 1
            return f"PO-2026-9{n:03d}"
        n = self._q("SELECT COUNT(*) n FROM receipts WHERE source='tester'")[0]["n"] + 1
        return f"GRN-9{n:03d}"

    def create_vendor(self, v: dict, actor="tester", source="tester") -> dict:
        from ..gstin import is_valid
        from ..taxid import validate
        name = (v.get("name") or "").strip()
        if not name:
            raise ErpError("A vendor needs a name")
        country = (v.get("country") or "IN").upper()
        gstin = (v.get("gstin") or "").strip().upper()
        tax_id = (v.get("tax_id") or "").strip().upper()
        if country == "IN":
            if not gstin or not is_valid(gstin):
                raise ErpError("An Indian vendor needs a valid GSTIN (15 characters, correct check digit)")
        elif tax_id and not validate(tax_id)[0]:
            raise ErpError(f"Tax number {tax_id} is not valid: {validate(tax_id)[1]}")
        vid = (v.get("vendor_id") or "").strip() or self.next_id("vendor")
        if self.vendor(vid):
            raise ErpError(f"Vendor {vid} already exists")
        if gstin and self._q("SELECT 1 FROM vendors WHERE gstin=?", (gstin,)):
            raise ErpError(f"A vendor with GSTIN {gstin} already exists")
        self._x("INSERT INTO vendors (vendor_id, name, gstin, state, bank_account, bank_code, status, terms_days, country, "
                "tax_id, currency, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, name, gstin, gstin[:2] if gstin else "", (v.get("bank_account") or "").replace(" ", ""),
                 (v.get("bank_code") or "").strip().upper(), v.get("status") or "approved", int(v.get("terms_days") or 30),
                 country, tax_id, (v.get("currency") or ("INR" if country == "IN" else "USD")).upper(), source, time.time()))
        self._commit(actor, "create vendor", vid, {"name": name})
        return self.vendor(vid)

    def create_po(self, p: dict, actor="tester", source="tester", doc_id=None) -> dict:
        from ..company import ENTITIES
        vid = p.get("vendor_id")
        if not vid or not self.vendor(vid):
            raise ErpError("Choose a vendor that exists in the vendor master (add it first)")
        num = (p.get("po_number") or "").strip() or self.next_id("po")
        if self._q("SELECT 1 FROM purchase_orders WHERE po_number=?", (num,)):
            raise ErpError(f"PO {num} already exists")
        ent = p.get("entity") or "IN"
        if ent not in ENTITIES and not p.get("external_buyer"):
            raise ErpError(f"Unknown company {ent}")
        lines = p.get("lines") or []
        if not lines:
            raise ErpError("A PO needs at least one line")
        clean = []
        for i, l in enumerate(lines, 1):
            try:
                q, r = float(l.get("qty_ordered")), float(l.get("unit_price"))
                tax = float(l.get("tax_rate") or 0)
            except (TypeError, ValueError):
                raise ErpError(f"Line {i}: quantity, rate and tax rate must be numbers")
            if q <= 0 or r < 0 or not (0 <= tax <= 40):
                raise ErpError(f"Line {i}: quantity must be above 0, rate not negative, tax rate between 0 and 40%")
            if not (l.get("description") or "").strip():
                raise ErpError(f"Line {i}: description is empty")
            clean.append((num, int(l.get("line_no") or i), l["description"].strip(), (l.get("hsn") or "").strip(),
                          (l.get("uom") or "pcs").strip(), q, r, l.get("price_basis") or "excl_tax", tax,
                          0 if (p.get("kind") == "services" or l.get("requires_receipt") in (False, 0, "0")) else 1))
        with self._lock:
            self._conn.execute("INSERT INTO purchase_orders (po_number, vendor_id, po_date, status, kind, entity, currency, "
                               "external_buyer, source, doc_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (num, vid, p.get("po_date") or time.strftime("%Y-%m-%d"), p.get("status") or "open",
                                p.get("kind") or "goods", ent, (p.get("currency") or "INR").upper(),
                                p.get("external_buyer"), source, doc_id, time.time()))
            self._conn.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?,?,?,?,?)", clean)
        self._commit(actor, "create purchase order", num, {"lines": len(clean)})
        return self.purchase_order(num)

    def create_receipt(self, r: dict, actor="tester", source="tester", doc_id=None) -> dict:
        po = self.purchase_order(r.get("po_number") or "")
        if not po:
            raise ErpError("Choose the PO these goods were received against")
        if po["status"] != "open":
            raise ErpError(f"{po['po_number']} is {po['status']}; goods cannot be received against it")
        if po.get("external_buyer"):
            raise ErpError("That PO belongs to another buyer; it is kept for reference only")
        num = (r.get("receipt_no") or "").strip() or self.next_id("receipt")
        if self.receipt(num):
            raise ErpError(f"Receipt {num} already exists")
        by_no = {l["line_no"]: l for l in po["lines"]}
        so_far = self.received_so_far(po["po_number"])
        rows = []
        for l in r.get("lines") or []:
            try:
                ln, rec, acc = int(l.get("line_no")), float(l.get("received") or 0), float(l.get("accepted") or 0)
            except (TypeError, ValueError):
                raise ErpError("Received and accepted quantities must be numbers")
            if rec == 0 and acc == 0:
                continue
            if ln not in by_no:
                raise ErpError(f"{po['po_number']} has no line {ln}")
            if acc > rec or rec < 0 or acc < 0:
                raise ErpError(f"Line {ln}: accepted cannot be more than received")
            ordered = by_no[ln]["qty_ordered"]
            already = so_far.get(ln, {}).get("received", 0.0)
            if already + rec > ordered * 1.10 + 1e-9:  # a usual over-delivery tolerance
                raise ErpError(f"Line {ln}: {already + rec:g} received in all, more than 10% over the {ordered:g} ordered")
            rows.append((num, po["po_number"], ln, rec, acc))
        if not rows:
            raise ErpError("Enter the quantity received on at least one line")
        with self._lock:
            self._conn.execute("INSERT INTO receipts (receipt_no, po_number, receipt_date, received_by, source, doc_id, "
                               "created_at) VALUES (?,?,?,?,?,?,?)",
                               (num, po["po_number"], r.get("receipt_date") or time.strftime("%Y-%m-%d"),
                                r.get("received_by") or "Stores", source, doc_id, time.time()))
            self._conn.executemany("INSERT INTO receipt_lines VALUES (?,?,?,?,?)", rows)
        self._commit(actor, "record goods receipt", num, {"po": po["po_number"], "lines": len(rows)})
        return self.receipt(num)

    def reset_tester_records(self):
        """Remove everything testers added; the demo company's records stay."""
        with self._lock:
            for t in ("receipt_lines",):
                self._conn.execute(f"DELETE FROM {t} WHERE receipt_no IN (SELECT receipt_no FROM receipts WHERE source='tester')")
            self._conn.execute("DELETE FROM receipts WHERE source='tester'")
            self._conn.execute("DELETE FROM po_lines WHERE po_number IN (SELECT po_number FROM purchase_orders WHERE source='tester')")
            self._conn.execute("DELETE FROM purchase_orders WHERE source='tester'")
            self._conn.execute("DELETE FROM vendors WHERE source='tester'")
        self._commit("system", "remove tester records", "*")

    # ---------- seed ----------
    def seed(self):
        """The demo company's vendor master, POs and goods receipts, each with a printed document."""
        from ..company import GRNS, PO_LINES, POS, VENDORS
        from ..generator import render_po_pdf, render_receipt_pdf
        V = {v.vendor_id: v for v in VENDORS}
        with self._lock:
            for v in VENDORS:
                self._conn.execute("INSERT INTO vendors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (v.vendor_id, v.name, v.gstin, v.state, v.bank_account, v.ifsc, v.status, v.terms_days,
                                    v.country, v.tax_id, v.currency, "seed", time.time()))
            for p in POS:
                lines = [l for l in PO_LINES if l.po_number == p.po_number]
                path = os.path.join(self.docs_dir, f"{p.po_number}.pdf")
                render_po_pdf(p, lines, V[p.vendor_id], path)
                doc = self.add_document("po", f"{p.po_number}.pdf", open(path, "rb").read())
                os.remove(path)
                self._conn.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   (p.po_number, p.vendor_id, p.po_date, p.status, p.kind, p.entity, p.currency, None,
                                    "seed", doc, time.time()))
                self._conn.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?,?,?,?,?)",
                                       [(l.po_number, l.line_no, l.description, l.hsn, l.uom, l.qty_ordered, l.unit_price,
                                         l.price_basis, l.gst_rate, int(l.requires_grn)) for l in lines])
            P = {p.po_number: p for p in POS}
            for g in GRNS:
                pl = next(l for l in PO_LINES if l.po_number == g.po_number and l.line_no == g.line_no)
                path = os.path.join(self.docs_dir, f"{g.grn_number}.pdf")
                po = P[g.po_number]
                render_receipt_pdf(g.grn_number, po, [(pl, g.qty_received, g.qty_accepted)], V[po.vendor_id],
                                   g.receipt_date, g.received_by, path)
                doc = self.add_document("receipt", f"{g.grn_number}.pdf", open(path, "rb").read())
                os.remove(path)
                self._conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?)",
                                   (g.grn_number, g.po_number, g.receipt_date, g.received_by, "seed", doc, time.time()))
                self._conn.execute("INSERT INTO receipt_lines VALUES (?,?,?,?,?)",
                                   (g.grn_number, g.po_number, g.line_no, g.qty_received, g.qty_accepted))
        self._seed_reference()
        self._commit("system", "load demo company records", "*", self.counts())

    def _seed_reference(self):
        """A real public purchase order (GeM), entered from its PDF. It is another buyer's order, so invoices
        against it are not ours to pay: it shows what a real PO looks like next to the demo ones."""
        src = os.path.join(REFERENCE_DIR, "GEMC-511687772029047.pdf")
        if not os.path.exists(src):
            return
        doc = self.add_document("reference", "GEMC-511687772029047.pdf", open(src, "rb").read())
        self._conn.execute("INSERT OR IGNORE INTO vendors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("R001", "Narang Scientific Works Private Limited", "", "07", "", "", "approved", 30, "IN", "",
                            "INR", "reference", time.time()))
        self._conn.execute("INSERT OR IGNORE INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           ("GEMC-511687772029047", "R001", "2024-06-13", "closed", "goods", "IN", "INR",
                            "Indian Institute of Technology Hyderabad (GeM, public contract)", "reference", doc, time.time()))
        self._conn.execute("INSERT OR IGNORE INTO po_lines VALUES (?,?,?,?,?,?,?,?,?,?)",
                           ("GEMC-511687772029047", 1, "NSW INDIA Semi-Automatic Bench Type Industrial Oven, Warranty 2 year",
                            "8419", "pcs", 1, 274500.0, "incl_tax", 18, 1))

    def copy_to(self, folder):  # for tests
        shutil.copytree(self.folder, folder)
