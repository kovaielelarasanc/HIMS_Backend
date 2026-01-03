# FILE: app/schemas/pharmacy_stock_alerts.py
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, Field

try:
    # pydantic v2
    from pydantic import ConfigDict
except Exception:  # pragma: no cover
    ConfigDict = None  # type: ignore


Quantity = Decimal
Money = Decimal


# -------------------------
# Enums
# -------------------------
class AlertType(str, Enum):
    # Existing / backward-compatible
    LOW_STOCK = "LOW_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    OVER_STOCK = "OVER_STOCK"
    NEAR_EXPIRY = "NEAR_EXPIRY"
    EXPIRED = "EXPIRED"
    NON_MOVING = "NON_MOVING"
    FEFO_RISK = "FEFO_RISK"

    # New (meets your feature list)
    REORDER = "REORDER"                  # consumption × lead time / reorder_level
    BATCH_RISK = "BATCH_RISK"            # batch exists but no sale/issue since X days
    NEGATIVE_STOCK = "NEGATIVE_STOCK"    # negative qty or mismatch
    HIGH_VALUE_EXPIRY = "HIGH_VALUE_EXPIRY"
    CONTROLLED_DRUG = "CONTROLLED_DRUG"  # schedule H/H1/X etc (or flags)


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRIT = "CRIT"


class ReportType(str, Enum):
    LOW_STOCK = "LOW_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    NEAR_EXPIRY = "NEAR_EXPIRY"
    EXPIRED = "EXPIRED"
    NON_MOVING = "NON_MOVING"
    VALUATION = "VALUATION"


# -------------------------
# Filters
# -------------------------
class StockAlertsFiltersOut(BaseModel):
    location_id: Optional[int] = None
    item_type: Optional[str] = None              # DRUG / CONSUMABLE
    schedule_code: Optional[str] = None          # H / H1 / X / etc
    supplier_id: Optional[int] = None            # default_supplier_id filter

    # Expiry / movement windows
    days_near_expiry: int = 90
    non_moving_days: int = 60
    fast_moving_days: int = 30

    # Reorder intelligence
    lead_time_days: int = 7
    consumption_days: int = 30                   # avg daily consumption window

    # Value risk threshold (optional)
    high_value_expiry_threshold: Money = Decimal("0")  # if >0, alerts for batches above this value

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover (pydantic v1)
        class Config:
            orm_mode = True


# -------------------------
# Small DTOs
# -------------------------
class FastMovingOut(BaseModel):
    item_id: int
    code: str
    name: str
    out_qty: Quantity

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


class MovementBucketOut(BaseModel):
    key: str  # PURCHASE_IN / DISPENSE_OUT / RETURNS_IN / RETURNS_OUT / ADJUSTMENT
    qty: Quantity

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


class SpikeOut(BaseModel):
    metric: str  # e.g., "DISPENSE_OUT"
    today: Quantity
    avg_last_7_days: Quantity
    ratio: Decimal

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


class ItemBatchRowOut(BaseModel):
    batch_id: int
    batch_no: str
    expiry_date: Optional[date] = None
    current_qty: Quantity
    unit_cost: Money
    mrp: Money
    tax_percent: Money
    is_saleable: bool
    status: str

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


class FEFOItemSuggestionOut(BaseModel):
    location_id: int
    location_name: str
    item_id: int
    item_code: str
    item_name: str
    batches: List[ItemBatchRowOut] = Field(default_factory=list)  # FEFO order (earliest expiry first)

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


# -------------------------
# Pipeline / Compliance (computed)
# -------------------------
class ProcurementPipelineOut(BaseModel):
    po_draft: int = 0
    po_approved: int = 0
    po_sent: int = 0
    po_partially_received: int = 0
    po_completed: int = 0
    po_overdue: int = 0

    grn_draft: int = 0
    grn_posted: int = 0
    grn_cancelled: int = 0
    grn_posted_today: int = 0

    # placeholders for future QC pipeline (no QC tables in your models)
    qc_pending: int = 0
    stock_not_updated: int = 0

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


