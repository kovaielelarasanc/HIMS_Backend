# from __future__ import annotations
# from datetime import datetime, date
# from decimal import Decimal
# from sqlalchemy import (Column, Integer, String, DateTime, Date, Text,
#                         ForeignKey, Boolean, Numeric, UniqueConstraint, Index)
# from sqlalchemy.orm import relationship
# from app.db.base import Base

# # -------------------------
# # Masters
# # -------------------------


# class PharmacyLocation(Base):
#     __tablename__ = "ph_locations"
#     id = Column(Integer, primary_key=True)
#     code = Column(String(30), unique=True, nullable=False)
#     name = Column(String(120), nullable=False)
#     is_active = Column(Boolean, default=True)


# class PharmacySupplier(Base):
#     __tablename__ = "ph_suppliers"
#     id = Column(Integer, primary_key=True)
#     name = Column(String(160), nullable=False, unique=True)
#     contact_person = Column(String(120), default="")
#     phone = Column(String(30), default="")
#     email = Column(String(120), default="")
#     gstin = Column(String(30), default="")
#     address = Column(Text, default="")
#     payment_terms = Column(String(120), default="")
#     is_active = Column(Boolean, default=True)


# class PharmacyMedicine(Base):
#     __tablename__ = "ph_medicines"
#     id = Column(Integer, primary_key=True)
#     code = Column(String(60), unique=True, nullable=False)  # SKU
#     name = Column(String(200), nullable=False)  # Brand name
#     generic_name = Column(String(200), default="")
#     form = Column(String(40), nullable=False)  # tablet|injection|syrup|…
#     strength = Column(String(80), default="")  # 500mg, 5mg/ml
#     unit = Column(String(20), default="unit")  # tablet, ml, vial
#     pack_size = Column(Integer, default=1)
#     manufacturer = Column(String(160), default="")
#     class_name = Column(String(120), default="")  # therapeutic class
#     atc_code = Column(String(20), default="")
#     lasa_flag = Column(Boolean, default=False)

#     default_tax_percent = Column(Numeric(5, 2), nullable=True)
#     default_price = Column(Numeric(12, 2),
#                            nullable=True)  # base sell price per unit
#     default_mrp = Column(Numeric(12, 2), nullable=True)
#     reorder_level = Column(Integer, default=0)  # global fallback

#     is_active = Column(Boolean, default=True)

#     __table_args__ = (
#         Index("ix_ph_meds_name", "name"),
#         Index("ix_ph_meds_generic", "generic_name"),
#         Index("ix_ph_meds_form", "form"),
#     )


# # -------------------------
# # Inventory (Lots + Txns)
# # -------------------------


# class PharmacyInventoryLot(Base):
#     __tablename__ = "ph_inventory_lots"
#     id = Column(Integer, primary_key=True)
#     medicine_id = Column(Integer,
#                          ForeignKey("ph_medicines.id"),
#                          index=True,
#                          nullable=False)
#     location_id = Column(Integer,
#                          ForeignKey("ph_locations.id"),
#                          index=True,
#                          nullable=False)
#     batch = Column(String(60), nullable=False)
#     expiry = Column(Date, nullable=False)
#     on_hand = Column(Integer, default=0)  # units

#     unit_cost = Column(Numeric(12, 2), nullable=True)  # per unit
#     sell_price = Column(Numeric(12, 2), nullable=True)  # per unit
#     mrp = Column(Numeric(12, 2), nullable=True)

#     created_at = Column(DateTime, default=datetime.utcnow)

#     __table_args__ = (
#         UniqueConstraint("medicine_id",
#                          "location_id",
#                          "batch",
#                          "expiry",
#                          name="uq_ph_lot"),
#         Index("ix_ph_lot_exp", "expiry"),
#     )


# class PharmacyInventoryTxn(Base):
#     __tablename__ = "ph_inventory_txns"
#     id = Column(Integer, primary_key=True)
#     ts = Column(DateTime, default=datetime.utcnow, index=True)
#     medicine_id = Column(Integer,
#                          ForeignKey("ph_medicines.id"),
#                          index=True,
#                          nullable=False)
#     location_id = Column(Integer,
#                          ForeignKey("ph_locations.id"),
#                          index=True,
#                          nullable=False)
#     lot_id = Column(Integer,
#                     ForeignKey("ph_inventory_lots.id"),
#                     index=True,
#                     nullable=False)
#     type = Column(
#         String(30), nullable=False
#     )  # grn|po_return|dispense|sale_return|adjust_in|adjust_out|transfer_out|transfer_in
#     qty_change = Column(Integer, nullable=False)  # +/- units
#     ref_type = Column(String(30),
#                       nullable=True)  # po|grn|sale|sale_return|adjust|transfer
#     ref_id = Column(Integer, nullable=True)
#     user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
#     note = Column(String(255), default="")


