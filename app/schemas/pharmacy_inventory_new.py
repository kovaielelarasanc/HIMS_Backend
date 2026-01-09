# FILE: app/schemas/pharmacy_inventory_new.py
# Pydantic v2 schemas for FULL Pharmacy Inventory workflows:
# PO -> GRN -> Purchase Invoice -> Batch/Expiry -> Ledger/Balance
# + Adjustments + Transfers + Stock Counts + Dispense (OP/IP/ER/OT) + Insurance split + Audit events
#
# NOTE:
# - IDs like patient_id/encounter_id/admission_id/prescription_id are optional and depend on your EMR tables.
# - Totals/cost fields are INCLUDED as schema fields, but should be computed on server during POST/POSTING.
# - Use "status" transitions via dedicated payloads: Submit/Approve/Post/Cancel/Issue/Receive/Freeze/etc.

from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# -------------------------
# Common Types / Constraints
# -------------------------
MONEY = Decimal
QTY = Decimal


def _d(v: Any) -> Decimal:
    if v is None:
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def _nonneg(v: Any) -> Decimal:
    dv = _d(v)
    if dv < 0:
        raise ValueError("must be >= 0")
    return dv


def _pos(v: Any) -> Decimal:
    dv = _d(v)
    if dv <= 0:
        raise ValueError("must be > 0")
    return dv


def _pct(v: Any) -> Decimal:
    dv = _d(v)
    if dv < 0 or dv > 100:
        raise ValueError("percent must be between 0 and 100")
    return dv


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# -------------------------
# Enums (match DB)
# -------------------------
class DocStatus(str, Enum):
    draft = "draft"
    submitted = "submitted"
    approved = "approved"
    posted = "posted"
    closed = "closed"
    cancelled = "cancelled"


class LedgerMove(str, Enum):
    IN = "IN"
    OUT = "OUT"


class LedgerReason(str, Enum):
    PURCHASE_GRN = "PURCHASE_GRN"
    PURCHASE_INVOICE_ADJ = "PURCHASE_INVOICE_ADJ"
    PURCHASE_RETURN_OUT = "PURCHASE_RETURN_OUT"
    PURCHASE_RETURN_IN = "PURCHASE_RETURN_IN"
    DISPENSE_OP = "DISPENSE_OP"
    DISPENSE_IP = "DISPENSE_IP"
    DISPENSE_ER = "DISPENSE_ER"
    DISPENSE_OT = "DISPENSE_OT"
    DISPENSE_RETURN_IN = "DISPENSE_RETURN_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    TRANSFER_IN = "TRANSFER_IN"
    ADJUSTMENT_PLUS = "ADJUSTMENT_PLUS"
    ADJUSTMENT_MINUS = "ADJUSTMENT_MINUS"
    WRITE_OFF_EXPIRED = "WRITE_OFF_EXPIRED"
    WRITE_OFF_DAMAGED = "WRITE_OFF_DAMAGED"
    STOCK_COUNT_ADJ = "STOCK_COUNT_ADJ"


class DispenseType(str, Enum):
    OP = "OP"
    IP = "IP"
    ER = "ER"
    OT = "OT"
    MANUAL = "MANUAL"


class PricingBasis(str, Enum):
    MRP = "MRP"
    CONTRACT = "CONTRACT"
    HOSPITAL_PRICE = "HOSPITAL_PRICE"


class CoverageStatus(str, Enum):
    COVERED = "COVERED"
    NON_COVERED = "NON_COVERED"
    REQUIRES_PREAUTH = "REQUIRES_PREAUTH"
    CAP_APPLIES = "CAP_APPLIES"


# ============================================================
# 0) COMMON WORKFLOW PAYLOADS
# ============================================================
class SubmitPayload(BaseModel):
    note: Optional[str] = Field(default=None, max_length=255)


class ApprovePayload(BaseModel):
    note: Optional[str] = Field(default=None, max_length=255)


class PostPayload(BaseModel):
    note: Optional[str] = Field(default=None, max_length=255)


class CancelPayload(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)
    note: Optional[str] = Field(default=None, max_length=255)


# ============================================================
# 1) MASTERS (minimal operational schemas; expand if needed)
# ============================================================
class UomOut(ORMModel):
    id: int
    code: str
    name: str
    is_base_uom: bool = False


class StoreOut(ORMModel):
    id: int
    name: str
    code: Optional[str] = None
    is_main: bool = False
    is_dispense_point: bool = True
    is_receiving_point: bool = True
    is_active: bool = True


class SupplierOut(ORMModel):
    id: int
    name: str
    code: Optional[str] = None
    gstin: Optional[str] = None
    dl_no: Optional[str] = None
    credit_days: int = 0
    is_active: bool = True


