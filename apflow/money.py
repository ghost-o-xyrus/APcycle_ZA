"""Rupee amounts as an Indian AP clerk writes them: ₹1,47,500.00."""
from .generator import inr


def rs(x: float) -> str:
    return "₹" + inr(float(x))
