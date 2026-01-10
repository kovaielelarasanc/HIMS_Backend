# # FILE: app/services/billing_auto.py
# from __future__ import annotations
# import os
# from datetime import datetime
# from typing import Optional, Iterable

# from sqlalchemy.orm import Session
# from sqlalchemy.exc import IntegrityError

# from app.models.billing import Invoice, InvoiceItem
# from app.models.opd import (
#     Visit,
#     LabOrder as OpdLabOrder,
#     RadiologyOrder as OpdRadiologyOrder,
#     LabTest,
#     RadiologyTest,
#     DoctorFee,
# )
# from app.models.user import User
# from app.models.department import Department

# # Optional downstream systems (LIS/RIS/OT/Pharmacy)
# try:
#     from app.models.lis import LisOrderItem
#     HAS_LIS = True
# except Exception:
#     HAS_LIS = False

# try:
#     from app.models.ris import RisOrder
#     HAS_RIS = True
# except Exception:
#     HAS_RIS = False

# try:
#     from app.models.ot import OtOrder
#     from app.models.ot_master import OtSurgeryMaster
#     HAS_OT = True
# except Exception:
#     HAS_OT = False

# try:
#     from app.models.pharmacy import PharmacySale
#     HAS_PHARMACY = True
# except Exception:
#     HAS_PHARMACY = False

# # ============== CONFIG ==============

# # If True -> auto create invoice and auto-add items.
# # Invoices will STILL remain in 'draft' (no auto finalize).
# BILLING_AUTOCREATE = os.getenv("BILLING_AUTOCREATE", "false").lower() in {
#     "1",
#     "true",
#     "yes",
# }

# DEFAULT_TAX = float(os.getenv("BILLING_DEFAULT_TAX", "0") or 0)

# OPD_CONSULT_DEFAULT_PRICE = float(
#     os.getenv("OPD_CONSULT_DEFAULT_PRICE", "300") or 300)

# # Prefer OPD order source for duplicate check (same description not added twice)
# BILLING_PREFER_OPD_ORDER_SOURCE = os.getenv(
#     "BILLING_PREFER_OPD_ORDER_SOURCE",
#     "true",
# ).lower() in {"1", "true", "yes"}

# # IMPORTANT:
# #   We completely disable auto-finalization regardless of any env.
# #   Finalization can happen ONLY via /billing/invoices/{id}/finalize API.
# AUTO_FINALIZATION_DISABLED = True

# # ============== COMMON HELPERS ==============


# def _recompute(inv: Invoice):
#     """Recompute totals from non-voided items."""
#     gross = 0.0
#     tax = 0.0
#     for it in inv.items:
#         if it.is_voided:
#             continue
#         gross += float(it.unit_price or 0) * int(it.quantity or 1)
#         tax += float(it.tax_amount or 0)
#     inv.gross_total = gross
#     inv.tax_total = tax
#     inv.net_total = gross + tax
#     inv.balance_due = float(inv.net_total or 0) - float(inv.amount_paid or 0)


# def _find_or_create_draft_invoice(
#     db: Session,
#     *,
#     patient_id: int,
#     context_type: Optional[str] = None,
#     context_id: Optional[int] = None,
#     user_id: Optional[int] = None,
# ) -> Invoice:
#     """
#     Auto-billing helper:

#     - ALWAYS returns an Invoice in 'draft' status.
#     - Reuses latest draft for same patient/context when possible.
#     - NEVER finalizes here.
#     """
#     inv = None
#     if context_type and context_id:
#         inv = (db.query(Invoice).filter(
#             Invoice.patient_id == patient_id,
#             Invoice.status == "draft",
#             Invoice.context_type == context_type,
#             Invoice.context_id == context_id,
#         ).order_by(Invoice.id.desc()).first())
#     if not inv:
#         inv = (db.query(Invoice).filter(
#             Invoice.patient_id == patient_id,
#             Invoice.status == "draft",
#         ).order_by(Invoice.id.desc()).first())
#     if inv:
#         return inv

