# FILE: app/models/pharmacy_inventory.py
from __future__ import annotations
import enum
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Date,
    DateTime,
    Numeric,
    ForeignKey,
    Text,
    Enum,
    CheckConstraint,
    Index
)
from sqlalchemy.orm import relationship

from app.db.base import Base

Money = Numeric(14, 2)  # for invoice/totals
Qty = Numeric(14, 4)    # for quantities/rates

class GRNStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"
    
class InventoryLocation(Base):
    """
    Pharmacy / Store location
    e.g., Main Pharmacy, Ward Store, OT Store
    """
    __tablename__ = "inv_locations"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(String(500), default="")
    is_pharmacy = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    expiry_alert_days = Column(Integer, default=90,
                               nullable=False)  # near-expiry window

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    batches = relationship("ItemBatch", back_populates="location")
    purchase_orders = relationship("PurchaseOrder", back_populates="location")
    grns = relationship("GRN", back_populates="location")
    returns = relationship("ReturnNote", back_populates="location")
    transactions = relationship("StockTransaction", back_populates="location")


class Supplier(Base):
    """
    Vendor / supplier master
    """
    __tablename__ = "inv_suppliers"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    contact_person = Column(String(255), default="")
    phone = Column(String(50), default="")
    email = Column(String(255), default="")
    address = Column(String(1000), default="")
    gstin = Column(String(50), default="")
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")
    grns = relationship("GRN", back_populates="supplier")
    returns = relationship("ReturnNote", back_populates="supplier")


class InventoryItem(Base):
    """
    Pharmacy item master (medicines + consumables)
    """
    __tablename__ = "inv_items"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(100), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)  # Brand name
    generic_name = Column(String(255), default="")
    qr_number = Column(String(50), unique=True, index=True, nullable=True)
    form = Column(
        String(100),
        default="")  # tablet / capsule / syrup / injection / consumable etc
    strength = Column(String(100), default="")
    unit = Column(String(50), default="unit")  # tablet, ml, vial etc
    pack_size = Column(String(50), default="1")
    manufacturer = Column(String(255), default="")
    class_name = Column(String(255), default="")  # therapeutic class
    atc_code = Column(String(50), default="")
    hsn_code = Column(String(50), default="")

    lasa_flag = Column(Boolean, default=False,
                       nullable=False)  # Look-Alike / Sound-Alike
    is_consumable = Column(Boolean, default=False, nullable=False)

    default_tax_percent = Column(Numeric(5, 2), default=0)
    default_price = Column(Numeric(14, 4), default=0)  # default purchase rate
    default_mrp = Column(Numeric(14, 4), default=0)

    reorder_level = Column(Numeric(14, 4),
                           default=0)  # low-stock alert threshold
    max_level = Column(Numeric(14, 4), default=0)  # over-stock alert threshold

    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    batches = relationship("ItemBatch", back_populates="item")
    po_items = relationship("PurchaseOrderItem", back_populates="item")
    grn_items = relationship("GRNItem", back_populates="item")
    return_items = relationship("ReturnNoteItem", back_populates="item")
    transactions = relationship("StockTransaction", back_populates="item")


class ItemBatch(Base):
    """
    Batch-wise stock per location
    """
    __tablename__ = "inv_item_batches"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer,
                     ForeignKey("inv_items.id"),
                     nullable=False,
                     index=True)
    location_id = Column(Integer,
                         ForeignKey("inv_locations.id"),
                         nullable=False,
                         index=True)

    batch_no = Column(String(100), nullable=False)
    expiry_date = Column(Date, nullable=True)

    current_qty = Column(Numeric(14, 4), default=0, nullable=False)
    unit_cost = Column(Numeric(14, 4), default=0)  # last purchase cost
    mrp = Column(Numeric(14, 4), default=0)
    tax_percent = Column(Numeric(5, 2), default=0)

    is_active = Column(Boolean, default=True, nullable=False)
    is_saleable = Column(Boolean, nullable=False, default=True)  # exclude from dispensing
    status = Column(
        Enum(
            "ACTIVE",
            "EXPIRED",
            "RETURNED",
            "WRITTEN_OFF",
            "QUARANTINE",
            name="inventory_batch_status",
        ),
        nullable=False,
        default="ACTIVE",
    )

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    item = relationship("InventoryItem", back_populates="batches")
    location = relationship("InventoryLocation", back_populates="batches")
    transactions = relationship("StockTransaction", back_populates="batch")


