# FILE: app/models/pharmacy_inventory_new.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
import enum

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    ForeignKey,
    Numeric,
    Text,
    Enum,
    UniqueConstraint,
    Index,
    CheckConstraint,
    Date,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.db.base import Base


# -----------------------------
# Helpers / Mixins
# -----------------------------
def utcnow() -> datetime:
    return datetime.utcnow()


DEC_QTY = Numeric(18, 6)  # quantities (supports liquids + conversions)
DEC_MONEY = Numeric(18, 4)  # rates/costs
DEC_MONEY2 = Numeric(18, 2)  # MRP / display amounts


class TimestampMixin:
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime,
                        nullable=False,
                        default=utcnow,
                        onupdate=utcnow)


class SoftDeleteMixin:
    is_deleted = Column(Boolean, nullable=False, default=False)
    deleted_at = Column(DateTime, nullable=True)
    deleted_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class AuditActorMixin:
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    cancelled_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String(255), nullable=True)


# -----------------------------
# Enums (stored as strings)
# -----------------------------
class DocStatus(str, enum.Enum):
    draft = "draft"
    submitted = "submitted"
    approved = "approved"
    posted = "posted"
    closed = "closed"
    cancelled = "cancelled"


class LedgerMove(str, enum.Enum):
    IN = "IN"
    OUT = "OUT"


class LedgerReason(str, enum.Enum):
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


class DispenseType(str, enum.Enum):
    OP = "OP"
    IP = "IP"
    ER = "ER"
    OT = "OT"
    MANUAL = "MANUAL"


class PricingBasis(str, enum.Enum):
    MRP = "MRP"
    CONTRACT = "CONTRACT"
    HOSPITAL_PRICE = "HOSPITAL_PRICE"


class CoverageStatus(str, enum.Enum):
    COVERED = "COVERED"
    NON_COVERED = "NON_COVERED"
    REQUIRES_PREAUTH = "REQUIRES_PREAUTH"
    CAP_APPLIES = "CAP_APPLIES"


# ============================================================
# 1) MASTERS
# ============================================================
class PhManufacturer(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_manufacturers"

    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False, unique=True)
    code = Column(String(40), nullable=True, unique=True)
    gstin = Column(String(20), nullable=True)
    address = Column(Text, nullable=True)
    phone = Column(String(30), nullable=True)
    email = Column(String(120), nullable=True)

    meta = Column(JSON, nullable=True)


class PhTaxCode(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_tax_codes"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False, unique=True)  # e.g., GST 12%
    gst_percent = Column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    hsn = Column(String(30), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    meta = Column(JSON, nullable=True)


class PhUom(Base, TimestampMixin, SoftDeleteMixin):
    """
    Unit of Measure master:
    TAB / ML / GM / PCS / STRIP / BOX / BOTTLE ...
    """
    __tablename__ = "ph_uoms"

    id = Column(Integer, primary_key=True)
    code = Column(String(20), nullable=False, unique=True)
    name = Column(String(60), nullable=False)
    is_base_uom = Column(Boolean, nullable=False, default=False)

    meta = Column(JSON, nullable=True)


class PhCategory(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True)
    parent_id = Column(Integer, ForeignKey("ph_categories.id"), nullable=True)
    parent = relationship("PhCategory", remote_side=[id], backref="children")

    meta = Column(JSON, nullable=True)


class PhItem(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_items"

    id = Column(Integer, primary_key=True)

    # Identity
    sku = Column(String(60), nullable=True, unique=True)  # internal code
    name = Column(String(100), nullable=False, index=True)
    generic_name = Column(String(150), nullable=True, index=True)
    brand_name = Column(String(150), nullable=True, index=True)

    category_id = Column(Integer,
                         ForeignKey("ph_categories.id"),
                         nullable=True)
    category = relationship("PhCategory")

    manufacturer_id = Column(Integer,
                             ForeignKey("ph_manufacturers.id"),
                             nullable=True)
    manufacturer = relationship("PhManufacturer")

    # Pharmacy compliance
    is_medicine = Column(Boolean, nullable=False, default=True)
    is_consumable = Column(Boolean, nullable=False, default=False)
    is_device = Column(Boolean, nullable=False, default=False)
    is_controlled = Column(Boolean, nullable=False,
                           default=False)  # narcotics etc.
    prescription_required = Column(Boolean, nullable=False, default=True)

    # Tax & regulatory
    tax_code_id = Column(Integer, ForeignKey("ph_tax_codes.id"), nullable=True)
    tax_code = relationship("PhTaxCode")
    hsn = Column(String(30), nullable=True)
    gst_percent_override = Column(Numeric(6, 3),
                                  nullable=True)  # if item-specific

    # Storage
    storage_type = Column(String(40), nullable=False,
                          default="room_temp")  # room_temp/refrigerated/frozen
    temp_min_c = Column(Numeric(6, 2), nullable=True)
    temp_max_c = Column(Numeric(6, 2), nullable=True)

    # Units
    base_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=False)
    base_uom = relationship("PhUom", foreign_keys=[base_uom_id])

    purchase_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=True)
    purchase_uom = relationship("PhUom", foreign_keys=[purchase_uom_id])

    sale_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=True)
    sale_uom = relationship("PhUom", foreign_keys=[sale_uom_id])

    # Reorder defaults (store overrides exist in PhItemStoreSetting)
    min_stock_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    max_stock_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    reorder_point_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    reorder_qty_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))

    # Sales & returns
    allow_substitution = Column(Boolean, nullable=False, default=True)
    return_allowed = Column(Boolean, nullable=False, default=True)
    return_window_days = Column(Integer, nullable=True)

    # Barcodes (item-level)
    barcode = Column(String(80), nullable=True,
                     index=True)  # GTIN / internal barcode
    alt_barcodes = Column(JSON, nullable=True)  # list of other barcodes

    # Pricing (fallback defaults; final selling uses rules/pricelists/contracts)
    default_mrp = Column(DEC_MONEY2, nullable=True)
    default_sale_price = Column(DEC_MONEY, nullable=True)  # per sale_uom

    is_active = Column(Boolean, nullable=False, default=True)

    meta = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_ph_items_name_generic_brand", "name", "generic_name", "brand_name"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )


