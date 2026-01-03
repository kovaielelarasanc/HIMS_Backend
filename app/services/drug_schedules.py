# FILE: app/services/drug_schedules.py
from __future__ import annotations

from typing import Any, Dict, Optional


# ------------------------------------------------------------
# India — Drugs & Cosmetics Act schedules (alphabet based)
# We enforce only H / H1 / X strictly in the pharmacy dispense flow.
# Others are stored as meta for UI / info / future validations.
# ------------------------------------------------------------
IN_DCA: Dict[str, Dict[str, Any]] = {
    "A":  {"label": "Schedule A",  "desc": "Forms and formats for applications/licenses."},
    "B":  {"label": "Schedule B",  "desc": "Fees for government analysis/testing of drugs."},
    "C":  {"label": "Schedule C",  "desc": "Biological/special products (vaccines, insulin etc.)."},
    "C1": {"label": "Schedule C1", "desc": "Other special products (certain antibiotics etc.)."},
    "D":  {"label": "Schedule D",  "desc": "Exemptions for certain imported drugs."},
    "E1": {"label": "Schedule E1", "desc": "Poisonous substances (Ayurveda/Siddha/Unani)."},
    "F":  {"label": "Schedule F",  "desc": "Standards for blood banks, vaccines, sera etc."},
    "F1": {"label": "Schedule F1", "desc": "Additional standards for certain biologicals."},
    "FF": {"label": "Schedule FF", "desc": "Standards for ophthalmic preparations."},
    "G":  {"label": "Schedule G",  "desc": "Caution: to be taken under medical supervision.", "requires_prescription": False},
    "H":  {"label": "Schedule H",  "desc": "Prescription drug (Rx only).", "requires_prescription": True},
    "H1": {"label": "Schedule H1", "desc": "High-surveillance Rx; register tracking required.", "requires_prescription": True, "requires_register": True},
    "J":  {"label": "Schedule J",  "desc": "Diseases/ailments for which drug cannot claim cure/prevention."},
    "K":  {"label": "Schedule K",  "desc": "Exemptions for certain rules for specific classes of drugs."},
    "M":  {"label": "Schedule M",  "desc": "GMP requirements for manufacturing."},
    "N":  {"label": "Schedule N",  "desc": "Minimum equipment requirements for retail pharmacy."},
    "O":  {"label": "Schedule O",  "desc": "Standards for disinfectant fluids."},
    "P":  {"label": "Schedule P",  "desc": "Shelf life (expiry) requirements for drugs."},
    "P1": {"label": "Schedule P1", "desc": "Permitted colors in cosmetics."},
    "Q":  {"label": "Schedule Q",  "desc": "Permitted dyes/pigments for cosmetics/soaps."},
    "R":  {"label": "Schedule R",  "desc": "Standards for condoms/contraceptives/devices."},
    "R1": {"label": "Schedule R1", "desc": "Additional standards for devices."},
    "S":  {"label": "Schedule S",  "desc": "Standards for cosmetics/toiletries."},
    "T":  {"label": "Schedule T",  "desc": "GMP for Ayurvedic/Siddha/Unani drugs."},
    "U":  {"label": "Schedule U",  "desc": "Documentation and record keeping."},
    "V":  {"label": "Schedule V",  "desc": "Standards for patent/proprietary medicines."},
    "W":  {"label": "Schedule W",  "desc": "Lists of generic drugs."},
    "X":  {"label": "Schedule X",  "desc": "Narcotic/psychotropic; strict storage/sale/records.", "requires_prescription": True, "requires_register": True, "strict_storage": True},
    "Y":  {"label": "Schedule Y",  "desc": "Clinical trials guidelines."},
    "Z":  {"label": "Schedule Z",  "desc": "Pharmacovigilance/veterinary guidelines."},
}

# ------------------------------------------------------------
# USA — Controlled Substances Act schedules (I–V)
# ------------------------------------------------------------
US_CSA: Dict[str, Dict[str, Any]] = {
    "I":   {"label": "US Schedule I",   "desc": "High abuse potential; no accepted medical use.", "blocks_dispense": True},
    "II":  {"label": "US Schedule II",  "desc": "High abuse potential; accepted medical use.", "requires_prescription": True, "requires_register": True},
    "III": {"label": "US Schedule III", "desc": "Moderate/low dependence potential.", "requires_prescription": True},
    "IV":  {"label": "US Schedule IV",  "desc": "Lower abuse potential relative to III.", "requires_prescription": True},
    "V":   {"label": "US Schedule V",   "desc": "Lowest abuse potential; limited narcotics.", "requires_prescription": False},
}


def _norm_system(system: Optional[str]) -> str:
    s = (system or "").strip().upper()
    if s in ("US", "USA", "US_CSA", "CSA"):
        return "US_CSA"
    if s in ("IN", "INDIA", "IN_DCA", "DCA"):
        return "IN_DCA"
    # default to India system
    return "IN_DCA"


def _norm_code(code: Optional[str]) -> str:
    c = (code or "").strip().upper()
    if not c:
        return ""
    # Normalize common variants: "H-1" -> "H1", "C-1" -> "C1"
    c = c.replace("-", "").replace(" ", "")
    return c


def get_schedule_meta(system: Optional[str], code: Optional[str]) -> Dict[str, Any]:
    """
    Returns normalized schedule meta:
      {
        system, code, label, desc,
        requires_prescription, requires_register, blocks_dispense, strict_storage
      }
    Safe defaults if unknown.
    """
    sys_norm = _norm_system(system)
    code_norm = _norm_code(code)

    if not code_norm:
        return {
            "system": sys_norm,
            "code": "",
            "label": "",
            "desc": "",
            "requires_prescription": False,
            "requires_register": False,
            "blocks_dispense": False,
            "strict_storage": False,
        }

    if sys_norm == "US_CSA":
        base = US_CSA.get(code_norm, {})
    else:
        base = IN_DCA.get(code_norm, {})

    return {
        "system": sys_norm,
        "code": code_norm,
        "label": base.get("label", f"{sys_norm} {code_norm}"),
        "desc": base.get("desc", ""),
        "requires_prescription": bool(base.get("requires_prescription", False)),
        "requires_register": bool(base.get("requires_register", False)),
        "blocks_dispense": bool(base.get("blocks_dispense", False)),
        "strict_storage": bool(base.get("strict_storage", False)),
    }