class ItemOut(ORMModel):
    id: int
    sku: Optional[str] = None
    name: str
    generic_name: Optional[str] = None
    brand_name: Optional[str] = None

    is_medicine: bool = True
    is_consumable: bool = False
    is_device: bool = False
    is_controlled: bool = False
    prescription_required: bool = True

    storage_type: str = "room_temp"
    temp_min_c: Optional[Decimal] = None
    temp_max_c: Optional[Decimal] = None

    base_uom_id: int
    purchase_uom_id: Optional[int] = None
    sale_uom_id: Optional[int] = None

    min_stock_base: Decimal = Decimal("0")
    max_stock_base: Decimal = Decimal("0")
    reorder_point_base: Decimal = Decimal("0")
    reorder_qty_base: Decimal = Decimal("0")

    allow_substitution: bool = True
    return_allowed: bool = True
    return_window_days: Optional[int] = None

    barcode: Optional[str] = None
    alt_barcodes: Optional[List[str]] = None

    default_mrp: Optional[Decimal] = None
    default_sale_price: Optional[Decimal] = None

    is_active: bool = True
    meta: Optional[Dict[str, Any]] = None


class ItemStoreSettingOut(ORMModel):
    id: int
    store_id: int
    item_id: int

    min_stock_base: Decimal = Decimal("0")
    max_stock_base: Decimal = Decimal("0")
    reorder_point_base: Decimal = Decimal("0")
    reorder_qty_base: Decimal = Decimal("0")

    expiry_warn_days: int = 90
    expiry_block_days: int = 0
    allow_negative_stock: bool = False
    meta: Optional[Dict[str, Any]] = None


# ============================================================
# 2) INSURANCE / CONTRACT (operational evaluation)
# ============================================================
class PayerOut(ORMModel):
    id: int
    name: str
    code: Optional[str] = None
    payer_type: str = "TPA"
    is_active: bool = True


class PlanOut(ORMModel):
    id: int
    payer_id: int
    name: str
    code: Optional[str] = None
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    is_active: bool = True


class CoverageRuleOut(ORMModel):
    id: int
    payer_id: int
    plan_id: Optional[int] = None
    pricing_basis: PricingBasis = PricingBasis.CONTRACT
    copay_percent: Decimal = Decimal("0")
    max_cap_amount: Optional[Decimal] = None
    prior_auth_required_default: bool = False
    enforce_generic: bool = False
    allow_brand_override: bool = True
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    is_active: bool = True
    meta: Optional[Dict[str, Any]] = None


class ContractPriceOut(ORMModel):
    id: int
    payer_id: int
    plan_id: Optional[int] = None
    item_id: int
    coverage_status: CoverageStatus = CoverageStatus.COVERED
    prior_auth_required: bool = False

    allowed_rate_per_base: Optional[Decimal] = None
    allowed_rate_per_sale_uom: Optional[Decimal] = None
    currency: str = "INR"

    max_qty_per_day_base: Optional[Decimal] = None
    max_qty_per_encounter_base: Optional[Decimal] = None
    max_amount_per_encounter: Optional[Decimal] = None

    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    is_active: bool = True
    meta: Optional[Dict[str, Any]] = None


# ============================================================
# 3) PROCUREMENT - PO (Purchase Order)
# ============================================================
class PoLineIn(BaseModel):
    line_no: int = Field(..., ge=1)
    item_id: int
    purchase_uom_id: Optional[int] = None

    ordered_qty: Decimal = Field(...)
    rate: Decimal = Field(default=Decimal("0"))

    discount_percent: Decimal = Field(default=Decimal("0"))
    discount_amount: Decimal = Field(default=Decimal("0"))

    tax_percent: Decimal = Field(default=Decimal("0"))
    tax_amount: Decimal = Field(default=Decimal("0"))

    free_qty: Decimal = Field(default=Decimal("0"))
    mrp: Optional[Decimal] = None

    line_total: Decimal = Field(default=Decimal("0"))
    remark: Optional[str] = Field(default=None, max_length=255)
    meta: Optional[Dict[str, Any]] = None

    @field_validator("ordered_qty")
    @classmethod
    def _v_qty(cls, v):
        return _pos(v)

    @field_validator("rate", "discount_amount", "tax_amount", "line_total",
                     "free_qty")
    @classmethod
    def _v_nonneg(cls, v):
        return _nonneg(v)

    @field_validator("discount_percent", "tax_percent")
    @classmethod
    def _v_pct(cls, v):
        return _pct(v)


