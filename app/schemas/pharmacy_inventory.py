# FILE: app/schemas/pharmacy_inventory.py
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Literal, Any
import re
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict, condecimal, field_validator, EmailStr, model_validator, AliasChoices
from app.models.pharmacy_inventory import GRNStatus  # ✅ import your model Enum
# ---------- Locations ----------


class LocationBase(BaseModel):
    code: str
    name: str
    description: str | None = ""
    is_pharmacy: bool = True
    is_active: bool = True
    expiry_alert_days: int = 90


class LocationCreate(LocationBase):
    pass


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_pharmacy: Optional[bool] = None
    is_active: Optional[bool] = None
    expiry_alert_days: Optional[int] = None


class LocationOut(LocationBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


PaymentMethod = Literal["UPI", "BANK_TRANSFER", "CASH", "CHEQUE", "OTHER"]
Quantity = condecimal(max_digits=14, decimal_places=4)
Money = condecimal(max_digits=14, decimal_places=4)
Percent = condecimal(max_digits=5, decimal_places=2)
UPI_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,}@[a-zA-Z]{2,}$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
CODE_RE = re.compile(r"^[A-Z0-9/_\.\-]{2,50}$")


def _none_if_blank(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return None if s == "" else s


class SupplierBaseOut(BaseModel):
    # ✅ tolerant output (won't crash for old data)
    code: str
    name: str
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    gstin: Optional[str] = None
    payment_terms: Optional[str] = None
    is_active: bool = True

    payment_method: Optional[str] = "UPI"
    upi_id: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    bank_name: Optional[str] = None
    bank_branch: Optional[str] = None

    @field_validator("email", mode="before")
    @classmethod
    def _email_blank_to_none(cls, v):
        return _none_if_blank(v)

    model_config = ConfigDict(from_attributes=True)


class SupplierOut(SupplierBaseOut):
    id: int
    created_at: datetime
    updated_at: datetime


# ✅ strict input (create)
class SupplierCreate(BaseModel):
    code: str
    name: str
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    address: Optional[str] = None
    gstin: Optional[str] = None
    payment_terms: Optional[str] = None
    is_active: bool = True

    payment_method: PaymentMethod = "UPI"
    upi_id: Optional[str] = None

    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    bank_name: Optional[str] = None
    bank_branch: Optional[str] = None

    @field_validator("code", mode="before")
    @classmethod
    def _code_norm(cls, v):
        s = str(v or "").strip().upper().replace(" ", "_")
        if not CODE_RE.match(s):
            raise ValueError("Invalid supplier code (use A-Z, 0-9, -, _, /, .)")
        return s

    @field_validator("upi_id", mode="before")
    @classmethod
    def _upi_norm(cls, v):
        return _none_if_blank(v)

    @field_validator("bank_ifsc", mode="before")
    @classmethod
    def _ifsc_norm(cls, v):
        vv = _none_if_blank(v)
        return vv.upper() if vv else None

    @field_validator("bank_account_number", mode="before")
    @classmethod
    def _acc_norm(cls, v):
        vv = _none_if_blank(v)
        return re.sub(r"\s+", "", vv) if vv else None

    @model_validator(mode="after")
    def _validate_payment(self):
        pm = self.payment_method

        if pm == "UPI":
            if not self.upi_id:
                raise ValueError("UPI ID is required when payment_method is UPI")
            if not UPI_RE.match(self.upi_id):
                raise ValueError("Invalid UPI ID (example: name@bank)")

        if pm == "BANK_TRANSFER":
            if not self.bank_account_name:
                raise ValueError("Account name is required for bank transfer")
            if not self.bank_account_number or not re.match(r"^\d{6,20}$", self.bank_account_number):
                raise ValueError("Account number must be 6–20 digits")
            if not self.bank_ifsc or not IFSC_RE.match(self.bank_ifsc):
                raise ValueError("Invalid IFSC (example: HDFC0001234)")

        return self


# ✅ strict-ish update (no code change)
class SupplierUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    address: Optional[str] = None
    gstin: Optional[str] = None
    payment_terms: Optional[str] = None
    is_active: Optional[bool] = None

    payment_method: Optional[PaymentMethod] = None
    upi_id: Optional[str] = None

    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    bank_name: Optional[str] = None
    bank_branch: Optional[str] = None

    @field_validator("upi_id", mode="before")
    @classmethod
    def _upi_norm_u(cls, v):
        return _none_if_blank(v)

    @field_validator("bank_ifsc", mode="before")
    @classmethod
    def _ifsc_norm_u(cls, v):
        vv = _none_if_blank(v)
        return vv.upper() if vv else None

    @field_validator("bank_account_number", mode="before")
    @classmethod
    def _acc_norm_u(cls, v):
        vv = _none_if_blank(v)
        return re.sub(r"\s+", "", vv) if vv else None


SCHEDULE_RE = re.compile(r"^[A-Z0-9]{1,6}$")

def _norm_schedule(v: Optional[str]) -> Optional[str]:
    """
    Accepts: "H", "H1", "X", "Schedule H", "SCHEDULE-H", "schedule h1", "rx", "otc"
    Returns normalized: "H" / "H1" / "X" / "RX" / "OTC" or None
    """
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s:
        return None

    # remove common prefixes like "SCHEDULE", "SCHEDULE-"
    s = s.replace("SCHEDULE", "").replace(" ", "").replace("-", "").replace("_", "")
    # after removing, examples:
    # "SCHEDULEH" -> "H"
    # "H1" -> "H1"

    if s in ("RX", "OTC"):
        return s

    # allow schedule codes like H / H1 / X etc.
    if not SCHEDULE_RE.match(s):
        raise ValueError("Invalid schedule_code. Use H/H1/X (or RX/OTC).")
    return s


class SupplierMini(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None


# -----------------------------
# Normalizers / helpers
# -----------------------------
def _s(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


def _u(v: Any) -> str:
    return _s(v).upper()


def _float(v: Any, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except Exception:
        raise ValueError("must be a number")


def _dec(v: Any, default: Decimal = Decimal("0")) -> Decimal:
    if v is None or v == "":
        return default
    try:
        return Decimal(str(v))
    except Exception:
        raise ValueError("must be a decimal number")


def _norm_schedule(v: Any) -> Optional[str]:
    """
    Normalize schedule_code:
    - Accepts: None / '' / 'H' / 'H1' / 'X' / 'rx' / 'otc'
    - Returns: None or upper normalized string
    """
    s = _u(v)
    if not s:
        return None
    # allow RX/OTC to be passed in schedule_code field too
    if s in ("RX", "OTC", "H", "H1", "X"):
        return s
    raise ValueError("schedule_code must be one of: H, H1, X, RX, OTC")


def _ensure_non_negative(name: str, value: float) -> float:
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


# -----------------------------
# Base schema
# -----------------------------
class ItemBase(BaseModel):
    model_config = ConfigDict(
        extra="ignore",     
        from_attributes=True,    # ORM support
        populate_by_name=True,   # allow aliases
        str_strip_whitespace=True,
    )

    # identity
    code: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=255)
    qr_number: Optional[str] = Field(default=None, max_length=50)

    # classification
    item_type: str = Field(default="DRUG", description="DRUG | CONSUMABLE | EQUIPMENT")
    is_consumable: bool = Field(default=False, description="Compatibility flag")

    # flags
    lasa_flag: bool = False
    high_alert_flag: bool = False
    requires_double_check: bool = False

    # stock metadata
    unit: str = Field(default="unit", max_length=50)
    pack_size: str = Field(default="1", max_length=50)

    # ✅ UOM conversion
    base_uom: str = Field(default="unit", max_length=30)
    purchase_uom: str = Field(default="unit", max_length=30)
    conversion_factor: Decimal = Field(default=Decimal("1"))

    reorder_level: Decimal = Field(default=Decimal("0"))
    max_level: Decimal = Field(default=Decimal("0"))

    # supplier / procurement
    manufacturer: str = Field(default="", max_length=255)
    default_supplier_id: Optional[int] = None
    procurement_date: Optional[date] = None

    # storage
    storage_condition: str = Field(
        default="ROOM_TEMP",
        max_length=30,
        validation_alias=AliasChoices("storage_condition", "storage_conditions"),
    )

    # defaults (suggestions)
    default_tax_percent: Decimal = Field(default=Decimal("0"))
    default_price: Decimal = Field(default=Decimal("0"))
    default_mrp: Decimal = Field(default=Decimal("0"))

    # Regulatory schedule (NEW fields from DB model)
    schedule_system: str = Field(default="IN_DCA", max_length=20, description="IN_DCA (default)")
    schedule_code: Optional[str] = Field(default=None, description="H/H1/X or RX/OTC")
    schedule_notes: str = Field(default="", max_length=255)

    # DRUG fields
    generic_name: str = Field(default="", max_length=255)
    brand_name: str = Field(default="", max_length=255)
    dosage_form: str = Field(
        default="",
        max_length=100,
        validation_alias=AliasChoices("dosage_form", "form"),
    )
    strength: str = Field(default="", max_length=100)
    active_ingredients: Optional[List[str]] = None  # stored JSON
    route: str = Field(
        default="",
        max_length=50,
        validation_alias=AliasChoices("route", "route_of_administration"),
    )
    therapeutic_class: str = Field(
        default="",
        max_length=255,
        validation_alias=AliasChoices("therapeutic_class", "class_name"),
    )

    # OTC | RX | SCHEDULED
    prescription_status: str = Field(default="RX", max_length=20)

    side_effects: str = Field(default="")
    drug_interactions: str = Field(default="")

    # CONSUMABLE fields
    material_type: str = Field(default="", max_length=100)
    sterility_status: str = Field(default="", max_length=20)  # STERILE / NON_STERILE
    size_dimensions: str = Field(default="", max_length=120)
    intended_use: str = Field(default="")
    reusable_status: str = Field(
        default="",
        max_length=20,
        validation_alias=AliasChoices("reusable_status", "reusable_disposable"),
    )

    # codes
    atc_code: str = Field(default="", max_length=50)
    hsn_code: str = Field(default="", max_length=50)

    is_active: bool = True

    # -----------------------------
    # Field validators (normalize)
    # -----------------------------
    @field_validator("code", mode="before")
    @classmethod
    def _code_norm(cls, v: Any) -> str:
        s = _s(v)
        if not s:
            raise ValueError("code is required")
        return s.upper()  # keep consistent codes

    @field_validator("item_type", mode="before")
    @classmethod
    def _item_type_norm(cls, v: Any) -> str:
        s = _u(v) or "DRUG"
        if s not in ("DRUG", "CONSUMABLE", "EQUIPMENT"):
            raise ValueError("item_type must be one of: DRUG, CONSUMABLE, EQUIPMENT")
        return s

    @field_validator("prescription_status", mode="before")
    @classmethod
    def _ps_norm(cls, v: Any) -> str:
        s = _u(v) or "RX"
        # allow some variants
        if s == "SCHEDULE":
            s = "SCHEDULED"
        if s not in ("OTC", "RX", "SCHEDULED"):
            raise ValueError("prescription_status must be one of: OTC, RX, SCHEDULED")
        return s

    @field_validator("schedule_code", mode="before")
    @classmethod
    def _schedule_norm(cls, v: Any) -> Optional[str]:
        return _norm_schedule(v)

    @field_validator("conversion_factor", mode="before")
    @classmethod
    def _conv_factor_parse(cls, v: Any) -> Decimal:
        d = _dec(v, Decimal("1"))
        if d <= 0:
            raise ValueError("conversion_factor must be > 0")
        return d

    @field_validator("reorder_level", "max_level", "default_tax_percent", "default_price", "default_mrp", mode="before")
    @classmethod
    def _decimal_parse(cls, v: Any, info) -> Decimal:
        d = _dec(v, Decimal("0"))
        # allow zero, but not negative
        if d < 0:
            raise ValueError(f"{info.field_name} must be >= 0")
        return d

    @field_validator("active_ingredients", mode="before")
    @classmethod
    def _ai_parse(cls, v: Any) -> Optional[List[str]]:
        """
        Accept:
        - list[str]
        - comma-separated string
        - None
        """
        if v is None or v == "":
            return None
        if isinstance(v, list):
            out = [ _s(x) for x in v if _s(x) ]
            return out or None
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",")]
            parts = [p for p in parts if p]
            return parts or None
        raise ValueError("active_ingredients must be a list of strings or comma-separated text")

    # -----------------------------
    # Cross-field validation / sync rules
    # -----------------------------
    @model_validator(mode="after")
    def _sync_and_validate(self):
        """
        - Keep item_type <-> is_consumable consistent
        - High-alert implies double-check (recommended)
        - max_level >= reorder_level
        - schedule_code <-> prescription_status consistency
        - Basic required fields by item_type (light rules)
        """
        # item_type <-> is_consumable sync
        if self.item_type == "CONSUMABLE":
            self.is_consumable = True
        else:
            self.is_consumable = False

        # high alert implies double-check (common hospital rule)
        if self.high_alert_flag and not self.requires_double_check:
            self.requires_double_check = True

        # stock levels
        if self.max_level < self.reorder_level:
            raise ValueError("max_level must be >= reorder_level")

        # Schedule rules:
        sc = (self.schedule_code or "").strip().upper() if self.schedule_code else None
        ps = (self.prescription_status or "RX").strip().upper()

        if sc in ("RX", "OTC"):
            # if schedule_code is RX/OTC, we treat as prescription_status
            self.prescription_status = sc
            # optional: keep schedule_code NULL for RX/OTC in DB
            # self.schedule_code = None
            return self

        if sc in ("H", "H1", "X"):
            self.prescription_status = "SCHEDULED"
            return self

        # If prescription_status says scheduled, schedule_code must be provided
        if ps == "SCHEDULED" and not sc:
            raise ValueError("schedule_code is required when prescription_status is SCHEDULED")

        # Light rules by type (optional but helpful)
        if self.item_type == "DRUG":
            # at least something meaningful
            if not (_s(self.generic_name) or _s(self.brand_name) or _s(self.name)):
                raise ValueError("For DRUG items, provide generic_name or brand_name")
        elif self.item_type == "CONSUMABLE":
            if not _s(self.material_type):
                # don't hard-fail if you don't want; but this is useful
                raise ValueError("For CONSUMABLE items, material_type is required")

        return self


# -----------------------------
# Create / Update / Output
# -----------------------------
class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    """
    PATCH/PUT schema: all optional, still validated.
    """
    model_config = ConfigDict(extra="forbid", from_attributes=True, str_strip_whitespace=True)

    code: Optional[str] = Field(default=None, min_length=1, max_length=100)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    qr_number: Optional[str] = Field(default=None, max_length=50)

    item_type: Optional[str] = None
    is_consumable: Optional[bool] = None

    lasa_flag: Optional[bool] = None
    high_alert_flag: Optional[bool] = None
    requires_double_check: Optional[bool] = None

    unit: Optional[str] = Field(default=None, max_length=50)
    pack_size: Optional[str] = Field(default=None, max_length=50)

    base_uom: Optional[str] = Field(default=None, max_length=30)
    purchase_uom: Optional[str] = Field(default=None, max_length=30)
    conversion_factor: Optional[Decimal] = None

    reorder_level: Optional[Decimal] = None
    max_level: Optional[Decimal] = None

    manufacturer: Optional[str] = Field(default=None, max_length=255)
    default_supplier_id: Optional[int] = None
    procurement_date: Optional[date] = None

    storage_condition: Optional[str] = Field(default=None, max_length=30)

    default_tax_percent: Optional[Decimal] = None
    default_price: Optional[Decimal] = None
    default_mrp: Optional[Decimal] = None

    schedule_system: Optional[str] = Field(default=None, max_length=20)
    schedule_code: Optional[str] = Field(default=None)
    schedule_notes: Optional[str] = Field(default=None, max_length=255)

    generic_name: Optional[str] = Field(default=None, max_length=255)
    brand_name: Optional[str] = Field(default=None, max_length=255)
    dosage_form: Optional[str] = Field(default=None, max_length=100)
    strength: Optional[str] = Field(default=None, max_length=100)
    active_ingredients: Optional[List[str]] = None
    route: Optional[str] = Field(default=None, max_length=50)
    therapeutic_class: Optional[str] = Field(default=None, max_length=255)
    prescription_status: Optional[str] = Field(default=None, max_length=20)
    side_effects: Optional[str] = None
    drug_interactions: Optional[str] = None

    material_type: Optional[str] = Field(default=None, max_length=100)
    sterility_status: Optional[str] = Field(default=None, max_length=20)
    size_dimensions: Optional[str] = Field(default=None, max_length=120)
    intended_use: Optional[str] = None
    reusable_status: Optional[str] = Field(default=None, max_length=20)

    atc_code: Optional[str] = Field(default=None, max_length=50)
    hsn_code: Optional[str] = Field(default=None, max_length=50)

    is_active: Optional[bool] = None

    # Reuse same validators from ItemBase (copy minimal important ones)
    @field_validator("code", mode="before")
    @classmethod
    def _code_norm(cls, v: Any) -> Any:
        if v is None:
            return None
        s = _s(v)
        if not s:
            raise ValueError("code cannot be empty")
        return s.upper()

    @field_validator("item_type", mode="before")
    @classmethod
    def _item_type_norm(cls, v: Any) -> Any:
        if v is None:
            return None
        s = _u(v)
        if s not in ("DRUG", "CONSUMABLE", "EQUIPMENT"):
            raise ValueError("item_type must be one of: DRUG, CONSUMABLE, EQUIPMENT")
        return s

    @field_validator("prescription_status", mode="before")
    @classmethod
    def _ps_norm(cls, v: Any) -> Any:
        if v is None:
            return None
        s = _u(v)
        if s == "SCHEDULE":
            s = "SCHEDULED"
        if s not in ("OTC", "RX", "SCHEDULED"):
            raise ValueError("prescription_status must be one of: OTC, RX, SCHEDULED")
        return s

    @field_validator("schedule_code", mode="before")
    @classmethod
    def _schedule_norm(cls, v: Any) -> Any:
        # allow explicit nulling schedule_code by sending "" or null
        if v is None or v == "":
            return None
        return _norm_schedule(v)

    @field_validator("conversion_factor", mode="before")
    @classmethod
    def _conv_factor_parse(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        d = _dec(v, Decimal("1"))
        if d <= 0:
            raise ValueError("conversion_factor must be > 0")
        return d

    @field_validator("reorder_level", "max_level", "default_tax_percent", "default_price", "default_mrp", mode="before")
    @classmethod
    def _decimal_parse(cls, v: Any, info) -> Any:
        if v is None or v == "":
            return None
        d = _dec(v, Decimal("0"))
        if d < 0:
            raise ValueError(f"{info.field_name} must be >= 0")
        return d

    @field_validator("active_ingredients", mode="before")
    @classmethod
    def _ai_parse(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, list):
            out = [_s(x) for x in v if _s(x)]
            return out or None
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",")]
            parts = [p for p in parts if p]
            return parts or None
        raise ValueError("active_ingredients must be a list of strings or comma-separated text")



