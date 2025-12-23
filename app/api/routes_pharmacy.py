# FILE: app/api/routes_pharmacy.py
from __future__ import annotations

from datetime import date
from typing import List, Optional
from fastapi.responses import StreamingResponse
from fastapi import APIRouter, Depends, HTTPException, Query, Body, status
from sqlalchemy.orm import Session, selectinload
from io import BytesIO
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
from app.models.patient import Patient
from app.services.pdf_prescription import build_prescription_pdf

from types import SimpleNamespace
from sqlalchemy import func, case
from app.models.ui_branding import UiBranding
from app.services.pdf_branding import render_brand_header_html, brand_header_css  # (optional usage elsewhere)
from app.services.id_gen import make_op_episode_id, make_ip_admission_code, make_rx_number
from app.models.pharmacy_inventory import ItemBatch
from app.models.pharmacy_inventory import InventoryItem, InventoryLocation  # if you have these names
from app.schemas.pharmacy_inventory import PharmacyBatchPickOut
from app.models.pharmacy_prescription import (  # type: ignore
        PharmacyPrescription, PharmacyPrescriptionLine, PharmacySale,
        PharmacySaleItem,
    )
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


def _ensure_rx_numbers(db: Session, rx: PharmacyPrescription) -> None:
    """
    Persist UUID-style numbers on the rx row (rx_number / op_number / ip_number)
    so UI + PDF never depends on raw ids.
    """
    dt_ref = getattr(rx, "rx_datetime", None) or getattr(
        rx, "created_at", None)

    if not (getattr(rx, "rx_number", None) or "").strip():
        rx.rx_number = make_rx_number(db, rx.id, on_date=dt_ref)

    if not (getattr(rx, "op_number", None) or "").strip():
        if getattr(rx, "visit_id", None):
            rx.op_number = make_op_episode_id(db,
                                              int(rx.visit_id),
                                              on_date=dt_ref)

    if not (getattr(rx, "ip_number", None) or "").strip():
        if getattr(rx, "ipd_admission_id", None):
            rx.ip_number = make_ip_admission_code(db,
                                                  int(rx.ipd_admission_id),
                                                  on_date=dt_ref)


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


# inside app/api/routes_pharmacy.py


def _rx_base_options(query):
    return query.options(
        selectinload(PharmacyPrescription.lines).selectinload(PharmacyPrescriptionLine.batch),
        selectinload(PharmacyPrescription.patient),
        selectinload(PharmacyPrescription.doctor).selectinload(User.department),
    )


def _doctor_display_name(u: User | None) -> str:
    if not u:
        return "—"
    # your User.full_name property returns name
    nm = (getattr(u, "full_name", None) or getattr(u, "name", None)
          or "").strip()
    return nm or "—"


def _doctor_department_name(u: User | None) -> str:
    if not u:
        return "—"
    dep = getattr(u, "department", None)
    if dep and getattr(dep, "name", None):
        return str(dep.name).strip() or "—"
    return "—"


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