class PoCreate(BaseModel):
    po_no: str = Field(..., min_length=1, max_length=40)
    supplier_id: int
    store_id: int

    po_date: Optional[datetime] = None
    expected_date: Optional[datetime] = None

    currency: str = Field(default="INR", max_length=8)
    notes: Optional[str] = None
    terms: Optional[str] = None

    shipping_charges: Decimal = Decimal("0")
    other_charges: Decimal = Decimal("0")
    round_off: Decimal = Decimal("0")

    lines: List[PoLineIn]

    meta: Optional[Dict[str, Any]] = None

    @field_validator("shipping_charges", "other_charges", "round_off")
    @classmethod
    def _v_nonneg(cls, v):
        return _nonneg(v)


class PoUpdate(BaseModel):
    supplier_id: Optional[int] = None
    store_id: Optional[int] = None
    expected_date: Optional[datetime] = None

    currency: Optional[str] = Field(default=None, max_length=8)
    notes: Optional[str] = None
    terms: Optional[str] = None

    shipping_charges: Optional[Decimal] = None
    other_charges: Optional[Decimal] = None
    round_off: Optional[Decimal] = None

    # full replacement of lines OR manage via dedicated endpoints in your router
    lines: Optional[List[PoLineIn]] = None
    meta: Optional[Dict[str, Any]] = None

    @field_validator("shipping_charges", "other_charges", "round_off")
    @classmethod
    def _v_nonneg(cls, v):
        if v is None: return v
        return _nonneg(v)


class PoLineOut(ORMModel):
    id: int
    po_id: int
    line_no: int
    item_id: int
    purchase_uom_id: Optional[int] = None

    ordered_qty: Decimal
    rate: Decimal

    discount_percent: Decimal
    discount_amount: Decimal

    tax_percent: Decimal
    tax_amount: Decimal

    free_qty: Decimal
    mrp: Optional[Decimal] = None
    line_total: Decimal

    remark: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class PoOut(ORMModel):
    id: int
    po_no: str
    status: DocStatus

    supplier_id: int
    store_id: int
    po_date: datetime
    expected_date: Optional[datetime] = None

    currency: str
    notes: Optional[str] = None
    terms: Optional[str] = None

    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    shipping_charges: Decimal
    other_charges: Decimal
    round_off: Decimal
    grand_total: Decimal

    revision_no: int = 0

    created_at: datetime
    updated_at: datetime

    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    cancelled_by: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None
    lines: List[PoLineOut] = []


# ============================================================
# 4) PROCUREMENT - GRN (Goods Receipt)
# ============================================================
class GrnLineIn(BaseModel):
    line_no: int = Field(..., ge=1)
    po_line_id: Optional[int] = None

    item_id: int
    purchase_uom_id: Optional[int] = None

    received_qty: Decimal
    free_qty: Decimal = Decimal("0")

    batch_no: str = Field(..., min_length=1, max_length=80)
    mfg_date: Optional[date] = None
    expiry_date: date

    mrp: Optional[Decimal] = None
    purchase_rate: Decimal

    tax_percent: Decimal = Decimal("0")
    discount_percent: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")

    landed_cost_total: Decimal = Decimal("0")
    landed_cost_per_base: Optional[Decimal] = None

    batch_barcode: Optional[str] = Field(default=None, max_length=120)

    is_damaged: bool = False
    damage_note: Optional[str] = Field(default=None, max_length=255)
    is_quarantined: bool = False
    quarantine_reason: Optional[str] = Field(default=None, max_length=255)

    remark: Optional[str] = Field(default=None, max_length=255)
    meta: Optional[Dict[str, Any]] = None

    @field_validator("received_qty")
    @classmethod
    def _v_qty(cls, v):
        return _pos(v)

    @field_validator("free_qty", "purchase_rate", "discount_amount",
                     "tax_amount", "landed_cost_total")
    @classmethod
    def _v_nonneg(cls, v):
        return _nonneg(v)

    @field_validator("discount_percent", "tax_percent")
    @classmethod
    def _v_pct(cls, v):
        return _pct(v)

    @model_validator(mode="after")
    def _v_dates(self):
        if self.mfg_date and self.expiry_date and self.expiry_date <= self.mfg_date:
            raise ValueError("expiry_date must be after mfg_date")
        return self