class ItemOut(ItemBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    id: int
    qty_on_hand: float = 0
    supplier: Optional[SupplierMini] = Field(
        default=None,
        validation_alias=AliasChoices("supplier", "default_supplier"),
    )

    created_at: datetime
    updated_at: datetime

    


# ---------- Batches & Stock ----------
# ---------- Billing / Dispense Batch Picker ----------

class PharmacyBatchPickOut(BaseModel):
    batch_id: int
    item_id: int

    code: str
    name: str
    generic_name: str | None = ""
    form: str | None = ""
    strength: str | None = ""
    unit: str | None = "unit"

    batch_no: str
    expiry_date: date | None

    available_qty: Quantity  # from ItemBatch.current_qty

    unit_cost: Money
    mrp: Money
    tax_percent: Percent

    location_id: int
    location_name: str | None = None


class ItemBatchOut(BaseModel):
    id: int
    item_id: int
    location_id: int
    batch_no: str
    expiry_date: date | None
    current_qty: Quantity
    unit_cost: Money
    mrp: Money
    tax_percent: Percent
    is_active: bool
    is_saleable: bool
    status: str
    created_at: datetime
    updated_at: datetime

    # For nice UI: you get brand, generic, location name etc.
    item: ItemOut
    location: LocationOut

    model_config = ConfigDict(from_attributes=True)


class StockSummaryOut(BaseModel):
    item_id: int
    code: str
    name: str
    location_id: int | None = None
    location_name: str | None = None
    total_qty: Quantity
    reorder_level: Quantity
    max_level: Quantity
    is_low: bool
    is_over: bool


# ---------- Purchase Orders ----------


class PurchaseOrderItemIn(BaseModel):
    item_id: int
    ordered_qty: Quantity = Field(..., gt=0)
    unit_cost: Money = 0
    tax_percent: Percent = 0
    mrp: Money = 0


class PurchaseOrderItemOut(BaseModel):
    id: int
    item_id: int
    ordered_qty: Quantity
    received_qty: Quantity
    unit_cost: Money
    tax_percent: Percent
    mrp: Money
    line_total: Money
    item: ItemOut

    model_config = ConfigDict(from_attributes=True)


class PurchaseOrderBase(BaseModel):
    supplier_id: int
    location_id: int
    order_date: date | None = None
    expected_date: date | None = None
    notes: str | None = ""


class PurchaseOrderCreate(PurchaseOrderBase):
    items: List[PurchaseOrderItemIn]


class PurchaseOrderUpdate(BaseModel):
    supplier_id: Optional[int] = None
    location_id: Optional[int] = None
    order_date: Optional[date] = None
    expected_date: Optional[date] = None
    notes: Optional[str] = None
    items: Optional[
        List[PurchaseOrderItemIn]] = None  # replace all items in DRAFT


class PurchaseOrderOut(BaseModel):
    id: int
    po_number: str
    supplier: SupplierOut
    location: LocationOut
    order_date: date
    expected_date: date | None
    status: str
    notes: str
    email_sent_to: str
    email_sent_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: List[PurchaseOrderItemOut]

    model_config = ConfigDict(from_attributes=True)


# ---------- GRN ----------
# GRNStatus = Literal["DRAFT", "POSTED", "CANCELLED"]

# ---------------------------
# GRN Items
# ---------------------------

class GRNItemIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    item_id: int
    po_item_id: Optional[int] = None

    batch_no: str = Field(..., min_length=1, max_length=100)
    expiry_date: Optional[date] = None

    quantity: Quantity = Field(..., gt=0)

    # ✅ supports frontend key "free" OR "free_quantity"
    free_quantity: Quantity = Field(default=Decimal("0.00"), ge=0, alias="free")

    unit_cost: Money = Field(default=Decimal("0.00"), ge=0)
    mrp: Money = Field(default=Decimal("0.00"), ge=0)

    discount_percent: Percent = Field(default=Decimal("0.00"), ge=0)
    discount_amount: Money = Field(default=Decimal("0.00"), ge=0)

    tax_percent: Percent = Field(default=Decimal("0.00"), ge=0)
    cgst_percent: Percent = Field(default=Decimal("0.00"), ge=0)
    sgst_percent: Percent = Field(default=Decimal("0.00"), ge=0)
    igst_percent: Percent = Field(default=Decimal("0.00"), ge=0)

    scheme: str = Field(default="", max_length=100)
    remarks: str = Field(default="", max_length=255)

    @field_validator("batch_no", "scheme", "remarks")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()


class GRNItemUpdate(BaseModel):
    # For patching an item (optional fields)
    batch_no: Optional[str] = Field(None, min_length=1, max_length=100)
    expiry_date: Optional[date] = None

    quantity: Optional["Quantity"] = Field(None, gt=0)
    free_quantity: Optional["Quantity"] = Field(None, ge=0)

    unit_cost: Optional["Money"] = Field(None, ge=0)
    mrp: Optional["Money"] = Field(None, ge=0)

    discount_percent: Optional["Percent"] = Field(None, ge=0)
    discount_amount: Optional["Money"] = Field(None, ge=0)

    tax_percent: Optional["Percent"] = Field(None, ge=0)
    cgst_percent: Optional["Percent"] = Field(None, ge=0)
    sgst_percent: Optional["Percent"] = Field(None, ge=0)
    igst_percent: Optional["Percent"] = Field(None, ge=0)

    scheme: Optional[str] = Field(None, max_length=100)
    remarks: Optional[str] = Field(None, max_length=255)


# ---------------------------
# GRN Header - Inputs
# ---------------------------
Money = Decimal  # or your custom type alias

class GRNBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    po_id: Optional[int] = None
    supplier_id: int
    location_id: int

    received_date: Optional[date] = None

    invoice_number: str = Field(default="", max_length=100)
    invoice_date: Optional[date] = None

    supplier_invoice_amount: Money = Field(default=Decimal("0.00"), ge=0)
    freight_amount: Money = Field(default=Decimal("0.00"), ge=0)
    other_charges: Money = Field(default=Decimal("0.00"), ge=0)

    # ✅ allow negative round_off
    round_off: Money = Field(default=Decimal("0.00"))

    difference_reason: str = Field(default="", max_length=255)
    notes: str = Field(default="", max_length=1000)

    @field_validator("invoice_number", "difference_reason", "notes")
    @classmethod
    def _trim_str(cls, v: str) -> str:
        return (v or "").strip()


class GRNCreate(GRNBase):
    items: List[GRNItemIn] = Field(default_factory=list, min_length=1)


class GRNUpdate(BaseModel):
    # patch header fields
    po_id: Optional[int] = None
    supplier_id: Optional[int] = None
    location_id: Optional[int] = None

    received_date: Optional[date] = None
    invoice_number: Optional[str] = Field(None, max_length=100)
    invoice_date: Optional[date] = None

    supplier_invoice_amount: Optional["Money"] = Field(None, ge=0)
    freight_amount: Optional["Money"] = Field(None, ge=0)
    other_charges: Optional["Money"] = Field(None, ge=0)
    round_off: Optional["Money"] = Field(None, ge=0)

    difference_reason: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = Field(None, max_length=1000)


class GRNPostIn(BaseModel):
    difference_reason: str = Field(default="", max_length=255)

    @field_validator("difference_reason")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()


class GRNCancelIn(BaseModel):
    cancel_reason: str = Field(..., min_length=3, max_length=255)


# ---------------------------
# Outputs
# ---------------------------

class GRNOutItem(BaseModel):
    id: int
    item: "ItemOut"
    batch_no: str
    expiry_date: Optional[date]

    quantity: "Quantity"
    free_quantity: "Quantity"

    unit_cost: "Money"
    mrp: "Money"

    discount_percent: "Percent"
    discount_amount: "Money"

    tax_percent: "Percent"
    cgst_percent: "Percent"
    sgst_percent: "Percent"
    igst_percent: "Percent"

    taxable_amount: "Money"
    cgst_amount: "Money"
    sgst_amount: "Money"
    igst_amount: "Money"

    line_total: "Money"

    scheme: str
    remarks: str

    batch_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class GRNOut(BaseModel):
    id: int
    grn_number: str
    status: GRNStatus

    purchase_order: Optional["PurchaseOrderOut"] = None
    supplier: "SupplierOut"
    location: "LocationOut"

    received_date: date
    invoice_number: str
    invoice_date: Optional[date]

    # ✅ invoice header totals
    supplier_invoice_amount: "Money"
    taxable_amount: "Money"
    discount_amount: "Money"

    cgst_amount: "Money"
    sgst_amount: "Money"
    igst_amount: "Money"

    freight_amount: "Money"
    other_charges: "Money"
    round_off: "Money"

    calculated_grn_amount: "Money"
    amount_difference: "Money"
    difference_reason: str

    # ✅ audit
    created_by_id: Optional[int] = None
    posted_by_id: Optional[int] = None
    posted_at: Optional[datetime] = None
    cancelled_by_id: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: str

    notes: str
    created_at: datetime
    updated_at: datetime

    items: List[GRNOutItem]

    model_config = ConfigDict(from_attributes=True)


# ---------- Returns ----------


class ReturnItemIn(BaseModel):
    item_id: int
    batch_id: int | None = None
    batch_no: Optional[str] = None
    quantity: Quantity = Field(..., gt=0)
    reason: str | None = ""


class ReturnBase(BaseModel):
    type: str  # TO_SUPPLIER / FROM_CUSTOMER / INTERNAL
    supplier_id: int | None = None
    location_id: int
    return_date: date | None = None
    reason: str | None = ""


class ReturnCreate(ReturnBase):
    items: List[ReturnItemIn]


class ReturnItemOut(BaseModel):
    id: int
    item: ItemOut
    batch: ItemBatchOut | None
    quantity: Quantity
    reason: str

    model_config = ConfigDict(from_attributes=True)


class ReturnOut(BaseModel):
    id: int
    return_number: str
    type: str
    supplier: SupplierOut | None
    location: LocationOut
    return_date: date
    status: str
    reason: str
    created_at: datetime
    updated_at: datetime
    items: List[ReturnItemOut]

    model_config = ConfigDict(from_attributes=True)


# ---------- Transactions ----------


class StockTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    txn_time: Optional[datetime] = None

    location_id: Optional[int] = None
    item_id: Optional[int] = None
    batch_id: Optional[int] = None

    quantity_change: Optional[float] = None
    txn_type: Optional[str] = None

    ref_type: Optional[str] = None
    ref_id: Optional[int] = None

    unit_cost: Optional[float] = None
    mrp: Optional[float] = None

    patient_id: Optional[int] = None
    visit_id: Optional[int] = None

    user_id: Optional[int] = None
    doctor_id: Optional[int] = None

    item_name: Optional[str] = None
    item_code: Optional[str] = None
    batch_no: Optional[str] = None
    location_name: Optional[str] = None

    user_name: Optional[str] = None
    doctor_name: Optional[str] = None

    ref_display: Optional[str] = None



# ---------- Dispense (stock OUT) ----------


class DispenseLineIn(BaseModel):
    item_id: int
    batch_id: int | None = None
    quantity: Quantity = Field(..., gt=0)


class DispenseRequestIn(BaseModel):
    location_id: int
    patient_id: int | None = None
    visit_id: int | None = None
    remark: str | None = ""
    lines: List[DispenseLineIn]