@router.get("/prescriptions/{rx_id}")
def get_prescription_details(
        rx_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    # ✅ load everything properly (lines + patient + doctor)
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx_id).first())
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found")

    patient = getattr(rx, "patient", None)
    doctor = getattr(rx, "doctor", None)

    rx_dt_ref = getattr(rx, "rx_datetime", None) or getattr(
        rx, "created_at", None)

    # Public IDs (no DB write here; just compute for display)
    rx_uuid = getattr(rx, "rx_number", None) or make_rx_number(
        db, rx.id, on_date=rx_dt_ref)

    op_uid = getattr(rx, "op_number", None)
    if not op_uid and getattr(rx, "visit_id", None):
        op_uid = make_op_episode_id(db, int(rx.visit_id), on_date=rx_dt_ref)

    ip_uid = getattr(rx, "ip_number", None)
    if not ip_uid and getattr(rx, "ipd_admission_id", None):
        ip_uid = make_ip_admission_code(db,
                                        int(rx.ipd_admission_id),
                                        on_date=rx_dt_ref)

    lines = []
    for ln in (rx.lines or []):
        req = getattr(ln, "requested_qty", None)
        disp = getattr(ln, "dispensed_qty", None) or 0
        try:
            remaining = max((req or 0) - (disp or 0), 0)
        except Exception:
            remaining = None
            
        batch = getattr(ln, "batch", None)
        lines.append({
            # ✅ CRITICAL: include ids
            "id":
            ln.id,
            "line_id":
            ln.id,  # (extra alias for frontend safety)
            "rx_line_id":
            ln.id,  # (extra alias if some code still uses old name)
            
                    # ✅ batch fields
            "batch_id": getattr(ln, "batch_id", None),
            "batch_no": getattr(ln, "batch_no_snapshot", None) or getattr(batch, "batch_no", None),
            "expiry_date": getattr(ln, "expiry_date_snapshot", None) or getattr(batch, "expiry_date", None),
            "batch_current_qty": getattr(batch, "current_qty", None),

            # item details
            "item_id":
            getattr(ln, "item_id", None),
            "item_name": (getattr(ln, "item_name", None)
                          or getattr(getattr(ln, "item", None), "name", None)),
            "item_strength":
            getattr(ln, "item_strength", None)
            or getattr(ln, "strength", None),

            # instructions
            "dose_text":
            getattr(ln, "dose_text", None),
            "frequency_code":
            getattr(ln, "frequency_code", None),
            "duration_days":
            getattr(ln, "duration_days", None),
            "route":
            getattr(ln, "route", None),
            "timing":
            getattr(ln, "timing", None),
            "instructions":
            getattr(ln, "instructions", None),

            # quantities
            "requested_qty":
            req,
            "dispensed_qty":
            disp,
            "remaining_qty":
            remaining,
        })

    return {
        "id":
        rx.id,
        "rx_number":
        rx_uuid,
        "type":
        getattr(rx, "type", None),  # ✅ IMPORTANT (OPD/IPD/OT/COUNTER)
        "status":
        getattr(rx, "status", None),
        "rx_datetime":
        rx_dt_ref,
        "created_at":
        getattr(rx, "created_at", None),
        "location_id":
        getattr(rx, "location_id",
                None),  # ✅ IMPORTANT for preselecting location
        "patient_id":
        getattr(rx, "patient_id", None),
        "doctor_user_id":
        getattr(rx, "doctor_user_id", None),
        "notes":
        getattr(rx, "notes", None),
        "op_uid":
        op_uid,
        "ip_uid":
        ip_uid,
        "lines":
        lines,
        "patient": ({
            "id":
            patient.id,
            "prefix":
            getattr(patient, "prefix", None),
            "first_name":
            getattr(patient, "first_name", None),
            "last_name":
            getattr(patient, "last_name", None),
            "full_name":
            (getattr(patient, "full_name", None) or
             f"{getattr(patient,'first_name','')} {getattr(patient,'last_name','')}"
             .strip()),
            "uhid":
            getattr(patient, "uhid", None),
            "phone":
            getattr(patient, "phone", None),
            "dob":
            getattr(patient, "dob", None)
            or getattr(patient, "date_of_birth", None),
            "gender":
            getattr(patient, "gender", None) or getattr(patient, "sex", None),
            "age_display":
            getattr(patient, "age_display", None)
            or getattr(patient, "age", None),
        } if patient else None),
        "doctor": ({
            "id":
            doctor.id,
            "full_name":
            getattr(doctor, "full_name", None)
            or getattr(doctor, "name", None),
            "registration_no":
            getattr(doctor, "registration_no", None),
        } if doctor else None),
    }


def _user_display_name(u: User) -> str:
    if not u:
        return "—"
    for k in ("full_name", "display_name", "name"):
        v = (getattr(u, k, None) or "").strip()
        if v:
            return v
    fn = (getattr(u, "first_name", "") or "").strip()
    ln = (getattr(u, "last_name", "") or "").strip()
    nm = f"{fn} {ln}".strip()
    return nm or (getattr(u, "email", None) or "—")