class GrnCreate(BaseModel):
    grn_no: str = Field(..., min_length=1, max_length=40)
    store_id: int
    supplier_id: int

    po_id: Optional[int] = None
    received_at: Optional[datetime] = None

    supplier_delivery_note: Optional[str] = Field(default=None, max_length=80)
    vehicle_no: Optional[str] = Field(default=None, max_length=40)
    received_by_name: Optional[str] = Field(default=None, max_length=120)

    notes: Optional[str] = None

    shipping_charges: Decimal = Decimal("0")
    other_charges: Decimal = Decimal("0")
    round_off: Decimal = Decimal("0")

    lines: List[GrnLineIn]
    meta: Optional[Dict[str, Any]] = None

    @field_validator("shipping_charges", "other_charges", "round_off")
    @classmethod
    def _v_nonneg(cls, v):
        return _nonneg(v)


class GrnUpdate(BaseModel):
    store_id: Optional[int] = None
    supplier_id: Optional[int] = None
    po_id: Optional[int] = None

    received_at: Optional[datetime] = None

    supplier_delivery_note: Optional[str] = Field(default=None, max_length=80)
    vehicle_no: Optional[str] = Field(default=None, max_length=40)
    received_by_name: Optional[str] = Field(default=None, max_length=120)
    notes: Optional[str] = None

    shipping_charges: Optional[Decimal] = None
    other_charges: Optional[Decimal] = None
    round_off: Optional[Decimal] = None

    lines: Optional[List[GrnLineIn]] = None
    meta: Optional[Dict[str, Any]] = None

    @field_validator("shipping_charges", "other_charges", "round_off")
    @classmethod
    def _v_nonneg(cls, v):
        if v is None: return v
        return _nonneg(v)


class GrnLineOut(ORMModel):
    id: int
    grn_id: int
    line_no: int
    po_line_id: Optional[int] = None

    item_id: int
    purchase_uom_id: Optional[int] = None

    received_qty: Decimal
    free_qty: Decimal

    batch_no: str
    mfg_date: Optional[date] = None
    expiry_date: date

    mrp: Optional[Decimal] = None
    purchase_rate: Decimal

    tax_percent: Decimal
    discount_percent: Decimal
    discount_amount: Decimal
    tax_amount: Decimal

    landed_cost_total: Decimal
    landed_cost_per_base: Optional[Decimal] = None

    batch_barcode: Optional[str] = None

    is_damaged: bool
    damage_note: Optional[str] = None
    is_quarantined: bool
    quarantine_reason: Optional[str] = None

    remark: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class GrnOut(ORMModel):
    id: int
    grn_no: str
    status: DocStatus

    store_id: int
    supplier_id: int
    po_id: Optional[int] = None

    received_at: datetime
    supplier_delivery_note: Optional[str] = None
    vehicle_no: Optional[str] = None
    received_by_name: Optional[str] = None

    notes: Optional[str] = None

    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    shipping_charges: Decimal
    other_charges: Decimal
    round_off: Decimal
    grand_total: Decimal

    posted_at: Optional[datetime] = None
    posted_by: Optional[int] = None

    created_at: datetime
    updated_at: datetime

    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    cancelled_by: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None
    lines: List[GrnLineOut] = []


# ============================================================
# 5) PROCUREMENT - Purchase Invoice (Supplier Bill)
# ============================================================
class PurchaseInvoiceLineIn(BaseModel):
    line_no: int = Field(..., ge=1)
    grn_line_id: Optional[int] = None

    item_id: int
    purchase_uom_id: Optional[int] = None

    billed_qty: Decimal = Decimal("0")
    rate: Decimal = Decimal("0")

    discount_percent: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    tax_percent: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")

    line_total: Decimal = Decimal("0")

    batch_no: Optional[str] = Field(default=None, max_length=80)
    expiry_date: Optional[date] = None
    mrp: Optional[Decimal] = None

    remark: Optional[str] = Field(default=None, max_length=255)
    meta: Optional[Dict[str, Any]] = None

    @field_validator("billed_qty", "rate", "discount_amount", "tax_amount",
                     "line_total")
    @classmethod
    def _v_nonneg(cls, v):
        return _nonneg(v)

    @field_validator("discount_percent", "tax_percent")
    @classmethod
    def _v_pct(cls, v):
        return _pct(v)


class PurchaseInvoiceCreate(BaseModel):
    invoice_no: str = Field(..., min_length=1, max_length=60)
    invoice_date: date

    supplier_id: int
    store_id: int
    grn_id: Optional[int] = None

    currency: str = Field(default="INR", max_length=8)
    notes: Optional[str] = None

    shipping_charges: Decimal = Decimal("0")
    other_charges: Decimal = Decimal("0")
    round_off: Decimal = Decimal("0")

    due_date: Optional[date] = None
    payment_status: str = Field(default="unpaid",
                                max_length=30)  # unpaid/partial/paid
    paid_amount: Decimal = Decimal("0")

    lines: List[PurchaseInvoiceLineIn]
    meta: Optional[Dict[str, Any]] = None

    @field_validator("shipping_charges", "other_charges", "round_off",
                     "paid_amount")
    @classmethod
    def _v_nonneg(cls, v):
        return _nonneg(v)


