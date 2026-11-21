"""Shared data shapes. Every extraction route returns an `Extraction`."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Field:
    value: Any = None
    confidence: float = 0.0
    evidence: str = ""  # the text the value was read from

    @property
    def present(self) -> bool:
        return self.value not in (None, "", [])


@dataclass
class Line:
    description: str
    qty: float
    unit_price: float  # pre-tax, as printed on the invoice
    amount: float      # pre-tax line amount
    hsn: str | None = None
    uom: str | None = None
    gst_rate: float | None = None
    confidence: float = 1.0


HEADER_FIELDS = (
    "doc_type", "vendor_name", "vendor_gstin", "buyer_gstin", "invoice_number",
    "invoice_date", "po_reference", "subtotal", "cgst", "sgst", "igst", "total",
    "bank_account", "ifsc",
)


@dataclass
class Extraction:
    route: str  # text | ocr | llm
    fields: dict[str, Field] = field(default_factory=dict)
    lines: list[Line] = field(default_factory=list)
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)

    def get(self, name: str, default=None):
        f = self.fields.get(name)
        return f.value if f and f.present else default

    def conf(self, name: str) -> float:
        f = self.fields.get(name)
        return f.confidence if f and f.present else 0.0

    def mean_confidence(self, names=HEADER_FIELDS) -> float:
        vals = [self.fields[n].confidence for n in names if n in self.fields and self.fields[n].present]
        return sum(vals) / len(vals) if vals else 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "Extraction":
        ex = cls(route=d["route"], notes=list(d.get("notes", [])))
        ex.fields = {k: Field(**v) for k, v in d.get("fields", {}).items()}
        ex.lines = [Line(**l) for l in d.get("lines", [])]
        return ex

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "fields": {k: asdict(v) for k, v in self.fields.items()},
            "lines": [asdict(l) for l in self.lines],
            "notes": self.notes,
        }


@dataclass
class CheckResult:
    id: str
    name: str
    status: str  # pass | warn | fail | skip
    severity: str  # reject | hold | info
    message: str
    evidence: dict = field(default_factory=dict)
    owner: str | None = None  # who resolves it when it fails
    question: str | None = None


@dataclass
class Decision:
    outcome: str  # APPROVE | HOLD | REJECT
    reasons: list[str]
    owner: str | None = None
    question: str | None = None
    vendor_message: str | None = None
    approvable_amount: float | None = None  # pre-tax, for part-approval
    held_amount: float | None = None
    approval_level: str | None = None
    summary: str = ""
