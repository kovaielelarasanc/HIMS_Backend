from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from io import BytesIO, StringIO
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.pharmacy_inventory import InventoryItem, Supplier


# -----------------------------
# Template columns (EXACT DB FIELD NAMES)
# -----------------------------
TEMPLATE_HEADERS = [
    # identity
    "code",
    "name",
    "qr_number",

    # classification
    "item_type",
    "is_consumable",
    "lasa_flag",

    # stock metadata
    "unit",
    "pack_size",
    "reorder_level",
    "max_level",

    # supplier/procurement
    "manufacturer",
    "default_supplier_id",
    "default_supplier_code",  # helper (not DB field, used to resolve supplier id)
    "procurement_date",

    # storage
    "storage_condition",

    # defaults
    "default_tax_percent",
    "default_price",
    "default_mrp",

    # regulatory schedule
    "schedule_system",
    "schedule_code",
    "schedule_notes",

    # drug fields
    "generic_name",
    "brand_name",
    "dosage_form",
    "strength",
    "active_ingredients",
    "route",
    "therapeutic_class",
    "prescription_status",
    "side_effects",
    "drug_interactions",

    # consumable fields
    "material_type",
    "sterility_status",
    "size_dimensions",
    "intended_use",
    "reusable_status",

    # other codes
    "atc_code",
    "hsn_code",

    # misc
    "is_active",
]

REQUIRED_HEADERS = ["code", "name"]

# Accept older/non-DB names in Excel and map to DB fields
HEADER_ALIASES = {
    # identity
    "item_code": "code",
    "itemcode": "code",
    "medicine_code": "code",
    "item_name": "name",
    "medicine_name": "name",
    "barcode": "qr_number",
    "bar_code": "qr_number",
    "bar_code_number": "qr_number",
    "qr": "qr_number",

    # form/class older
    "form": "dosage_form",
    "class_name": "therapeutic_class",
    "class": "therapeutic_class",

    # pricing/tax older
    "gst": "default_tax_percent",
    "tax": "default_tax_percent",
    "tax_percent": "default_tax_percent",
    "tax%": "default_tax_percent",
    "mrp": "default_mrp",
    "rate": "default_price",
    "purchase_rate": "default_price",
    "purchaseprice": "default_price",

    # stock older
    "min_stock": "reorder_level",
    "minimum_stock": "reorder_level",
    "max_stock": "max_level",
    "maximum_stock": "max_level",

    # supplier older
    "supplier": "default_supplier_code",
    "supplier_code": "default_supplier_code",
    "default_supplier": "default_supplier_code",

    # schedule older
    "schedule": "schedule_code",
    "schedulecode": "schedule_code",
    "drug_schedule": "schedule_code",
    "schedule_system_type": "schedule_system",

    # active
    "active": "is_active",
}

NA_SET = {"", "-", "na", "n/a", "null", "none", "nil"}


def _norm_header(h: Any) -> str:
    s = ("" if h is None else str(h)).strip().lower()
    s = s.replace("\ufeff", "")
    s = re.sub(r"\s+", "_", s)
    s = s.replace("%", "percent")
    return HEADER_ALIASES.get(s, s)


def _safe_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in NA_SET:
        return None
    return s


def _parse_bool(v: Any) -> Optional[bool]:
    s = _safe_text(v)
    if s is None:
        return None
    s2 = s.strip().lower()
    if s2 in {"1", "true", "t", "yes", "y"}:
        return True
    if s2 in {"0", "false", "f", "no", "n"}:
        return False
    return None


def _parse_int(v: Any) -> Optional[int]:
    s = _safe_text(v)
    if s is None:
        return None
    try:
        return int(str(s).strip())
    except Exception as e:
        raise ValueError(f"Invalid integer '{v}'") from e


def _parse_date(v: Any) -> Optional[date]:
    """
    Accepts:
      - yyyy-mm-dd
      - dd-mm-yyyy
      - dd/mm/yyyy
      - Excel date/datetime objects
    """
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()

    s = _safe_text(v)
    if s is None:
        return None

    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass

    raise ValueError(f"Invalid date '{v}' (use YYYY-MM-DD or DD-MM-YYYY)")


def _parse_decimal(v: Any) -> Optional[Decimal]:
    """
    Safe Decimal parser:
    - accepts 1,234.50
    - accepts (123.45) as -123.45
    - accepts 5% as 5
    - empty/NA -> None
    """
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))

    s = str(v).strip()
    if s.lower() in NA_SET:
        return None

    s = s.replace(",", "").strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1].strip()

    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"Invalid number '{v}'") from e