class PurchaseInvoiceUpdate(BaseModel):
    invoice_date: Optional[date] = None
    supplier_id: Optional[int] = None
    store_id: Optional[int] = None
    grn_id: Optional[int] = None

    currency: Optional[str] = Field(default=None, max_length=8)
    notes: Optional[str] = None

    shipping_charges: Optional[Decimal] = None
    other_charges: Optional[Decimal] = None
    round_off: Optional[Decimal] = None

    due_date: Optional[date] = None
    payment_status: Optional[str] = Field(default=None, max_length=30)
    paid_amount: Optional[Decimal] = None

    lines: Optional[List[PurchaseInvoiceLineIn]] = None
    meta: Optional[Dict[str, Any]] = None

    @field_validator("shipping_charges", "other_charges", "round_off",
                     "paid_amount")
    @classmethod
    def _v_nonneg(cls, v):
        if v is None: return v
        return _nonneg(v)


class PurchaseInvoiceLineOut(ORMModel):
    id: int
    invoice_id: int
    line_no: int
    grn_line_id: Optional[int] = None

    item_id: int
    purchase_uom_id: Optional[int] = None

    billed_qty: Decimal
    rate: Decimal

    discount_percent: Decimal
    discount_amount: Decimal
    tax_percent: Decimal
    tax_amount: Decimal

    line_total: Decimal

    batch_no: Optional[str] = None
    expiry_date: Optional[date] = None
    mrp: Optional[Decimal] = None

    remark: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class PurchaseInvoiceOut(ORMModel):
    id: int
    invoice_no: str
    invoice_date: date
    status: DocStatus

    supplier_id: int
    store_id: int
    grn_id: Optional[int] = None

    currency: str
    notes: Optional[str] = None

    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    shipping_charges: Decimal
    other_charges: Decimal
    round_off: Decimal
    grand_total: Decimal

    payment_status: str
    due_date: Optional[date] = None
    paid_amount: Decimal

    posted_at: Optional[datetime] = None
    posted_by: Optional[int] = None

    created_at: datetime
    updated_at: datetime

    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    cancelled_by: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None
    lines: List[PurchaseInvoiceLineOut] = []


# ============================================================
# 6) BATCH / STOCK BALANCE / LEDGER (operational read schemas)
# ============================================================
class BatchOut(ORMModel):
    id: int
    item_id: int
    batch_no: str
    mfg_date: Optional[date] = None
    expiry_date: date
    mrp: Optional[Decimal] = None
    batch_barcode: Optional[str] = None

    supplier_id: Optional[int] = None
    first_grn_line_id: Optional[int] = None

    landed_cost_per_base: Optional[Decimal] = None
    last_purchase_rate: Optional[Decimal] = None
    currency: str = "INR"

    is_recalled: bool = False
    recall_note: Optional[str] = None
    is_quarantined: bool = False
    quarantine_note: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None


class StockBalanceOut(ORMModel):
    id: int
    store_id: int
    batch_id: int
    item_id: int

    on_hand_base: Decimal
    reserved_base: Decimal
    available_base: Decimal

    last_movement_at: Optional[datetime] = None
    last_movement_reason: Optional[str] = None

    created_at: datetime
    updated_at: datetime


class LedgerOut(ORMModel):
    id: int
    moved_at: datetime
    move: LedgerMove
    reason: LedgerReason

    store_id: int
    item_id: int
    batch_id: Optional[int] = None

    qty_base: Decimal
    uom_id: Optional[int] = None
    qty_uom: Optional[Decimal] = None

    unit_cost_per_base: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None

    source_doc_type: str
    source_doc_id: int
    source_line_id: Optional[int] = None

    actor_user_id: Optional[int] = None
    actor_user_name: Optional[str] = None
    note: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


# ============================================================
# 7) STOCK OPERATIONS - Adjustments
# ============================================================
class StockAdjustmentLineIn(BaseModel):
    line_no: int = Field(..., ge=1)
    item_id: int
    batch_id: Optional[int] = None

    qty_delta_base: Decimal  # + / -
    unit_cost_per_base: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None

    remark: Optional[str] = Field(default=None, max_length=255)
    meta: Optional[Dict[str, Any]] = None

    @field_validator("qty_delta_base")
    @classmethod
    def _v_nonzero(cls, v):
        dv = _d(v)
        if dv == 0:
            raise ValueError("qty_delta_base must be non-zero")
        return dv

    @field_validator("unit_cost_per_base", "total_cost")
    @classmethod
    def _v_nonneg_opt(cls, v):
        if v is None: return v
        return _nonneg(v)