#     inv = Invoice(
#         patient_id=patient_id,
#         context_type=context_type,
#         context_id=context_id,
#         status="draft",  # <--- NEVER changed here
#         created_by=user_id,
#         created_at=datetime.utcnow(),
#     )
#     db.add(inv)
#     db.flush()
#     return inv


# def _ensure_item(
#     db: Session,
#     *,
#     inv: Invoice,
#     service_type: str,
#     service_ref_id: int,
#     description: str,
#     unit_price: float,
#     quantity: int = 1,
#     tax_rate: float = DEFAULT_TAX,
#     user_id: Optional[int] = None,
# ) -> InvoiceItem:
#     """
#     Ensure an item for (service_type, service_ref_id) exists.

#     - If already billed anywhere in system (non-voided), returns existing.
#     - Else creates a new line on the given invoice.
#     """
#     existing = (db.query(InvoiceItem).filter(
#         InvoiceItem.service_type == service_type,
#         InvoiceItem.service_ref_id == service_ref_id,
#         InvoiceItem.is_voided.is_(False),
#     ).first())
#     if existing:
#         return existing

#     # Additional safety: same description in same invoice
#     existing_same_desc = (db.query(InvoiceItem).filter(
#         InvoiceItem.invoice_id == inv.id,
#         InvoiceItem.service_type == service_type,
#         InvoiceItem.description == description,
#         InvoiceItem.is_voided.is_(False),
#     ).first())
#     if existing_same_desc:
#         return existing_same_desc

#     qty = int(quantity or 1)
#     price = float(unit_price or 0)
#     tax_amount = round(price * qty * (tax_rate / 100.0), 2)
#     line_total = round(price * qty + tax_amount, 2)

#     line = InvoiceItem(
#         invoice_id=inv.id,
#         service_type=service_type,
#         service_ref_id=service_ref_id,
#         description=description,
#         quantity=qty,
#         unit_price=price,
#         tax_rate=tax_rate,
#         tax_amount=tax_amount,
#         line_total=line_total,
#         created_by=user_id,
#         created_at=datetime.utcnow(),
#     )
#     db.add(line)
#     _recompute(inv)
#     return line


# def _doctor_consult_fee(db: Session, v: Visit) -> float:
#     """
#     Resolve doctor consultation fee with the following priority:
#     1. DoctorFee master (doctor_fees table) – base_fee
#     2. User.consult_fee (if present)
#     3. Department.consult_fee (if present)
#     4. OPD_CONSULT_DEFAULT_PRICE (env, default 300)
#     """
#     price: Optional[float] = None

#     # 1) DoctorFee master
#     fee = (db.query(DoctorFee).filter(
#         DoctorFee.doctor_user_id == v.doctor_user_id,
#         DoctorFee.is_active.is_(True),
#     ).order_by(DoctorFee.id.desc()).first())
#     if fee and fee.base_fee is not None:
#         price = float(fee.base_fee)

#     # 2) Doctor-level consult fee
#     if price is None:
#         doc: User | None = db.get(User, v.doctor_user_id)
#         if doc and hasattr(doc, "consult_fee") and doc.consult_fee is not None:
#             price = float(doc.consult_fee)

#     # 3) Department-level consult fee
#     if price is None:
#         dep: Department | None = db.get(Department, v.department_id)
#         if (dep and hasattr(dep, "consult_fee")
#                 and dep.consult_fee is not None):
#             price = float(dep.consult_fee)

#     # 4) Fallback
#     return float(price if price is not None else OPD_CONSULT_DEFAULT_PRICE)


# def _price_desc_for(db: Session, service_type: str, ref_id: int):
#     """
#     Given a service_type + reference record ID, resolve (unit_price, description)
#     for auto-billing purpose. Returns (None, None) if not billable.
#     """
#     # ========== OPD CONSULTATION ==========
#     if service_type == "opd_consult":
#         v = db.get(Visit, ref_id)
#         if not v:
#             return None, None
#         amount = _doctor_consult_fee(db, v)
#         doc = db.get(User, v.doctor_user_id)
#         dep = db.get(Department, v.department_id)
#         doc_name = getattr(doc, "name", f"Doctor #{v.doctor_user_id}")
#         dep_name = getattr(dep, "name", "Consultation")
#         return amount, f"Consultation – {doc_name} ({dep_name})"