def _parse_list(v: Any) -> Optional[List[str]]:
    """
    active_ingredients:
      - "a,b,c" -> ["a","b","c"]
      - "a | b" -> ["a","b"]
      - '["a","b"]' -> ["a","b"]
      - empty/NA -> None
    """
    s = _safe_text(v)
    if s is None:
        return None

    if s.strip().startswith("["):
        try:
            obj = json.loads(s)
            if isinstance(obj, list):
                out = []
                for x in obj:
                    xs = _safe_text(x)
                    if xs:
                        out.append(xs)
                return out or None
        except Exception:
            pass

    # allow pipe OR comma separated
    parts = re.split(r"[,\|]", str(s))
    parts = [p.strip() for p in parts]
    parts = [p for p in parts if p and p.lower() not in NA_SET]
    return parts or None


def _norm_item_type(v: Any) -> Optional[str]:
    s = _safe_text(v)
    if s is None:
        return None
    s = s.strip().upper()
    if s in {"DRUG", "MED", "MEDICINE"}:
        return "DRUG"
    if s in {"CONSUMABLE", "CONSUMABLES"}:
        return "CONSUMABLE"
    if s in {"EQUIPMENT", "DEVICE"}:
        return "EQUIPMENT"
    return s


def _norm_storage(v: Any) -> Optional[str]:
    s = _safe_text(v)
    if s is None:
        return None
    s = s.strip().upper()
    s = re.sub(r"\s+", "_", s)
    return s


def _norm_prescription_status(v: Any) -> Optional[str]:
    s = _safe_text(v)
    if s is None:
        return None
    s = s.strip().upper()
    if s in {"SCHEDULE", "SCHEDULED"}:
        return "SCHEDULED"
    if s in {"RX", "PRESCRIPTION"}:
        return "RX"
    if s in {"OTC", "NONRX", "NON_RX"}:
        return "OTC"
    return s


def _norm_schedule_system(v: Any) -> Optional[str]:
    s = _safe_text(v)
    if s is None:
        return None
    s = s.strip().upper()
    if s in {"IN", "INDIA", "IN_DCA", "DCA"}:
        return "IN_DCA"
    if s in {"US", "USA", "US_CSA", "CSA"}:
        return "US_CSA"
    # keep as upper (but DB expects one of these)
    return s


SCHEDULE_IN_RE = re.compile(r"^[A-Z0-9]{1,6}$")      # H, H1, X, G, C1...
SCHEDULE_US_RE = re.compile(r"^(I|II|III|IV|V|VI)$") # II, III, IV...


def _norm_schedule_code(v: Any) -> Optional[str]:
    s = _safe_text(v)
    if s is None:
        return None
    s = s.strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    if s.lower() in NA_SET or s == "":
        return None
    return s


def _apply_type_sync(nrow: Dict[str, Any]) -> None:
    """
    Keep item_type + is_consumable consistent.
    """
    itype = (nrow.get("item_type") or "DRUG").upper()
    nrow["item_type"] = itype

    is_cons = nrow.get("is_consumable")
    if itype == "CONSUMABLE":
        nrow["is_consumable"] = True if is_cons is None else bool(is_cons)
    elif itype == "DRUG":
        nrow["is_consumable"] = False if is_cons is None else bool(is_cons)
    else:
        # equipment/future types -> keep user value or false
        nrow["is_consumable"] = False if is_cons is None else bool(is_cons)


def _apply_schedule_logic(nrow: Dict[str, Any], errors: List["UploadError"], idx: int, code: str) -> None:
    """
    - schedule_code present => prescription_status becomes SCHEDULED
    - if prescription_status is SCHEDULED => schedule_code required
    - Accept convenience: if user entered schedule_code as RX/OTC, move it to prescription_status.
    - Validate schedule_code format based on schedule_system if possible.
    """
    sysv = (nrow.get("schedule_system") or "IN_DCA").upper()
    sc = nrow.get("schedule_code")
    ps = (nrow.get("prescription_status") or "RX").upper()

    # convenience mapping
    if sc in ("RX", "OTC"):
        nrow["prescription_status"] = sc
        nrow["schedule_code"] = ""
        return

    if sc:
        nrow["prescription_status"] = "SCHEDULED"

        # validate format if possible
        if sysv == "US_CSA":
            if not SCHEDULE_US_RE.match(sc):
                errors.append(UploadError(idx, code, "schedule_code", "Invalid US_CSA schedule_code (use II / III / IV / V etc.)"))
        else:
            if not SCHEDULE_IN_RE.match(sc):
                errors.append(UploadError(idx, code, "schedule_code", "Invalid IN_DCA schedule_code (examples: H, H1, X, G, C1...)"))
        return

    # no schedule_code
    if ps in ("SCHEDULED", "SCHEDULE"):
        errors.append(UploadError(idx, code, "schedule_code", "schedule_code is required when prescription_status is SCHEDULED"))