class StockAdjustmentCreate(BaseModel):
    adj_no: str = Field(..., min_length=1, max_length=40)
    store_id: int
    adj_at: Optional[datetime] = None

    reason_code: str = Field(
        ..., min_length=2,
        max_length=60)  # DAMAGE/EXPIRED/MISSING/FOUND/TEMP_BREACH/OTHER
    reason_note: Optional[str] = None

    lines: List[StockAdjustmentLineIn]
    meta: Optional[Dict[str, Any]] = None


class StockAdjustmentOut(ORMModel):
    id: int
    adj_no: str
    status: DocStatus
    store_id: int
    adj_at: datetime
    reason_code: str
    reason_note: Optional[str] = None

    posted_at: Optional[datetime] = None
    posted_by: Optional[int] = None

    created_at: datetime
    updated_at: datetime

    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    cancelled_by: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None
    lines: List[StockAdjustmentLineIn] = [
    ]  # or create StockAdjustmentLineOut if you prefer


# ============================================================
# 8) STOCK OPERATIONS - Transfers (two-stage Issue/Receive)
# ============================================================
class StockTransferLineIn(BaseModel):
    line_no: int = Field(..., ge=1)
    item_id: int
    batch_id: Optional[int] = None

    qty_base: Decimal
    unit_cost_per_base: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None

    remark: Optional[str] = Field(default=None, max_length=255)
    meta: Optional[Dict[str, Any]] = None

    @field_validator("qty_base")
    @classmethod
    def _v_qty(cls, v):
        return _pos(v)

    @field_validator("unit_cost_per_base", "total_cost")
    @classmethod
    def _v_nonneg_opt(cls, v):
        if v is None: return v
        return _nonneg(v)


class StockTransferCreate(BaseModel):
    transfer_no: str = Field(..., min_length=1, max_length=40)
    from_store_id: int
    to_store_id: int
    requested_at: Optional[datetime] = None

    courier_ref: Optional[str] = Field(default=None, max_length=80)
    note: Optional[str] = None

    lines: List[StockTransferLineIn]
    meta: Optional[Dict[str, Any]] = None


class TransferIssuePayload(BaseModel):
    issued_at: Optional[datetime] = None
    note: Optional[str] = Field(default=None, max_length=255)


class TransferReceivePayload(BaseModel):
    received_at: Optional[datetime] = None
    note: Optional[str] = Field(default=None, max_length=255)


class StockTransferOut(ORMModel):
    id: int
    transfer_no: str
    status: DocStatus

    from_store_id: int
    to_store_id: int

    requested_at: datetime
    issued_at: Optional[datetime] = None
    received_at: Optional[datetime] = None

    issued_by: Optional[int] = None
    received_by: Optional[int] = None

    courier_ref: Optional[str] = None
    note: Optional[str] = None

    created_at: datetime
    updated_at: datetime

    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    cancelled_by: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None
    lines: List[StockTransferLineIn] = []  # or StockTransferLineOut


# ============================================================
# 9) STOCK OPERATIONS - Stock Count (Cycle/Annual) + Freeze + Post variance
# ============================================================
class StockCountLineIn(BaseModel):
    line_no: int = Field(..., ge=1)
    item_id: int
    batch_id: Optional[int] = None

    system_qty_base: Decimal = Decimal("0")
    counted_qty_base: Decimal = Decimal("0")
    variance_qty_base: Decimal = Decimal("0")

    unit_cost_per_base: Optional[Decimal] = None
    variance_cost: Optional[Decimal] = None

    remark: Optional[str] = Field(default=None, max_length=255)
    meta: Optional[Dict[str, Any]] = None

    @field_validator("system_qty_base", "counted_qty_base",
                     "variance_qty_base")
    @classmethod
    def _v_nonneg(cls, v):
        return _nonneg(v)

    @field_validator("unit_cost_per_base", "variance_cost")
    @classmethod
    def _v_nonneg_opt(cls, v):
        if v is None: return v
        return _nonneg(v)


class StockCountCreate(BaseModel):
    count_no: str = Field(..., min_length=1, max_length=40)
    store_id: int
    count_at: Optional[datetime] = None
    count_type: str = Field(default="cycle",
                            max_length=30)  # cycle/annual/spot
    freeze_stock: bool = False
    note: Optional[str] = None

    lines: List[StockCountLineIn]
    meta: Optional[Dict[str, Any]] = None


