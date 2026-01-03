# FILE: app/utils/text.py
from __future__ import annotations

import re

_SMALL = {"mg", "ml", "mcg", "g", "kg", "iu", "l", "mm", "cm", "m", "hr", "hrs"}
_UP = {"iv", "im", "po", "prn", "od", "bd", "tid", "qid", "hs", "stat", "sos"}

def smart_title(s: str) -> str:
    """
    Title-case but preserves common medical units/acronyms.
    Example: "paracetamol 500 mg iv" -> "Paracetamol 500 mg IV"
    """
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s.strip())
    words = s.split(" ")
    out = []
    for w in words:
        lw = w.lower()
        if lw in _SMALL:
            out.append(lw)
        elif lw in _UP:
            out.append(lw.upper())
        else:
            # keep codes like "B12", "D3"
            if re.fullmatch(r"[A-Za-z]\d+", w) or re.fullmatch(r"\d+[A-Za-z]+", w):
                out.append(w.upper())
            else:
                out.append(w[:1].upper() + w[1:].lower() if w else w)
    return " ".join(out)
