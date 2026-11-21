"""Every threshold the process uses, in one place, with the reason it has that value.

Values here are assumptions (see the design doc, section 1). Changing one should
never require touching check code.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # Buyer (the invented company). GSTIN is generated with a valid checksum.
    company_name: str = "Kaveri Instruments Pvt Ltd"
    company_gstin: str = "29AAGCK4421M1Z1"  # fixed up by gstin.with_checksum at import
    company_state: str = "29"  # Karnataka

    # Arithmetic and price tolerances (finding A4): rounding only, not a % band.
    money_abs_tol: float = 1.00          # rupees, for line, subtotal, tax and total sums
    price_abs_tol_per_unit: float = 1.00  # rupees per unit, after tax normalisation
    price_rel_tol: float = 0.005          # 0.5% for high unit prices (whichever is larger)

    # Duplicate detection (finding A6).
    duplicate_hold_score: float = 0.85
    duplicate_window_days: int = 60

    # PO inference (finding A2).
    po_infer_min_score: float = 0.60     # below this: "no PO found"
    po_infer_min_margin: float = 0.10    # top candidate must beat the runner-up by this

    # Approval tiers (pre-tax value in rupees).
    manager_approval_above: float = 500_000.0

    # Extraction router.
    text_layer_min_chars: int = 200
    escalate_below_confidence: float = 0.80
    scan_model_first: bool = True  # scans go to the model first when one is configured (see routes.extract)

    required_fields: tuple = ("invoice_number", "invoice_date", "total")


def _fix(g: str) -> str:
    from .gstin import with_checksum
    return with_checksum(g[:14])


SETTINGS = Settings(company_gstin=_fix(Settings.company_gstin))