class PhItemUomConversion(Base, TimestampMixin, SoftDeleteMixin):
    """
    Conversion graph, per item:
    e.g. 1 STRIP = 10 TAB
         1 BOX = 10 STRIP
    Store factors as: to_qty = from_qty * factor
    """
    __tablename__ = "ph_item_uom_conversions"

    id = Column(Integer, primary_key=True)

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem", backref="uom_conversions")

    from_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=False)
    to_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=False)

    factor = Column(DEC_QTY, nullable=False)  # multiply from to get to
    is_active = Column(Boolean, nullable=False, default=True)

    note = Column(String(255), nullable=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("item_id",
                         "from_uom_id",
                         "to_uom_id",
                         name="uq_item_uom_conv"),
        CheckConstraint("factor > 0", name="ck_item_uom_factor_gt0"),
        Index("ix_item_uom_conv_item_from_to", "item_id", "from_uom_id",
              "to_uom_id"),
    )


class PhSupplier(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_suppliers"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    code = Column(String(40), nullable=True, unique=True)

    gstin = Column(String(20), nullable=True)
    dl_no = Column(String(80), nullable=True)  # drug license
    address = Column(Text, nullable=True)
    phone = Column(String(30), nullable=True)
    email = Column(String(120), nullable=True)

    credit_days = Column(Integer, nullable=False, default=0)
    payment_terms = Column(String(180), nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)
    meta = Column(JSON, nullable=True)


class PhStore(Base, TimestampMixin, SoftDeleteMixin):
    """
    Even "pharmacy-only" should support multiple stores/counters.
    """
    __tablename__ = "ph_stores"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True)
    code = Column(String(40), nullable=True, unique=True)

    is_main = Column(Boolean, nullable=False, default=False)
    is_dispense_point = Column(Boolean, nullable=False, default=True)
    is_receiving_point = Column(Boolean, nullable=False, default=True)

    address = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    meta = Column(JSON, nullable=True)


class PhItemStoreSetting(Base, TimestampMixin, SoftDeleteMixin):
    """
    Store-level min/max/reorder overrides + alert thresholds.
    """
    __tablename__ = "ph_item_store_settings"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer,
                      ForeignKey("ph_stores.id"),
                      nullable=False,
                      index=True)
    store = relationship("PhStore", backref="item_settings")

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem", backref="store_settings")

    # Reorder thresholds in BASE UOM qty
    min_stock_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    max_stock_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    reorder_point_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    reorder_qty_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))

    # Expiry alert lead time
    expiry_warn_days = Column(Integer, nullable=False, default=90)
    expiry_block_days = Column(Integer, nullable=False,
                               default=0)  # 0 => block only if expired

    # Negative stock policy
    allow_negative_stock = Column(Boolean, nullable=False, default=False)

    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("store_id", "item_id", name="uq_item_store_setting"),
        Index("ix_item_store_setting_store_item", "store_id", "item_id"),
    )