#     # ========== LAB (LIS/OPD) ==========
#     if service_type == "lab":
#         # LIS first (if present)
#         if HAS_LIS:
#             from app.models.lis import LisOrderItem  # local import

#             it = db.get(LisOrderItem, ref_id)
#             if it:
#                 mt = (db.get(LabTest, getattr(it, "test_id", None)) if getattr(
#                     it, "test_id", None) else None)
#                 price = float(getattr(mt, "price", 0) or 0)
#                 desc = f"Lab: {getattr(it, 'test_name', 'Test')} ({getattr(it, 'test_code', 'CODE')})"
#                 return price, desc

#         # fallback: OPD Lab order
#         o = db.get(OpdLabOrder, ref_id)
#         if o:
#             mt = db.get(LabTest, o.test_id)
#             price = float(getattr(mt, "price", 0) or 0)
#             code = getattr(mt, "code", "") or getattr(mt, "name", "Test")
#             name = getattr(mt, "name", "Test")
#             return price, f"Lab: {name} ({code})"

#         return None, None

#     # ========== RADIOLOGY / RIS ==========
#     if service_type in {"radiology", "ris"}:
#         # RIS first
#         if HAS_RIS:
#             ro = db.get(RisOrder, ref_id)
#             if ro:
#                 mt = (db.get(RadiologyTest, getattr(ro, "test_id", None))
#                       if getattr(ro, "test_id", None) else None)
#                 price = float(getattr(mt, "price", 0) or 0)
#                 mod = getattr(mt, "modality", None) or "Radiology"
#                 return price, f"{mod}: {getattr(ro, 'test_name', 'Study')} ({getattr(ro, 'test_code', 'CODE')})"

#         # fallback: OPD radiology order
#         o = db.get(OpdRadiologyOrder, ref_id)
#         if o:
#             mt = db.get(RadiologyTest, o.test_id)
#             price = float(getattr(mt, "price", 0) or 0)
#             mod = getattr(mt, "modality", None) or "Radiology"
#             code = getattr(mt, "code", "") or getattr(mt, "name", "Study")
#             name = getattr(mt, "name", "Study")
#             return price, f"{mod}: {name} ({code})"

#         return None, None

#     # ========== OT ==========
#     if service_type == "ot" and HAS_OT:
#         oc = db.get(OtOrder, ref_id)
#         if not oc:
#             return None, None
#         price = float(oc.estimated_cost or 0)
#         if price == 0 and getattr(oc, "surgery_master_id", None):
#             m = db.get(OtSurgeryMaster, oc.surgery_master_id)
#             if m:
#                 price = float(m.default_cost or 0)
#         return price, f"OT: {getattr(oc, 'surgery_name', 'Surgery')}"

#     # ========== PHARMACY ==========
#     if service_type == "pharmacy" and HAS_PHARMACY:
#         sale = db.get(PharmacySale, ref_id)
#         if not sale:
#             return None, None
#         return float(sale.total_amount or 0), f"Pharmacy sale #{sale.id}"

#     # Unknown types -> no auto-billing
#     return None, None


# # For some legacy logic: OPD pending check (now only used if you re-enable auto finalize manually).
# _PENDING_STATUSES: set[str] = {
#     "ordered", "scheduled", "in_progress", "pending"
# }


# def _opd_has_pending_orders(db: Session, visit_id: int) -> bool:
#     cnt = (db.query(OpdLabOrder).filter(
#         OpdLabOrder.visit_id == visit_id,
#         OpdLabOrder.status.in_(_PENDING_STATUSES),
#     ).count())
#     if cnt > 0:
#         return True
#     cnt = (db.query(OpdRadiologyOrder).filter(
#         OpdRadiologyOrder.visit_id == visit_id,
#         OpdRadiologyOrder.status.in_(_PENDING_STATUSES),
#     ).count())
#     return cnt > 0


