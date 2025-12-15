# FILE: app/api/routes_pharmacy.py
from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body, status
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.pharmacy_prescription import (
    PharmacyPrescription,
    PharmacySale,
    PharmacyPayment,
)
from app.schemas.pharmacy_prescription import (
    PrescriptionCreate,
    PrescriptionUpdate,
    PrescriptionOut,
    PrescriptionSummaryOut,
    RxLineCreate,
    RxLineUpdate,
    DispenseFromRxIn,
    DispenseFromRxOut,
    CounterSaleCreateIn,
    SaleOut,
    SaleSummaryOut,
    PaymentCreate,
    PaymentOut,
)
import json
from fastapi.encoders import jsonable_encoder
from app.services import pharmacy as pharmacy_service

router = APIRouter(prefix="/pharmacy", tags=["pharmacy"])

# ------------------------------------------------------------------
# INTERNAL HELPERS – for display fields (patient / doctor / items)
# ------------------------------------------------------------------

def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False

def _attach_display_fields(rx: PharmacyPrescription) -> PharmacyPrescription:
    """
    Add helper attributes used by schemas / frontend:
    - patient_name
    - patient_uhid
    - doctor_name
    - item_count
    """

    # Patient
    patient = getattr(rx, "patient", None)
    if patient is not None:
        first = (getattr(patient, "first_name", "") or "").strip()
        last = (getattr(patient, "last_name", "") or "").strip()
        full = f"{first} {last}".strip()
        if not full:
            full = (getattr(patient, "full_name", None)
                    or getattr(patient, "name", None))
        rx.patient_name = full or None
        rx.patient_uhid = getattr(patient, "uhid", None)
    else:
        rx.patient_name = None
        rx.patient_uhid = None

    # Doctor
    doctor = getattr(rx, "doctor", None)
    if doctor is not None:
        d_full = (getattr(doctor, "full_name", None)
                  or getattr(doctor, "display_name", None))
        if not d_full:
            d_first = (getattr(doctor, "first_name", "") or "").strip()
            d_last = (getattr(doctor, "last_name", "") or "").strip()
            d_full = f"{d_first} {d_last}".strip() or None
        rx.doctor_name = d_full
    else:
        rx.doctor_name = None

    # Item count
    try:
        lines = rx.lines or []
        rx.item_count = len(lines)
    except Exception:
        rx.item_count = None

    return rx


def _attach_display_fields_many(
    items: List[PharmacyPrescription], ) -> List[PharmacyPrescription]:
    for rx in items:
        _attach_display_fields(rx)
    return items


def _rx_base_options(query):
    return query.options(
        selectinload(PharmacyPrescription.lines),
        selectinload(PharmacyPrescription.patient),
        selectinload(PharmacyPrescription.doctor),
    )


# ------------------------------------------------------------------
# Rx CRUD
# ------------------------------------------------------------------


@router.get("/prescriptions", response_model=List[PrescriptionSummaryOut])
def list_prescriptions(
        db: Session = Depends(get_db),
        type: Optional[str] = Query(None,
                                    description="OPD/IPD/COUNTER/GENERAL"),
        status_filter: Optional[str] = Query(None,
                                             alias="status",
                                             description="Status filter"),
        patient_id: Optional[int] = None,
        visit_id: Optional[int] = None,
        ipd_admission_id: Optional[int] = None,
        location_id: Optional[int] = None,
        doctor_user_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        current_user: User = Depends(auth_current_user),
):
    q = _rx_base_options(db.query(PharmacyPrescription))

    if type:
        q = q.filter(PharmacyPrescription.type == type)
    if status_filter:
        q = q.filter(PharmacyPrescription.status == status_filter)
    if patient_id:
        q = q.filter(PharmacyPrescription.patient_id == patient_id)
    if visit_id:
        q = q.filter(PharmacyPrescription.visit_id == visit_id)
    if ipd_admission_id:
        q = q.filter(PharmacyPrescription.ipd_admission_id == ipd_admission_id)
    if location_id:
        q = q.filter(PharmacyPrescription.location_id == location_id)
    if doctor_user_id:
        q = q.filter(PharmacyPrescription.doctor_user_id == doctor_user_id)
    if date_from:
        q = q.filter(PharmacyPrescription.created_at >= date_from)
    if date_to:
        q = q.filter(PharmacyPrescription.created_at < date_to)

    q = q.order_by(PharmacyPrescription.created_at.desc())
    items = q.all()
    return _attach_display_fields_many(items)