class FreezePayload(BaseModel):
    freeze: bool = True
    note: Optional[str] = Field(default=None, max_length=255)


class StockCountOut(ORMModel):
    id: int
    count_no: str
    status: DocStatus
    store_id: int
    count_at: datetime
    count_type: str
    freeze_stock: bool

    note: Optional[str] = None

    posted_at: Optional[datetime] = None
    posted_by: Optional[int] = None

    created_at: datetime
    updated_at: datetime

    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    cancelled_by: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None
    lines: List[StockCountLineIn] = []  # or StockCountLineOut


# ============================================================
# 10) DISPENSE / ISSUE + INSURANCE SPLIT + RETURNS
# ============================================================
class DispenseLineIn(BaseModel):
    line_no: int = Field(..., ge=1)
    item_id: int
    batch_id: Optional[int] = None

    base_uom_id: Optional[int] = None
    qty_base: Decimal

    sale_uom_id: Optional[int] = None
    qty_sale_uom: Optional[Decimal] = None

    mrp: Optional[Decimal] = None
    selling_rate: Decimal = Decimal(
        "0")  # per sale_uom or per base (your standard)
    discount_percent: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    tax_percent: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")
    line_total: Decimal = Decimal("0")

    unit_cost_per_base: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None

    allowed_amount: Decimal = Decimal("0")
    payer_amount: Decimal = Decimal("0")
    patient_amount: Decimal = Decimal("0")
    nonpayable_amount: Decimal = Decimal("0")
    denial_reason: Optional[str] = Field(default=None, max_length=255)
    prior_auth_ref: Optional[str] = Field(default=None, max_length=80)

    is_returned: bool = False
    returned_qty_base: Decimal = Decimal("0")
    return_reason: Optional[str] = Field(default=None, max_length=255)

    remark: Optional[str] = Field(default=None, max_length=255)
    meta: Optional[Dict[str, Any]] = None

    @field_validator("qty_base")
    @classmethod
    def _v_qty(cls, v):
        return _pos(v)

    @field_validator(
        "selling_rate",
        "discount_amount",
        "tax_amount",
        "line_total",
        "allowed_amount",
        "payer_amount",
        "patient_amount",
        "nonpayable_amount",
        "returned_qty_base",
    )
    @classmethod
    def _v_nonneg(cls, v):
        return _nonneg(v)

    @field_validator("discount_percent", "tax_percent")
    @classmethod
    def _v_pct(cls, v):
        return _pct(v)

    @model_validator(mode="after")
    def _v_return_qty(self):
        if self.returned_qty_base and self.returned_qty_base > self.qty_base:
            raise ValueError("returned_qty_base cannot exceed qty_base")
        return self


class DispenseCreate(BaseModel):
    dispense_no: str = Field(..., min_length=1, max_length=40)
    dispense_type: DispenseType = DispenseType.MANUAL

    store_id: int
    dispensed_at: Optional[datetime] = None

    # clinical links (optional)
    patient_id: Optional[int] = None
    encounter_id: Optional[int] = None
    admission_id: Optional[int] = None
    prescription_id: Optional[int] = None

    ordered_by_user_id: Optional[int] = None
    prescriber_user_id: Optional[int] = None

    # insurance context
    payer_id: Optional[int] = None
    plan_id: Optional[int] = None
    pricing_basis: PricingBasis = PricingBasis.MRP

    # totals (server computes; client can send 0)
    round_off: Decimal = Decimal("0")

    # controlled override (if any)
    override_reason: Optional[str] = Field(default=None, max_length=255)

    note: Optional[str] = None
    lines: List[DispenseLineIn]
    meta: Optional[Dict[str, Any]] = None

    @field_validator("round_off")
    @classmethod
    def _v_nonneg(cls, v):
        return _nonneg(v)


class DispenseUpdate(BaseModel):
    dispense_type: Optional[DispenseType] = None
    store_id: Optional[int] = None
    dispensed_at: Optional[datetime] = None

    patient_id: Optional[int] = None
    encounter_id: Optional[int] = None
    admission_id: Optional[int] = None
    prescription_id: Optional[int] = None

    ordered_by_user_id: Optional[int] = None
    prescriber_user_id: Optional[int] = None

    payer_id: Optional[int] = None
    plan_id: Optional[int] = None
    pricing_basis: Optional[PricingBasis] = None

    round_off: Optional[Decimal] = None
    override_reason: Optional[str] = Field(default=None, max_length=255)

    note: Optional[str] = None
    lines: Optional[List[DispenseLineIn]] = None
    meta: Optional[Dict[str, Any]] = None

    @field_validator("round_off")
    @classmethod
    def _v_nonneg(cls, v):
        if v is None: return v
        return _nonneg(v)