class PurchaseOrder(Base):
    """
    Purchase Order header
    """
    __tablename__ = "inv_purchase_orders"

    id = Column(Integer, primary_key=True, index=True)
    po_number = Column(String(50), unique=True, nullable=False, index=True)

    supplier_id = Column(Integer,
                         ForeignKey("inv_suppliers.id"),
                         nullable=False)
    location_id = Column(Integer,
                         ForeignKey("inv_locations.id"),
                         nullable=False)

    order_date = Column(Date, nullable=False, default=date.today)
    expected_date = Column(Date, nullable=True)

    status = Column(
        String(20), nullable=False, default="DRAFT"
    )  # DRAFT/SENT/PARTIALLY_RECEIVED/COMPLETED/CANCELLED/CLOSED

    notes = Column(String(1000), default="")

    email_sent_to = Column(String(255), default="")
    email_sent_at = Column(DateTime, nullable=True)

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    supplier = relationship("Supplier", back_populates="purchase_orders")
    location = relationship("InventoryLocation",
                            back_populates="purchase_orders")
    created_by = relationship("User", backref="inventory_purchase_orders")

    items = relationship("PurchaseOrderItem",
                         back_populates="purchase_order",
                         cascade="all, delete-orphan")
    grns = relationship("GRN", back_populates="purchase_order")


class PurchaseOrderItem(Base):
    """
    Purchase Order line items
    """
    __tablename__ = "inv_purchase_order_items"

    id = Column(Integer, primary_key=True, index=True)
    po_id = Column(Integer,
                   ForeignKey("inv_purchase_orders.id"),
                   nullable=False,
                   index=True)
    item_id = Column(Integer,
                     ForeignKey("inv_items.id"),
                     nullable=False,
                     index=True)

    ordered_qty = Column(Numeric(14, 4), nullable=False, default=0)
    received_qty = Column(Numeric(14, 4), nullable=False, default=0)

    unit_cost = Column(Numeric(14, 4), default=0)
    tax_percent = Column(Numeric(5, 2), default=0)
    mrp = Column(Numeric(14, 4), default=0)

    line_total = Column(Numeric(14, 4), default=0)

    purchase_order = relationship("PurchaseOrder", back_populates="items")
    item = relationship("InventoryItem", back_populates="po_items")