@router.get("/prescriptions/{rx_id}", response_model=PrescriptionOut)
def get_prescription(
        rx_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx_id).first())
    if not rx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found.",
        )

    _attach_display_fields(rx)
    return rx


@router.post("/prescriptions", response_model=PrescriptionOut, status_code=201)
def create_prescription(
        payload: PrescriptionCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx = pharmacy_service.create_prescription(db, payload, current_user)
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx.id).first())
    _attach_display_fields(rx)
    return rx


@router.put("/prescriptions/{rx_id}", response_model=PrescriptionOut)
def update_prescription(
        rx_id: int,
        payload: PrescriptionUpdate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx = pharmacy_service.update_prescription(db, rx_id, payload, current_user)
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx.id).first())
    _attach_display_fields(rx)
    return rx


@router.post("/prescriptions/{rx_id}/lines", response_model=PrescriptionOut)
def add_rx_line(
        rx_id: int,
        payload: RxLineCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx = pharmacy_service.add_rx_line(db, rx_id, payload, current_user)
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx.id).first())
    _attach_display_fields(rx)
    return rx


@router.put("/lines/{line_id}", response_model=PrescriptionOut)
def update_rx_line(
        line_id: int,
        payload: RxLineUpdate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx = pharmacy_service.update_rx_line(db, line_id, payload, current_user)
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx.id).first())
    _attach_display_fields(rx)
    return rx


@router.delete("/lines/{line_id}", response_model=PrescriptionOut)
def delete_rx_line(
        line_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx = pharmacy_service.delete_rx_line(db, line_id, current_user)
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx.id).first())
    _attach_display_fields(rx)
    return rx


@router.post("/prescriptions/{rx_id}/sign", response_model=PrescriptionOut)
def sign_prescription(
        rx_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx = pharmacy_service.sign_prescription(db, rx_id, current_user)
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx.id).first())
    _attach_display_fields(rx)
    return rx


@router.post("/prescriptions/{rx_id}/cancel", response_model=PrescriptionOut)
def cancel_prescription(
        rx_id: int,
        reason: str = Body(..., embed=True),
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx = pharmacy_service.cancel_prescription(db, rx_id, reason, current_user)
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx.id).first())
    _attach_display_fields(rx)
    return rx


# ------------------------------------------------------------------
# Rx Queue
# ------------------------------------------------------------------


@router.get("/rx-queue", response_model=List[PrescriptionSummaryOut])
def get_rx_queue(
        db: Session = Depends(get_db),
        type: Optional[str] = Query(None,
                                    description="OPD/IPD/COUNTER/GENERAL"),
        status: Optional[str] = Query(
            "PENDING", description="PENDING|PARTIAL|DISPENSED|ALL"),
        location_id: Optional[int] = None,
        limit: int = Query(100, ge=1, le=500),
        current_user: User = Depends(auth_current_user),
):
    """_rx_base_options
    Queue for pharmacy dispense.

    Frontend status mapping:
    - PENDING   -> DRAFT + ISSUED
    - PARTIAL   -> PARTIALLY_DISPENSED
    - DISPENSED -> DISPENSED
    - ALL       -> all above except CANCELLED
    """
    q = _rx_base_options(db.query(PharmacyPrescription))

    s = (status or "").upper()

    if s in ("", "ALL"):
        allowed_statuses = (
            "DRAFT",
            "ISSUED",
            "PARTIALLY_DISPENSED",
            "DISPENSED",
        )
    elif s == "PENDING":
        allowed_statuses = ("DRAFT", "ISSUED")
    elif s == "PARTIAL":
        allowed_statuses = ("PARTIALLY_DISPENSED", )
    elif s == "DISPENSED":
        allowed_statuses = ("DISPENSED", )
    else:
        # fallback: allow direct DB status if you ever pass it
        allowed_statuses = (s, )

    q = q.filter(PharmacyPrescription.status.in_(allowed_statuses))

    if type:
        q = q.filter(PharmacyPrescription.type == type)
    if location_id:
        q = q.filter(PharmacyPrescription.location_id == location_id)

    q = q.order_by(PharmacyPrescription.created_at.desc()).limit(limit)
    items = q.all()
    return _attach_display_fields_many(items)


# ------------------------------------------------------------------
# Dispense
# ------------------------------------------------------------------