# # -------------------------
# # Procurement (PO/GRN)
# # -------------------------


# class PharmacyPO(Base):
#     __tablename__ = "ph_pos"
#     id = Column(Integer, primary_key=True)

#     supplier_id = Column(Integer,
#                          ForeignKey("ph_suppliers.id"),
#                          nullable=False,
#                          index=True)
#     location_id = Column(Integer,
#                          ForeignKey("ph_locations.id"),
#                          nullable=False,
#                          index=True)

#     status = Column(String(20),
#                     default="draft")  # draft|approved|cancelled|closed

#     created_at = Column(DateTime, default=datetime.utcnow)
#     created_by = Column(Integer, ForeignKey("users.id"),
#                         nullable=True)  # <-- added

#     approved_at = Column(DateTime, nullable=True)
#     approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)

#     cancelled_at = Column(DateTime, nullable=True)  # <-- added
#     cancelled_by = Column(Integer, ForeignKey("users.id"),
#                           nullable=True)  # <-- added


# class PharmacyPOItem(Base):
#     __tablename__ = "ph_po_items"
#     id = Column(Integer, primary_key=True)
#     po_id = Column(Integer,
#                    ForeignKey("ph_pos.id"),
#                    index=True,
#                    nullable=False)
#     medicine_id = Column(Integer,
#                          ForeignKey("ph_medicines.id"),
#                          nullable=False,
#                          index=True)
#     qty = Column(Integer, nullable=False)


# class PharmacyGRN(Base):
#     __tablename__ = "ph_grns"
#     id = Column(Integer, primary_key=True)
#     supplier_id = Column(Integer,
#                          ForeignKey("ph_suppliers.id"),
#                          nullable=False,
#                          index=True)
#     location_id = Column(Integer,
#                          ForeignKey("ph_locations.id"),
#                          nullable=False,
#                          index=True)
#     po_id = Column(Integer, ForeignKey("ph_pos.id"), nullable=True, index=True)
#     received_at = Column(DateTime, default=datetime.utcnow)
#     created_by = Column(Integer, ForeignKey("users.id"), nullable=True)


# class PharmacyGRNItem(Base):
#     __tablename__ = "ph_grn_items"
#     id = Column(Integer, primary_key=True)
#     grn_id = Column(Integer,
#                     ForeignKey("ph_grns.id"),
#                     index=True,
#                     nullable=False)
#     medicine_id = Column(Integer,
#                          ForeignKey("ph_medicines.id"),
#                          nullable=False)
#     batch = Column(String(60), nullable=False)
#     expiry = Column(Date, nullable=False)
#     qty = Column(Integer, nullable=False)
#     unit_cost = Column(Numeric(12, 2), nullable=False)
#     tax_percent = Column(Numeric(5, 2), nullable=True)
#     mrp = Column(Numeric(12, 2), nullable=True)
#     sell_price = Column(Numeric(12, 2), nullable=True)


# # -------------------------
# # Sales / Dispense (OPD/IPD)
# # -------------------------


# class PharmacySale(Base):
#     __tablename__ = "ph_sales"
#     id = Column(Integer, primary_key=True)
#     patient_id = Column(Integer,
#                         ForeignKey("patients.id"),
#                         index=True,
#                         nullable=False)
#     context_type = Column(String(10), nullable=False)  # opd|ipd
#     visit_id = Column(Integer, ForeignKey("opd_visits.id"),
#                       nullable=True)  # OPD
#     admission_id = Column(Integer,
#                           ForeignKey("ipd_admissions.id"),
#                           nullable=True)  # IPD
#     location_id = Column(Integer,
#                          ForeignKey("ph_locations.id"),
#                          nullable=False)
#     total_amount = Column(Numeric(12, 2), default=Decimal("0.00"))
#     payment_mode = Column(String(20),
#                           default="on-account")  # cash|upi|card|on-account
#     created_at = Column(DateTime, default=datetime.utcnow)
#     created_by = Column(Integer, ForeignKey("users.id"), nullable=True)