class GRN(Base):
    """
    Goods Receipt Note header
    """
    __tablename__ = "inv_grns"

    id = Column(Integer, primary_key=True, index=True)
    grn_number = Column(String(50), unique=True, nullable=False, index=True)

    po_id = Column(Integer, ForeignKey("inv_purchase_orders.id"), nullable=True, index=True)
    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    received_date = Column(Date, nullable=False, default=date.today)

    # Supplier Invoice
    invoice_number = Column(String(100), nullable=False, default="")
    invoice_date = Column(Date, nullable=True)

    # ✅ Supplier Net Payable (what supplier bill says)
    supplier_invoice_amount = Column(Money, nullable=False, default=0)

    # ✅ System calculated breakup (good for audit & mismatch warning)
    taxable_amount = Column(Money, nullable=False, default=0)
    cgst_amount = Column(Money, nullable=False, default=0)
    sgst_amount = Column(Money, nullable=False, default=0)
    igst_amount = Column(Money, nullable=False, default=0)

    discount_amount = Column(Money, nullable=False, default=0)   # total discount
    freight_amount = Column(Money, nullable=False, default=0)    # transport
    other_charges = Column(Money, nullable=False, default=0)     # packing/handling/etc
    round_off = Column(Money, nullable=False, default=0)

    calculated_grn_amount = Column(Money, nullable=False, default=0)
    amount_difference = Column(Money, nullable=False, default=0)
    difference_reason = Column(String(255), nullable=False, default="")

    status = Column(String(20), nullable=False, default=GRNStatus.DRAFT.value)  # DRAFT/POSTED/CANCELLED

    notes = Column(String(1000), nullable=False, default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    posted_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    posted_at = Column(DateTime, nullable=True)
    cancelled_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String(255), nullable=False, default="")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # relationships
    purchase_order = relationship("PurchaseOrder", back_populates="grns")
    supplier = relationship("Supplier", back_populates="grns")
    location = relationship("InventoryLocation", back_populates="grns")
    created_by = relationship("User", foreign_keys=[created_by_id], backref="inventory_grns_created")
    posted_by = relationship("User", foreign_keys=[posted_by_id], backref="inventory_grns_posted")
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id], backref="inventory_grns_cancelled")

    items = relationship("GRNItem", back_populates="grn", cascade="all, delete-orphan")

    __table_args__ = (
        # basic sanity checks
        CheckConstraint("supplier_invoice_amount >= 0", name="ck_grn_supplier_invoice_amount_nonneg"),
        CheckConstraint("taxable_amount >= 0", name="ck_grn_taxable_amount_nonneg"),
        CheckConstraint("cgst_amount >= 0", name="ck_grn_cgst_nonneg"),
        CheckConstraint("sgst_amount >= 0", name="ck_grn_sgst_nonneg"),
        CheckConstraint("igst_amount >= 0", name="ck_grn_igst_nonneg"),
        CheckConstraint("discount_amount >= 0", name="ck_grn_discount_nonneg"),
        CheckConstraint("freight_amount >= 0", name="ck_grn_freight_nonneg"),
        CheckConstraint("other_charges >= 0", name="ck_grn_other_charges_nonneg"),
        Index("ix_inv_grns_supplier_invoice", "supplier_id", "invoice_number"),
    )


class GRNItem(Base):
    """
    GRN line item, batch-wise
    """
    __tablename__ = "inv_grn_items"

    id = Column(Integer, primary_key=True, index=True)
    grn_id = Column(Integer, ForeignKey("inv_grns.id"), nullable=False, index=True)

    po_item_id = Column(Integer, ForeignKey("inv_purchase_order_items.id"), nullable=True, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)

    batch_no = Column(String(100), nullable=False, index=True)
    expiry_date = Column(Date, nullable=True, index=True)

    quantity = Column(Qty, nullable=False, default=0)
    free_quantity = Column(Qty, nullable=False, default=0)

    unit_cost = Column(Qty, nullable=False, default=0)  # purchase rate
    mrp = Column(Qty, nullable=False, default=0)

    # ✅ Discount support (common in pharmacy invoices)
    discount_percent = Column(Numeric(5, 2), nullable=False, default=0)
    discount_amount = Column(Money, nullable=False, default=0)

    # ✅ Tax split
    tax_percent = Column(Numeric(5, 2), nullable=False, default=0)
    cgst_percent = Column(Numeric(5, 2), nullable=False, default=0)
    sgst_percent = Column(Numeric(5, 2), nullable=False, default=0)
    igst_percent = Column(Numeric(5, 2), nullable=False, default=0)

    taxable_amount = Column(Money, nullable=False, default=0)
    cgst_amount = Column(Money, nullable=False, default=0)
    sgst_amount = Column(Money, nullable=False, default=0)
    igst_amount = Column(Money, nullable=False, default=0)

    line_total = Column(Money, nullable=False, default=0)  # final line total

    # optional: allow per-item notes / scheme refs
    scheme = Column(String(100), nullable=False, default="")   # e.g., "10+1"
    remarks = Column(String(255), nullable=False, default="")

    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True, index=True)

    grn = relationship("GRN", back_populates="items")
    po_item = relationship("PurchaseOrderItem")
    item = relationship("InventoryItem", back_populates="grn_items")
    batch = relationship("ItemBatch")

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_grn_item_qty_nonneg"),
        CheckConstraint("free_quantity >= 0", name="ck_grn_item_free_qty_nonneg"),
        CheckConstraint("unit_cost >= 0", name="ck_grn_item_unit_cost_nonneg"),
        CheckConstraint("mrp >= 0", name="ck_grn_item_mrp_nonneg"),
        CheckConstraint("discount_percent >= 0", name="ck_grn_item_disc_pct_nonneg"),
        CheckConstraint("discount_amount >= 0", name="ck_grn_item_disc_amt_nonneg"),
        CheckConstraint("taxable_amount >= 0", name="ck_grn_item_taxable_nonneg"),
        Index("ix_inv_grn_items_grn_item_batch", "grn_id", "item_id", "batch_no"),
    )