# # ============== AUTO-FINALIZE (NOW DISABLED) ==============


# def _maybe_autofinalize_opd(db: Session, inv: Invoice):
#     """
#     NO-OP IMPLEMENTATION.

#     Previously this could auto-finalize OPD invoices based on env settings.
#     Now, for your requirement, it does NOTHING.

#     - Invoice status ALWAYS stays as-is (draft).
#     - Finalization must be done explicitly via /billing/invoices/{id}/finalize.
#     """
#     return


# def maybe_finalize_visit_invoice(db: Session, visit_id: int):
#     """
#     Legacy hook to finalize OPD visit invoice.

#     Now completely disabled:
#     - We only recompute totals in other flows.
#     - No status change to 'finalized' happens here.
#     """
#     return


# # ============== PUBLIC HOOKS ==============


# def auto_add_item_for_event(
#     db: Session,
#     *,
#     service_type: str,
#     ref_id: int,
#     patient_id: int,
#     context_type: Optional[str] = None,
#     context_id: Optional[int] = None,
#     user_id: Optional[int] = None,
# ):
#     """
#     Core auto-billing hook for OPD / LIS / RIS / OT / Pharmacy.

#     Behaviour:
#       - If BILLING_AUTOCREATE == False => NO-OP.
#       - Else:
#           * Find or create a DRAFT invoice for patient/context.
#           * Add item if not already billed.
#           * Recompute totals.
#           * DO NOT auto-finalize.

#     Result:
#       - Invoice remains in 'draft' status.
#       - Only API /billing/invoices/{id}/finalize can change it to 'finalized'.
#     """
#     if not BILLING_AUTOCREATE:
#         return

#     price, desc = _price_desc_for(db, service_type, ref_id)
#     if price is None:
#         return

#     inv = _find_or_create_draft_invoice(
#         db,
#         patient_id=patient_id,
#         context_type=context_type,
#         context_id=context_id,
#         user_id=user_id,
#     )

#     # Avoid duplicates at description level when context_type == 'opd'
#     if BILLING_PREFER_OPD_ORDER_SOURCE and context_type == "opd":
#         dup = (db.query(InvoiceItem).filter(
#             InvoiceItem.invoice_id == inv.id,
#             InvoiceItem.service_type == service_type,
#             InvoiceItem.description == desc,
#             InvoiceItem.is_voided.is_(False),
#         ).first())
#         if dup:
#             return

#     _ensure_item(
#         db,
#         inv=inv,
#         service_type=service_type,
#         service_ref_id=ref_id,
#         description=desc,
#         unit_price=float(price or 0),
#         quantity=1,
#         user_id=user_id,
#     )
#     db.flush()
#     # NO auto-finalize call here anymore
#     # _maybe_autofinalize_opd(db, inv)


# def auto_void_items_for_event(
#     db: Session,
#     *,
#     service_type: str,
#     ref_ids: Iterable[int] | int,
#     reason: str = "Cancelled",
#     user_id: Optional[int] = None,
# ):
#     """
#     Void billing items when underlying clinical event is cancelled.

#     - This DOES NOT change invoice status (still draft/finalized as-is).
#     """
#     ids = [ref_ids] if isinstance(ref_ids, int) else list(ref_ids)
#     if not ids:
#         return 0

#     q = (db.query(InvoiceItem).filter(
#         InvoiceItem.service_type == service_type,
#         InvoiceItem.service_ref_id.in_(ids),
#         InvoiceItem.is_voided.is_(False),
#     ))
#     count = 0
#     for it in q.all():
#         it.is_voided = True
#         it.void_reason = reason
#         it.voided_by = user_id
#         it.voided_at = datetime.utcnow()
#         count += 1
#         inv = db.get(Invoice, it.invoice_id)
#         if inv:
#             _recompute(inv)
#     db.flush()
#     return count
