from __future__ import annotations

from datetime import datetime, date
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models.vitals_registers import (
    BirthRegister, DeathRegister, StillBirthRegister, MCCDRecord,
    VitalSequenceCounter, VitalAmendmentRequest, utcnow
)
from app.models.vital_audit import VitalEventAudit
from app.schemas.vitals_registers import (
    BirthCreate, BirthUpdate, BirthOut, BirthSubmit,
    DeathCreate, DeathUpdate, DeathOut, DeathSubmit,
    StillBirthCreate, StillBirthOut,
    MCCDCreateOrUpdate, ActionNote, VoidRequest,
    AmendmentCreate, AmendmentOut, AmendmentReview
)
from app.services.pdfs.vitals_forms import build_birth_form_pdf, build_death_form_pdf, build_mccd_pdf

# Your project deps (adjust import paths if needed)
from app.api.deps import get_db, current_user  # noqa


router = APIRouter(prefix="/vitals", tags=["Vital Registers"])


# ---------------------------
# Permission helper (works with your existing user object)
# ---------------------------
def need_any(user: Any, codes: List[str]):
    perm_candidates = []
    for attr in ("perm_codes", "permissions", "perms"):
        v = getattr(user, attr, None)
        if v:
            perm_candidates = v
            break

    perm_set = set()
    if isinstance(perm_candidates, dict):
        # {module:[{code}]} or {module:[code]} - flatten
        for val in perm_candidates.values():
            if isinstance(val, list):
                for x in val:
                    if isinstance(x, dict) and x.get("code"):
                        perm_set.add(x["code"])
                    elif isinstance(x, str):
                        perm_set.add(x)
    elif isinstance(perm_candidates, list):
        for x in perm_candidates:
            if isinstance(x, dict) and x.get("code"):
                perm_set.add(x["code"])
            elif isinstance(x, str):
                perm_set.add(x)

    # wildcard
    if "*" in perm_set:
        return

    if not any(c in perm_set for c in codes):
        raise HTTPException(status_code=403, detail="Permission denied")


def _audit(
    db: Session,
    request: Request,
    entity_type: str,
    entity_id: int,
    action: str,
    actor_user_id: Optional[int],
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
    note: Optional[str] = None,
):
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    db.add(VitalEventAudit(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_user_id=actor_user_id,
        ip_addr=ip,
        user_agent=ua,
        before=before,
        after=after,
        note=note
    ))


def _as_dict_birth(x: BirthRegister) -> Dict[str, Any]:
    return {
        "id": x.id,
        "internal_no": x.internal_no,
        "birth_datetime": x.birth_datetime.isoformat() if x.birth_datetime else None,
        "child_name": x.child_name,
        "child_sex": x.child_sex,
        "mother_name": x.mother_name,
        "father_name": x.father_name,
        "status": x.status,
        "crs_registration_no": x.crs_registration_no,
    }


def _as_dict_death(x: DeathRegister) -> Dict[str, Any]:
    return {
        "id": x.id,
        "internal_no": x.internal_no,
        "death_datetime": x.death_datetime.isoformat() if x.death_datetime else None,
        "deceased_name": x.deceased_name,
        "sex": x.sex,
        "status": x.status,
        "crs_registration_no": x.crs_registration_no,
        "mccd_given_to_kin": x.mccd_given_to_kin,
    }


def _next_internal_no(db: Session, kind: str, prefix: str) -> str:
    year = datetime.utcnow().year
    # lock row for update to avoid duplicates in concurrent usage
    row = db.execute(
        select(VitalSequenceCounter).where(
            VitalSequenceCounter.kind == kind,
            VitalSequenceCounter.year == year
        ).with_for_update()
    ).scalar_one_or_none()

    if not row:
        row = VitalSequenceCounter(kind=kind, year=year, next_value=1)
        db.add(row)
        db.flush()

    val = row.next_value
    row.next_value = val + 1
    db.flush()

    return f"{prefix}-{year}-{val:06d}"