@dataclass
class UploadError:
    row: int
    code: Optional[str]
    column: Optional[str]
    message: str


def parse_upload_to_rows(filename: str, content_type: str, raw: bytes) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Returns: (file_type, list_of_row_dicts_normalized_headers)
    Supports: CSV/TSV/TXT and XLSX
    """
    name = (filename or "").lower()

    # XLSX
    if name.endswith(".xlsx") or content_type in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    }:
        try:
            from openpyxl import load_workbook
        except Exception as e:
            raise ValueError("openpyxl is required for Excel uploads. Install: pip install openpyxl") from e

        wb = load_workbook(BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return ("xlsx", [])

        headers = [_norm_header(h) for h in rows[0]]
        out: List[Dict[str, Any]] = []
        for i, r in enumerate(rows[1:], start=2):
            d: Dict[str, Any] = {}
            for j, h in enumerate(headers):
                if not h:
                    continue
                d[h] = r[j] if j < len(r) else None
            out.append(d)
        return ("xlsx", out)

    # CSV/TSV/TXT
    try:
        text = raw.decode("utf-8-sig")
    except Exception:
        text = raw.decode("latin-1", errors="replace")

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=[",", "\t", ";", "|"])
        delim = dialect.delimiter
    except Exception:
        delim = ","

    reader = csv.DictReader(StringIO(text), delimiter=delim)
    out: List[Dict[str, Any]] = []
    for row in reader:
        d = {_norm_header(k): v for k, v in (row or {}).items()}
        out.append(d)

    return ("csv", out)


def validate_item_rows(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[UploadError]]:
    """
    Normalizes + validates rows.
    - Returns normalized_rows (typed) and errors
    - Does not touch DB
    """
    errors: List[UploadError] = []
    normalized: List[Dict[str, Any]] = []
    seen_codes = set()

    for idx, row in enumerate(rows, start=2):
        code = _safe_text(row.get("code"))
        name = _safe_text(row.get("name"))

        if not code:
            continue

        if code in seen_codes:
            errors.append(UploadError(idx, code, "code", "Duplicate code in uploaded file"))
            continue
        seen_codes.add(code)

        if not name:
            errors.append(UploadError(idx, code, "name", "Name is required"))
            continue

        def dec(col: str) -> Optional[Decimal]:
            try:
                return _parse_decimal(row.get(col))
            except ValueError as e:
                errors.append(UploadError(idx, code, col, str(e)))
                return None

        def boo(col: str) -> Optional[bool]:
            rawv = row.get(col)
            b = _parse_bool(rawv)
            if rawv not in (None, "", " ", "-", "NA", "na") and b is None:
                errors.append(UploadError(idx, code, col, f"Invalid boolean '{rawv}' (use TRUE/FALSE/1/0)"))
            return b

        def dt(col: str) -> Optional[date]:
            try:
                return _parse_date(row.get(col))
            except ValueError as e:
                errors.append(UploadError(idx, code, col, str(e)))
                return None

        # normalize schedule fields
        schedule_system = _norm_schedule_system(row.get("schedule_system")) or "IN_DCA"
        schedule_code = _norm_schedule_code(row.get("schedule_code"))
        prescription_status = _norm_prescription_status(row.get("prescription_status")) or "RX"

        nrow: Dict[str, Any] = {
            # identity
            "code": code,
            "name": name,
            "qr_number": _safe_text(row.get("qr_number")),

            # classification
            "item_type": _norm_item_type(row.get("item_type")) or "DRUG",
            "is_consumable": boo("is_consumable"),
            "lasa_flag": boo("lasa_flag"),

            # stock metadata
            "unit": _safe_text(row.get("unit")),
            "pack_size": _safe_text(row.get("pack_size")),
            "reorder_level": dec("reorder_level"),
            "max_level": dec("max_level"),

            # supplier/procurement
            "manufacturer": _safe_text(row.get("manufacturer")),
            "default_supplier_id": None,
            "default_supplier_code": _safe_text(row.get("default_supplier_code")),
            "procurement_date": dt("procurement_date"),

            # storage
            "storage_condition": _norm_storage(row.get("storage_condition")),

            # defaults
            "default_tax_percent": dec("default_tax_percent"),
            "default_price": dec("default_price"),
            "default_mrp": dec("default_mrp"),

            # regulatory schedule
            "schedule_system": schedule_system,
            "schedule_code": schedule_code or "",
            "schedule_notes": _safe_text(row.get("schedule_notes")),

            # drug fields
            "generic_name": _safe_text(row.get("generic_name")),
            "brand_name": _safe_text(row.get("brand_name")),
            "dosage_form": _safe_text(row.get("dosage_form")),
            "strength": _safe_text(row.get("strength")),
            "active_ingredients": _parse_list(row.get("active_ingredients")),
            "route": _safe_text(row.get("route")),
            "therapeutic_class": _safe_text(row.get("therapeutic_class")),
            "prescription_status": prescription_status,
            "side_effects": _safe_text(row.get("side_effects")),
            "drug_interactions": _safe_text(row.get("drug_interactions")),

            # consumable fields
            "material_type": _safe_text(row.get("material_type")),
            "sterility_status": _safe_text(row.get("sterility_status")),
            "size_dimensions": _safe_text(row.get("size_dimensions")),
            "intended_use": _safe_text(row.get("intended_use")),
            "reusable_status": _safe_text(row.get("reusable_status")),

            # other codes
            "atc_code": _safe_text(row.get("atc_code")),
            "hsn_code": _safe_text(row.get("hsn_code")),

            # misc
            "is_active": boo("is_active"),
        }

        # supplier id parse if provided
        try:
            nrow["default_supplier_id"] = _parse_int(row.get("default_supplier_id"))
        except ValueError as e:
            errors.append(UploadError(idx, code, "default_supplier_id", str(e)))
            nrow["default_supplier_id"] = None

        # type sync
        _apply_type_sync(nrow)

        # schedule logic + validation
        _apply_schedule_logic(nrow, errors, idx, code)

        normalized.append(nrow)

    return normalized, errors


def apply_items_import(
    db: Session,
    normalized_rows: List[Dict[str, Any]],
    *,
    update_blanks: bool = False,
) -> Tuple[int, int, int, List[UploadError]]:
    """
    Commits valid rows to DB.
    - update_blanks=False: blank values do NOT overwrite existing values
    """
    created = 0
    updated = 0
    skipped = 0
    errors: List[UploadError] = []

    if not normalized_rows:
        return 0, 0, 0, []

    # existing items by code
    codes = [r["code"] for r in normalized_rows]
    existing_items = db.query(InventoryItem).filter(InventoryItem.code.in_(codes)).all()
    existing_by_code = {it.code: it for it in existing_items}

    # QR uniqueness if provided
    qrs = [r.get("qr_number") for r in normalized_rows if r.get("qr_number")]
    if qrs:
        qr_existing = db.query(InventoryItem).filter(InventoryItem.qr_number.in_(qrs)).all()
        qr_to_code = {it.qr_number: it.code for it in qr_existing if it.qr_number}
        for i, r in enumerate(normalized_rows, start=2):
            qr = r.get("qr_number")
            if qr and qr in qr_to_code:
                other_code = qr_to_code[qr]
                if other_code != r["code"]:
                    errors.append(UploadError(i, r["code"], "qr_number", f"QR already used by item code '{other_code}'"))

    if errors:
        return 0, 0, 0, errors

    # resolve supplier from default_supplier_code if present and supplier_id not provided
    supplier_keys = { (r.get("default_supplier_code") or "").strip() for r in normalized_rows if r.get("default_supplier_code") }
    supplier_keys = {k for k in supplier_keys if k}

    supplier_by_code: Dict[str, Supplier] = {}
    supplier_by_name: Dict[str, Supplier] = {}

    if supplier_keys:
        sups = (
            db.query(Supplier)
            .filter(or_(Supplier.code.in_(supplier_keys), Supplier.name.in_(supplier_keys)))
            .all()
        )
        for s in sups:
            if getattr(s, "code", None):
                supplier_by_code[str(s.code).strip().upper()] = s
            if getattr(s, "name", None):
                supplier_by_name[str(s.name).strip().lower()] = s

        for row_idx, r in enumerate(normalized_rows, start=2):
            if r.get("default_supplier_id"):
                continue
            key = (r.get("default_supplier_code") or "").strip()
            if not key:
                continue
            sup = supplier_by_code.get(key.upper()) or supplier_by_name.get(key.lower())
            if not sup:
                errors.append(UploadError(row_idx, r["code"], "default_supplier_code", f"Supplier '{key}' not found"))
            else:
                r["default_supplier_id"] = int(sup.id)

    if errors:
        return 0, 0, 0, errors

    def should_set(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, str) and v.strip() == "" and not update_blanks:
            return False
        if isinstance(v, list) and len(v) == 0 and not update_blanks:
            return False
        return True

    try:
        for row_idx, r in enumerate(normalized_rows, start=2):
            code = r["code"]
            existing = existing_by_code.get(code)

            # helper-only key (not in DB)
            r = dict(r)
            r.pop("default_supplier_code", None)

            if existing:
                # UPDATE
                for field, val in r.items():
                    if field == "code":
                        continue
                    if should_set(val):
                        setattr(existing, field, val)

                # keep is_active if not provided
                if r.get("is_active") is None:
                    pass

                # enforce schedule consistency on update
                sc = (getattr(existing, "schedule_code", "") or "").strip()
                ps = (getattr(existing, "prescription_status", "RX") or "RX").upper()
                if sc and ps != "SCHEDULED":
                    existing.prescription_status = "SCHEDULED"
                if ps == "SCHEDULED" and not sc:
                    raise ValueError(f"Row {row_idx} ({code}): schedule_code is required for SCHEDULED medicines")

                # enforce type consistency on update
                itype = (getattr(existing, "item_type", "DRUG") or "DRUG").upper()
                if itype == "CONSUMABLE":
                    existing.is_consumable = True
                elif itype == "DRUG":
                    existing.is_consumable = False

                updated += 1

            else:
                # CREATE (set safe defaults for nullable=False fields)
                payload = dict(r)

                payload["item_type"] = (payload.get("item_type") or "DRUG").upper()
                payload["is_consumable"] = bool(payload.get("is_consumable") or (payload["item_type"] == "CONSUMABLE"))
                payload["lasa_flag"] = bool(payload.get("lasa_flag") or False)
                payload["is_active"] = True if payload.get("is_active") is None else bool(payload.get("is_active"))

                payload["unit"] = payload.get("unit") or "unit"
                payload["pack_size"] = payload.get("pack_size") or "1"

                payload["manufacturer"] = payload.get("manufacturer") or ""
                payload["storage_condition"] = payload.get("storage_condition") or "ROOM_TEMP"

                payload["default_tax_percent"] = payload.get("default_tax_percent") or Decimal("0")
                payload["default_price"] = payload.get("default_price") or Decimal("0")
                payload["default_mrp"] = payload.get("default_mrp") or Decimal("0")

                payload["reorder_level"] = payload.get("reorder_level") or Decimal("0")
                payload["max_level"] = payload.get("max_level") or Decimal("0")

                payload["schedule_system"] = (payload.get("schedule_system") or "IN_DCA").upper()
                payload["schedule_code"] = (payload.get("schedule_code") or "").upper()
                payload["schedule_notes"] = payload.get("schedule_notes") or ""

                payload["generic_name"] = payload.get("generic_name") or ""
                payload["brand_name"] = payload.get("brand_name") or ""
                payload["dosage_form"] = payload.get("dosage_form") or ""
                payload["strength"] = payload.get("strength") or ""
                payload["route"] = payload.get("route") or ""
                payload["therapeutic_class"] = payload.get("therapeutic_class") or ""
                payload["prescription_status"] = (payload.get("prescription_status") or "RX").upper()
                payload["side_effects"] = payload.get("side_effects") or ""
                payload["drug_interactions"] = payload.get("drug_interactions") or ""

                payload["material_type"] = payload.get("material_type") or ""
                payload["sterility_status"] = payload.get("sterility_status") or ""
                payload["size_dimensions"] = payload.get("size_dimensions") or ""
                payload["intended_use"] = payload.get("intended_use") or ""
                payload["reusable_status"] = payload.get("reusable_status") or ""

                payload["atc_code"] = payload.get("atc_code") or ""
                payload["hsn_code"] = payload.get("hsn_code") or ""

                # schedule safety
                if payload["schedule_code"]:
                    payload["prescription_status"] = "SCHEDULED"
                if payload["prescription_status"] == "SCHEDULED" and not payload["schedule_code"]:
                    raise ValueError(f"Row {row_idx} ({code}): schedule_code is required for SCHEDULED medicines")

                item = InventoryItem(**payload)
                db.add(item)
                db.flush()  # get item.id

                if not item.qr_number:
                    item.qr_number = f"MED-{item.id:06d}"

                created += 1

        db.commit()
        return created, updated, skipped, []

    except IntegrityError as e:
        db.rollback()
        return 0, 0, 0, [UploadError(row=0, code=None, column=None, message=f"DB constraint error: {str(e.orig)}")]
    except Exception as e:
        db.rollback()
        return 0, 0, 0, [UploadError(row=0, code=None, column=None, message=f"Unexpected error: {str(e)}")]