# ============================================================
# 2) INSURANCE / CONTRACT PRICING (PHARMACY)
# ============================================================
class InsPayer(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ins_payers"

    id = Column(Integer, primary_key=True)
    name = Column(String(180), nullable=False, unique=True)
    code = Column(String(40), nullable=True, unique=True)
    payer_type = Column(String(40), nullable=False,
                        default="TPA")  # TPA/INSURER/CORPORATE/SELF
    phone = Column(String(30), nullable=True)
    email = Column(String(120), nullable=True)
    address = Column(Text, nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)
    meta = Column(JSON, nullable=True)


class InsPlan(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ins_plans"

    id = Column(Integer, primary_key=True)
    payer_id = Column(Integer,
                      ForeignKey("ins_payers.id"),
                      nullable=False,
                      index=True)
    payer = relationship("InsPayer", backref="plans")

    name = Column(String(180), nullable=False)
    code = Column(String(60), nullable=True)

    valid_from = Column(Date, nullable=True)
    valid_to = Column(Date, nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("payer_id", "name", name="uq_plan_payer_name"),
        Index("ix_ins_plan_payer_active", "payer_id", "is_active"),
    )


class InsCoverageRule(Base, TimestampMixin, SoftDeleteMixin):
    """
    Rules for coverage & pricing behavior (high-level).
    Item-specific contracts are in InsContractPrice.
    """
    __tablename__ = "ins_coverage_rules"

    id = Column(Integer, primary_key=True)
    payer_id = Column(Integer,
                      ForeignKey("ins_payers.id"),
                      nullable=False,
                      index=True)
    plan_id = Column(Integer,
                     ForeignKey("ins_plans.id"),
                     nullable=True,
                     index=True)

    payer = relationship("InsPayer")
    plan = relationship("InsPlan")

    # Defaults
    pricing_basis = Column(Enum(PricingBasis, native_enum=False),
                           nullable=False,
                           default=PricingBasis.CONTRACT)
    copay_percent = Column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    max_cap_amount = Column(DEC_MONEY,
                            nullable=True)  # optional cap per line/item/day
    prior_auth_required_default = Column(Boolean,
                                         nullable=False,
                                         default=False)

    # Substitution policy
    enforce_generic = Column(Boolean, nullable=False, default=False)
    allow_brand_override = Column(Boolean, nullable=False, default=True)

    valid_from = Column(Date, nullable=True)
    valid_to = Column(Date, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    meta = Column(JSON, nullable=True)

    __table_args__ = (Index("ix_ins_cov_rule_payer_plan_active", "payer_id",
                            "plan_id", "is_active"), )


class InsContractPrice(Base, TimestampMixin, SoftDeleteMixin):
    """
    Item-level negotiated rates / allowed rates.
    Store contract rates per SALE UOM or BASE UOM (recommended: BASE).
    """
    __tablename__ = "ins_contract_prices"

    id = Column(Integer, primary_key=True)
    payer_id = Column(Integer,
                      ForeignKey("ins_payers.id"),
                      nullable=False,
                      index=True)
    plan_id = Column(Integer,
                     ForeignKey("ins_plans.id"),
                     nullable=True,
                     index=True)

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    # coverage behavior
    coverage_status = Column(Enum(CoverageStatus, native_enum=False),
                             nullable=False,
                             default=CoverageStatus.COVERED)
    prior_auth_required = Column(Boolean, nullable=False, default=False)

    # allowed pricing
    allowed_rate_per_base = Column(DEC_MONEY, nullable=True)  # recommended
    allowed_rate_per_sale_uom = Column(DEC_MONEY, nullable=True)
    currency = Column(String(8), nullable=False, default="INR")

    # caps / rules
    max_qty_per_day_base = Column(DEC_QTY, nullable=True)
    max_qty_per_encounter_base = Column(DEC_QTY, nullable=True)
    max_amount_per_encounter = Column(DEC_MONEY, nullable=True)

    valid_from = Column(Date, nullable=True)
    valid_to = Column(Date, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("payer_id",
                         "plan_id",
                         "item_id",
                         name="uq_contract_price"),
        Index("ix_contract_price_item_payer_plan", "item_id", "payer_id",
              "plan_id"),
    )


# ============================================================
# 3) PROCUREMENT (PO / GRN / PURCHASE INVOICE)
# ============================================================
class PhPurchaseOrder(Base, TimestampMixin, SoftDeleteMixin, AuditActorMixin):
    __tablename__ = "ph_purchase_orders"

    id = Column(Integer, primary_key=True)
    po_no = Column(String(40), nullable=False, unique=True, index=True)

    status = Column(Enum(DocStatus, native_enum=False),
                    nullable=False,
                    default=DocStatus.draft)

    supplier_id = Column(Integer,
                         ForeignKey("ph_suppliers.id"),
                         nullable=False,
                         index=True)
    supplier = relationship("PhSupplier")

    store_id = Column(Integer,
                      ForeignKey("ph_stores.id"),
                      nullable=False,
                      index=True)
    store = relationship("PhStore")

    po_date = Column(DateTime, nullable=False, default=utcnow)
    expected_date = Column(DateTime, nullable=True)

    currency = Column(String(8), nullable=False, default="INR")
    notes = Column(Text, nullable=True)
    terms = Column(Text, nullable=True)

    # Totals snapshot (computed)
    subtotal = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    discount_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    tax_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    shipping_charges = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    other_charges = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    round_off = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    revision_no = Column(Integer, nullable=False,
                         default=0)  # increment on amendment
    meta = Column(JSON, nullable=True)

    lines = relationship("PhPurchaseOrderLine",
                         back_populates="po",
                         cascade="all, delete-orphan")


class PhPurchaseOrderLine(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_purchase_order_lines"

    id = Column(Integer, primary_key=True)
    po_id = Column(Integer,
                   ForeignKey("ph_purchase_orders.id", ondelete="CASCADE"),
                   nullable=False,
                   index=True)
    po = relationship("PhPurchaseOrder", back_populates="lines")

    line_no = Column(Integer, nullable=False, default=1)

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    purchase_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=True)
    purchase_uom = relationship("PhUom")

    ordered_qty = Column(DEC_QTY, nullable=False)
    rate = Column(DEC_MONEY, nullable=False,
                  default=Decimal("0"))  # per purchase_uom

    discount_percent = Column(Numeric(6, 3),
                              nullable=False,
                              default=Decimal("0"))
    discount_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    tax_percent = Column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    tax_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    free_qty = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    mrp = Column(DEC_MONEY2, nullable=True)

    line_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    remark = Column(String(255), nullable=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("po_id", "line_no", name="uq_po_line_no"),
        Index("ix_po_line_po_item", "po_id", "item_id"),
    )


class PhGoodsReceiptNote(Base, TimestampMixin, SoftDeleteMixin,
                         AuditActorMixin):
    __tablename__ = "ph_grns"

    id = Column(Integer, primary_key=True)
    grn_no = Column(String(40), nullable=False, unique=True, index=True)

    status = Column(Enum(DocStatus, native_enum=False),
                    nullable=False,
                    default=DocStatus.draft)

    store_id = Column(Integer,
                      ForeignKey("ph_stores.id"),
                      nullable=False,
                      index=True)
    store = relationship("PhStore")

    supplier_id = Column(Integer,
                         ForeignKey("ph_suppliers.id"),
                         nullable=False,
                         index=True)
    supplier = relationship("PhSupplier")

    po_id = Column(Integer,
                   ForeignKey("ph_purchase_orders.id"),
                   nullable=True,
                   index=True)
    po = relationship("PhPurchaseOrder")

    received_at = Column(DateTime, nullable=False, default=utcnow)
    supplier_delivery_note = Column(String(80), nullable=True)
    vehicle_no = Column(String(40), nullable=True)
    received_by_name = Column(String(120), nullable=True)

    notes = Column(Text, nullable=True)

    # Totals snapshot
    subtotal = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    discount_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    tax_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    shipping_charges = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    other_charges = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    round_off = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    # Posting/ledger linkage
    posted_at = Column(DateTime, nullable=True)
    posted_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    meta = Column(JSON, nullable=True)

    lines = relationship("PhGoodsReceiptLine",
                         back_populates="grn",
                         cascade="all, delete-orphan")


class PhGoodsReceiptLine(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_grn_lines"

    id = Column(Integer, primary_key=True)
    grn_id = Column(Integer,
                    ForeignKey("ph_grns.id", ondelete="CASCADE"),
                    nullable=False,
                    index=True)
    grn = relationship("PhGoodsReceiptNote", back_populates="lines")

    line_no = Column(Integer, nullable=False, default=1)

    po_line_id = Column(Integer,
                        ForeignKey("ph_purchase_order_lines.id"),
                        nullable=True,
                        index=True)
    po_line = relationship("PhPurchaseOrderLine")

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    purchase_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=True)
    purchase_uom = relationship("PhUom")

    received_qty = Column(DEC_QTY, nullable=False)
    free_qty = Column(DEC_QTY, nullable=False, default=Decimal("0"))

    batch_no = Column(String(80), nullable=False, index=True)
    mfg_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=False, index=True)

    mrp = Column(DEC_MONEY2,
                 nullable=True)  # per sale pack/unit as per labeling
    purchase_rate = Column(DEC_MONEY, nullable=False)  # per purchase_uom
    tax_percent = Column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    discount_percent = Column(Numeric(6, 3),
                              nullable=False,
                              default=Decimal("0"))

    discount_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    tax_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    # Computed landed cost snapshots (store after posting)
    landed_cost_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    landed_cost_per_base = Column(
        DEC_MONEY, nullable=True)  # "single piece cost" per BASE UOM

    # Batch barcode / scan support
    batch_barcode = Column(String(120), nullable=True, index=True)

    # Quality / receipt flags
    is_damaged = Column(Boolean, nullable=False, default=False)
    damage_note = Column(String(255), nullable=True)
    is_quarantined = Column(Boolean, nullable=False, default=False)
    quarantine_reason = Column(String(255), nullable=True)

    remark = Column(String(255), nullable=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("grn_id", "line_no", name="uq_grn_line_no"),
        Index("ix_grn_line_item_exp_batch", "item_id", "expiry_date",
              "batch_no"),
        CheckConstraint("received_qty >= 0",
                        name="ck_grn_received_qty_nonneg"),
        CheckConstraint("free_qty >= 0", name="ck_grn_free_qty_nonneg"),
    )


class PhPurchaseInvoice(Base, TimestampMixin, SoftDeleteMixin,
                        AuditActorMixin):
    __tablename__ = "ph_purchase_invoices"

    id = Column(Integer, primary_key=True)
    invoice_no = Column(String(60), nullable=False,
                        index=True)  # supplier invoice number
    invoice_date = Column(Date, nullable=False)

    status = Column(Enum(DocStatus, native_enum=False),
                    nullable=False,
                    default=DocStatus.draft)

    supplier_id = Column(Integer,
                         ForeignKey("ph_suppliers.id"),
                         nullable=False,
                         index=True)
    supplier = relationship("PhSupplier")

    store_id = Column(Integer,
                      ForeignKey("ph_stores.id"),
                      nullable=False,
                      index=True)
    store = relationship("PhStore")

    grn_id = Column(Integer,
                    ForeignKey("ph_grns.id"),
                    nullable=True,
                    index=True)
    grn = relationship("PhGoodsReceiptNote")

    currency = Column(String(8), nullable=False, default="INR")
    notes = Column(Text, nullable=True)

    # Charges
    subtotal = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    discount_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    tax_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    shipping_charges = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    other_charges = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    round_off = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    # Accounting refs
    payment_status = Column(String(30), nullable=False,
                            default="unpaid")  # unpaid/partial/paid
    due_date = Column(Date, nullable=True)
    paid_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    posted_at = Column(DateTime, nullable=True)
    posted_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    meta = Column(JSON, nullable=True)

    __table_args__ = (Index("ix_purchase_invoice_supplier_date", "supplier_id",
                            "invoice_date"), )

    lines = relationship("PhPurchaseInvoiceLine",
                         back_populates="invoice",
                         cascade="all, delete-orphan")


class PhPurchaseInvoiceLine(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_purchase_invoice_lines"

    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer,
                        ForeignKey("ph_purchase_invoices.id",
                                   ondelete="CASCADE"),
                        nullable=False,
                        index=True)
    invoice = relationship("PhPurchaseInvoice", back_populates="lines")

    line_no = Column(Integer, nullable=False, default=1)

    grn_line_id = Column(Integer,
                         ForeignKey("ph_grn_lines.id"),
                         nullable=True,
                         index=True)
    grn_line = relationship("PhGoodsReceiptLine")

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    purchase_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=True)
    purchase_uom = relationship("PhUom")

    billed_qty = Column(DEC_QTY, nullable=False,
                        default=Decimal("0"))  # per purchase_uom
    rate = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    discount_percent = Column(Numeric(6, 3),
                              nullable=False,
                              default=Decimal("0"))
    discount_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    tax_percent = Column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    tax_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    line_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    # Optional: batch info snapshot for invoice line (for reconciliation)
    batch_no = Column(String(80), nullable=True)
    expiry_date = Column(Date, nullable=True)
    mrp = Column(DEC_MONEY2, nullable=True)

    remark = Column(String(255), nullable=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("invoice_id",
                         "line_no",
                         name="uq_purchase_invoice_line_no"),
        Index("ix_purchase_invoice_line_item", "item_id"),
    )


# ============================================================
# 4) BATCH / STOCK BALANCE / LEDGER (IMMUTABLE)
# ============================================================
class PhBatch(Base, TimestampMixin, SoftDeleteMixin):
    """
    Batch master per item+batch+expiry (+mrp optional).
    Keep cost snapshot per base unit for audit.
    """
    __tablename__ = "ph_batches"

    id = Column(Integer, primary_key=True)

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem", backref="batches")

    batch_no = Column(String(80), nullable=False, index=True)
    mfg_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=False, index=True)

    mrp = Column(DEC_MONEY2, nullable=True)
    batch_barcode = Column(String(120), nullable=True, index=True)

    # Origin
    supplier_id = Column(Integer,
                         ForeignKey("ph_suppliers.id"),
                         nullable=True,
                         index=True)
    supplier = relationship("PhSupplier")

    first_grn_line_id = Column(Integer,
                               ForeignKey("ph_grn_lines.id"),
                               nullable=True)
    first_grn_line = relationship("PhGoodsReceiptLine")

    # Cost snapshots
    landed_cost_per_base = Column(DEC_MONEY,
                                  nullable=True)  # single piece cost
    last_purchase_rate = Column(DEC_MONEY,
                                nullable=True)  # per purchase_uom (snapshot)
    currency = Column(String(8), nullable=False, default="INR")

    # Recall / quarantine / status
    is_recalled = Column(Boolean, nullable=False, default=False)
    recall_note = Column(String(255), nullable=True)
    is_quarantined = Column(Boolean, nullable=False, default=False)
    quarantine_note = Column(String(255), nullable=True)

    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("item_id",
                         "batch_no",
                         "expiry_date",
                         "mrp",
                         name="uq_item_batch_exp_mrp"),
        Index("ix_batch_item_exp", "item_id", "expiry_date"),
    )


