# FILE: app/api/routes_patients.py
from __future__ import annotations

import os
import io
import calendar
from pathlib import Path
from typing import List, Optional
from datetime import date

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Form,
    Body,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db, current_user as auth_current_user
from app.core.config import settings
from app.models.user import User
from app.models.patient import (
    Patient,
    PatientAddress,
    PatientDocument,
    PatientConsent,
)
from app.models.payer import Payer, Tpa, CreditPlan
from app.schemas.patient import (
    PatientCreate,
    PatientUpdate,
    PatientOut,
    AddressIn,
    AddressOut,
    DocumentOut,
    ConsentIn,
    ConsentOut,
)

router = APIRouter()

# --------- utils ----------

UPLOAD_DIR = Path(settings.STORAGE_DIR).joinpath("patient_docs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def make_uhid(id_num: int) -> str:
    return f"NH-{id_num:06d}"


def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def calc_age(dob: Optional[date]):
    """
    Return:
      (years, months, days, full_text, short_text)
    where:
      full_text  -> "24 years 5 months 16 days"
      short_text -> "24 yrs"
    """
    if not dob:
        return None, None, None, None, None

    today = date.today()
    years = today.year - dob.year
    months = today.month - dob.month
    days = today.day - dob.day

    if days < 0:
        months -= 1
        prev_month = today.month - 1 or 12
        prev_year = today.year if today.month > 1 else today.year - 1
        days_in_prev = calendar.monthrange(prev_year, prev_month)[1]
        days += days_in_prev

    if months < 0:
        years -= 1
        months += 12

    parts = []
    if years is not None and years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")

    full_text = " ".join(parts) if parts else None
    short_text = f"{years} yrs" if years is not None else None

    return years, months, days, full_text, short_text


def serialize_patient(p: Patient, db: Session) -> PatientOut:
    years, months, days, full_text, short_text = calc_age(p.dob)
    data = PatientOut.model_validate(p, from_attributes=True)

    # Age fields
    data.age_years = years
    data.age_months = months
    data.age_days = days
    data.age_text = full_text
    data.age_short_text = short_text

    # Resolve doctor & credit master display names
    if p.ref_doctor_id:
        doc = db.query(User).get(p.ref_doctor_id)
        if doc:
            data.ref_doctor_name = doc.name

    if p.credit_payer_id:
        payer = db.query(Payer).get(p.credit_payer_id)
        if payer:
            data.credit_payer_name = payer.name

    if p.credit_tpa_id:
        tpa = db.query(Tpa).get(p.credit_tpa_id)
        if tpa:
            data.credit_tpa_name = tpa.name

    if p.credit_plan_id:
        plan = db.query(CreditPlan).get(p.credit_plan_id)
        if plan:
            data.credit_plan_name = plan.name

    return data


# --------- core patient endpoints ----------


@router.post("", response_model=PatientOut)
@router.post("/", response_model=PatientOut)
def create_patient(
        payload: PatientCreate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Create patient.
    Works for both:
      - POST /api/patients
      - POST /api/patients/
    """
    if not has_perm(user, "patients.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # uniqueness checks (current design keeps phone/email unique)
    if payload.phone:
        exists = db.query(Patient).filter(
            Patient.phone == payload.phone).first()
        if exists:
            raise HTTPException(status_code=400,
                                detail="Phone already registered")
    if payload.email:
        exists = db.query(Patient).filter(
            Patient.email == payload.email).first()
        if exists:
            raise HTTPException(status_code=400,
                                detail="Email already registered")

    p = Patient(
        uhid="TEMP",
        first_name=(payload.first_name or "").strip(),
        last_name=(payload.last_name or "").strip() or None,
        gender=payload.gender,
        dob=payload.dob,
        phone=payload.phone,
        email=payload.email,
        aadhar_last4=(payload.aadhar_last4 or "")[:4] or None,
        blood_group=payload.blood_group,
        marital_status=payload.marital_status,
        ref_source=payload.ref_source,
        ref_doctor_id=payload.ref_doctor_id,
        ref_details=payload.ref_details,
        id_proof_type=payload.id_proof_type,
        id_proof_no=payload.id_proof_no,
        guardian_name=payload.guardian_name,
        guardian_phone=payload.guardian_phone,
        guardian_relation=payload.guardian_relation,
        patient_type=payload.patient_type or "none",
        tag=payload.tag,
        religion=payload.religion,
        occupation=payload.occupation,
        file_number=payload.file_number,
        file_location=payload.file_location,
        credit_type=payload.credit_type,
        credit_payer_id=payload.credit_payer_id,
        credit_tpa_id=payload.credit_tpa_id,
        credit_plan_id=payload.credit_plan_id,
        principal_member_name=payload.principal_member_name,
        principal_member_address=payload.principal_member_address,
        policy_number=payload.policy_number,
        policy_name=payload.policy_name,
        family_id=payload.family_id,
        is_active=True,
    )
    db.add(p)
    db.flush()  # get id
    p.uhid = make_uhid(p.id)

    # create initial address if sent
    if payload.address:
        a = PatientAddress(
            patient_id=p.id,
            type=payload.address.type or "current",
            line1=payload.address.line1,
            line2=payload.address.line2 or "",
            city=payload.address.city or "",
            state=payload.address.state or "",
            pincode=payload.address.pincode or "",
            country=payload.address.country or "India",
        )
        db.add(a)

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        msg = str(e.orig).lower()
        if "phone" in msg:
            raise HTTPException(status_code=400,
                                detail="Phone already registered")
        if "email" in msg:
            raise HTTPException(status_code=400,
                                detail="Email already registered")
        raise
    db.refresh(p)
    return serialize_patient(p, db)


@router.get("", response_model=List[PatientOut])
@router.get("/", response_model=List[PatientOut])
def list_patients(
        q: Optional[str] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    List patients.
    Works for both:
      - GET /api/patients
      - GET /api/patients/
    """
    if not has_perm(user, "patients.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    qry = db.query(Patient).filter(Patient.is_active.is_(True))
    if q:
        ql = f"%{q.lower()}%"
        qry = qry.filter((Patient.uhid.like(ql))
                         | (Patient.phone.like(ql))
                         | (Patient.email.like(ql))
                         | (Patient.first_name.like(ql))
                         | (Patient.last_name.like(ql)))
    patients = qry.order_by(Patient.id.desc()).limit(200).all()
    return [serialize_patient(p, db) for p in patients]


@router.get("/{patient_id}", response_model=PatientOut)
def get_patient(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    p = db.query(Patient).get(patient_id)
    if not p or not p.is_active:
        raise HTTPException(status_code=404, detail="Not found")
    return serialize_patient(p, db)


@router.put("/{patient_id}", response_model=PatientOut)
def update_patient(
        patient_id: int,
        payload: PatientUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    p = db.query(Patient).get(patient_id)
    if not p or not p.is_active:
        raise HTTPException(status_code=404, detail="Not found")

    data = payload.dict(exclude_unset=True)

    # uniqueness when changing phone/email
    if "phone" in data:
        new_phone = data["phone"]
        if new_phone:
            exists = (db.query(Patient).filter(Patient.phone == new_phone,
                                               Patient.id
                                               != patient_id).first())
            if exists:
                raise HTTPException(status_code=400,
                                    detail="Phone already registered")

    if "email" in data:
        new_email = data["email"]
        if new_email:
            exists = (db.query(Patient).filter(Patient.email == new_email,
                                               Patient.id
                                               != patient_id).first())
            if exists:
                raise HTTPException(status_code=400,
                                    detail="Email already registered")

    for k, v in data.items():
        setattr(p, k, v)

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        msg = str(e.orig).lower()
        if "phone" in msg:
            raise HTTPException(status_code=400,
                                detail="Phone already registered")
        if "email" in msg:
            raise HTTPException(status_code=400,
                                detail="Email already registered")
        raise
    db.refresh(p)
    return serialize_patient(p, db)


@router.patch("/{patient_id}/deactivate")
def deactivate_patient(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.deactivate"):
        raise HTTPException(status_code=403, detail="Not permitted")
    p = db.query(Patient).get(patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    p.is_active = False
    db.commit()
    return {"message": "Deactivated"}


# -------- addresses --------


@router.post("/{patient_id}/addresses", response_model=AddressOut)
def add_address(
        patient_id: int,
        payload: AddressIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.addresses.create"):
        raise HTTPException(status_code=403, detail="Not permitted")
    p = db.query(Patient).get(patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    a = PatientAddress(
        patient_id=patient_id,
        type=payload.type or "current",
        line1=payload.line1,
        line2=payload.line2,
        city=payload.city,
        state=payload.state,
        pincode=payload.pincode,
        country=payload.country or "India",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


@router.get("/{patient_id}/addresses", response_model=List[AddressOut])
def list_addresses(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.addresses.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return (db.query(PatientAddress).filter(
        PatientAddress.patient_id == patient_id).order_by(
            PatientAddress.id.desc()).all())


@router.put("/addresses/{addr_id}", response_model=AddressOut)
def update_address(
        addr_id: int,
        payload: AddressIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.addresses.update"):
        raise HTTPException(status_code=403, detail="Not permitted")
    a = db.query(PatientAddress).get(addr_id)
    if not a:
        raise HTTPException(status_code=404, detail="Address not found")
    for k, v in payload.dict(exclude_unset=True).items():
        setattr(a, k, v)
    db.commit()
    db.refresh(a)
    return a


@router.delete("/addresses/{addr_id}")
def delete_address(
        addr_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.addresses.delete"):
        raise HTTPException(status_code=403, detail="Not permitted")
    a = db.query(PatientAddress).get(addr_id)
    if not a:
        raise HTTPException(status_code=404, detail="Address not found")
    db.delete(a)
    db.commit()
    return {"message": "Deleted"}


# -------- documents (upload) --------


@router.post("/{patient_id}/documents", response_model=DocumentOut)
async def upload_document(
        patient_id: int,
        type: str = Form("other"),
        file: UploadFile = File(...),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.attachments.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")
    p = db.query(Patient).get(patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    ext = os.path.splitext(file.filename or "")[1]
    safe_name = f"p{patient_id}_{os.urandom(6).hex()}{ext}"
    dest = UPLOAD_DIR.joinpath(safe_name)
    content = await file.read()
    dest.write_bytes(content)

    doc = PatientDocument(
        patient_id=patient_id,
        type=type,
        filename=file.filename or safe_name,
        mime=file.content_type or "",
        size=len(content),
        storage_path=str(dest),
        uploaded_by=user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@router.get("/{patient_id}/documents", response_model=List[DocumentOut])
def list_documents(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return (db.query(PatientDocument).filter(
        PatientDocument.patient_id == patient_id).order_by(
            PatientDocument.id.desc()).all())


# -------- consents --------


@router.post("/{patient_id}/consents", response_model=ConsentOut)
def create_consent(
        patient_id: int,
        payload: ConsentIn = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.consents.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    p = db.query(Patient).get(patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")

    c = PatientConsent(patient_id=patient_id,
                       type=payload.type,
                       text=payload.text)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.get("/{patient_id}/consents", response_model=List[ConsentOut])
def list_consents(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.consents.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    return (db.query(PatientConsent).filter(
        PatientConsent.patient_id == patient_id).order_by(
            PatientConsent.id.desc()).all())


# -------- print info (PDF / HTML) --------


@router.get("/{patient_id}/print-info")
def print_patient_info(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Print patient info:
    - If WeasyPrint and OS libs available -> return PDF
    - Else -> return HTML (so browser print works)
    """
    if not has_perm(user, "patients.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    p = db.query(Patient).get(patient_id)
    if not p or not p.is_active:
        raise HTTPException(status_code=404, detail="Not found")

    _, _, _, age_text, _ = calc_age(p.dob)
    addr = p.addresses[0] if p.addresses else None

    def safe(val: Optional[str]) -> str:
        return val or "—"

    address_block = "—"
    if addr:
        parts = [
            addr.line1 or "",
            addr.line2 or "",
            " ".join(x for x in [addr.city, addr.state, addr.pincode]
                     if x and x.strip()),
            addr.country or "",
        ]
        address_block = "<br/>".join([p for p in parts if p])

    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Patient Info - {p.uhid}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 12px;
      margin: 16px;
      color: #111827;
    }}
    .card {{
      border-radius: 8px;
      border: 1px solid #e5e7eb;
      padding: 12px 16px;
      margin-bottom: 8px;
    }}
    .title {{
      font-size: 16px;
      font-weight: 600;
      margin-bottom: 4px;
    }}
    .muted {{
      color: #6b7280;
      font-size: 11px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4px 12px;
      margin-top: 6px;
    }}
    .label {{
      font-weight: 500;
    }}
    .value {{
      color: #111827;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="title">Patient Information</div>
    <div class="muted">UHID: {p.uhid}</div>

    <div class="grid">
      <div><span class="label">Name:</span> <span class="value">{safe(p.first_name)} {p.last_name or ""}</span></div>
      <div><span class="label">Gender:</span> <span class="value">{safe(p.gender)}</span></div>
      <div><span class="label">DOB:</span> <span class="value">{p.dob or "—"}</span></div>
      <div><span class="label">Age:</span> <span class="value">{age_text or "—"}</span></div>
      <div><span class="label">Blood Group:</span> <span class="value">{safe(p.blood_group)}</span></div>
      <div><span class="label">Marital Status:</span> <span class="value">{safe(p.marital_status)}</span></div>
      <div><span class="label">Mobile:</span> <span class="value">{safe(p.phone)}</span></div>
      <div><span class="label">Email:</span> <span class="value">{safe(p.email)}</span></div>
      <div><span class="label">Patient Type:</span> <span class="value">{safe(p.patient_type)}</span></div>
      <div><span class="label">Tag:</span> <span class="value">{safe(p.tag)}</span></div>
      <div><span class="label">Religion:</span> <span class="value">{safe(p.religion)}</span></div>
      <div><span class="label">Occupation:</span> <span class="value">{safe(p.occupation)}</span></div>
      <div><span class="label">File No:</span> <span class="value">{safe(p.file_number)}</span></div>
      <div><span class="label">File Location:</span> <span class="value">{safe(p.file_location)}</span></div>
      <div><span class="label">Guardian:</span> <span class="value">{safe(p.guardian_name)}</span></div>
      <div><span class="label">Guardian Phone:</span> <span class="value">{safe(p.guardian_phone)}</span></div>
      <div><span class="label">ID Proof:</span> <span class="value">{safe(p.id_proof_type)} {safe(p.id_proof_no)}</span></div>
      <div><span class="label">Aadhaar last 4:</span> <span class="value">{safe(p.aadhar_last4)}</span></div>
    </div>
  </div>

  <div class="card">
    <div class="title">Address</div>
    <div class="muted">Primary address</div>
    <div class="value">
      {address_block}
    </div>
  </div>
</body>
</html>
    """.strip()

    # Lazy + safe import of WeasyPrint
    try:
        from weasyprint import HTML as _HTML  # type: ignore
        HTML = _HTML
    except Exception:
        HTML = None

    if HTML is None:
        # Fallback: return HTML so browser print dialog can be used
        return StreamingResponse(
            io.BytesIO(html.encode("utf-8")),
            media_type="text/html; charset=utf-8",
        )

    pdf_bytes = HTML(string=html).write_pdf()
    filename = f"patient-{p.uhid}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
