from __future__ import annotations

import enum
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    Date,
    DateTime,
    Numeric,
    ForeignKey,
    Text,
    Enum as SAEnum,
    CheckConstraint,
    Index,
    UniqueConstraint,
    func
)
from sqlalchemy.orm import relationship, synonym
from sqlalchemy.types import JSON

from app.db.base import Base

# -------------------------
# Common numeric types
# -------------------------
Money = Numeric(14, 2)
Qty = Numeric(14, 4)
Rate = Numeric(14, 4)
Pct = Numeric(5, 2)

MYSQL_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}

# -------------------------
# Enums
# -------------------------
class GRNStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"


class POStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SENT = "SENT"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class BatchStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RETURNED = "RETURNED"
    WRITTEN_OFF = "WRITTEN_OFF"
    QUARANTINE = "QUARANTINE"


class ReturnKind(str, enum.Enum):
    RETURN = "RETURN"
    WASTAGE = "WASTAGE"
    WRITE_OFF = "WRITE_OFF"
    RECALL = "RECALL"


# -------------------------
# Masters
# -------------------------
class InventoryLocation(Base):
    __tablename__ = "inv_locations"
    __table_args__ = (
        UniqueConstraint("code", name="uq_inv_locations_code"),
        Index("ix_inv_locations_active", "is_active"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(String(500), default="", nullable=False)

    is_pharmacy = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    expiry_alert_days = Column(Integer, default=90, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # relationships
    batches = relationship("ItemBatch", back_populates="location")
    purchase_orders = relationship("PurchaseOrder", back_populates="location")
    grns = relationship("GRN", back_populates="location")
    returns = relationship("ReturnNote", back_populates="location")
    transactions = relationship("StockTransaction", back_populates="location")
    stock = relationship("ItemLocationStock", back_populates="location")


class Supplier(Base):
    __tablename__ = "inv_suppliers"
    __table_args__ = (
        UniqueConstraint("code", name="uq_inv_suppliers_code"),
        Index("ix_inv_suppliers_active", "is_active"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)

    code = Column(String(50), nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)

    contact_person = Column(String(255), default="", nullable=False)
    phone = Column(String(50), default="", nullable=False, index=True)
    email = Column(String(255), default="", nullable=False, index=True)
    address = Column(String(1000), default="", nullable=False)

    # Store in DB as gstin, expose gst_number for FE compatibility
    gstin = Column(String(50), default="", nullable=False, index=True)
    gst_number = synonym("gstin")

    payment_terms = Column(String(255), default="", nullable=False)

    # Payment details
    payment_method = Column(String(30), default="UPI", nullable=False)  # UPI / BANK_TRANSFER / CASH / CHEQUE / OTHER
    upi_id = Column(String(120), nullable=True)

    bank_account_name = Column(String(255), nullable=True)
    bank_account_number = Column(String(50), nullable=True)
    bank_ifsc = Column(String(20), nullable=True)
    bank_name = Column(String(255), nullable=True)
    bank_branch = Column(String(255), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")
    grns = relationship("GRN", back_populates="supplier")
    returns = relationship("ReturnNote", back_populates="supplier")
    price_history = relationship("ItemPriceHistory", back_populates="supplier")


class InventoryItem(Base):
    __tablename__ = "inv_items"
    __table_args__ = (
        UniqueConstraint("code", name="uq_inv_items_code"),
        UniqueConstraint("qr_number", name="uq_inv_items_qr_number"),
        Index("ix_inv_items_type_consumable", "item_type", "is_consumable"),
        Index("ix_inv_items_active", "is_active"),
        Index("ix_inv_items_lasa", "lasa_flag"),
        Index("ix_inv_items_high_alert", "high_alert_flag"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)

    # identity
    code = Column(String(100), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    qr_number = Column(String(50), nullable=True, index=True)

    # classification
    item_type = Column(String(20), nullable=False, default="DRUG", index=True)  # DRUG | CONSUMABLE | EQUIPMENT
    is_consumable = Column(Boolean, default=False, nullable=False, index=True)

    # flags
    lasa_flag = Column(Boolean, default=False, nullable=False, index=True)
    high_alert_flag = Column(Boolean, default=False, nullable=False, index=True)
    requires_double_check = Column(Boolean, default=False, nullable=False, index=True)

    # stock metadata
    unit = Column(String(50), default="unit", nullable=False)  # display
    pack_size = Column(String(50), default="1", nullable=False)

    # ✅ UOM conversion (recommended)
    base_uom = Column(String(30), default="unit", nullable=False)
    purchase_uom = Column(String(30), default="unit", nullable=False)
    conversion_factor = Column(Numeric(14, 6), default=Decimal("1"), nullable=False)

    reorder_level = Column(Qty, default=Decimal("0"), nullable=False)
    max_level = Column(Qty, default=Decimal("0"), nullable=False)

    # supplier / procurement
    manufacturer = Column(String(255), default="", nullable=False)
    default_supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=True, index=True)
    procurement_date = Column(Date, nullable=True)

    # storage
    storage_condition = Column(String(30), default="ROOM_TEMP", nullable=False)

    # defaults (suggestions only)
    default_tax_percent = Column(Pct, default=Decimal("0"), nullable=False)
    default_price = Column(Rate, default=Decimal("0"), nullable=False)
    default_mrp = Column(Rate, default=Decimal("0"), nullable=False)

    # Regulatory schedule
    schedule_system = Column(String(20), default="IN_DCA", nullable=False, index=True)
    schedule_code = Column(String(10), default="", nullable=False, index=True)
    schedule_notes = Column(String(255), default="", nullable=False)

    # DRUG fields
    generic_name = Column(String(255), default="", nullable=False)
    brand_name = Column(String(255), default="", nullable=False)
    dosage_form = Column(String(100), default="", nullable=False)
    strength = Column(String(100), default="", nullable=False)
    active_ingredients = Column(JSON, nullable=True)
    route = Column(String(50), default="", nullable=False)
    therapeutic_class = Column(String(255), default="", nullable=False)
    prescription_status = Column(String(20), default="RX", nullable=False)  # OTC | RX | SCHEDULED
    side_effects = Column(Text, default="", nullable=False)
    drug_interactions = Column(Text, default="", nullable=False)

    # CONSUMABLE fields
    material_type = Column(String(100), default="", nullable=False)
    sterility_status = Column(String(20), default="", nullable=False)  # STERILE / NON_STERILE
    size_dimensions = Column(String(120), default="", nullable=False)
    intended_use = Column(Text, default="", nullable=False)
    reusable_status = Column(String(20), default="", nullable=False)  # DISPOSABLE / REUSABLE

    # codes
    atc_code = Column(String(50), default="", nullable=False)
    hsn_code = Column(String(50), default="", nullable=False)

    is_active = Column(Boolean, default=True, nullable=False, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # relationships
    batches = relationship("ItemBatch", back_populates="item")
    po_items = relationship("PurchaseOrderItem", back_populates="item")
    grn_items = relationship("GRNItem", back_populates="item")
    return_items = relationship("ReturnNoteItem", back_populates="item")
    transactions = relationship("StockTransaction", back_populates="item")
    stock = relationship("ItemLocationStock", back_populates="item")
    price_history = relationship("ItemPriceHistory", back_populates="item")

    supplier = relationship("Supplier", foreign_keys=[default_supplier_id])


# -------------------------
# Stock fast table
# -------------------------
class ItemLocationStock(Base):
    __tablename__ = "inv_item_location_stock"
    __table_args__ = (
        UniqueConstraint("item_id", "location_id", name="uq_inv_item_location_stock"),
        Index("ix_inv_stock_location_item", "location_id", "item_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    on_hand_qty = Column(Qty, nullable=False, default=Decimal("0"))
    reserved_qty = Column(Qty, nullable=False, default=Decimal("0"))

    last_unit_cost = Column(Rate, nullable=False, default=Decimal("0"))
    last_mrp = Column(Rate, nullable=False, default=Decimal("0"))
    last_tax_percent = Column(Pct, nullable=False, default=Decimal("0"))

    last_grn_id = Column(Integer, nullable=True)
    last_grn_date = Column(Date, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    item = relationship("InventoryItem", back_populates="stock")
    location = relationship("InventoryLocation", back_populates="stock")


class ItemBatch(Base):
    """
    Batch-wise stock per location.
    expiry_key fixes UNIQUE+NULL expiry issue (use 0 when expiry_date is NULL).
    """
    __tablename__ = "inv_item_batches"
    __table_args__ = (
        UniqueConstraint("item_id", "location_id", "batch_no", "expiry_key", name="uq_inv_batch_unique"),
        Index("ix_inv_batch_item_loc", "item_id", "location_id"),
        Index("ix_inv_batch_loc_exp", "location_id", "expiry_date"),
        Index("ix_inv_batch_status", "status"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    batch_no = Column(String(100), nullable=False)
    mfg_date = Column(Date, nullable=True, index=True)

    expiry_date = Column(Date, nullable=True, index=True)
    expiry_key = Column(Integer, nullable=False, default=0)

    current_qty = Column(Qty, default=Decimal("0"), nullable=False)
    reserved_qty = Column(Qty, default=Decimal("0"), nullable=False)

    unit_cost = Column(Rate, default=Decimal("0"), nullable=False)
    mrp = Column(Rate, default=Decimal("0"), nullable=False)
    tax_percent = Column(Pct, default=Decimal("0"), nullable=False)

    is_active = Column(Boolean, default=True, nullable=False)
    is_saleable = Column(Boolean, nullable=False, default=True)

    status = Column(
        SAEnum(BatchStatus, name="inventory_batch_status"),
        nullable=False,
        default=BatchStatus.ACTIVE,
    )

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    item = relationship("InventoryItem", back_populates="batches")
    location = relationship("InventoryLocation", back_populates="batches")
    transactions = relationship("StockTransaction", back_populates="batch")


class ItemPriceHistory(Base):
    __tablename__ = "inv_item_price_history"
    __table_args__ = (
        Index("ix_inv_price_item_sup_loc", "item_id", "supplier_id", "location_id", "created_at"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=True, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=True, index=True)

    ref_type = Column(String(30), nullable=False, default="GRN")
    ref_id = Column(Integer, nullable=True)

    unit_cost = Column(Rate, nullable=False, default=Decimal("0"))
    mrp = Column(Rate, nullable=False, default=Decimal("0"))
    tax_percent = Column(Pct, nullable=False, default=Decimal("0"))

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    item = relationship("InventoryItem", back_populates="price_history")
    supplier = relationship("Supplier", back_populates="price_history")


# -------------------------
# Number series (safe counter)
# -------------------------
class InvNumberSeries(Base):
    __tablename__ = "inv_number_series"
    __table_args__ = (
        UniqueConstraint("key", "date_key", name="uq_inv_number_series_key_date"),
        Index("ix_inv_number_series_key", "key"),
        Index("ix_inv_number_series_date", "date_key"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    key = Column(String(30), nullable=False)
    date_key = Column(Integer, nullable=False)  # YYYYMMDD
    next_seq = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


# -------------------------
# Purchase Orders
# -------------------------
class PurchaseOrder(Base):
    __tablename__ = "inv_purchase_orders"
    __table_args__ = (
        UniqueConstraint("po_number", name="uq_inv_purchase_orders_po_number"),
        Index("ix_inv_po_supplier_date", "supplier_id", "order_date"),
        Index("ix_inv_po_location_date", "location_id", "order_date"),
        Index("ix_inv_po_status_date", "status", "order_date"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    po_number = Column(String(64), nullable=False, index=True)

    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    order_date = Column(Date, nullable=False, default=date.today)
    expected_date = Column(Date, nullable=True)

    currency = Column(String(8), nullable=False, default="INR")
    payment_terms = Column(String(255), nullable=False, default="")
    quotation_ref = Column(String(100), nullable=False, default="")
    notes = Column(Text, nullable=False, default="")

    status = Column(SAEnum(POStatus, name="inv_po_status"), nullable=False, default=POStatus.DRAFT)

    sub_total = Column(Money, nullable=False, default=Decimal("0.00"))
    tax_total = Column(Money, nullable=False, default=Decimal("0.00"))
    grand_total = Column(Money, nullable=False, default=Decimal("0.00"))

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    approved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    approved_at = Column(DateTime, nullable=True)

    cancelled_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String(255), nullable=False, default="")

    email_sent_to = Column(String(255), nullable=False, default="")
    email_sent_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    supplier = relationship("Supplier", back_populates="purchase_orders")
    location = relationship("InventoryLocation", back_populates="purchase_orders")
    created_by = relationship("User", foreign_keys=[created_by_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])

    items = relationship("PurchaseOrderItem", back_populates="purchase_order", cascade="all, delete-orphan")
    grns = relationship("GRN", back_populates="purchase_order")


class PurchaseOrderItem(Base):
    __tablename__ = "inv_purchase_order_items"
    __table_args__ = (
        UniqueConstraint("po_id", "item_id", name="uq_inv_po_items_po_item"),
        Index("ix_inv_po_items_po", "po_id"),
        Index("ix_inv_po_items_item", "item_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)

    po_id = Column(Integer, ForeignKey("inv_purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)

    ordered_qty = Column(Qty, nullable=False, default=Decimal("0"))
    received_qty = Column(Qty, nullable=False, default=Decimal("0"))

    unit_cost = Column(Rate, nullable=False, default=Decimal("0"))
    tax_percent = Column(Pct, nullable=False, default=Decimal("0"))
    mrp = Column(Rate, nullable=False, default=Decimal("0"))

    line_sub_total = Column(Money, nullable=False, default=Decimal("0.00"))
    line_tax_total = Column(Money, nullable=False, default=Decimal("0.00"))
    line_total = Column(Money, nullable=False, default=Decimal("0.00"))

    remarks = Column(String(255), nullable=False, default="")

    purchase_order = relationship("PurchaseOrder", back_populates="items")
    item = relationship("InventoryItem", back_populates="po_items")


# -------------------------
# GRN
# -------------------------
class GRN(Base):
    __tablename__ = "inv_grns"
    __table_args__ = (
        Index("ix_inv_grns_supplier_invoice", "supplier_id", "invoice_number"),
        Index("ix_inv_grns_po", "po_id"),
        Index("ix_inv_grns_status_date", "status", "received_date"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    grn_number = Column(String(64), unique=True, nullable=False, index=True)

    po_id = Column(Integer, ForeignKey("inv_purchase_orders.id"), nullable=True, index=True)
    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    received_date = Column(Date, nullable=False, default=date.today)

    invoice_number = Column(String(100), nullable=False, default="")
    invoice_date = Column(Date, nullable=True)

    supplier_invoice_amount = Column(Money, nullable=False, default=Decimal("0.00"))

    taxable_amount = Column(Money, nullable=False, default=Decimal("0.00"))
    cgst_amount = Column(Money, nullable=False, default=Decimal("0.00"))
    sgst_amount = Column(Money, nullable=False, default=Decimal("0.00"))
    igst_amount = Column(Money, nullable=False, default=Decimal("0.00"))

    discount_amount = Column(Money, nullable=False, default=Decimal("0.00"))
    freight_amount = Column(Money, nullable=False, default=Decimal("0.00"))
    other_charges = Column(Money, nullable=False, default=Decimal("0.00"))
    round_off = Column(Money, nullable=False, default=Decimal("0.00"))

    calculated_grn_amount = Column(Money, nullable=False, default=Decimal("0.00"))
    amount_difference = Column(Money, nullable=False, default=Decimal("0.00"))
    difference_reason = Column(String(255), nullable=False, default="")

    status = Column(SAEnum(GRNStatus, name="inv_grn_status"), nullable=False, default=GRNStatus.DRAFT)
    notes = Column(String(1000), nullable=False, default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    posted_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    posted_at = Column(DateTime, nullable=True)

    cancelled_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String(255), nullable=False, default="")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    purchase_order = relationship("PurchaseOrder", back_populates="grns")
    supplier = relationship("Supplier", back_populates="grns")
    location = relationship("InventoryLocation", back_populates="grns")

    created_by = relationship("User", foreign_keys=[created_by_id])
    posted_by = relationship("User", foreign_keys=[posted_by_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])

    items = relationship("GRNItem", back_populates="grn", cascade="all, delete-orphan")


class GRNItem(Base):
    __tablename__ = "inv_grn_items"
    __table_args__ = (
        Index("ix_inv_grn_items_grn_item_batch", "grn_id", "item_id", "batch_no"),
        CheckConstraint("quantity >= 0", name="ck_grn_item_qty_nonneg"),
        CheckConstraint("free_quantity >= 0", name="ck_grn_item_free_qty_nonneg"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    grn_id = Column(Integer, ForeignKey("inv_grns.id", ondelete="CASCADE"), nullable=False, index=True)

    po_item_id = Column(Integer, ForeignKey("inv_purchase_order_items.id"), nullable=True, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)

    batch_no = Column(String(100), nullable=False, index=True)
    mfg_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=True, index=True)

    quantity = Column(Qty, nullable=False, default=Decimal("0"))
    free_quantity = Column(Qty, nullable=False, default=Decimal("0"))

    # ✅ FIX: cost & mrp must be price type, NOT Qty
    unit_cost = Column(Rate, nullable=False, default=Decimal("0"))
    mrp = Column(Rate, nullable=False, default=Decimal("0"))

    discount_percent = Column(Pct, nullable=False, default=Decimal("0"))
    discount_amount = Column(Money, nullable=False, default=Decimal("0.00"))

    tax_percent = Column(Pct, nullable=False, default=Decimal("0"))
    cgst_percent = Column(Pct, nullable=False, default=Decimal("0"))
    sgst_percent = Column(Pct, nullable=False, default=Decimal("0"))
    igst_percent = Column(Pct, nullable=False, default=Decimal("0"))

    taxable_amount = Column(Money, nullable=False, default=Decimal("0.00"))
    cgst_amount = Column(Money, nullable=False, default=Decimal("0.00"))
    sgst_amount = Column(Money, nullable=False, default=Decimal("0.00"))
    igst_amount = Column(Money, nullable=False, default=Decimal("0.00"))

    line_total = Column(Money, nullable=False, default=Decimal("0.00"))

    scheme = Column(String(100), nullable=False, default="")
    remarks = Column(String(255), nullable=False, default="")

    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True, index=True)

    grn = relationship("GRN", back_populates="items")
    po_item = relationship("PurchaseOrderItem")
    item = relationship("InventoryItem", back_populates="grn_items")
    batch = relationship("ItemBatch")


class StockTransaction(Base):
    __tablename__ = "inv_stock_txns"
    __table_args__ = (
        Index("ix_inv_txn_loc_time", "location_id", "txn_time"),
        Index("ix_inv_txn_item_time", "item_id", "txn_time"),
        Index("ix_inv_txn_patient_time", "patient_id", "txn_time"),
        Index("ix_inv_txn_ref", "ref_type", "ref_id"),
        Index("ix_inv_txn_ref_line", "ref_line_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)

    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True, index=True)

    txn_time = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Examples: GRN / ISSUE / DISPENSE / SALE / RETURN / ADJUSTMENT / WRITE_OFF
    txn_type = Column(String(50), nullable=False)

    ref_type = Column(String(50), nullable=False, default="")
    ref_id = Column(Integer, nullable=True)
    # ✅ NEW: line-level traceability (sale_item_id / dispense_item_id / grn_item_id / issue_item_id)
    ref_line_id = Column(Integer, nullable=True)

    quantity_change = Column(Qty, nullable=False)  # +IN / -OUT
    unit_cost = Column(Rate, nullable=False, default=Decimal("0"))
    mrp = Column(Rate, nullable=False, default=Decimal("0"))

    remark = Column(String(1000), default="", nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    patient_id = Column(Integer, nullable=True, index=True)
    visit_id = Column(Integer, nullable=True, index=True)
    doctor_id = Column(Integer, nullable=True, index=True)

    location = relationship("InventoryLocation", back_populates="transactions")
    item = relationship("InventoryItem", back_populates="transactions")
    batch = relationship("ItemBatch", back_populates="transactions")
    user = relationship("User", backref="inventory_stock_transactions")


# -------------------------
# Returns / Wastage / Write-off / Recall
# -------------------------
class ReturnNote(Base):
    __tablename__ = "inv_return_notes"
    __table_args__ = (
        UniqueConstraint("return_number", name="uq_inv_return_notes_return_number"),
        Index("ix_inv_return_notes_type_date", "type", "return_date"),
        Index("ix_inv_return_notes_ref", "ref_type", "ref_id"),
        Index("ix_inv_return_notes_kind", "return_kind"),
        Index("ix_inv_return_notes_status", "status"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    return_number = Column(String(64), unique=True, nullable=False, index=True)

    # TO_SUPPLIER / FROM_CUSTOMER / INTERNAL / FROM_WARD / TO_WARD etc (keep flexible)
    type = Column(String(20), nullable=False)

    # ✅ NEW: source linkage
    ref_type = Column(String(30), nullable=False, default="")  # SALE / DISPENSE / INDENT / GRN / OTHER
    ref_id = Column(Integer, nullable=True, index=True)

    # ✅ NEW: return kind
    return_kind = Column(SAEnum(ReturnKind, name="inv_return_kind"), nullable=False, default=ReturnKind.RETURN)

    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=True, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    return_date = Column(Date, nullable=False, default=date.today)
    status = Column(String(20), nullable=False, default="DRAFT")

    reason = Column(String(1000), default="", nullable=False)

    # ✅ approval (for write-off/wastage/recall)
    approved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    approved_at = Column(DateTime, nullable=True)
    writeoff_reason = Column(String(255), nullable=False, default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    supplier = relationship("Supplier", back_populates="returns")
    location = relationship("InventoryLocation", back_populates="returns")
    created_by = relationship("User", foreign_keys=[created_by_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])

    items = relationship("ReturnNoteItem", back_populates="return_note", cascade="all, delete-orphan")


class ReturnNoteItem(Base):
    __tablename__ = "inv_return_note_items"
    __table_args__ = (
        Index("ix_inv_return_note_items_return", "return_id"),
        Index("ix_inv_return_note_items_item", "item_id"),
        Index("ix_inv_return_note_items_batch", "batch_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    return_id = Column(Integer, ForeignKey("inv_return_notes.id", ondelete="CASCADE"), nullable=False, index=True)

    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True, index=True)

    quantity = Column(Qty, nullable=False, default=Decimal("0"))
    reason = Column(String(1000), default="", nullable=False)

    return_note = relationship("ReturnNote", back_populates="items")
    item = relationship("InventoryItem", back_populates="return_items")
    batch = relationship("ItemBatch")


class StockReservation(Base):
    __tablename__ = "inv_stock_reservations"
    __table_args__ = (
        Index("ix_inv_res_item_batch", "item_id", "batch_id"),
        Index("ix_inv_res_loc", "location_id"),
    )

    id = Column(Integer, primary_key=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True, index=True)

    qty_reserved = Column(Qty, nullable=False, default=Decimal("0"))
    ref_type = Column(String(30), nullable=False, default="")  # RX / DISPENSE / SALE / INDENT
    ref_id = Column(Integer, nullable=True, index=True)
    expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())