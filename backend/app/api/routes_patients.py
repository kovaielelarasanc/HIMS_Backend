import os
from pathlib import Path
from typing import List, Optional
from fastapi import Body
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db, current_user as auth_current_user
from app.core.config import settings
from app.models.user import User
from app.models.patient import Patient, PatientAddress, PatientDocument, PatientConsent
from app.schemas.patient import (PatientCreate, PatientUpdate, PatientOut,
                                 AddressIn, AddressOut, DocumentOut, ConsentIn,
                                 ConsentOut)

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


# --------- endpoints ----------
@router.post("/", response_model=PatientOut)
def create_patient(payload: PatientCreate,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    if not has_perm(user, "patients.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # 🔒 API-level uniqueness checks
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
        first_name=payload.first_name.strip(),
        last_name=(payload.last_name or "").strip(),
        gender=payload.gender,
        dob=payload.dob,
        phone=payload.phone,
        email=payload.email,
        aadhar_last4=(payload.aadhar_last4 or "")[:4] or None,
        is_active=True,
    )
    db.add(p)
    db.flush()  # get id
    p.uhid = make_uhid(p.id)

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

    # 🔐 catch DB unique-index violations gracefully
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
    return p


@router.get("/", response_model=List[PatientOut])
def list_patients(
        q: Optional[str] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    qry = db.query(Patient).filter(Patient.is_active.is_(True))
    if q:
        ql = f"%{q.lower()}%"
        qry = qry.filter((Patient.uhid.like(ql)) | (Patient.phone.like(ql))
                         | (Patient.email.like(ql))
                         | (Patient.first_name.like(ql))
                         | (Patient.last_name.like(ql)))
    return qry.order_by(Patient.id.desc()).limit(200).all()


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
    return p


@router.put("/{patient_id}", response_model=PatientOut)
def update_patient(patient_id: int,
                   payload: PatientUpdate,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    if not has_perm(user, "patients.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    p = db.query(Patient).get(patient_id)
    if not p or not p.is_active:
        raise HTTPException(status_code=404, detail="Not found")

    # 🔒 uniqueness when changing phone/email
    if "phone" in payload.dict(exclude_unset=True):
        new_phone = payload.phone
        if new_phone:
            exists = db.query(Patient).filter(Patient.phone == new_phone,
                                              Patient.id
                                              != patient_id).first()
            if exists:
                raise HTTPException(status_code=400,
                                    detail="Phone already registered")

    if "email" in payload.dict(exclude_unset=True):
        new_email = payload.email
        if new_email:
            exists = db.query(Patient).filter(Patient.email == new_email,
                                              Patient.id
                                              != patient_id).first()
            if exists:
                raise HTTPException(status_code=400,
                                    detail="Email already registered")

    for k, v in payload.dict(exclude_unset=True).items():
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
    return p


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
def add_address(patient_id: int,
                payload: AddressIn,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    if not has_perm(user, "patients.addresses.create"):
        raise HTTPException(status_code=403, detail="Not permitted")
    p = db.query(Patient).get(patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    a = PatientAddress(patient_id=patient_id, **payload.dict())
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


@router.get("/{patient_id}/addresses", response_model=List[AddressOut])
def list_addresses(patient_id: int,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    if not has_perm(user, "patients.addresses.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return db.query(PatientAddress).filter(
        PatientAddress.patient_id == patient_id).order_by(
            PatientAddress.id.desc()).all()


@router.put("/addresses/{addr_id}", response_model=AddressOut)
def update_address(addr_id: int,
                   payload: AddressIn,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
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
def delete_address(addr_id: int,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
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
    payload: ConsentIn = Body(...),  # make intent explicit: JSON body
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user)):
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
def list_consents(patient_id: int,
                  db: Session = Depends(get_db),
                  user: User = Depends(auth_current_user)):
    if not has_perm(user, "patients.consents.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    return (db.query(PatientConsent).filter(
        PatientConsent.patient_id == patient_id).order_by(
            PatientConsent.id.desc()).all())