def _ensure_editable(status: str):
    if status in ("FINALIZED", "SUBMITTED", "VOIDED"):
        raise HTTPException(status_code=409, detail=f"Record is locked (status={status}). Use amendment workflow.")


def _ensure_can_finalize(status: str):
    if status not in ("DRAFT", "VERIFIED"):
        raise HTTPException(status_code=409, detail=f"Cannot finalize from status={status}")


def _ensure_can_submit(status: str):
    if status != "FINALIZED":
        raise HTTPException(status_code=409, detail="Only FINALIZED records can be submitted")


# ==========================================================
# Birth Register
# ==========================================================
@router.post("/births", response_model=BirthOut)
def create_birth(
    payload: BirthCreate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.birth.create", "vitals.birth.manage"])

    internal_no = _next_internal_no(db, kind="BIRTH", prefix="BR")

    obj = BirthRegister(
        internal_no=internal_no,
        patient_id=payload.patient_id,
        admission_id=payload.admission_id,

        birth_datetime=payload.birth_datetime,
        place_of_birth=payload.place_of_birth or "Hospital",
        delivery_method=payload.delivery_method,
        gestation_weeks=payload.gestation_weeks,
        birth_weight_kg=payload.birth_weight_kg,
        child_sex=payload.child_sex,
        child_name=payload.child_name,
        plurality=payload.plurality,
        birth_order=payload.birth_order,

        mother_name=payload.mother_name,
        mother_age_years=payload.mother_age_years,
        mother_dob=payload.mother_dob,
        mother_id_no=payload.mother_id_no,
        mother_mobile=payload.mother_mobile,
        mother_address=payload.mother_address.model_dump() if payload.mother_address else None,

        father_name=payload.father_name,
        father_age_years=payload.father_age_years,
        father_dob=payload.father_dob,
        father_id_no=payload.father_id_no,
        father_mobile=payload.father_mobile,
        father_address=payload.father_address.model_dump() if payload.father_address else None,

        informant_user_id=payload.informant_user_id or getattr(user, "id", None),
        informant_name=payload.informant_name or getattr(user, "name", None),
        informant_designation=payload.informant_designation,
        status="DRAFT",
        created_at=utcnow(),
    )

    db.add(obj)
    db.flush()

    _audit(db, request, "birth", obj.id, "create", getattr(user, "id", None), after=_as_dict_birth(obj))
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/births", response_model=List[BirthOut])
def list_births(
    db: Session = Depends(get_db),
    user=Depends(current_user),
    q: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    need_any(user, ["vitals.birth.view", "vitals.birth.manage"])

    stmt = select(BirthRegister).where(BirthRegister.is_active == True)  # noqa
    if status:
        stmt = stmt.where(BirthRegister.status == status)
    if date_from:
        stmt = stmt.where(BirthRegister.birth_datetime >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        stmt = stmt.where(BirthRegister.birth_datetime <= datetime.combine(date_to, datetime.max.time()))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            (BirthRegister.internal_no.like(like)) |
            (BirthRegister.mother_name.like(like)) |
            (BirthRegister.father_name.like(like)) |
            (BirthRegister.child_name.like(like)) |
            (BirthRegister.crs_registration_no.like(like))
        )

    stmt = stmt.order_by(BirthRegister.birth_datetime.desc()).limit(limit)
    return db.execute(stmt).scalars().all()


@router.get("/births/{birth_id}", response_model=BirthOut)
def get_birth(
    birth_id: int,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.birth.view", "vitals.birth.manage"])

    obj = db.get(BirthRegister, birth_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Birth record not found")
    return obj


@router.patch("/births/{birth_id}", response_model=BirthOut)
def update_birth(
    birth_id: int,
    payload: BirthUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.birth.update", "vitals.birth.manage"])

    obj = db.get(BirthRegister, birth_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Birth record not found")

    _ensure_editable(obj.status)

    before = _as_dict_birth(obj)

    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k in ("mother_address", "father_address") and v is not None:
            setattr(obj, k, v.model_dump() if hasattr(v, "model_dump") else v)
        else:
            setattr(obj, k, v)

    obj.updated_at = utcnow()
    db.flush()

    _audit(db, request, "birth", obj.id, "update", getattr(user, "id", None), before=before, after=_as_dict_birth(obj))
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/births/{birth_id}/verify", response_model=BirthOut)
def verify_birth(
    birth_id: int,
    payload: ActionNote,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.birth.verify", "vitals.birth.manage"])
    obj = db.get(BirthRegister, birth_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Birth record not found")

    _ensure_editable(obj.status)

    before = _as_dict_birth(obj)
    obj.status = "VERIFIED"
    obj.verified_by_user_id = getattr(user, "id", None)
    obj.verified_at = utcnow()
    obj.updated_at = utcnow()
    db.flush()

    _audit(db, request, "birth", obj.id, "verify", getattr(user, "id", None), before=before, after=_as_dict_birth(obj), note=payload.note)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/births/{birth_id}/finalize", response_model=BirthOut)
def finalize_birth(
    birth_id: int,
    payload: ActionNote,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.birth.finalize", "vitals.birth.manage"])
    obj = db.get(BirthRegister, birth_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Birth record not found")

    _ensure_can_finalize(obj.status)

    # minimal completeness checks (CRS-friendly)
    if not obj.mother_name or not obj.child_sex or not obj.birth_datetime:
        raise HTTPException(status_code=422, detail="Missing mandatory fields for finalize")

    before = _as_dict_birth(obj)
    obj.status = "FINALIZED"
    obj.locked_at = utcnow()
    obj.finalized_by_user_id = getattr(user, "id", None)
    obj.finalized_at = utcnow()
    obj.updated_at = utcnow()
    db.flush()

    _audit(db, request, "birth", obj.id, "finalize", getattr(user, "id", None), before=before, after=_as_dict_birth(obj), note=payload.note)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/births/{birth_id}/submit", response_model=BirthOut)
def submit_birth(
    birth_id: int,
    payload: BirthSubmit,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.birth.submit", "vitals.birth.manage"])
    obj = db.get(BirthRegister, birth_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Birth record not found")

    _ensure_can_submit(obj.status)

    before = _as_dict_birth(obj)

    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(obj, k, v)
    obj.status = "SUBMITTED"
    obj.submitted_by_user_id = getattr(user, "id", None)
    obj.submitted_at = utcnow()
    obj.updated_at = utcnow()
    db.flush()

    _audit(db, request, "birth", obj.id, "submit", getattr(user, "id", None), before=before, after=_as_dict_birth(obj))
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/births/{birth_id}/void", response_model=BirthOut)
def void_birth(
    birth_id: int,
    payload: VoidRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.birth.void", "vitals.birth.manage"])
    obj = db.get(BirthRegister, birth_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Birth record not found")

    if obj.status == "VOIDED":
        return obj

    before = _as_dict_birth(obj)
    obj.status = "VOIDED"
    obj.locked_at = utcnow()
    obj.updated_at = utcnow()
    db.flush()

    _audit(db, request, "birth", obj.id, "void", getattr(user, "id", None), before=before, after=_as_dict_birth(obj), note=payload.reason)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/births/{birth_id}/form1.pdf")
def birth_form_pdf(
    birth_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.birth.view", "vitals.birth.export", "vitals.birth.manage"])

    obj = db.get(BirthRegister, birth_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Birth record not found")

    # Optional: plug your branding table here
    hospital = {"name": "NUTRYAH Facility", "address": "", "phone": "", "website": ""}

    pdf = build_birth_form_pdf(obj, hospital=hospital)
    _audit(db, request, "birth", obj.id, "print", getattr(user, "id", None), note="birth_form_pdf")
    db.commit()

    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="birth_{obj.internal_no}.pdf"'})


# ==========================================================
# Stillbirth Register (minimal endpoints)
# ==========================================================
@router.post("/stillbirths", response_model=StillBirthOut)
def create_stillbirth(
    payload: StillBirthCreate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.stillbirth.create", "vitals.stillbirth.manage"])

    internal_no = _next_internal_no(db, kind="STILLBIRTH", prefix="SB")
    obj = StillBirthRegister(
        internal_no=internal_no,
        patient_id=payload.patient_id,
        admission_id=payload.admission_id,
        event_datetime=payload.event_datetime,
        place_of_occurrence=payload.place_of_occurrence or "Hospital",
        gestation_weeks=payload.gestation_weeks,
        foetus_sex=payload.foetus_sex,
        mother_name=payload.mother_name,
        mother_age_years=payload.mother_age_years,
        mother_address=payload.mother_address.model_dump() if payload.mother_address else None,
        father_name=payload.father_name,
        father_address=payload.father_address.model_dump() if payload.father_address else None,
        informant_user_id=getattr(user, "id", None),
        informant_name=getattr(user, "name", None),
        status="DRAFT",
        created_at=utcnow(),
    )
    db.add(obj)
    db.flush()

    _audit(db, request, "stillbirth", obj.id, "create", getattr(user, "id", None))
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/stillbirths", response_model=List[StillBirthOut])
def list_stillbirths(
    db: Session = Depends(get_db),
    user=Depends(current_user),
    limit: int = Query(default=100, ge=1, le=500),
):
    need_any(user, ["vitals.stillbirth.view", "vitals.stillbirth.manage"])
    stmt = select(StillBirthRegister).order_by(StillBirthRegister.event_datetime.desc()).limit(limit)
    return db.execute(stmt).scalars().all()


# ==========================================================
# Death Register + MCCD
# ==========================================================
@router.post("/deaths", response_model=DeathOut)
def create_death(
    payload: DeathCreate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.death.create", "vitals.death.manage"])

    internal_no = _next_internal_no(db, kind="DEATH", prefix="DR")

    obj = DeathRegister(
        internal_no=internal_no,
        patient_id=payload.patient_id,
        admission_id=payload.admission_id,

        death_datetime=payload.death_datetime,
        place_of_death=payload.place_of_death or "Hospital",
        ward_or_unit=payload.ward_or_unit,

        deceased_name=payload.deceased_name,
        sex=payload.sex,
        age_years=payload.age_years,
        dob=payload.dob,

        address=payload.address.model_dump() if payload.address else None,
        id_no=payload.id_no,
        mobile=payload.mobile,
        manner_of_death=payload.manner_of_death,

        informant_user_id=getattr(user, "id", None),
        informant_name=getattr(user, "name", None),
        status="DRAFT",
        created_at=utcnow(),
    )

    db.add(obj)
    db.flush()

    _audit(db, request, "death", obj.id, "create", getattr(user, "id", None), after=_as_dict_death(obj))
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/deaths", response_model=List[DeathOut])
def list_deaths(
    db: Session = Depends(get_db),
    user=Depends(current_user),
    q: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    need_any(user, ["vitals.death.view", "vitals.death.manage"])
    stmt = select(DeathRegister)
    if status:
        stmt = stmt.where(DeathRegister.status == status)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            (DeathRegister.internal_no.like(like)) |
            (DeathRegister.deceased_name.like(like)) |
            (DeathRegister.crs_registration_no.like(like))
        )
    stmt = stmt.order_by(DeathRegister.death_datetime.desc()).limit(limit)
    return db.execute(stmt).scalars().all()


@router.get("/deaths/{death_id}", response_model=DeathOut)
def get_death(
    death_id: int,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.death.view", "vitals.death.manage"])
    obj = db.get(DeathRegister, death_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Death record not found")
    return obj


@router.patch("/deaths/{death_id}", response_model=DeathOut)
def update_death(
    death_id: int,
    payload: DeathUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.death.update", "vitals.death.manage"])
    obj = db.get(DeathRegister, death_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Death record not found")

    _ensure_editable(obj.status)
    before = _as_dict_death(obj)

    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k == "address" and v is not None:
            setattr(obj, k, v.model_dump() if hasattr(v, "model_dump") else v)
        else:
            setattr(obj, k, v)

    obj.updated_at = utcnow()
    db.flush()

    _audit(db, request, "death", obj.id, "update", getattr(user, "id", None), before=before, after=_as_dict_death(obj))
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/deaths/{death_id}/mccd", response_model=DeathOut)
def upsert_mccd(
    death_id: int,
    payload: MCCDCreateOrUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.mccd.create", "vitals.mccd.manage", "vitals.death.manage"])
    death = db.get(DeathRegister, death_id)
    if not death:
        raise HTTPException(status_code=404, detail="Death record not found")

    # allow MCCD edits while not SUBMITTED/VOIDED; if FINALIZED allow only by doctor/authorized
    if death.status in ("SUBMITTED", "VOIDED"):
        raise HTTPException(status_code=409, detail="Cannot edit MCCD after submission/void")

    mccd = death.mccd
    before = _as_dict_death(death)

    if not mccd:
        mccd = MCCDRecord(
            death_id=death.id,
            immediate_cause=payload.immediate_cause,
            antecedent_cause=payload.antecedent_cause,
            underlying_cause=payload.underlying_cause,
            other_significant_conditions=payload.other_significant_conditions,
            pregnancy_status=payload.pregnancy_status,
            tobacco_use=payload.tobacco_use,
            certifying_doctor_user_id=getattr(user, "id", None),
            certified_at=utcnow(),
            signed=False,
            created_at=utcnow(),
        )
        db.add(mccd)
    else:
        mccd.immediate_cause = payload.immediate_cause
        mccd.antecedent_cause = payload.antecedent_cause
        mccd.underlying_cause = payload.underlying_cause
        mccd.other_significant_conditions = payload.other_significant_conditions
        mccd.pregnancy_status = payload.pregnancy_status
        mccd.tobacco_use = payload.tobacco_use
        mccd.certifying_doctor_user_id = mccd.certifying_doctor_user_id or getattr(user, "id", None)
        mccd.certified_at = mccd.certified_at or utcnow()
        mccd.updated_at = utcnow()

    death.updated_at = utcnow()
    db.flush()

    _audit(db, request, "mccd", mccd.id, "upsert", getattr(user, "id", None), note=f"death_id={death.id}")
    _audit(db, request, "death", death.id, "update", getattr(user, "id", None), before=before, after=_as_dict_death(death), note="mccd_updated")
    db.commit()
    db.refresh(death)
    return death


@router.post("/deaths/{death_id}/mccd/sign", response_model=DeathOut)
def sign_mccd(
    death_id: int,
    payload: ActionNote,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.mccd.sign", "vitals.mccd.manage", "vitals.death.manage"])
    death = db.get(DeathRegister, death_id)
    if not death:
        raise HTTPException(status_code=404, detail="Death record not found")
    if not death.mccd:
        raise HTTPException(status_code=422, detail="MCCD not recorded")

    if death.status in ("SUBMITTED", "VOIDED"):
        raise HTTPException(status_code=409, detail="Cannot sign after submission/void")

    mccd = death.mccd
    mccd.signed = True
    mccd.signed_at = utcnow()
    mccd.updated_at = utcnow()
    db.flush()

    _audit(db, request, "mccd", mccd.id, "sign", getattr(user, "id", None), note=payload.note)
    db.commit()
    db.refresh(death)
    return death


@router.post("/deaths/{death_id}/verify", response_model=DeathOut)
def verify_death(
    death_id: int,
    payload: ActionNote,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.death.verify", "vitals.death.manage"])
    obj = db.get(DeathRegister, death_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Death record not found")

    _ensure_editable(obj.status)

    before = _as_dict_death(obj)
    obj.status = "VERIFIED"
    obj.verified_by_user_id = getattr(user, "id", None)
    obj.verified_at = utcnow()
    obj.updated_at = utcnow()
    db.flush()

    _audit(db, request, "death", obj.id, "verify", getattr(user, "id", None), before=before, after=_as_dict_death(obj), note=payload.note)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/deaths/{death_id}/finalize", response_model=DeathOut)
def finalize_death(
    death_id: int,
    payload: ActionNote,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.death.finalize", "vitals.death.manage"])
    obj = db.get(DeathRegister, death_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Death record not found")

    _ensure_can_finalize(obj.status)

    # enforce MCCD presence & sign for institutional death finalize (medico-legal)
    if not obj.mccd or not obj.mccd.immediate_cause:
        raise HTTPException(status_code=422, detail="MCCD is required before finalizing death")
    if not obj.mccd.signed:
        raise HTTPException(status_code=422, detail="MCCD must be signed before finalizing death")

    if not obj.deceased_name or not obj.sex or not obj.death_datetime:
        raise HTTPException(status_code=422, detail="Missing mandatory fields for finalize")

    before = _as_dict_death(obj)
    obj.status = "FINALIZED"
    obj.locked_at = utcnow()
    obj.finalized_by_user_id = getattr(user, "id", None)
    obj.finalized_at = utcnow()
    obj.updated_at = utcnow()
    db.flush()

    _audit(db, request, "death", obj.id, "finalize", getattr(user, "id", None), before=before, after=_as_dict_death(obj), note=payload.note)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/deaths/{death_id}/submit", response_model=DeathOut)
def submit_death(
    death_id: int,
    payload: DeathSubmit,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.death.submit", "vitals.death.manage"])
    obj = db.get(DeathRegister, death_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Death record not found")

    _ensure_can_submit(obj.status)

    before = _as_dict_death(obj)

    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(obj, k, v)

    obj.status = "SUBMITTED"
    obj.submitted_by_user_id = getattr(user, "id", None)
    obj.submitted_at = utcnow()
    obj.updated_at = utcnow()
    db.flush()

    _audit(db, request, "death", obj.id, "submit", getattr(user, "id", None), before=before, after=_as_dict_death(obj))
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/deaths/{death_id}/mccd/given-to-kin", response_model=DeathOut)
def mark_mccd_given_to_kin(
    death_id: int,
    to_name: str = Query(..., min_length=2, max_length=120),
    request: Request = None,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.mccd.manage", "vitals.death.manage", "vitals.death.update"])
    obj = db.get(DeathRegister, death_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Death record not found")
    if not obj.mccd:
        raise HTTPException(status_code=422, detail="MCCD not recorded")

    obj.mccd_given_to_kin = True
    obj.mccd_given_to_name = to_name.strip()
    obj.mccd_given_at = utcnow()
    obj.updated_at = utcnow()
    db.flush()

    if request is not None:
        _audit(db, request, "death", obj.id, "mccd_given_to_kin", getattr(user, "id", None), note=f"to={to_name.strip()}")
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/deaths/{death_id}/form2.pdf")
def death_form_pdf(
    death_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.death.view", "vitals.death.export", "vitals.death.manage"])
    obj = db.get(DeathRegister, death_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Death record not found")

    hospital = {"name": "NUTRYAH Facility", "address": "", "phone": "", "website": ""}

    pdf = build_death_form_pdf(obj, hospital=hospital)
    _audit(db, request, "death", obj.id, "print", getattr(user, "id", None), note="death_form_pdf")
    db.commit()

    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="death_{obj.internal_no}.pdf"'})


@router.get("/deaths/{death_id}/mccd.pdf")
def mccd_pdf(
    death_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.mccd.view", "vitals.mccd.export", "vitals.death.manage"])
    obj = db.get(DeathRegister, death_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Death record not found")

    hospital = {"name": "NUTRYAH Facility", "address": "", "phone": "", "website": ""}

    pdf = build_mccd_pdf(obj, hospital=hospital)
    _audit(db, request, "death", obj.id, "print", getattr(user, "id", None), note="mccd_pdf")
    db.commit()

    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="mccd_{obj.internal_no}.pdf"'})


# ==========================================================
# Amendments (controlled changes after finalize/submit)
# ==========================================================
@router.post("/amendments", response_model=AmendmentOut)
def create_amendment_request(
    payload: AmendmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.amendments.request", "vitals.amendments.manage"])

    req = VitalAmendmentRequest(
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        requested_changes=payload.requested_changes,
        reason=payload.reason,
        status="PENDING",
        requested_by_user_id=getattr(user, "id", None),
        created_at=utcnow(),
    )
    db.add(req)
    db.flush()

    _audit(db, request, "amendment", req.id, "create", getattr(user, "id", None), note=f"{payload.entity_type}:{payload.entity_id}")
    db.commit()
    db.refresh(req)
    return req


@router.get("/amendments", response_model=List[AmendmentOut])
def list_amendments(
    db: Session = Depends(get_db),
    user=Depends(current_user),
    entity_type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    need_any(user, ["vitals.amendments.view", "vitals.amendments.manage"])

    stmt = select(VitalAmendmentRequest)
    if entity_type:
        stmt = stmt.where(VitalAmendmentRequest.entity_type == entity_type)
    if status:
        stmt = stmt.where(VitalAmendmentRequest.status == status)
    stmt = stmt.order_by(VitalAmendmentRequest.created_at.desc()).limit(limit)
    return db.execute(stmt).scalars().all()


def _apply_amendment_to_birth(obj: BirthRegister, changes: Dict[str, Any]):
    for field, diff in changes.items():
        if "to" in diff:
            setattr(obj, field, diff["to"])


def _apply_amendment_to_death(obj: DeathRegister, changes: Dict[str, Any]):
    for field, diff in changes.items():
        if "to" in diff:
            setattr(obj, field, diff["to"])


@router.post("/amendments/{amendment_id}/review", response_model=AmendmentOut)
def review_amendment(
    amendment_id: int,
    payload: AmendmentReview,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    need_any(user, ["vitals.amendments.approve", "vitals.amendments.manage"])

    req = db.get(VitalAmendmentRequest, amendment_id)
    if not req:
        raise HTTPException(status_code=404, detail="Amendment request not found")
    if req.status != "PENDING":
        raise HTTPException(status_code=409, detail="Amendment already reviewed")

    req.status = payload.status
    req.review_note = payload.review_note
    req.reviewed_by_user_id = getattr(user, "id", None)
    req.reviewed_at = utcnow()

    # If approved, apply changes
    if payload.status == "APPROVED":
        if req.entity_type == "birth":
            obj = db.get(BirthRegister, req.entity_id)
            if not obj:
                raise HTTPException(status_code=404, detail="Birth record not found")
            _apply_amendment_to_birth(obj, req.requested_changes)
            obj.updated_at = utcnow()
        elif req.entity_type == "death":
            obj = db.get(DeathRegister, req.entity_id)
            if not obj:
                raise HTTPException(status_code=404, detail="Death record not found")
            _apply_amendment_to_death(obj, req.requested_changes)
            obj.updated_at = utcnow()
        elif req.entity_type == "stillbirth":
            obj = db.get(StillBirthRegister, req.entity_id)
            if not obj:
                raise HTTPException(status_code=404, detail="Stillbirth record not found")
            for field, diff in req.requested_changes.items():
                if "to" in diff:
                    setattr(obj, field, diff["to"])
            obj.updated_at = utcnow()

    db.flush()
    _audit(db, request, "amendment", req.id, "review", getattr(user, "id", None), note=f"status={payload.status}")
    db.commit()
    db.refresh(req)
    return req
