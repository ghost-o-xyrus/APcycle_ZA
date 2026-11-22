"""Extraction evaluation: field-level accuracy against generator ground truth.

Usage: python -m apflow.evaluate [--n-per-layout 8] [--scans 6]
Writes out/eval/report.json and prints a table by route x layout.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict

from .generator import build
from .scenarios import random_clean
from .extract.routes import extract_text_route, extract_ocr_route, needs_escalation

FIELDS = ["vendor_gstin", "buyer_gstin", "invoice_number", "invoice_date", "po_reference",
          "subtotal", "cgst", "sgst", "igst", "total", "bank_account", "ifsc"]
# Suppliers abroad (layout X): tax numbers, VAT or sales tax, and currency instead of GST fields.
FIELDS_X = ["vendor_tax_id", "buyer_tax_id", "buyer_gstin", "invoice_number", "invoice_date", "po_reference",
            "subtotal", "tax", "total", "currency", "bank_account", "ifsc"]


def field_ok(name: str, truth_v, got) -> bool:
    if name in ("subtotal", "cgst", "sgst", "igst", "tax", "total"):
        t = float(truth_v or 0.0)
        g = float(got) if got is not None else 0.0
        return abs(t - g) < 0.005
    return (truth_v or None) == (got or None)


def lines_ok(truth_lines, got_lines) -> tuple[int, int]:
    """(matched, expected): a line matches only when description, HSN, qty, unit price and amount are all exact."""
    got = [(l.description, l.hsn, round(l.qty, 2), round(l.unit_price, 2), round(l.amount, 2)) for l in got_lines]
    hit = 0
    for t in truth_lines:
        key = (t["description"], t["hsn"], round(t["qty"], 2), round(t["unit_price"], 2), round(t["amount"], 2))
        if key in got:
            got.remove(key)
            hit += 1
    return hit, len(truth_lines)


def score(truth: dict, ex, fields=FIELDS) -> dict:
    res = {f: field_ok(f, truth.get(f), ex.get(f)) for f in fields}
    h, n = lines_ok(truth["lines"], ex.lines)
    res["_lines_hit"], res["_lines_n"] = h, n
    res["_escalate"] = bool(needs_escalation(ex))
    return res


def run(n_per_layout=8, scans=6, out="out/eval"):
    os.makedirs(out, exist_ok=True)
    rows = []
    seed = 1000
    plan = [(lay, False, n_per_layout) for lay in "ABCD"] + [(lay, True, scans) for lay in "ABC"] + \
        [("X", False, n_per_layout), ("X", True, scans)]
    for lay, scanned, n in plan:
        for _ in range(n):
            seed += 1
            spec = random_clean(seed, layout=lay, scanned=scanned)
            pdf, t = build(spec, os.path.join(out, "docs"), f"{lay}{'s' if scanned else 't'}_{seed}")
            ex = extract_ocr_route(pdf) if scanned else extract_text_route(pdf)
            r = score(t, ex, FIELDS_X if lay == "X" else FIELDS)
            r.update(layout=lay, route="ocr" if scanned else "text", seed=seed)
            rows.append(r)
    json.dump(rows, open(os.path.join(out, "report.json"), "w"), indent=1)
    return rows


def summarise(rows) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[(r["route"], r["layout"])].append(r)
    out = {}
    for k, rs in sorted(groups.items()):
        fl = FIELDS_X if k[1] == "X" else FIELDS
        fa = sum(r[f] for r in rs for f in fl) / (len(rs) * len(fl))
        la = sum(r["_lines_hit"] for r in rs) / max(1, sum(r["_lines_n"] for r in rs))
        per_field = {f: sum(r[f] for r in rs) / len(rs) for f in fl}
        esc = sum(r["_escalate"] for r in rs) / len(rs)
        out[f"{k[0]}/{k[1]}"] = {"docs": len(rs), "field_acc": round(fa, 3), "line_acc": round(la, 3),
                                 "escalated": round(esc, 3), "per_field": {f: round(v, 2) for f, v in per_field.items()}}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-layout", type=int, default=8)
    ap.add_argument("--scans", type=int, default=6)
    a = ap.parse_args()
    s = summarise(run(a.n_per_layout, a.scans))
    print(f"{'route/layout':14s} docs  fields  lines  escalated  weakest fields")
    for k, v in s.items():
        weak = ", ".join(f"{f} {x:.0%}" for f, x in sorted(v["per_field"].items(), key=lambda kv: kv[1])[:3] if x < 1)
        print(f"{k:14s} {v['docs']:4d}  {v['field_acc']:.1%}  {v['line_acc']:.1%}  {v['escalated']:.0%}  {weak}")
    json.dump(s, open("out/eval/summary.json", "w"), indent=1)