class ReturnNote(Base):
    """
    Returns: to supplier / from customer / internal
    """
    __tablename__ = "inv_return_notes"

    id = Column(Integer, primary_key=True, index=True)
    return_number = Column(String(50), unique=True, nullable=False, index=True)

    type = Column(String(20),
                  nullable=False)  # TO_SUPPLIER / FROM_CUSTOMER / INTERNAL

    supplier_id = Column(Integer,
                         ForeignKey("inv_suppliers.id"),
                         nullable=True)
    location_id = Column(Integer,
                         ForeignKey("inv_locations.id"),
                         nullable=False)

    return_date = Column(Date, nullable=False, default=date.today)

    status = Column(String(20), nullable=False,
                    default="DRAFT")  # DRAFT/POSTED/CANCELLED

    reason = Column(String(1000), default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    supplier = relationship("Supplier", back_populates="returns")
    location = relationship("InventoryLocation", back_populates="returns")
    created_by = relationship("User", backref="inventory_returns")

    items = relationship("ReturnNoteItem",
                         back_populates="return_note",
                         cascade="all, delete-orphan")


class ReturnNoteItem(Base):
    __tablename__ = "inv_return_note_items"

    id = Column(Integer, primary_key=True, index=True)
    return_id = Column(Integer,
                       ForeignKey("inv_return_notes.id"),
                       nullable=False,
                       index=True)
    item_id = Column(Integer,
                     ForeignKey("inv_items.id"),
                     nullable=False,
                     index=True)
    batch_id = Column(Integer,
                      ForeignKey("inv_item_batches.id"),
                      nullable=True)

    quantity = Column(Numeric(14, 4), nullable=False, default=0)
    reason = Column(String(1000), default="")

    return_note = relationship("ReturnNote", back_populates="items")
    item = relationship("InventoryItem", back_populates="return_items")
    batch = relationship("ItemBatch")


class StockTransaction(Base):
    """
    Full audit trail of stock movements.

    txn_type examples:
    - OPENING
    - GRN
    - DISPENSE
    - ADJUSTMENT
    - RETURN_TO_SUPPLIER
    - RETURN_FROM_CUSTOMER
    - TRANSFER_IN
    - TRANSFER_OUT
    """
    __tablename__ = "inv_stock_txns"

    id = Column(Integer, primary_key=True, index=True)
    location_id = Column(Integer,
                         ForeignKey("inv_locations.id"),
                         nullable=False,
                         index=True)
    item_id = Column(Integer,
                     ForeignKey("inv_items.id"),
                     nullable=False,
                     index=True)
    batch_id = Column(Integer,
                      ForeignKey("inv_item_batches.id"),
                      nullable=True)

    txn_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    txn_type = Column(String(50), nullable=False)
    ref_type = Column(String(50),
                      default="")  # e.g. "GRN", "PO", "RETURN", "DISPENSE"
    ref_id = Column(Integer, nullable=True)

    quantity_change = Column(Numeric(14, 4),
                             nullable=False)  # + for IN, - for OUT
    unit_cost = Column(Numeric(14, 4), default=0)
    mrp = Column(Numeric(14, 4), default=0)

    remark = Column(String(1000), default="")

    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    patient_id = Column(Integer, nullable=True)  # do NOT FK to avoid coupling
    visit_id = Column(Integer, nullable=True)

    location = relationship("InventoryLocation", back_populates="transactions")
    item = relationship("InventoryItem", back_populates="transactions")
    batch = relationship("ItemBatch", back_populates="transactions")
    user = relationship("User", backref="inventory_stock_transactions")
