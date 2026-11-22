"""How the invoice desk reads the ERP: vendors, purchase orders and goods receipts.

Two ways in, same records:
  api   - HTTP to the ERP's integration API with its token (APFLOW_ERP_URL, APFLOW_ERP_TOKEN).
          "self" means the ERP hosted in this same service. This is how a client's NetSuite or
          SAP would be read; swapping systems means a new client with the same snapshot().
  local - straight from the ERP database through the same serialisers (tests, scripts).
Only our own companies' records reach the checks; reference records (another buyer's orders) stay in the ERP.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .company import GRN, LEDGER, PO, POLine, Repo, Vendor

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ErpUnavailable(RuntimeError):
    pass


class ErpClient:
    def __init__(self, db=None, base_url: str | None = None, token: str | None = None, timeout: float = 10.0,
                 cache_seconds: float = 2.0):
        self.db, self.base_url, self.token, self.timeout = db, (base_url or "").rstrip("/") or None, token, timeout
        self.cache_seconds = cache_seconds
        self._cache, self._cache_at, self._lock = None, 0.0, threading.Lock()
        self.last_sync, self.last_error, self.calls = None, None, 0

    @property
    def mode(self):
        return "api" if self.base_url else "local"

    # ---------- transport ----------
    def _get(self, path: str):
        url = f"{self.base_url}/erp/api/record/v1/{path}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        host = urllib.parse.urlsplit(url).hostname or ""
        opener = _DIRECT.open if host in ("127.0.0.1", "localhost", "::1") else urllib.request.urlopen
        try:
            with opener(req, timeout=self.timeout) as r:
                self.calls += 1
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise ErpUnavailable(f"ERP refused the request ({e.code}): "
                                 f"{e.read().decode(errors='replace')[:200]}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ErpUnavailable(f"ERP not reachable at {self.base_url}: {e}") from e

    def snapshot(self) -> dict:
        """{'vendors': [...], 'purchaseOrders': [...], 'itemReceipts': [...]} as the API shows them."""
        with self._lock:
            if self._cache and time.time() - self._cache_at < self.cache_seconds and \
                    (self.db is None or self.db.changed_at <= self._cache_at):
                return self._cache
            try:
                if self.mode == "api":
                    snap = {"vendors": self._get("vendor")["items"], "purchaseOrders": self._get("purchaseOrder")["items"],
                            "itemReceipts": self._get("itemReceipt")["items"]}
                else:
                    from .erp.api import po_json, receipt_json, vendor_json
                    snap = {"vendors": [vendor_json(v) for v in self.db.vendors() if v["source"] != "reference"],
                            "purchaseOrders": [po_json(p) for p in self.db.purchase_orders() if p["source"] != "reference"],
                            "itemReceipts": [receipt_json(r) for r in self.db.receipts()]}
            except ErpUnavailable as e:
                self.last_error = str(e)
                raise
            self._cache, self._cache_at = snap, time.time()
            self.last_sync, self.last_error = self._cache_at, None
            return snap

    # ---------- into the checks' view ----------
    def repo(self, extra_ledger=(), approved_files=None) -> Repo:
        s = self.snapshot()
        vendors = [Vendor(vendor_id=v["id"], name=v["companyName"], gstin=v.get("gstin") or "", state=v.get("state") or "",
                          bank_account=v.get("bankAccount") or "", ifsc=v.get("bankCode") or "", status=v["status"],
                          terms_days=v.get("terms") or 30, country=v.get("country") or "IN", tax_id=v.get("taxId") or "",
                          currency=v.get("currency") or "INR") for v in s["vendors"]]
        pos, lines = [], []
        for p in s["purchaseOrders"]:
            pos.append(PO(po_number=p["id"], vendor_id=p["entity"]["id"], po_date=p["tranDate"], status=p["status"],
                          kind=p.get("kind") or "goods", entity=p.get("subsidiary") or "IN", currency=p.get("currency") or "INR"))
            for i in p["item"]["items"]:
                lines.append(POLine(po_number=p["id"], line_no=i["line"], description=i["description"], hsn=i.get("hsn") or "",
                                    uom=i.get("units") or "pcs", qty_ordered=i["quantity"], unit_price=i["rate"],
                                    price_basis=i.get("priceBasis") or "excl_tax", gst_rate=i.get("taxRate") or 0.0,
                                    requires_grn=bool(i.get("requiresReceipt", True))))
        grns = [GRN(grn_number=r["id"], po_number=r["createdFrom"]["id"], line_no=i["orderLine"],
                    qty_received=i["quantityReceived"], qty_accepted=i["quantityAccepted"], receipt_date=r["tranDate"],
                    received_by=r.get("receivedBy") or "") for r in s["itemReceipts"] for i in r["item"]["items"]]
        repo = Repo(vendors, pos, lines, grns, list(LEDGER) + list(extra_ledger))
        repo.approved_files = dict(approved_files or {})
        repo.receipt_docs = {r["id"]: r.get("document") for r in s["itemReceipts"]}
        repo.po_docs = {p["id"]: p.get("document") for p in s["purchaseOrders"]}
        return repo

    def status(self) -> dict:
        return {"mode": self.mode, "url": self.base_url, "last_sync": self.last_sync, "error": self.last_error,
                "calls": self.calls,
                "records": None if not self._cache else {k: len(v) for k, v in self._cache.items()}}