class StockKpisOut(BaseModel):
    # Master counts
    total_items_count: int = 0
    active_items_count: int = 0
    inactive_items_count: int = 0
    locations_count: int = 0

    # Valuation (batch-wise)
    stock_value_purchase: Money = Decimal("0")
    stock_value_mrp: Money = Decimal("0")

    # Core alerts
    low_stock_count: int = 0
    out_of_stock_count: int = 0
    over_stock_count: int = 0

    near_expiry_7: int = 0
    near_expiry_30: int = 0
    near_expiry_60: int = 0
    near_expiry_90: int = 0

    expired_count: int = 0
    expired_value_purchase: Money = Decimal("0")
    expired_value_mrp: Money = Decimal("0")

    non_moving_30_count: int = 0
    non_moving_60_count: int = 0
    non_moving_90_count: int = 0

    # Smart alerts counts
    reorder_count: int = 0
    batch_risk_count: int = 0
    negative_stock_count: int = 0
    high_value_expiry_count: int = 0
    controlled_drug_count: int = 0

    # Fast moving
    fast_moving_top: List[FastMovingOut] = Field(default_factory=list)

    # Optional premium-ish metric (approximation)
    fefo_compliance_pct: Optional[Decimal] = None  # 0..100

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


class LocationStockSummaryOut(BaseModel):
    location_id: int
    location_name: str

    items_with_stock: int = 0
    low_stock_count: int = 0
    out_of_stock_count: int = 0
    expiry_risk_count: int = 0

    stock_value_purchase: Money = Decimal("0")
    stock_value_mrp: Money = Decimal("0")

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


# -------------------------
# Alert row
# -------------------------
class StockAlertOut(BaseModel):
    type: AlertType
    severity: AlertSeverity

    message: str
    suggested_action: Optional[str] = None

    location_id: Optional[int] = None
    location_name: Optional[str] = None

    supplier_id: Optional[int] = None

    item_id: Optional[int] = None
    item_code: Optional[str] = None
    item_name: Optional[str] = None

    on_hand_qty: Optional[Quantity] = None
    reorder_level: Optional[Quantity] = None
    max_level: Optional[Quantity] = None

    # Smart fields
    avg_daily_consumption: Optional[Quantity] = None
    lead_time_days: Optional[int] = None
    reorder_point: Optional[Quantity] = None
    suggested_reorder_qty: Optional[Quantity] = None
    days_of_stock_remaining: Optional[Decimal] = None
    predicted_stockout_date: Optional[date] = None

    # Batch-wise (mandatory visibility)
    batch_id: Optional[int] = None
    batch_no: Optional[str] = None
    expiry_date: Optional[date] = None
    days_to_expiry: Optional[int] = None

    unit_cost: Optional[Money] = None
    mrp: Optional[Money] = None

    value_risk_purchase: Optional[Money] = None
    value_risk_mrp: Optional[Money] = None

    # Optional: include FEFO / batch breakdown for item-level alerts
    batch_rows: Optional[List[ItemBatchRowOut]] = None  # small list, FEFO order

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


# -------------------------
# Dashboard
# -------------------------
class StockAuditComplianceOut(BaseModel):
    # Placeholder fields (no audit tables exist in your shared models)
    last_audit_date: Optional[date] = None
    not_audited_30_count: int = 0
    not_audited_60_count: int = 0
    not_audited_90_count: int = 0
    variance_alerts_count: int = 0

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True


class StockAlertsDashboardOut(BaseModel):
    as_of: datetime
    filters: StockAlertsFiltersOut

    kpis: StockKpisOut
    locations: List[LocationStockSummaryOut]

    # Quick table on summary screen
    alerts_preview: List[StockAlertOut] = Field(default_factory=list)

    # Movement
    movement_today: List[MovementBucketOut] = Field(default_factory=list)
    movement_week: List[MovementBucketOut] = Field(default_factory=list)
    spikes: List[SpikeOut] = Field(default_factory=list)

    # Procurement pipeline
    pipeline: ProcurementPipelineOut

    # FEFO suggestions (e.g., show for top fast-moving items)
    fefo_next_to_dispense: List[FEFOItemSuggestionOut] = Field(default_factory=list)

    # Compliance (future)
    audit: StockAuditComplianceOut = Field(default_factory=StockAuditComplianceOut)

    if ConfigDict:
        model_config = ConfigDict(from_attributes=True)
    else:  # pragma: no cover
        class Config:
            orm_mode = True
