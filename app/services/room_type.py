from __future__ import annotations
from typing import Optional

ROOM_TYPE_ALIASES = {
    "general ward": "General",
    "general": "General",
    "gen": "General",
    "gw": "General",
    "semi private": "Semi Private",
    "semi-private": "Semi Private",
    "semiprivate": "Semi Private",
    "private": "Private",
    "private ward": "Private",
    "pvt": "Private",
    "deluxe": "Deluxe",
    "dlx": "Deluxe",
    "delux": "Deluxe",
    "icu": "ICU",
    "icu ward": "ICU",
    "intensive care": "ICU",
    "intensive care unit": "ICU",
    "nicu": "NICU",
    "picu": "PICU",
    "hdu": "HDU",
    "isolation": "Isolation",
    "isolation ward": "Isolation",
}


def normalize_room_type(x: Optional[str]) -> str:
    s = (x or "General").strip()
    k = s.lower().strip()
    if k in ROOM_TYPE_ALIASES:
        return ROOM_TYPE_ALIASES[k]
    # fallback: title-case (keeps unknown types stable)
    return s.title()
