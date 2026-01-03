# FILE: app/schemas/pharmacy_inventory.py
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Literal
import re
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict, condecimal, field_validator, EmailStr, model_validator
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
    id: int
    name: str


class ItemBase(BaseModel):
    code: str
    name: str

    item_type: str = Field(default="DRUG")  # DRUG | CONSUMABLE | EQUIPMENT
    lasa_flag: bool = False
    is_consumable: bool = False  # kept for compatibility

    unit: str = "unit"
    pack_size: str = "1"
    reorder_level: float = 0
    max_level: float = 0

    manufacturer: Optional[str] = ""
    default_supplier_id: Optional[int] = None
    procurement_date: Optional[date] = None

    storage_condition: str = "ROOM_TEMP"

    default_tax_percent: float = 0
    default_price: float = 0
    default_mrp: float = 0

    # drug
    generic_name: Optional[str] = ""
    brand_name: Optional[str] = ""
    dosage_form: Optional[str] = ""
    strength: Optional[str] = ""
    active_ingredients: Optional[List[str]] = None
    route: Optional[str] = ""
    therapeutic_class: Optional[str] = ""

    # ✅ main status (your existing)
    # OTC | RX | SCHEDULED
    prescription_status: str = "RX"

    # ✅ NEW: schedule code for scheduled drugs (H/H1/X)
    # You can also send RX/OTC, we normalize it.
    schedule_code: Optional[str] = Field(default=None, description="H/H1/X or RX/OTC")

    side_effects: Optional[str] = ""
    drug_interactions: Optional[str] = ""

    # consumable
    material_type: Optional[str] = ""
    sterility_status: Optional[str] = ""
    size_dimensions: Optional[str] = ""
    intended_use: Optional[str] = ""
    reusable_status: Optional[str] = ""

    atc_code: Optional[str] = ""
    hsn_code: Optional[str] = ""
    qr_number: Optional[str] = None
    is_active: bool = True

    @field_validator("schedule_code", mode="before")
    @classmethod
    def _schedule_norm(cls, v):
        return _norm_schedule(v)

    @field_validator("prescription_status", mode="before")
    @classmethod
    def _ps_norm(cls, v):
        s = str(v or "").strip().upper()
        return s or "RX"

    @model_validator(mode="after")
    def _sync_schedule_and_status(self):
        """
        Rules:
        - If schedule_code is H/H1/X -> prescription_status becomes SCHEDULED
        - If schedule_code is RX/OTC -> prescription_status becomes RX/OTC and schedule_code cleared (optional)
        - If prescription_status is SCHEDULED and schedule_code is empty -> raise (force schedule)
        """
        sc = (self.schedule_code or "").strip().upper() if self.schedule_code else None
        ps = (self.prescription_status or "RX").strip().upper()

        if sc in ("RX", "OTC"):
            self.prescription_status = sc
            # if you want schedule column blank for RX/OTC, uncomment next line:
            # self.schedule_code = None
            return self

        if sc:  # H/H1/X etc
            self.prescription_status = "SCHEDULED"
            return self

        # if status says scheduled, schedule_code must be provided
        if ps in ("SCHEDULED", "SCHEDULE"):
            raise ValueError("schedule_code is required when prescription_status is SCHEDULED")
        return self


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    # all optional
    code: Optional[str] = None
    name: Optional[str] = None
    item_type: Optional[str] = None
    lasa_flag: Optional[bool] = None
    is_consumable: Optional[bool] = None

    unit: Optional[str] = None
    pack_size: Optional[str] = None
    reorder_level: Optional[float] = None
    max_level: Optional[float] = None

    manufacturer: Optional[str] = None
    default_supplier_id: Optional[int] = None
    procurement_date: Optional[date] = None

    storage_condition: Optional[str] = None

    default_tax_percent: Optional[float] = None
    default_price: Optional[float] = None
    default_mrp: Optional[float] = None

    generic_name: Optional[str] = None
    brand_name: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    active_ingredients: Optional[List[str]] = None
    route: Optional[str] = None
    therapeutic_class: Optional[str] = None

    prescription_status: Optional[str] = None

    # ✅ NEW (update)
    schedule_code: Optional[str] = None

    side_effects: Optional[str] = None
    drug_interactions: Optional[str] = None

    material_type: Optional[str] = None
    sterility_status: Optional[str] = None
    size_dimensions: Optional[str] = None
    intended_use: Optional[str] = None
    reusable_status: Optional[str] = None

    atc_code: Optional[str] = None
    hsn_code: Optional[str] = None
    qr_number: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("schedule_code", mode="before")
    @classmethod
    def _schedule_norm_u(cls, v):
        return _norm_schedule(v)

    @field_validator("prescription_status", mode="before")
    @classmethod
    def _ps_norm_u(cls, v):
        if v is None:
            return None
        s = str(v or "").strip().upper()
        return s or None


class ItemOut(ItemBase):
    id: int
    qty_on_hand: float = 0
    supplier: Optional[SupplierMini] = None

    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


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
