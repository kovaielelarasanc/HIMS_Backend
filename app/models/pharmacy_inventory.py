# FILE: app/models/pharmacy_inventory.py
from __future__ import annotations

import enum
from datetime import datetime, date
from decimal import Decimal
from sqlalchemy import (
    Column, Integer, String, Boolean, Date, DateTime, Numeric,
    ForeignKey, Text, Enum, CheckConstraint, Index, UniqueConstraint
)
from sqlalchemy.orm import relationship

from app.db.base import Base

Money = Numeric(14, 2)
Qty = Numeric(14, 4)


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


# -------------------------
# Masters
# -------------------------
class InventoryLocation(Base):
    __tablename__ = "inv_locations"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(String(500), default="")
    is_pharmacy = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    expiry_alert_days = Column(Integer, default=90, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # ✅ back_populates only (no backref duplicates)
    batches = relationship("ItemBatch", back_populates="location")
    purchase_orders = relationship("PurchaseOrder", back_populates="location")
    grns = relationship("GRN", back_populates="location")
    returns = relationship("ReturnNote", back_populates="location")
    transactions = relationship("StockTransaction", back_populates="location")
    stock = relationship("ItemLocationStock", back_populates="location")


class Supplier(Base):
    __tablename__ = "inv_suppliers"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    contact_person = Column(String(255), default="")
    phone = Column(String(50), default="")
    email = Column(String(255), default="")
    address = Column(String(1000), default="")
    gstin = Column(String(50), default="")
    payment_terms = Column(String(255), default="")  # ✅ helpful for PO UX
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")
    grns = relationship("GRN", back_populates="supplier")
    returns = relationship("ReturnNote", back_populates="supplier")
    price_history = relationship("ItemPriceHistory", back_populates="supplier")


class InventoryItem(Base):
    __tablename__ = "inv_items"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(100), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    generic_name = Column(String(255), default="")
    qr_number = Column(String(50), unique=True, index=True, nullable=True)

    form = Column(String(100), default="")
    strength = Column(String(100), default="")
    unit = Column(String(50), default="unit")
    pack_size = Column(String(50), default="1")
    manufacturer = Column(String(255), default="")
    class_name = Column(String(255), default="")
    atc_code = Column(String(50), default="")
    hsn_code = Column(String(50), default="")

    lasa_flag = Column(Boolean, default=False, nullable=False)
    is_consumable = Column(Boolean, default=False, nullable=False)

    # Defaults = suggestions only (real price comes from GRN batches)
    default_tax_percent = Column(Numeric(5, 2), default=0)
    default_price = Column(Numeric(14, 4), default=0)
    default_mrp = Column(Numeric(14, 4), default=0)

    reorder_level = Column(Numeric(14, 4), default=0)
    max_level = Column(Numeric(14, 4), default=0)

    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    batches = relationship("ItemBatch", back_populates="item")
    po_items = relationship("PurchaseOrderItem", back_populates="item")
    grn_items = relationship("GRNItem", back_populates="item")
    return_items = relationship("ReturnNoteItem", back_populates="item")
    transactions = relationship("StockTransaction", back_populates="item")
    stock = relationship("ItemLocationStock", back_populates="item")
    price_history = relationship("ItemPriceHistory", back_populates="item")


# -------------------------
# Stock (FAST + user-friendly)
# -------------------------
class ItemLocationStock(Base):
    """
    ✅ Fast stock table so UI can show:
    - On-hand per location
    - Low stock suggestions instantly
    - Last purchase price (for PO auto-fill)
    """
    __tablename__ = "inv_item_location_stock"
    __table_args__ = (
        UniqueConstraint("item_id", "location_id", name="uq_inv_item_location_stock"),
        Index("ix_inv_stock_location_item", "location_id", "item_id"),
    )

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    on_hand_qty = Column(Qty, nullable=False, default=Decimal("0"))
    reserved_qty = Column(Qty, nullable=False, default=Decimal("0"))  # optional future use

    last_unit_cost = Column(Numeric(14, 4), nullable=False, default=Decimal("0"))
    last_mrp = Column(Numeric(14, 4), nullable=False, default=Decimal("0"))
    last_tax_percent = Column(Numeric(5, 2), nullable=False, default=Decimal("0"))

    last_grn_id = Column(Integer, nullable=True)
    last_grn_date = Column(Date, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    item = relationship("InventoryItem", back_populates="stock")
    location = relationship("InventoryLocation", back_populates="stock")



class ItemBatch(Base):
    """
    Batch-wise stock per location.
    ✅ expiry_key fixes UNIQUE+NULL expiry issue.
    """
    __tablename__ = "inv_item_batches"
    __table_args__ = (
        UniqueConstraint("item_id", "location_id", "batch_no", "expiry_key", name="uq_inv_batch_unique"),
        Index("ix_inv_batch_item_loc", "item_id", "location_id"),
        Index("ix_inv_batch_loc_exp", "location_id", "expiry_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    batch_no = Column(String(100), nullable=False)
    expiry_date = Column(Date, nullable=True)
    expiry_key = Column(Integer, nullable=False, default=0)  # ✅ 0 when expiry_date is None

    current_qty = Column(Qty, default=0, nullable=False)
    unit_cost = Column(Numeric(14, 4), default=0)
    mrp = Column(Numeric(14, 4), default=0)
    tax_percent = Column(Numeric(5, 2), default=0)

    is_active = Column(Boolean, default=True, nullable=False)
    is_saleable = Column(Boolean, nullable=False, default=True)
    status = Column(
        Enum("ACTIVE", "EXPIRED", "RETURNED", "WRITTEN_OFF", "QUARANTINE", name="inventory_batch_status"),
        nullable=False,
        default="ACTIVE",
    )

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    item = relationship("InventoryItem", back_populates="batches")
    location = relationship("InventoryLocation", back_populates="batches")
    transactions = relationship("StockTransaction", back_populates="batch")


class ItemPriceHistory(Base):
    """
    ✅ Makes PO auto-fill easy:
    - last purchase rate by supplier/location/item
    - audit trail for price changes
    """
    __tablename__ = "inv_item_price_history"
    __table_args__ = (
        Index("ix_inv_price_item_sup_loc", "item_id", "supplier_id", "location_id", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=True, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=True, index=True)

    ref_type = Column(String(30), nullable=False, default="GRN")  # GRN/OPENING/ADJUSTMENT
    ref_id = Column(Integer, nullable=True)

    unit_cost = Column(Numeric(14, 4), nullable=False, default=Decimal("0"))
    mrp = Column(Numeric(14, 4), nullable=False, default=Decimal("0"))
    tax_percent = Column(Numeric(5, 2), nullable=False, default=Decimal("0"))

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    item = relationship("InventoryItem", back_populates="price_history")
    supplier = relationship("Supplier", back_populates="price_history")


# -------------------------
# Safe number generator
# -------------------------
class InvNumberSeries(Base):
    __tablename__ = "inv_number_series"
    __table_args__ = (
        UniqueConstraint("key", "date_key", name="uq_inv_number_series_key_date"),
    )

    id = Column(Integer, primary_key=True)
    key = Column(String(30), nullable=False)         # PO / GRN / RTN etc.
    date_key = Column(Integer, nullable=False)      # YYYYMMDD
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
    )

    id = Column(Integer, primary_key=True)

    po_number = Column(String(50), nullable=False, index=True)

    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    order_date = Column(Date, nullable=False, default=date.today)
    expected_date = Column(Date, nullable=True)

    currency = Column(String(8), nullable=False, default="INR")
    payment_terms = Column(String(255), nullable=False, default="")
    quotation_ref = Column(String(100), nullable=False, default="")
    notes = Column(Text, nullable=False, default="")

    status = Column(Enum(POStatus, name="inv_po_status"), nullable=False, default=POStatus.DRAFT)

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
    )

    id = Column(Integer, primary_key=True)

    po_id = Column(Integer, ForeignKey("inv_purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)

    ordered_qty = Column(Qty, nullable=False, default=Decimal("0"))
    received_qty = Column(Qty, nullable=False, default=Decimal("0"))

    unit_cost = Column(Numeric(14, 4), nullable=False, default=Decimal("0"))
    tax_percent = Column(Numeric(5, 2), nullable=False, default=Decimal("0"))
    mrp = Column(Numeric(14, 4), nullable=False, default=Decimal("0"))

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
    )

    id = Column(Integer, primary_key=True, index=True)
    grn_number = Column(String(50), unique=True, nullable=False, index=True)

    po_id = Column(Integer, ForeignKey("inv_purchase_orders.id"), nullable=True, index=True)
    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    received_date = Column(Date, nullable=False, default=date.today)

    invoice_number = Column(String(100), nullable=False, default="")
    invoice_date = Column(Date, nullable=True)

    supplier_invoice_amount = Column(Money, nullable=False, default=0)

    taxable_amount = Column(Money, nullable=False, default=0)
    cgst_amount = Column(Money, nullable=False, default=0)
    sgst_amount = Column(Money, nullable=False, default=0)
    igst_amount = Column(Money, nullable=False, default=0)

    discount_amount = Column(Money, nullable=False, default=0)
    freight_amount = Column(Money, nullable=False, default=0)
    other_charges = Column(Money, nullable=False, default=0)
    round_off = Column(Money, nullable=False, default=0)

    calculated_grn_amount = Column(Money, nullable=False, default=0)
    amount_difference = Column(Money, nullable=False, default=0)
    difference_reason = Column(String(255), nullable=False, default="")

    status = Column(Enum(GRNStatus, name="inv_grn_status"), nullable=False, default=GRNStatus.DRAFT)
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
    )

    id = Column(Integer, primary_key=True, index=True)
    grn_id = Column(Integer, ForeignKey("inv_grns.id"), nullable=False, index=True)

    po_item_id = Column(Integer, ForeignKey("inv_purchase_order_items.id"), nullable=True, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)

    batch_no = Column(String(100), nullable=False, index=True)
    expiry_date = Column(Date, nullable=True, index=True)

    quantity = Column(Qty, nullable=False, default=0)
    free_quantity = Column(Qty, nullable=False, default=0)

    unit_cost = Column(Qty, nullable=False, default=0)
    mrp = Column(Qty, nullable=False, default=0)

    discount_percent = Column(Numeric(5, 2), nullable=False, default=0)
    discount_amount = Column(Money, nullable=False, default=0)

    tax_percent = Column(Numeric(5, 2), nullable=False, default=0)
    cgst_percent = Column(Numeric(5, 2), nullable=False, default=0)
    sgst_percent = Column(Numeric(5, 2), nullable=False, default=0)
    igst_percent = Column(Numeric(5, 2), nullable=False, default=0)

    taxable_amount = Column(Money, nullable=False, default=0)
    cgst_amount = Column(Money, nullable=False, default=0)
    sgst_amount = Column(Money, nullable=False, default=0)
    igst_amount = Column(Money, nullable=False, default=0)

    line_total = Column(Money, nullable=False, default=0)

    scheme = Column(String(100), nullable=False, default="")
    remarks = Column(String(255), nullable=False, default="")

    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True, index=True)

    grn = relationship("GRN", back_populates="items")
    po_item = relationship("PurchaseOrderItem")
    item = relationship("InventoryItem", back_populates="grn_items")
    batch = relationship("ItemBatch")


# -------------------------
# Stock Transactions
# -------------------------
class StockTransaction(Base):
    __tablename__ = "inv_stock_txns"
    __table_args__ = (
        Index("ix_inv_txn_loc_time", "location_id", "txn_time"),
        Index("ix_inv_txn_item_time", "item_id", "txn_time"),
        # optional: Index("ix_inv_txn_patient_time", "patient_id", "txn_time"),
    )

    id = Column(Integer, primary_key=True, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True)

    txn_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    txn_type = Column(String(50), nullable=False)  # GRN / DISPENSE / ADJUSTMENT etc.
    ref_type = Column(String(50), default="")
    ref_id = Column(Integer, nullable=True)

    quantity_change = Column(Qty, nullable=False)  # +IN / -OUT
    unit_cost = Column(Numeric(14, 4), default=0)
    mrp = Column(Numeric(14, 4), default=0)

    remark = Column(String(1000), default="")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # ✅ NEW (NO FK to avoid guessing your table names)
    patient_id = Column(Integer, nullable=True, index=True)
    visit_id = Column(Integer, nullable=True, index=True)

    location = relationship("InventoryLocation", back_populates="transactions")
    item = relationship("InventoryItem", back_populates="transactions")
    batch = relationship("ItemBatch", back_populates="transactions")
    user = relationship("User", backref="inventory_stock_transactions")


# -------------------------
# Returns (keep as you had)
# -------------------------
class ReturnNote(Base):
    __tablename__ = "inv_return_notes"

    id = Column(Integer, primary_key=True, index=True)
    return_number = Column(String(50), unique=True, nullable=False, index=True)

    type = Column(String(20), nullable=False)  # TO_SUPPLIER / FROM_CUSTOMER / INTERNAL

    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False)

    return_date = Column(Date, nullable=False, default=date.today)
    status = Column(String(20), nullable=False, default="DRAFT")
    reason = Column(String(1000), default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    supplier = relationship("Supplier", back_populates="returns")
    location = relationship("InventoryLocation", back_populates="returns")
    created_by = relationship("User", backref="inventory_returns")

    items = relationship("ReturnNoteItem", back_populates="return_note", cascade="all, delete-orphan")


class ReturnNoteItem(Base):
    __tablename__ = "inv_return_note_items"

    id = Column(Integer, primary_key=True, index=True)
    return_id = Column(Integer, ForeignKey("inv_return_notes.id"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True)

    quantity = Column(Qty, nullable=False, default=0)
    reason = Column(String(1000), default="")

    return_note = relationship("ReturnNote", back_populates="items")
    item = relationship("InventoryItem", back_populates="return_items")
    batch = relationship("ItemBatch")