def _user_department_name(db: Session, u: User) -> str:
    if not u:
        return "—"

    # 1) string column patterns
    for k in ("department", "department_name", "dept", "dept_name"):
        v = getattr(u, k, None)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # 2) relationship patterns
    dep = getattr(u, "department", None)
    if dep is not None and not isinstance(dep, str):
        nm = (getattr(dep, "name", None) or "").strip()
        if nm:
            return nm

    # 3) department_id fallback (only if you have Department model)
    dep_id = getattr(u, "department_id", None)
    if dep_id:
        try:
            from app.models.department import Department  # if exists in your project
            d = db.get(Department, int(dep_id))
            if d and getattr(d, "name", None):
                return str(d.name).strip()
        except Exception:
            pass

    return "—"


@router.get("/prescriptions/{rx_id}/pdf")
def prescription_pdf(
        rx_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx_id).first())
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found")

    # ✅ Ensure UUID numbers are persisted
    _ensure_rx_numbers(db, rx)
    db.add(rx)
    db.commit()
    db.refresh(rx)

    # ✅ Use relationship-loaded objects
    patient_obj = getattr(rx, "patient", None) or (db.get(
        Patient, rx.patient_id) if rx.patient_id else None)

    doctor_obj = getattr(rx, "doctor", None)
    # safety: ensure department is loaded even if relationship wasn't
    if getattr(rx, "doctor_user_id", None):
        doctor_obj = (db.query(User).options(selectinload(
            User.department)).filter(User.id == rx.doctor_user_id).first())

    # Branding
    b = db.query(UiBranding).order_by(UiBranding.id.desc()).first()
    branding_obj = b or SimpleNamespace(
        org_name="NUTRYAH HIMS",
        org_tagline="",
        org_address="",
        org_phone="",
        org_email="",
        org_website="",
        org_gstin="",
        logo_path="",
    )

    # Public IDs (use persisted ones after _ensure_rx_numbers)
    rx_dt_ref = getattr(rx, "rx_datetime", None) or getattr(
        rx, "created_at", None)
    rx_uuid = getattr(rx, "rx_number", None) or make_rx_number(
        db, rx.id, on_date=rx_dt_ref)
    op_uid = getattr(rx, "op_number", None) or "—"
    ip_uid = getattr(rx, "ip_number", None) or "—"

    # RX payload for PDF service
    payload = {
        "id":
        rx.id,
        "rx_number":
        rx_uuid,
        "rx_datetime":
        rx_dt_ref,
        "notes":
        getattr(rx, "notes", None),
        "op_uid":
        op_uid,
        "ip_uid":
        ip_uid,
        "lines": [{
            "item_name":
            getattr(ln, "item_name", None)
            or getattr(getattr(ln, "item", None), "name", None),
            "dose_text":
            getattr(ln, "dose_text", None),
            "frequency_code":
            getattr(ln, "frequency_code", None),
            "duration_days":
            getattr(ln, "duration_days", None),
            "route":
            getattr(ln, "route", None),
            "timing":
            getattr(ln, "timing", None),
            "requested_qty":
            getattr(ln, "requested_qty", None),
            "instructions":
            getattr(ln, "instructions", None),
        } for ln in (rx.lines or [])],
    }

    # Patient dict
    p = None
    if patient_obj:
        p = {
            "prefix":
            getattr(patient_obj, "prefix", None),
            "first_name":
            getattr(patient_obj, "first_name", None),
            "last_name":
            getattr(patient_obj, "last_name", None),
            "full_name":
            getattr(patient_obj, "full_name", None),
            "uhid":
            getattr(patient_obj, "uhid", None),
            "phone":
            getattr(patient_obj, "phone", None),
            "dob":
            getattr(patient_obj, "dob", None)
            or getattr(patient_obj, "date_of_birth", None),
            "gender":
            getattr(patient_obj, "gender", None)
            or getattr(patient_obj, "sex", None),
        }

    # Doctor dict (✅ includes department.name)
    d = None
    if doctor_obj:
        d = {
            "full_name":
            getattr(doctor_obj, "full_name", None)
            or getattr(doctor_obj, "name", None),
            "registration_no":
            getattr(doctor_obj, "registration_no", None),
            "department": {
                "name": doctor_obj.department.name
            } if getattr(doctor_obj, "department", None)
            and getattr(doctor_obj.department, "name", None) else None,
        }

    # ✅ IMPORTANT: build_prescription_pdf returns (bytes, media_type)
    pdf_data, media_type = build_prescription_pdf(
        branding_obj=branding_obj,
        rx=payload,
        patient=p,
        doctor=d,
    )

    safe_rx = str(rx_uuid).replace("/", "-").replace("\\",
                                                     "-").replace(" ", "_")
    filename = f"prescription_{safe_rx}.pdf"

    return StreamingResponse(
        BytesIO(pdf_data),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# @router.get("/prescriptions/{rx_id}", response_model=PrescriptionOut)
# def get_prescription(
#         rx_id: int,
#         db: Session = Depends(get_db),
#         current_user: User = Depends(auth_current_user),
# ):
#     rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
#         PharmacyPrescription.id == rx_id).first())
#     if not rx:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="Prescription not found.",
#         )

#     _attach_display_fields(rx)
#     return rx


@router.post("/prescriptions", response_model=PrescriptionOut, status_code=201)
def create_prescription(
        payload: PrescriptionCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    rx = pharmacy_service.create_prescription(db, payload, current_user)

    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx.id).first())

    _ensure_rx_numbers(db, rx)  # ✅ persist
    db.add(rx)
    db.commit()
    db.refresh(rx)

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

    _ensure_rx_numbers(db, rx)  # ✅ persist
    db.add(rx)
    db.commit()
    db.refresh(rx)

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


