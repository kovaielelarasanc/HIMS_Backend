from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.pharmacy_inventory import InventoryItem


# -----------------------------
# Template columns (header names)
# -----------------------------
TEMPLATE_HEADERS = [
    "code",
    "name",
    "generic_name",
    "form",
    "strength",
    "unit",
    "pack_size",
    "manufacturer",
    "class_name",
    "atc_code",
    "hsn_code",
    "lasa_flag",
    "is_consumable",
    "default_tax_percent",
    "default_price",
    "default_mrp",
    "reorder_level",
    "max_level",
    "is_active",
    "qr_number",
]

REQUIRED_HEADERS = ["code", "name"]

# Allow user-friendly column names in sheets
HEADER_ALIASES = {
    "item_code": "code",
    "itemcode": "code",
    "item": "name",
    "item_name": "name",
    "brand_name": "name",
    "generic": "generic_name",
    "gst": "default_tax_percent",
    "tax": "default_tax_percent",
    "tax_percent": "default_tax_percent",
    "tax%": "default_tax_percent",
    "mrp": "default_mrp",
    "purchase_rate": "default_price",
    "rate": "default_price",
    "min_stock": "reorder_level",
    "max_stock": "max_level",
    "active": "is_active",
    "qr": "qr_number",
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
    # Unknown -> treat as error by returning None? We'll keep None and validate if needed.
    return None


def _parse_decimal(v: Any) -> Optional[Decimal]:
    """
    Safe Decimal parser:
    - accepts 1,234.50
    - accepts (123.45) as -123.45
    - accepts 5% as 5
    - empty/NA -> None (so existing values are not overwritten in update mode)
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

    # remove commas
    s = s.replace(",", "").strip()

    # percent "5%" -> "5"
    if s.endswith("%"):
        s = s[:-1].strip()

    # (123.45) -> -123.45
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1].strip()

    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"Invalid number '{v}'") from e


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
        for i, r in enumerate(rows[1:], start=2):  # Excel row numbers start at 1, header at 1
            d = {}
            for j, h in enumerate(headers):
                if not h:
                    continue
                d[h] = r[j] if j < len(r) else None
            out.append(d)
        return ("xlsx", out)

    # CSV / TSV / TXT
    # Try decode robustly
    try:
        text = raw.decode("utf-8-sig")
    except Exception:
        text = raw.decode("latin-1", errors="replace")

    # detect delimiter
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=[",", "\t", ";", "|"])
        delim = dialect.delimiter
    except Exception:
        delim = ","  # default

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

    for idx, row in enumerate(rows, start=2):  # assume header is row 1
        code = _safe_text(row.get("code"))
        name = _safe_text(row.get("name"))

        if not code:
            # skip empty lines silently
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
            b = _parse_bool(row.get(col))
            if row.get(col) not in (None, "", " ", "-", "NA", "na") and b is None:
                errors.append(UploadError(idx, code, col, f"Invalid boolean '{row.get(col)}' (use TRUE/FALSE/1/0)"))
            return b

        nrow: Dict[str, Any] = {
            "code": code,
            "name": name,
            "generic_name": _safe_text(row.get("generic_name")),
            "form": _safe_text(row.get("form")),
            "strength": _safe_text(row.get("strength")),
            "unit": _safe_text(row.get("unit")),
            "pack_size": _safe_text(row.get("pack_size")),
            "manufacturer": _safe_text(row.get("manufacturer")),
            "class_name": _safe_text(row.get("class_name")),
            "atc_code": _safe_text(row.get("atc_code")),
            "hsn_code": _safe_text(row.get("hsn_code")),
            "qr_number": _safe_text(row.get("qr_number")),
            "lasa_flag": boo("lasa_flag"),
            "is_consumable": boo("is_consumable"),
            "default_tax_percent": dec("default_tax_percent"),
            "default_price": dec("default_price"),
            "default_mrp": dec("default_mrp"),
            "reorder_level": dec("reorder_level"),
            "max_level": dec("max_level"),
            "is_active": boo("is_active"),
        }

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
    - update_blanks=False: blank values do NOT overwrite existing
    """
    created = 0
    updated = 0
    skipped = 0
    errors: List[UploadError] = []

    if not normalized_rows:
        return 0, 0, 0, []

    codes = [r["code"] for r in normalized_rows]
    existing_items = db.query(InventoryItem).filter(InventoryItem.code.in_(codes)).all()
    existing_by_code = {it.code: it for it in existing_items}

    # Validate QR uniqueness (if provided)
    qrs = [r.get("qr_number") for r in normalized_rows if r.get("qr_number")]
    if qrs:
        qr_existing = (
            db.query(InventoryItem)
            .filter(InventoryItem.qr_number.in_(qrs))
            .all()
        )
        qr_to_code = {it.qr_number: it.code for it in qr_existing if it.qr_number}
        for i, r in enumerate(normalized_rows, start=2):
            qr = r.get("qr_number")
            if qr and qr in qr_to_code:
                other_code = qr_to_code[qr]
                if other_code != r["code"]:
                    errors.append(UploadError(i, r["code"], "qr_number", f"QR already used by item code '{other_code}'"))

    if errors:
        return 0, 0, 0, errors

    def should_set(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, str) and v.strip() == "" and not update_blanks:
            return False
        return True

    try:
        for row_idx, r in enumerate(normalized_rows, start=2):
            code = r["code"]
            existing = existing_by_code.get(code)

            if existing:
                # Update
                for field, val in r.items():
                    if field in {"code"}:
                        continue
                    if should_set(val):
                        setattr(existing, field, val)
                # If is_active not provided, keep current
                if r.get("is_active") is None:
                    pass
                updated += 1
            else:
                # Create (set safe defaults for nullable=False fields)
                payload = dict(r)
                # defaults if missing
                payload["generic_name"] = payload.get("generic_name") or ""
                payload["form"] = payload.get("form") or ""
                payload["strength"] = payload.get("strength") or ""
                payload["unit"] = payload.get("unit") or "unit"
                payload["pack_size"] = payload.get("pack_size") or "1"
                payload["manufacturer"] = payload.get("manufacturer") or ""
                payload["class_name"] = payload.get("class_name") or ""
                payload["atc_code"] = payload.get("atc_code") or ""
                payload["hsn_code"] = payload.get("hsn_code") or ""

                payload["lasa_flag"] = bool(payload.get("lasa_flag") or False)
                payload["is_consumable"] = bool(payload.get("is_consumable") or False)
                payload["is_active"] = True if payload.get("is_active") is None else bool(payload.get("is_active"))

                payload["default_tax_percent"] = payload.get("default_tax_percent") or Decimal("0")
                payload["default_price"] = payload.get("default_price") or Decimal("0")
                payload["default_mrp"] = payload.get("default_mrp") or Decimal("0")
                payload["reorder_level"] = payload.get("reorder_level") or Decimal("0")
                payload["max_level"] = payload.get("max_level") or Decimal("0")

                item = InventoryItem(**payload)
                db.add(item)
                db.flush()  # to get item.id for QR auto generation

                if not item.qr_number:
                    item.qr_number = f"MED-{item.id:06d}"

                created += 1

        db.commit()
        return created, updated, skipped, []

    except IntegrityError as e:
        db.rollback()
        # convert to user-friendly message
        return 0, 0, 0, [UploadError(row=0, code=None, column=None, message=f"DB constraint error: {str(e.orig)}")]
    except Exception as e:
        db.rollback()
        return 0, 0, 0, [UploadError(row=0, code=None, column=None, message=f"Unexpected error: {str(e)}")]
