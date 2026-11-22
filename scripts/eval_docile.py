"""Run the invoice desk on the 25 real DocILE documents and score it against their labels.

Usage: python scripts/eval_docile.py <docile_dir> <out.json> [none|auto]
  (auto uses GEMINI_API_KEY from the environment; none = text and OCR only)
Scores only what DocILE labels: invoice number, issue date, vendor name, total, net
subtotal, order reference, line amounts, and the document type. Decisions are recorded
in full; the safety rule is that none of these (they are not addressed to our company)
may be approved.
"""
import difflib
import glob
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from apflow.service import Service  # noqa: E402

PAYABLE = {"tax_invoice": "invoice", "utility_bill": "invoice", "debit_note": "invoice",
           "credit_note": "credit_note", "order": "purchase_order", "purchase_order": "purchase_order",
           "sales_order": "purchase_order", "proforma": "quotation", "receipt": "receipt"}


def norm_id(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper()).lstrip("0") or None


def money(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[^0-9,.\-]", "", str(s))
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", "") if len(t.split(",")[-1]) == 3 else t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


DATE_FMTS = ["%m/%d/%y", "%m/%d/%Y", "%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%m-%d-%y",
             "%m-%d-%Y", "%d-%b-%y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%d %b %Y", "%b. %d, %Y",
             "%m/%d/%Y.", "%Y/%m/%d", "%d-%m-%Y"]


def dates(s):
    out = set()
    s = (s or "").strip().replace("  ", " ")
    for f in DATE_FMTS:
        try:
            out.add(datetime.strptime(s, f).date())
        except ValueError:
            pass
    return out


def name_ok(t, g):
    a = re.sub(r"[^a-z0-9 ]", " ", (t or "").lower()).split()
    b = re.sub(r"[^a-z0-9 ]", " ", (g or "").lower()).split()
    if not a or not b:
        return False
    ja, jb = " ".join(a), " ".join(b)
    return ja in jb or jb in ja or difflib.SequenceMatcher(None, ja, jb).ratio() >= 0.8


def truth_of(d):
    f = {}
    for x in d.get("field_extractions", []):
        f.setdefault(x["fieldtype"], x["text"])
    lines = {}
    for x in d.get("line_item_extractions", []):
        lines.setdefault(x["line_item_id"], {})[x["fieldtype"]] = x["text"]
    amts = []
    for li in lines.values():
        a = money(li.get("line_item_amount_gross") or li.get("line_item_amount_net"))
        if a is not None:
            amts.append(a)
    return f, amts


def main(src, out_path, mode="none"):
    svc = Service(tempfile.mkdtemp(), as_of=date(2026, 11, 22), model_mode=mode)
    rows = []
    for pdf in sorted(glob.glob(os.path.join(src, "*.pdf"))):
        lab = json.load(open(pdf[:-4] + ".json"))
        f, amts = truth_of(lab)
        dtype = lab["metadata"].get("document_type")
        rid = svc.start_upload(open(pdf, "rb").read(), os.path.basename(pdf))
        run = svc.wait(rid, 600)
        ex = (run.get("extraction") or {}).get("fields", {})
        get = lambda k: (ex.get(k) or {}).get("value")  # noqa: E731
        got_lines = (run.get("extraction") or {}).get("lines", [])
        sc = {}
        if "document_id" in f:
            sc["invoice_number"] = norm_id(f["document_id"]) == norm_id(get("invoice_number"))
        if "date_issue" in f and dates(f["date_issue"]):
            g = get("invoice_date")
            try:
                gd = date.fromisoformat(g) if g else None
            except ValueError:
                gd = None
            sc["invoice_date"] = gd in dates(f["date_issue"])
        if "vendor_name" in f:
            sc["vendor_name"] = name_ok(f["vendor_name"], get("vendor_name"))
        tot = money(f.get("amount_total_gross") or f.get("amount_due"))
        if tot is not None:
            sc["total"] = get("total") is not None and abs(float(get("total")) - tot) < 0.011
        if "amount_total_net" in f and money(f["amount_total_net"]) is not None:
            sc["subtotal"] = get("subtotal") is not None and abs(float(get("subtotal")) - money(f["amount_total_net"])) < 0.011
        if "order_id" in f:
            sc["order_ref"] = norm_id(f["order_id"]) == norm_id(get("po_reference"))
        line_hit = None
        if amts:
            pool = [round(float(l.get("amount") or 0), 2) for l in got_lines]
            hit = 0
            for a in amts:
                if round(a, 2) in pool:
                    pool.remove(round(a, 2))
                    hit += 1
            line_hit = [hit, len(amts)]
        failed = [c["check_id"] for c in run["checks"] if c["status"] == "fail"]
        rows.append({
            "doc": os.path.basename(pdf)[:8], "label_type": dtype, "pages": lab["metadata"].get("page_count"),
            "route": run.get("route"), "state": run["state"], "error": run.get("error"),
            "our_type": get("doc_type"), "outcome": run.get("outcome"), "owner": run.get("owner"),
            "question": run.get("question"), "failed": failed,
            "scores": sc, "lines": line_hit, "got": {k: get(k) for k in
                ("invoice_number", "invoice_date", "vendor_name", "total", "subtotal", "po_reference")},
            "truth": {"invoice_number": f.get("document_id"), "invoice_date": f.get("date_issue"),
                      "vendor_name": f.get("vendor_name"), "total": f.get("amount_total_gross") or f.get("amount_due"),
                      "order_ref": f.get("order_id")},
            "ms": run.get("total_ms"), "route_log": run.get("route_log"),
        })
        print(rows[-1]["doc"], dtype, rows[-1]["route"], rows[-1]["our_type"], rows[-1]["outcome"], sc, line_hit, flush=True)
    json.dump(rows, open(out_path, "w"), indent=1, default=str)
    import collections
    tot, ok = collections.Counter(), collections.Counter()
    for r in rows:
        for k, v in r["scores"].items():
            tot[k] += 1
            ok[k] += bool(v)
    lh = [r["lines"] for r in rows if r["lines"]]
    print("fields right:", {k: f"{ok[k]}/{tot[k]}" for k in tot})
    print("line amounts found:", sum(a for a, b in lh), "of", sum(b for a, b in lh))
    print("decisions:", dict(collections.Counter(r["outcome"] for r in rows)))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], *(sys.argv[3:4]))