# class PharmacySaleItem(Base):
#     __tablename__ = "ph_sale_items"
#     id = Column(Integer, primary_key=True)
#     sale_id = Column(Integer,
#                      ForeignKey("ph_sales.id"),
#                      index=True,
#                      nullable=False)
#     medicine_id = Column(Integer,
#                          ForeignKey("ph_medicines.id"),
#                          nullable=False)
#     lot_id = Column(Integer,
#                     ForeignKey("ph_inventory_lots.id"),
#                     nullable=False)
#     qty = Column(Integer, nullable=False)
#     unit_price = Column(Numeric(12, 2), nullable=False)
#     tax_percent = Column(Numeric(5, 2), nullable=True)
#     amount = Column(Numeric(12, 2), nullable=False)
#     prescription_item_id = Column(Integer, nullable=True)


# # -------------------------
# # Adjustments & Transfers
# # -------------------------


# class PharmacyAdjustment(Base):
#     __tablename__ = "ph_adjustments"
#     id = Column(Integer, primary_key=True)
#     lot_id = Column(Integer, ForeignKey("ph_inventory_lots.id"), index=True)
#     qty_change = Column(Integer, nullable=False)  # + or -
#     reason = Column(String(120), default="stock_take")
#     created_at = Column(DateTime, default=datetime.utcnow)
#     user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


# class PharmacyTransfer(Base):
#     __tablename__ = "ph_transfers"
#     id = Column(Integer, primary_key=True)
#     from_location_id = Column(Integer,
#                               ForeignKey("ph_locations.id"),
#                               nullable=False)
#     to_location_id = Column(Integer,
#                             ForeignKey("ph_locations.id"),
#                             nullable=False)
#     lot_id = Column(Integer,
#                     ForeignKey("ph_inventory_lots.id"),
#                     nullable=False)
#     qty = Column(Integer, nullable=False)
#     created_at = Column(DateTime, default=datetime.utcnow)
#     user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


# # -------------------------
# # Prescriptions (Doctor → Pharmacy)
# # -------------------------


# # ---------- Prescription ----------
# class PharmacyPrescription(Base):
#     __tablename__ = "ph_rx"

#     id = Column(Integer, primary_key=True)
#     patient_id = Column(Integer,
#                         ForeignKey("patients.id"),
#                         index=True,
#                         nullable=False)
#     context_type = Column(String(10), nullable=False)  # opd|ipd
#     visit_id = Column(Integer, ForeignKey("opd_visits.id"), nullable=True)
#     admission_id = Column(Integer,
#                           ForeignKey("ipd_admissions.id"),
#                           nullable=True)
#     prescriber_user_id = Column(Integer,
#                                 ForeignKey("users.id"),
#                                 nullable=False)

#     # DB legacy statuses: draft|signed|sent|cancelled|partially_dispensed|fully_dispensed
#     # UI will map these to: new|in_progress|dispensed|cancelled
#     status = Column(String(30), default="draft")

#     notes = Column(Text, default="")
#     created_at = Column(DateTime, default=datetime.utcnow)
#     signed_at = Column(DateTime, nullable=True)
#     sent_at = Column(DateTime, nullable=True)
#     updated_at = Column(DateTime, default=datetime.utcnow)


# # ---------- Prescription Item ----------
# class PharmacyPrescriptionItem(Base):
#     __tablename__ = "ph_rx_items"

#     id = Column(Integer, primary_key=True)
#     rx_id = Column(Integer, ForeignKey("ph_rx.id"), index=True, nullable=False)
#     medicine_id = Column(Integer,
#                          ForeignKey("ph_medicines.id"),
#                          nullable=False)

#     dose = Column(String(80), default="")
#     frequency = Column(String(80),
#                        default="")  # e.g., "AM+PM"; we’ll still fill this
#     route = Column(String(30), default="po")

#     # NEW: persist time-of-day flags
#     am = Column(Boolean, nullable=False, default=False)
#     af = Column(Boolean, nullable=False, default=False)
#     pm = Column(Boolean, nullable=False, default=False)
#     night = Column(Boolean, nullable=False, default=False)

#     # duration should never be 0 for auto-qty; default to 1
#     duration_days = Column(Integer, nullable=False, default=1)

#     # auto-computed = (# of True ToD) * duration_days (but can be overridden)
#     quantity = Column(Integer, nullable=False)

#     instructions = Column(Text, default="")

#     # DB legacy statuses: pending|partially_dispensed|fully_dispensed|cancelled
#     # UI maps to: new|in_progress|dispensed|cancelled
#     status = Column(String(30), default="pending")

#     dispensed_qty = Column(Integer, default=0)