@router.post("/prescriptions/{rx_id}/dispense",
             response_model=DispenseFromRxOut)
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
        rx, sale = pharmacy_service.dispense_from_rx(db, rx_id, payload,
                                                     current_user)

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

    rx = (_rx_base_options(db.query(PharmacyPrescription)).filter(
        PharmacyPrescription.id == rx.id).first())
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

@router.get("/batch-picks", response_model=List[PharmacyBatchPickOut])
def list_batch_picks(
    location_id: int = Query(...),
    item_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    today = date.today()

    q = (
        db.query(ItemBatch)
        .filter(
            ItemBatch.location_id == location_id,
            ItemBatch.item_id == item_id,
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.status == "ACTIVE",
            ItemBatch.current_qty > 0,
            ((ItemBatch.expiry_date.is_(None)) | (ItemBatch.expiry_date >= today)),
        )
        .order_by(
            case((ItemBatch.expiry_date.is_(None), 1), else_=0),
            ItemBatch.expiry_date.asc(),
            ItemBatch.id.asc(),
        )
    )

    batches = q.all()

    out: List[PharmacyBatchPickOut] = []
    for b in batches:
        item = getattr(b, "item", None)
        loc = getattr(b, "location", None)

        out.append(
            PharmacyBatchPickOut(
                batch_id=b.id,
                item_id=b.item_id,

                code=getattr(item, "code", "") if item else "",
                name=getattr(item, "name", "") if item else "",
                generic_name=getattr(item, "generic_name", "") if item else "",
                form=getattr(item, "form", "") if item else "",
                strength=getattr(item, "strength", "") if item else "",
                unit=getattr(item, "unit", "unit") if item else "unit",

                batch_no=b.batch_no,
                expiry_date=b.expiry_date,
                available_qty=b.current_qty,

                unit_cost=b.unit_cost or 0,
                mrp=b.mrp or 0,
                tax_percent=b.tax_percent or 0,

                location_id=b.location_id,
                location_name=getattr(loc, "name", None) if loc else None,
            )
        )

    return out


