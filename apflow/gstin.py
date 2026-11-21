"""GSTIN validation, checksum and OCR repair.

Format: 2-digit state code, 10-char PAN, entity number, 'Z', checksum.
The checksum algorithm is verified in tests against three real GSTINs printed
in public GeM contracts.
"""
import re
from itertools import product

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

# Characters OCR commonly confuses, by the class the position expects.
_TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}
_TO_ALPHA = {v: k for k, v in {"O": "0", "I": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}.items()}
# Expected class per position: d = digit, a = letter, x = either.
_PATTERN = "ddaaaaaddddaxax"


def checksum_char(first14: str) -> str:
    total = 0
    for i, c in enumerate(first14):
        p = CHARS.index(c) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return CHARS[(36 - total % 36) % 36]


def with_checksum(first14: str) -> str:
    return first14 + checksum_char(first14)


def is_valid(g: str | None) -> bool:
    if not g:
        return False
    g = g.strip().upper()
    return bool(GSTIN_RE.match(g)) and checksum_char(g[:14]) == g[14]


def state_code(g: str | None) -> str | None:
    return g[:2] if g and len(g) >= 2 and g[:2].isdigit() else None


def repair(g: str | None) -> tuple[str | None, bool]:
    """Try to fix OCR character confusions so the GSTIN validates.

    Returns (gstin, repaired). Only accepts a repair that yields exactly one
    valid candidate, so a repair never guesses between two plausible values.
    """
    if not g:
        return None, False
    g = re.sub(r"[^0-9A-Za-z]", "", g).upper()
    if is_valid(g):
        return g, False
    if len(g) != 15:
        return g, False
    options = []
    for i, c in enumerate(g):
        kind = _PATTERN[i]
        opts = {c}
        if kind == "d" and c in _TO_DIGIT:
            opts.add(_TO_DIGIT[c])
        if kind in "ax" and c in _TO_ALPHA:
            opts.add(_TO_ALPHA[c])
        if kind == "x" and c in _TO_DIGIT:
            opts.add(_TO_DIGIT[c])
        options.append(sorted(opts))
    valid = {"".join(p) for p in product(*options) if is_valid("".join(p))}
    if not valid:
        return g, False
    # Prefer the fewest substitutions; accept only if that minimum is unique.
    changes = lambda v: sum(a != b for a, b in zip(v, g))
    best = min(changes(v) for v in valid)
    top = [v for v in valid if changes(v) == best]
    if len(top) == 1:
        return top[0], True
    return g, False