class PhStockBalance(Base, TimestampMixin):
    """
    Current stock for store+batch (derived from ledger; also persisted for fast reads).
    Update ONLY via posting services.
    """
    __tablename__ = "ph_stock_balances"

    id = Column(Integer, primary_key=True)

    store_id = Column(Integer,
                      ForeignKey("ph_stores.id"),
                      nullable=False,
                      index=True)
    store = relationship("PhStore")

    batch_id = Column(Integer,
                      ForeignKey("ph_batches.id"),
                      nullable=False,
                      index=True)
    batch = relationship("PhBatch")

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    on_hand_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    reserved_base = Column(DEC_QTY, nullable=False,
                           default=Decimal("0"))  # for pending issues
    available_base = Column(DEC_QTY, nullable=False,
                            default=Decimal("0"))  # derived

    last_movement_at = Column(DateTime, nullable=True)
    last_movement_reason = Column(String(60), nullable=True)

    __table_args__ = (
        UniqueConstraint("store_id", "batch_id",
                         name="uq_store_batch_balance"),
        Index("ix_balance_store_item", "store_id", "item_id"),
        CheckConstraint("on_hand_base >= 0", name="ck_balance_onhand_nonneg"),
    )


class PhStockLedger(Base, TimestampMixin):
    """
    Immutable movement log (source of truth).
    NEVER delete; reversals are new rows.
    """
    __tablename__ = "ph_stock_ledger"

    id = Column(Integer, primary_key=True)

    moved_at = Column(DateTime, nullable=False, default=utcnow, index=True)
    move = Column(Enum(LedgerMove, native_enum=False), nullable=False)
    reason = Column(Enum(LedgerReason, native_enum=False), nullable=False)

    store_id = Column(Integer,
                      ForeignKey("ph_stores.id"),
                      nullable=False,
                      index=True)
    store = relationship("PhStore")

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    batch_id = Column(Integer,
                      ForeignKey("ph_batches.id"),
                      nullable=True,
                      index=True)
    batch = relationship("PhBatch")

    # Qty in BASE UOM (must be used for costing + balances)
    qty_base = Column(DEC_QTY, nullable=False)

    # Optional: original UOM entry for display
    uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=True)
    uom = relationship("PhUom")
    qty_uom = Column(DEC_QTY, nullable=True)

    # Cost snapshots
    unit_cost_per_base = Column(
        DEC_MONEY, nullable=True)  # landed cost per base at movement time
    total_cost = Column(DEC_MONEY, nullable=True)

    # Source document link (generic)
    source_doc_type = Column(
        String(40),
        nullable=False)  # GRN/INVOICE/DISPENSE/TRANSFER/ADJ/COUNT...
    source_doc_id = Column(Integer, nullable=False)
    source_line_id = Column(Integer, nullable=True)

    # Audit context
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_user_name = Column(String(120), nullable=True)
    note = Column(String(255), nullable=True)

    meta = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_ledger_store_item_dt", "store_id", "item_id", "moved_at"),
        Index("ix_ledger_store_batch_dt", "store_id", "batch_id", "moved_at"),
        CheckConstraint("qty_base <> 0", name="ck_ledger_qty_nonzero"),
    )