class VerifyPayload(BaseModel):
    verified_by: Optional[int] = None  # server can set from user
    verified_at: Optional[datetime] = None
    note: Optional[str] = Field(default=None, max_length=255)


class DispenseLineOut(ORMModel):
    id: int
    dispense_id: int
    line_no: int

    item_id: int
    batch_id: Optional[int] = None

    base_uom_id: Optional[int] = None
    qty_base: Decimal

    sale_uom_id: Optional[int] = None
    qty_sale_uom: Optional[Decimal] = None

    mrp: Optional[Decimal] = None
    selling_rate: Decimal
    discount_percent: Decimal
    discount_amount: Decimal
    tax_percent: Decimal
    tax_amount: Decimal
    line_total: Decimal

    unit_cost_per_base: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None

    allowed_amount: Decimal
    payer_amount: Decimal
    patient_amount: Decimal
    nonpayable_amount: Decimal
    denial_reason: Optional[str] = None
    prior_auth_ref: Optional[str] = None

    is_returned: bool
    returned_qty_base: Decimal
    return_reason: Optional[str] = None

    remark: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class DispenseOut(ORMModel):
    id: int
    dispense_no: str
    status: DocStatus
    dispense_type: DispenseType

    store_id: int
    dispensed_at: datetime

    patient_id: Optional[int] = None
    encounter_id: Optional[int] = None
    admission_id: Optional[int] = None
    prescription_id: Optional[int] = None

    ordered_by_user_id: Optional[int] = None
    prescriber_user_id: Optional[int] = None

    payer_id: Optional[int] = None
    plan_id: Optional[int] = None
    pricing_basis: PricingBasis

    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    round_off: Decimal
    grand_total: Decimal

    allowed_total: Decimal
    payer_total: Decimal
    patient_total: Decimal
    nonpayable_total: Decimal

    posted_at: Optional[datetime] = None
    posted_by: Optional[int] = None

    override_reason: Optional[str] = None
    verified_by: Optional[int] = None
    verified_at: Optional[datetime] = None

    note: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    cancelled_by: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None
    lines: List[DispenseLineOut] = []


# ============================================================
# 11) ALERTS / DASHBOARD (operational outputs)
# ============================================================
class StockAlertType(str, Enum):
    BELOW_MIN = "BELOW_MIN"
    BELOW_REORDER = "BELOW_REORDER"
    ABOVE_MAX = "ABOVE_MAX"
    NEAR_EXPIRY = "NEAR_EXPIRY"
    EXPIRED = "EXPIRED"
    RECALL = "RECALL"
    QUARANTINE = "QUARANTINE"
    NEGATIVE_RISK = "NEGATIVE_RISK"
    LOW_MARGIN = "LOW_MARGIN"
    DEAD_STOCK = "DEAD_STOCK"


class StockAlertOut(BaseModel):
    alert_type: StockAlertType
    store_id: int
    item_id: int
    batch_id: Optional[int] = None

    message: str
    severity: str = Field(default="warning")  # info/warning/critical

    on_hand_base: Optional[Decimal] = None
    reorder_point_base: Optional[Decimal] = None
    min_stock_base: Optional[Decimal] = None
    max_stock_base: Optional[Decimal] = None

    expiry_date: Optional[date] = None
    days_to_expiry: Optional[int] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    meta: Optional[Dict[str, Any]] = None


# ============================================================
# 12) AUDIT EVENTS (workflow actions timeline)
# ============================================================
class AuditEventCreate(BaseModel):
    action: str = Field(
        ..., min_length=2,
        max_length=40)  # CREATE/UPDATE/SUBMIT/APPROVE/POST/CANCEL/OVERRIDE...
    entity_type: str = Field(..., min_length=2, max_length=80)  # table/class
    entity_id: int

    note: Optional[str] = Field(default=None, max_length=255)
    before_json: Optional[Dict[str, Any]] = None
    after_json: Optional[Dict[str, Any]] = None

    ip_address: Optional[str] = Field(default=None, max_length=60)
    user_agent: Optional[str] = Field(default=None, max_length=255)


class AuditEventOut(ORMModel):
    id: int
    actor_user_id: Optional[int] = None
    actor_name: Optional[str] = None

    action: str
    entity_type: str
    entity_id: int

    ip_address: Optional[str] = None
    user_agent: Optional[str] = None

    before_json: Optional[Dict[str, Any]] = None
    after_json: Optional[Dict[str, Any]] = None

    note: Optional[str] = None
    created_at: datetime
    updated_at: datetime
