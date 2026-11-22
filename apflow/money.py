"""Rupee amounts as an Indian AP clerk writes them: ₹1,47,500.00."""
from .generator import inr


def rs(x: float) -> str:
    return "₹" + inr(float(x))


SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


def fmt(x, currency: str | None = "INR") -> str:
    """An amount in its own currency: rupees in Indian grouping, others in international grouping."""
    cur = (currency or "INR").upper()
    if cur == "INR":
        return rs(x)
    sym = SYMBOLS.get(cur)
    body = f"{float(x):,.0f}" if cur == "JPY" else f"{float(x):,.2f}"
    return f"{sym}{body}" if sym else f"{cur} {body}"


def to_inr(x, currency: str | None, settings=None) -> float:
    from .config import SETTINGS
    rates = (settings or SETTINGS).fx_to_inr
    return float(x or 0.0) * rates.get((currency or "INR").upper(), 1.0)