# ============================================================
# 5) STOCK OPERATIONS (ADJUSTMENT / TRANSFER / STOCK COUNT)
# ============================================================
class PhStockAdjustment(Base, TimestampMixin, SoftDeleteMixin,
                        AuditActorMixin):
    __tablename__ = "ph_stock_adjustments"

    id = Column(Integer, primary_key=True)
    adj_no = Column(String(40), nullable=False, unique=True, index=True)
    status = Column(Enum(DocStatus, native_enum=False),
                    nullable=False,
                    default=DocStatus.draft)

    store_id = Column(Integer,
                      ForeignKey("ph_stores.id"),
                      nullable=False,
                      index=True)
    store = relationship("PhStore")

    adj_at = Column(DateTime, nullable=False, default=utcnow)
    reason_code = Column(
        String(60),
        nullable=False)  # DAMAGE/EXPIRED/MISSING/FOUND/TEMP_BREACH/OTHER
    reason_note = Column(Text, nullable=True)

    posted_at = Column(DateTime, nullable=True)
    posted_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    meta = Column(JSON, nullable=True)

    lines = relationship("PhStockAdjustmentLine",
                         back_populates="adj",
                         cascade="all, delete-orphan")


class PhStockAdjustmentLine(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_stock_adjustment_lines"

    id = Column(Integer, primary_key=True)
    adj_id = Column(Integer,
                    ForeignKey("ph_stock_adjustments.id", ondelete="CASCADE"),
                    nullable=False,
                    index=True)
    adj = relationship("PhStockAdjustment", back_populates="lines")

    line_no = Column(Integer, nullable=False, default=1)

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    batch_id = Column(Integer,
                      ForeignKey("ph_batches.id"),
                      nullable=True,
                      index=True)
    batch = relationship("PhBatch")

    # Positive => add stock, Negative => remove stock
    qty_delta_base = Column(DEC_QTY, nullable=False)

    unit_cost_per_base = Column(DEC_MONEY, nullable=True)
    total_cost = Column(DEC_MONEY, nullable=True)

    remark = Column(String(255), nullable=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("adj_id", "line_no", name="uq_adj_line_no"),
        CheckConstraint("qty_delta_base <> 0", name="ck_adj_line_qty_nonzero"),
    )


class PhStockTransfer(Base, TimestampMixin, SoftDeleteMixin, AuditActorMixin):
    __tablename__ = "ph_stock_transfers"

    id = Column(Integer, primary_key=True)
    transfer_no = Column(String(40), nullable=False, unique=True, index=True)
    status = Column(Enum(DocStatus, native_enum=False),
                    nullable=False,
                    default=DocStatus.draft)

    from_store_id = Column(Integer,
                           ForeignKey("ph_stores.id"),
                           nullable=False,
                           index=True)
    to_store_id = Column(Integer,
                         ForeignKey("ph_stores.id"),
                         nullable=False,
                         index=True)

    from_store = relationship("PhStore", foreign_keys=[from_store_id])
    to_store = relationship("PhStore", foreign_keys=[to_store_id])

    requested_at = Column(DateTime, nullable=False, default=utcnow)
    issued_at = Column(DateTime, nullable=True)
    received_at = Column(DateTime, nullable=True)

    issued_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    received_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    courier_ref = Column(String(80), nullable=True)
    note = Column(Text, nullable=True)

    meta = Column(JSON, nullable=True)

    lines = relationship("PhStockTransferLine",
                         back_populates="transfer",
                         cascade="all, delete-orphan")


class PhStockTransferLine(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_stock_transfer_lines"

    id = Column(Integer, primary_key=True)
    transfer_id = Column(Integer,
                         ForeignKey("ph_stock_transfers.id",
                                    ondelete="CASCADE"),
                         nullable=False,
                         index=True)
    transfer = relationship("PhStockTransfer", back_populates="lines")

    line_no = Column(Integer, nullable=False, default=1)

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    batch_id = Column(Integer,
                      ForeignKey("ph_batches.id"),
                      nullable=True,
                      index=True)
    batch = relationship("PhBatch")

    qty_base = Column(DEC_QTY, nullable=False)

    unit_cost_per_base = Column(DEC_MONEY, nullable=True)
    total_cost = Column(DEC_MONEY, nullable=True)

    remark = Column(String(255), nullable=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("transfer_id", "line_no", name="uq_transfer_line_no"),
        CheckConstraint("qty_base > 0", name="ck_transfer_qty_gt0"),
    )


class PhStockCount(Base, TimestampMixin, SoftDeleteMixin, AuditActorMixin):
    """
    Cycle/annual stock count with freeze + variance posting.
    """
    __tablename__ = "ph_stock_counts"

    id = Column(Integer, primary_key=True)
    count_no = Column(String(40), nullable=False, unique=True, index=True)
    status = Column(Enum(DocStatus, native_enum=False),
                    nullable=False,
                    default=DocStatus.draft)

    store_id = Column(Integer,
                      ForeignKey("ph_stores.id"),
                      nullable=False,
                      index=True)
    store = relationship("PhStore")

    count_at = Column(DateTime, nullable=False, default=utcnow)
    count_type = Column(String(30), nullable=False,
                        default="cycle")  # cycle/annual/spot
    freeze_stock = Column(Boolean, nullable=False, default=False)

    note = Column(Text, nullable=True)

    posted_at = Column(DateTime, nullable=True)
    posted_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    meta = Column(JSON, nullable=True)

    lines = relationship("PhStockCountLine",
                         back_populates="count",
                         cascade="all, delete-orphan")


class PhStockCountLine(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_stock_count_lines"

    id = Column(Integer, primary_key=True)
    count_id = Column(Integer,
                      ForeignKey("ph_stock_counts.id", ondelete="CASCADE"),
                      nullable=False,
                      index=True)
    count = relationship("PhStockCount", back_populates="lines")

    line_no = Column(Integer, nullable=False, default=1)

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    batch_id = Column(Integer,
                      ForeignKey("ph_batches.id"),
                      nullable=True,
                      index=True)
    batch = relationship("PhBatch")

    system_qty_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    counted_qty_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    variance_qty_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))

    unit_cost_per_base = Column(DEC_MONEY, nullable=True)
    variance_cost = Column(DEC_MONEY, nullable=True)

    remark = Column(String(255), nullable=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (UniqueConstraint("count_id",
                                       "line_no",
                                       name="uq_stock_count_line_no"), )


# ============================================================
# 6) DISPENSE / ISSUE (OP/IP/ER/OT) + INSURANCE SPLIT
# ============================================================
class PhDispense(Base, TimestampMixin, SoftDeleteMixin, AuditActorMixin):
    __tablename__ = "ph_dispenses"

    id = Column(Integer, primary_key=True)
    dispense_no = Column(String(40), nullable=False, unique=True, index=True)
    status = Column(Enum(DocStatus, native_enum=False),
                    nullable=False,
                    default=DocStatus.draft)

    dispense_type = Column(Enum(DispenseType, native_enum=False),
                           nullable=False,
                           default=DispenseType.MANUAL)

    store_id = Column(Integer,
                      ForeignKey("ph_stores.id"),
                      nullable=False,
                      index=True)
    store = relationship("PhStore")

    dispensed_at = Column(DateTime, nullable=False, default=utcnow)

    # Link to clinical context (adjust FK targets to your project)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=True,
                        index=True)
    encounter_id = Column(Integer,
                          ForeignKey("opd_visits.id"),
                          nullable=True,
                          index=True)  # OP/ER encounter
    admission_id = Column(Integer,
                          ForeignKey("ipd_admissions.id"),
                          nullable=True,
                          index=True)  # IP admission
    prescription_id = Column(Integer,
                             ForeignKey("pharmacy_prescriptions.id"),
                             nullable=True,
                             index=True)  # optional
    ordered_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    prescriber_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Billing / insurance
    payer_id = Column(Integer,
                      ForeignKey("ins_payers.id"),
                      nullable=True,
                      index=True)
    plan_id = Column(Integer,
                     ForeignKey("ins_plans.id"),
                     nullable=True,
                     index=True)
    pricing_basis = Column(Enum(PricingBasis, native_enum=False),
                           nullable=False,
                           default=PricingBasis.MRP)

    # Totals
    subtotal = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    discount_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    tax_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    round_off = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    # Claim split totals
    allowed_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    payer_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    patient_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    nonpayable_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    # Posting
    posted_at = Column(DateTime, nullable=True)
    posted_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Controlled override
    override_reason = Column(String(255), nullable=True)
    verified_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_at = Column(DateTime, nullable=True)

    note = Column(Text, nullable=True)
    meta = Column(JSON, nullable=True)

    lines = relationship("PhDispenseLine",
                         back_populates="dispense",
                         cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_dispense_store_dt", "store_id", "dispensed_at"),
        Index("ix_dispense_patient_dt", "patient_id", "dispensed_at"),
    )


class PhDispenseLine(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "ph_dispense_lines"

    id = Column(Integer, primary_key=True)
    dispense_id = Column(Integer,
                         ForeignKey("ph_dispenses.id", ondelete="CASCADE"),
                         nullable=False,
                         index=True)
    dispense = relationship("PhDispense", back_populates="lines")

    line_no = Column(Integer, nullable=False, default=1)

    item_id = Column(Integer,
                     ForeignKey("ph_items.id"),
                     nullable=False,
                     index=True)
    item = relationship("PhItem")

    batch_id = Column(Integer,
                      ForeignKey("ph_batches.id"),
                      nullable=True,
                      index=True)
    batch = relationship("PhBatch")

    # Qty in base + for display
    base_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=True)
    base_uom = relationship("PhUom", foreign_keys=[base_uom_id])
    qty_base = Column(DEC_QTY, nullable=False)

    sale_uom_id = Column(Integer, ForeignKey("ph_uoms.id"), nullable=True)
    sale_uom = relationship("PhUom", foreign_keys=[sale_uom_id])
    qty_sale_uom = Column(DEC_QTY, nullable=True)

    # Pricing snapshot
    mrp = Column(DEC_MONEY2, nullable=True)
    selling_rate = Column(
        DEC_MONEY, nullable=False,
        default=Decimal("0"))  # per sale_uom (or base if you set so)

    discount_percent = Column(Numeric(6, 3),
                              nullable=False,
                              default=Decimal("0"))
    discount_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    tax_percent = Column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    tax_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    line_total = Column(DEC_MONEY, nullable=False, default=Decimal("0"))

    # Cost at issue time (audit)
    unit_cost_per_base = Column(DEC_MONEY, nullable=True)
    total_cost = Column(DEC_MONEY, nullable=True)

    # Insurance split per line
    allowed_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    payer_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    patient_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    nonpayable_amount = Column(DEC_MONEY, nullable=False, default=Decimal("0"))
    denial_reason = Column(String(255), nullable=True)
    prior_auth_ref = Column(String(80), nullable=True)

    # Returns
    is_returned = Column(Boolean, nullable=False, default=False)
    returned_qty_base = Column(DEC_QTY, nullable=False, default=Decimal("0"))
    return_reason = Column(String(255), nullable=True)

    remark = Column(String(255), nullable=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("dispense_id", "line_no", name="uq_dispense_line_no"),
        CheckConstraint("qty_base > 0", name="ck_dispense_qty_gt0"),
        Index("ix_dispense_line_item_batch", "item_id", "batch_id"),
    )


# ============================================================
# 7) AUDIT LOGS (FIELD-LEVEL + DOC ACTIONS)
# ============================================================
class PhAuditLog(Base, TimestampMixin):
    """
    Generic audit trail for compliance:
    - action: CREATE/UPDATE/APPROVE/POST/CANCEL/DELETE_SOFT/LOGIN_OVERRIDE...
    - before/after JSON snapshots (keep short)
    """
    __tablename__ = "ph_audit_logs"

    id = Column(Integer, primary_key=True)

    actor_user_id = Column(Integer,
                           ForeignKey("users.id"),
                           nullable=True,
                           index=True)
    actor_name = Column(String(120), nullable=True)

    action = Column(String(40), nullable=False, index=True)
    entity_type = Column(String(80), nullable=False,
                         index=True)  # table/class name
    entity_id = Column(Integer, nullable=False, index=True)

    ip_address = Column(String(60), nullable=True)
    user_agent = Column(String(255), nullable=True)

    before_json = Column(JSON, nullable=True)
    after_json = Column(JSON, nullable=True)

    note = Column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_ph_audit_entity", "entity_type", "entity_id"),
        Index("ix_ph_audit_actor_dt", "actor_user_id", "created_at"),
    )