@router.post("/prescriptions/{rx_id}/dispense", response_model=DispenseFromRxOut)
def dispense_from_rx(
    rx_id: int,
    payload: DispenseFromRxIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    print("\n================ DISPENSE DEBUG ================")
    print("rx_id:", rx_id)
    print("user_id:", getattr(current_user, "id", None))
    print("payload:", json.dumps(jsonable_encoder(payload), indent=2))
    print("================================================\n")

    try:
        rx, sale = pharmacy_service.dispense_from_rx(db, rx_id, payload, current_user)

    except HTTPException as e:
        print("\n!!!!!!!! DISPENSE HTTPException !!!!!!!!")
        print("status_code:", e.status_code)
        print("detail:", e.detail)
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
        raise

    except Exception as e:
        import traceback
        print("\n!!!!!!!! DISPENSE UNKNOWN ERROR !!!!!!!!")
        traceback.print_exc()
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
        raise

    rx = (_rx_base_options(db.query(PharmacyPrescription))
          .filter(PharmacyPrescription.id == rx.id).first())
    _attach_display_fields(rx)
    sale_id = sale.id if sale else None
    return DispenseFromRxOut(prescription=rx, sale_id=sale_id)


# ------------------------------------------------------------------
# Counter sales
# ------------------------------------------------------------------


@router.post(
    "/counter-sales",
    response_model=SaleOut,
    status_code=status.HTTP_201_CREATED,
)
def create_counter_sale(
        payload: CounterSaleCreateIn,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx, sale = pharmacy_service.create_counter_sale(db, payload, current_user)
    sale = (db.query(PharmacySale).options(selectinload(
        PharmacySale.items)).get(sale.id))
    return sale


# ------------------------------------------------------------------
# Sales list / detail
# ------------------------------------------------------------------


@router.get("/sales", response_model=List[SaleSummaryOut])
def list_sales(
        db: Session = Depends(get_db),
        context_type: Optional[str] = Query(None),
        patient_id: Optional[int] = None,
        visit_id: Optional[int] = None,
        ipd_admission_id: Optional[int] = None,
        location_id: Optional[int] = None,
        invoice_status: Optional[str] = None,
        payment_status: Optional[str] = None,
        current_user: User = Depends(auth_current_user),
):
    q = db.query(PharmacySale)

    if context_type:
        q = q.filter(PharmacySale.context_type == context_type)
    if patient_id:
        q = q.filter(PharmacySale.patient_id == patient_id)
    if visit_id:
        q = q.filter(PharmacySale.visit_id == visit_id)
    if ipd_admission_id:
        q = q.filter(PharmacySale.ipd_admission_id == ipd_admission_id)
    if location_id:
        q = q.filter(PharmacySale.location_id == location_id)
    if invoice_status:
        q = q.filter(PharmacySale.invoice_status == invoice_status)
    if payment_status:
        q = q.filter(PharmacySale.payment_status == payment_status)

    q = q.order_by(PharmacySale.bill_datetime.desc())
    return q.all()


@router.get("/sales/{sale_id}", response_model=SaleOut)
def get_sale(
        sale_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    sale = (db.query(PharmacySale).options(selectinload(
        PharmacySale.items)).get(sale_id))
    if not sale:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pharmacy sale not found.",
        )
    return sale


# ------------------------------------------------------------------
# Finalize / Cancel sale
# ------------------------------------------------------------------


@router.post("/sales/{sale_id}/finalize", response_model=SaleOut)
def finalize_sale(
        sale_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    sale = pharmacy_service.finalize_sale(db, sale_id, current_user)
    sale = (db.query(PharmacySale).options(selectinload(
        PharmacySale.items)).get(sale.id))
    return sale


@router.post("/sales/{sale_id}/cancel", response_model=SaleOut)
def cancel_sale(
        sale_id: int,
        reason: str = Body(..., embed=True),
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    sale = pharmacy_service.cancel_sale(db, sale_id, reason, current_user)
    sale = (db.query(PharmacySale).options(selectinload(
        PharmacySale.items)).get(sale.id))
    return sale


# ------------------------------------------------------------------
# Payments
# ------------------------------------------------------------------


@router.post(
    "/sales/{sale_id}/payments",
    response_model=PaymentOut,
    status_code=status.HTTP_201_CREATED,
)
def add_payment(
        sale_id: int,
        payload: PaymentCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    payment = pharmacy_service.add_payment_to_sale(db, sale_id, payload,
                                                   current_user)
    return payment


@router.get("/sales/{sale_id}/payments", response_model=List[PaymentOut])
def list_payments(
        sale_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    q = db.query(PharmacyPayment).filter(PharmacyPayment.sale_id == sale_id)
    q = q.order_by(PharmacyPayment.paid_on.asc())
    return q.all()
