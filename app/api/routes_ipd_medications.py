# FILE: app/api/routes_ipd_medications.py
from __future__ import annotations
from pathlib import Path
from fastapi.responses import Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from datetime import datetime, time, timedelta, date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.user import User as UserModel
from app.models.ipd import (
    IpdAdmission,
    IpdMedicationOrder,
    IpdMedicationAdministration,
    IpdDrugChartMeta,
    IpdIvFluidOrder,
    IpdDrugChartNurseRow,
    IpdDrugChartDoctorAuth,
    
)
from app.schemas.ipd import (
    # Medication orders + administration
    IpdMedicationOrderCreate,
    IpdMedicationOrderUpdate,
    IpdMedicationOrderOut,
    IpdMedicationAdministrationOut,
    # Drug chart meta
    IpdDrugChartMetaCreate,
    IpdDrugChartMetaUpdate,
    IpdDrugChartMetaOut,
    # IV fluids
    IpdIvFluidOrderCreate,
    IpdIvFluidOrderUpdate,
    IpdIvFluidOrderOut,
    # Nurse rows
    IpdDrugChartNurseRowCreate,
    IpdDrugChartNurseRowUpdate,
    IpdDrugChartNurseRowOut,
    # Doctor daily authorisation
    IpdDrugChartDoctorAuthCreate,
    IpdDrugChartDoctorAuthUpdate,
    IpdDrugChartDoctorAuthOut,
)

router = APIRouter(prefix="/ipd", tags=["IPD - Medications / Drug Chart"])

# ---------- Jinja environment for PDF templates ----------
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
_pdf_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)
# -------------------------------------------------------------------
# Helpers / Auth
# -------------------------------------------------------------------
def has_perm(user: UserModel, code: str) -> bool:
    """
    Simple RBAC helper. Admins bypass check.
    """
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if p.code == code:
                return True
    return False


def _get_admission_or_404(db: Session, admission_id: int) -> IpdAdmission:
    adm = db.query(IpdAdmission).filter(IpdAdmission.id == admission_id).first()
    if not adm:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Admission not found",
        )
    return adm


def _parse_frequency_to_times(freq: str) -> List[time]:
    """
    Very simple mapper of frequency string -> list of times in a day.
    Used to auto-generate Drug Chart administration rows (Hrs grid).
    """
    if not freq:
        return [time(9, 0)]

    f = freq.strip().upper()

    if f in {"OD", "QD"}:
        return [time(9, 0)]
    if f in {"BD", "BID"}:
        return [time(9, 0), time(21, 0)]
    if f in {"TDS", "TID", "TDS."}:
        return [time(6, 0), time(14, 0), time(22, 0)]
    if f in {"QID"}:
        return [time(6, 0), time(12, 0), time(18, 0), time(22, 0)]
    if f in {"HS", "QHS"}:
        return [time(21, 0)]

    # simple custom pattern: e.g. "1-0-1" => morning + night
    if "-" in f:
        parts = f.split("-")
        while len(parts) < 3:
            parts.append("0")
        parts = parts[:3]
        times: List[time] = []
        if parts[0] != "0":
            times.append(time(9, 0))
        if parts[1] != "0":
            times.append(time(14, 0))
        if parts[2] != "0":
            times.append(time(21, 0))
        if times:
            return times

    # fallback: 9 AM
    return [time(9, 0)]


def _generate_administration_rows_for_order(
    db: Session,
    *,
    order: IpdMedicationOrder,
) -> None:
    """
    Generate IpdMedicationAdministration (Drug Chart) rows for a given order.
    These rows map to the Hrs/Sign grid in the paper drug chart.
    """
    start = order.start_datetime or datetime.utcnow()
    days = order.duration_days or 1

    times_per_day = _parse_frequency_to_times(order.frequency or "")

    for day_idx in range(days):
        base_date = (start + timedelta(days=day_idx)).date()
        for t in times_per_day:
            scheduled_dt = datetime.combine(base_date, t)

            admin = IpdMedicationAdministration(
                admission_id=order.admission_id,
                med_order_id=order.id,
                scheduled_datetime=scheduled_dt,
                given_status="pending",
            )
            db.add(admin)


# -------------------------------------------------------------------
# Medication Orders (regular / SOS / STAT / premed)
# -------------------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/medications",
    response_model=List[IpdMedicationOrderOut],
)
def list_medication_orders_for_admission(
    admission_id: int,
    order_type: Optional[str] = Query(
        None,
        description="Filter by order_type if set: regular / sos / stat / premed",
    ),
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    List all medication orders for a given admission.
    This acts as the electronic drug order sheet.
    """
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    q = (
        db.query(IpdMedicationOrder)
        .filter(IpdMedicationOrder.admission_id == admission_id)
        .order_by(IpdMedicationOrder.start_datetime.asc(), IpdMedicationOrder.id.asc())
    )
    if order_type:
        q = q.filter(IpdMedicationOrder.order_type == order_type.lower())

    return q.all()


@router.post(
    "/admissions/{admission_id}/medications",
    response_model=IpdMedicationOrderOut,
    status_code=status.HTTP_201_CREATED,
)
def create_medication_order_for_admission(
    admission_id: int,
    payload: IpdMedicationOrderCreate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Create a new medication order for an admission and auto-generate Drug Chart rows.

    Supports different order types:
      - regular (default)
      - sos
      - stat
      - premed
    """
    if not (has_perm(user, "ipd.doctor") or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    # Build order explicitly to avoid field-name mismatches (ordered_by vs ordered_by_id).
    order = IpdMedicationOrder(
        admission_id=admission_id,
        drug_id=payload.drug_id,
        drug_name=payload.drug_name,
        dose=payload.dose,
        dose_unit=payload.dose_unit or "",
        route=payload.route or "",
        frequency=payload.frequency or "",
        duration_days=payload.duration_days,
        start_datetime=payload.start_datetime or datetime.utcnow(),
        stop_datetime=payload.stop_datetime,
        special_instructions=payload.special_instructions or "",
        order_status=payload.order_status or "active",
        order_type=getattr(payload, "order_type", None) or "regular",
        ordered_by=user.id,
    )

    db.add(order)
    db.flush()  # get order.id

    # Auto-generate Hrs/Sign entries only for regular / stat orders (configurable).
    if order.order_type in {"regular", "stat"} and (order.duration_days or 0) > 0:
        _generate_administration_rows_for_order(db, order=order)

    db.commit()
    db.refresh(order)
    return order


@router.put(
    "/medications/{order_id}",
    response_model=IpdMedicationOrderOut,
)
def update_medication_order(
    order_id: int,
    payload: IpdMedicationOrderUpdate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Update an existing medication order.
    Use /medications/{order_id}/regenerate-admin to refresh Drug Chart entries
    if frequency/duration/start time changed.
    """
    if not (has_perm(user, "ipd.doctor") or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    order = db.query(IpdMedicationOrder).filter(IpdMedicationOrder.id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Medication order not found",
        )

    data = payload.dict(exclude_unset=True)
    # Never allow admission_id change from here
    data.pop("admission_id", None)

    # Map ordered_by_id (schema) -> ordered_by (model) if present
    ordered_by_id = data.pop("ordered_by_id", None)
    if ordered_by_id is not None:
        order.ordered_by = ordered_by_id

    for field, value in data.items():
        setattr(order, field, value)

    db.commit()
    db.refresh(order)
    return order


@router.post(
    "/medications/{order_id}/regenerate-admin",
    response_model=List[IpdMedicationAdministrationOut],
)
def regenerate_medication_administration(
    order_id: int,
    clear_existing: bool = Query(
        True,
        description="If true, delete existing admin rows for this order before regenerating.",
    ),
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Regenerate Drug Chart administration rows (Hrs/Sign grid) for a given
    medication order based on its start_datetime, duration_days & frequency.
    """
    if not (has_perm(user, "ipd.doctor") or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    order = db.query(IpdMedicationOrder).filter(IpdMedicationOrder.id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Medication order not found",
        )

    if clear_existing:
        db.query(IpdMedicationAdministration).filter(
            IpdMedicationAdministration.med_order_id == order_id
        ).delete()

    _generate_administration_rows_for_order(db, order=order)
    db.commit()

    admins = (
        db.query(IpdMedicationAdministration)
        .filter(IpdMedicationAdministration.med_order_id == order_id)
        .order_by(IpdMedicationAdministration.scheduled_datetime.asc())
        .all()
    )
    return admins


# -------------------------------------------------------------------
# Drug Chart (Medication Administration – Hrs / Nurse Sign grid)
# -------------------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/drug-chart",
    response_model=List[IpdMedicationAdministrationOut],
)
def get_drug_chart_for_admission(
    admission_id: int,
    from_datetime: Optional[datetime] = Query(
        None, description="Filter from this datetime (optional)"
    ),
    to_datetime: Optional[datetime] = Query(
        None, description="Filter up to this datetime (optional)"
    ),
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    List Drug Chart entries for an admission.
    These rows are used to build the graph/table PDF with Hrs & Nurse sign boxes.
    """
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    q = db.query(IpdMedicationAdministration).filter(
        IpdMedicationAdministration.admission_id == admission_id
    )

    if from_datetime is not None:
        q = q.filter(IpdMedicationAdministration.scheduled_datetime >= from_datetime)
    if to_datetime is not None:
        q = q.filter(IpdMedicationAdministration.scheduled_datetime <= to_datetime)

    q = q.order_by(IpdMedicationAdministration.scheduled_datetime.asc())
    return q.all()


@router.post(
    "/drug-chart/{admin_id}/mark",
    response_model=IpdMedicationAdministrationOut,
)
def mark_drug_chart_entry(
    admin_id: int,
    status_value: str = Query(
        ...,
        description="given / missed / refused / held / pending",
    ),
    remarks: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Update the status of a single Drug Chart entry (nurse documentation).
    """
    if not (
        has_perm(user, "ipd.nursing")
        or has_perm(user, "ipd.manage")
        or has_perm(user, "ipd.doctor")
    ):
        raise HTTPException(403, "Not permitted")

    admin = (
        db.query(IpdMedicationAdministration)
        .filter(IpdMedicationAdministration.id == admin_id)
        .first()
    )
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Drug chart entry not found",
        )

    allowed = {"pending", "given", "missed", "refused", "held"}
    value = status_value.lower()
    if value not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status '{status_value}'. Allowed: {', '.join(sorted(allowed))}",
        )

    admin.given_status = value
    if value == "given":
        admin.given_datetime = datetime.utcnow()
        admin.given_by = user.id
    elif value == "pending":
        admin.given_datetime = None
        admin.given_by = None

    if remarks is not None:
        admin.remarks = remarks

    db.commit()
    db.refresh(admin)
    return admin


# -------------------------------------------------------------------
# Drug Chart Meta (header: allergies, diagnosis, weight/height/BMI, diet)
# -------------------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/drug-chart/meta",
    response_model=IpdDrugChartMetaOut,
)
def get_drug_chart_meta(
    admission_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Get the drug chart header/meta for an admission:
    patient allergies, diagnosis, weight/height/BMI, blood group, BSA,
    and dietary advice (oral fluid/day, salt, calorie, protein, etc.).
    """
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    meta = (
        db.query(IpdDrugChartMeta)
        .filter(IpdDrugChartMeta.admission_id == admission_id)
        .first()
    )
    if not meta:
        # Return a blank object with admission_id; FE can show empty fields.
        meta = IpdDrugChartMeta(admission_id=admission_id)
        db.add(meta)
        db.commit()
        db.refresh(meta)
    return meta


@router.put(
    "/admissions/{admission_id}/drug-chart/meta",
    response_model=IpdDrugChartMetaOut,
)
def upsert_drug_chart_meta(
    admission_id: int,
    payload: IpdDrugChartMetaUpdate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Create/update drug chart meta for an admission.
    Nurses / doctors can update allergies, diagnosis, diet, weight/height etc.
    BMI is auto-calculated from weight + height in the schema.
    """
    if not (
        has_perm(user, "ipd.nursing")
        or has_perm(user, "ipd.doctor")
        or has_perm(user, "ipd.manage")
    ):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    meta = (
        db.query(IpdDrugChartMeta)
        .filter(IpdDrugChartMeta.admission_id == admission_id)
        .first()
    )
    if not meta:
        # Create new using Create schema behaviour (BMI auto-calc)
        data = IpdDrugChartMetaCreate(
            admission_id=admission_id, **payload.dict(exclude_unset=True)
        )
        meta = IpdDrugChartMeta(**data.dict())
        db.add(meta)
    else:
        for field, value in payload.dict(exclude_unset=True).items():
            setattr(meta, field, value)

    db.commit()
    db.refresh(meta)
    return meta


# -------------------------------------------------------------------
# IV Fluids section (Intravenous fluids table)
# -------------------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/drug-chart/iv-fluids",
    response_model=List[IpdIvFluidOrderOut],
)
def list_iv_fluid_orders(
    admission_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    List IV fluid orders for an admission:
    Date & Time, Fluid, Additive, Dose, Rate of infusion, Doctor sign,
    Start/Stop time, Nurse sign.
    """
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    q = (
        db.query(IpdIvFluidOrder)
        .filter(IpdIvFluidOrder.admission_id == admission_id)
        .order_by(IpdIvFluidOrder.ordered_datetime.asc(), IpdIvFluidOrder.id.asc())
    )
    return q.all()


@router.post(
    "/admissions/{admission_id}/drug-chart/iv-fluids",
    response_model=IpdIvFluidOrderOut,
    status_code=status.HTTP_201_CREATED,
)
def create_iv_fluid_order(
    admission_id: int,
    payload: IpdIvFluidOrderCreate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Create a new IV fluid order row.
    Doctor_id and nurse_ids come from User table; names can be snapshotted in model.
    """
    if not (has_perm(user, "ipd.doctor") or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    data = payload.dict()
    data["admission_id"] = admission_id

    obj = IpdIvFluidOrder(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put(
    "/drug-chart/iv-fluids/{iv_id}",
    response_model=IpdIvFluidOrderOut,
)
def update_iv_fluid_order(
    iv_id: int,
    payload: IpdIvFluidOrderUpdate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Update an existing IV fluid order row.
    """
    if not (has_perm(user, "ipd.doctor") or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    obj = db.query(IpdIvFluidOrder).filter(IpdIvFluidOrder.id == iv_id).first()
    if not obj:
        raise HTTPException(404, "IV fluid order not found")

    for field, value in payload.dict(exclude_unset=True).items():
        setattr(obj, field, value)

    db.commit()
    db.refresh(obj)
    return obj


# -------------------------------------------------------------------
# Nurse signature block (Name, Specimen Sign, Emp no.)
# -------------------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/drug-chart/nurses",
    response_model=List[IpdDrugChartNurseRowOut],
)
def list_drug_chart_nurse_rows(
    admission_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    List nurses linked to the drug chart for this admission.
    S.No, Name, specimen sign, Emp. no.
    """
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    rows = (
        db.query(IpdDrugChartNurseRow)
        .filter(IpdDrugChartNurseRow.admission_id == admission_id)
        .order_by(IpdDrugChartNurseRow.serial_no.asc(), IpdDrugChartNurseRow.id.asc())
        .all()
    )
    return rows


@router.post(
    "/admissions/{admission_id}/drug-chart/nurses",
    response_model=IpdDrugChartNurseRowOut,
    status_code=status.HTTP_201_CREATED,
)
def create_drug_chart_nurse_row(
    admission_id: int,
    payload: IpdDrugChartNurseRowCreate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Add a nurse to the drug chart nurse signature block.
    Nurse_id is from User; nurse_name/emp_no can be snapshotted from User.
    """
    if not (has_perm(user, "ipd.nursing") or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    data = payload.dict()
    data["admission_id"] = admission_id

    obj = IpdDrugChartNurseRow(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put(
    "/drug-chart/nurses/{row_id}",
    response_model=IpdDrugChartNurseRowOut,
)
def update_drug_chart_nurse_row(
    row_id: int,
    payload: IpdDrugChartNurseRowUpdate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Edit nurse details in the drug chart nurse block.
    """
    if not (has_perm(user, "ipd.nursing") or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    obj = db.query(IpdDrugChartNurseRow).filter(IpdDrugChartNurseRow.id == row_id).first()
    if not obj:
        raise HTTPException(404, "Nurse row not found")

    for field, value in payload.dict(exclude_unset=True).items():
        setattr(obj, field, value)

    db.commit()
    db.refresh(obj)
    return obj


# -------------------------------------------------------------------
# Doctor’s Daily Authorisation block
# -------------------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/drug-chart/doctor-auth",
    response_model=List[IpdDrugChartDoctorAuthOut],
)
def list_doctor_authorisations(
    admission_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    List Doctor’s Daily Authorisation entries for the drug chart.
    """
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    rows = (
        db.query(IpdDrugChartDoctorAuth)
        .filter(IpdDrugChartDoctorAuth.admission_id == admission_id)
        .order_by(IpdDrugChartDoctorAuth.auth_date.asc(), IpdDrugChartDoctorAuth.id.asc())
        .all()
    )
    return rows


@router.post(
    "/admissions/{admission_id}/drug-chart/doctor-auth",
    response_model=IpdDrugChartDoctorAuthOut,
    status_code=status.HTTP_201_CREATED,
)
def create_doctor_authorisation(
    admission_id: int,
    payload: IpdDrugChartDoctorAuthCreate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Create a new Doctor’s Daily Authorisation row.
    Doctor_id comes from User; doctor_name/sign can be snapshotted for PDF.
    """
    if not (has_perm(user, "ipd.doctor") or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    _get_admission_or_404(db, admission_id)

    data = payload.dict()
    data["admission_id"] = admission_id

    # default auth_date to today if not sent
    if not data.get("auth_date"):
        data["auth_date"] = date.today()

    obj = IpdDrugChartDoctorAuth(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put(
    "/drug-chart/doctor-auth/{auth_id}",
    response_model=IpdDrugChartDoctorAuthOut,
)
def update_doctor_authorisation(
    auth_id: int,
    payload: IpdDrugChartDoctorAuthUpdate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Edit an existing Doctor’s Daily Authorisation row.
    """
    if not (has_perm(user, "ipd.doctor") or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    obj = (
        db.query(IpdDrugChartDoctorAuth)
        .filter(IpdDrugChartDoctorAuth.id == auth_id)
        .first()
    )
    if not obj:
        raise HTTPException(404, "Doctor authorisation row not found")

    for field, value in payload.dict(exclude_unset=True).items():
        setattr(obj, field, value)

    db.commit()
    db.refresh(obj)
    return obj






@router.get(
    "/admissions/{admission_id}/drug-chart/pdf",
    response_class=Response,
)
def download_drug_chart_pdf(
    admission_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(current_user),
):
    """
    Generate NABH-style Drug Chart PDF for the given admission.

    Layout includes:
    - Patient details + Allergies + Diagnosis + Weight / Height / BMI / Blood group / BSA
    - Dietary advice (oral fluids, salt, calorie, protein)
    - Intravenous fluids table
    - Nurse signature block (Name, specimen sign, Emp no.)
    - Regular medication orders with Hrs & Nurse sign grid
    - SOS medications table
    - STAT / Premedication table
    - Doctor's Daily Authorisation + NOTE section
    """
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    adm = _get_admission_or_404(db, admission_id)

    # --- Load related data ---
    meta = (
        db.query(IpdDrugChartMeta)
        .filter(IpdDrugChartMeta.admission_id == admission_id)
        .first()
    )

    iv_fluids = (
        db.query(IpdIvFluidOrder)
        .filter(IpdIvFluidOrder.admission_id == admission_id)
        .order_by(IpdIvFluidOrder.ordered_datetime.asc(), IpdIvFluidOrder.id.asc())
        .all()
    )

    nurse_rows = (
        db.query(IpdDrugChartNurseRow)
        .filter(IpdDrugChartNurseRow.admission_id == admission_id)
        .order_by(IpdDrugChartNurseRow.serial_no.asc(), IpdDrugChartNurseRow.id.asc())
        .all()
    )

    doctor_auths = (
        db.query(IpdDrugChartDoctorAuth)
        .filter(IpdDrugChartDoctorAuth.admission_id == admission_id)
        .order_by(IpdDrugChartDoctorAuth.auth_date.asc(), IpdDrugChartDoctorAuth.id.asc())
        .all()
    )

    med_orders = (
        db.query(IpdMedicationOrder)
        .filter(IpdMedicationOrder.admission_id == admission_id)
        .order_by(IpdMedicationOrder.start_datetime.asc(), IpdMedicationOrder.id.asc())
        .all()
    )

    # Group orders by type (default -> regular)
    regular_orders = []
    sos_orders = []
    stat_orders = []
    premed_orders = []
    for o in med_orders:
        t = (o.order_type or "regular").lower()
        if t == "sos":
            sos_orders.append(o)
        elif t == "stat":
            stat_orders.append(o)
        elif t in {"premed", "pre-med", "pre_medic"}:
            premed_orders.append(o)
        else:
            regular_orders.append(o)

    # Drug chart administrations (for Hrs grid)
    admins = (
        db.query(IpdMedicationAdministration)
        .filter(IpdMedicationAdministration.admission_id == admission_id)
        .order_by(IpdMedicationAdministration.scheduled_datetime.asc())
        .all()
    )

    # Group admins by med_order_id
    admin_by_order = {}
    for a in admins:
        admin_by_order.setdefault(a.med_order_id, []).append(a)

    # --- Patient snapshot for header ---
    patient = getattr(adm, "patient", None)
    # You can adjust these field names as per your Patient model
    patient_name = ""
    if patient is not None:
        name_parts = [
            getattr(patient, "prefix", "") or "",
            getattr(patient, "first_name", "") or "",
            getattr(patient, "last_name", "") or "",
        ]
        patient_name = " ".join([p for p in name_parts if p]).strip()

    uhid = getattr(patient, "uhid", None) or getattr(patient, "patient_code", "") or ""
    gender = getattr(patient, "gender", "") or ""
    age_str = ""
    if getattr(patient, "dob", None):
        # simple age calc (years only)
        today = datetime.utcnow().date()
        dob = patient.dob
        if isinstance(dob, datetime):
            dob = dob.date()
        age_years = today.year - dob.year - (
            (today.month, today.day) < (dob.month, dob.day)
        )
        age_str = f"{age_years} Y"

    # Height/weight/BMI from meta
    weight_kg = getattr(meta, "weight_kg", None) if meta else None
    height_cm = getattr(meta, "height_cm", None) if meta else None
    bmi = getattr(meta, "bmi", None) if meta else None
    if bmi is None and weight_kg and height_cm:
        try:
            h_m = float(height_cm) / 100.0
            if h_m > 0:
                bmi = round(float(weight_kg) / (h_m * h_m), 2)
        except Exception:
            bmi = None

    # --- Build Jinja context ---
    ctx = {
        "admission": adm,
        "patient_name": patient_name,
        "uhid": uhid,
        "age_str": age_str,
        "gender": gender,
        "meta": meta,
        "weight_kg": weight_kg,
        "height_cm": height_cm,
        "bmi": bmi,
        "iv_fluids": iv_fluids,
        "nurse_rows": nurse_rows,
        "doctor_auths": doctor_auths,
        "regular_orders": regular_orders,
        "sos_orders": sos_orders,
        "stat_orders": stat_orders,
        "premed_orders": premed_orders,
        "admin_by_order": admin_by_order,
        # For Hrs header – 24 hour slots or reduce if you want
        "hour_slots": [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22],
        "generated_at": datetime.utcnow(),
    }

    template = _pdf_env.get_template("ipd/drug_chart.html")
    html_str = template.render(ctx)

    pdf_bytes = HTML(string=html_str, base_url=str(TEMPLATES_DIR)).write_pdf()

    filename = f"drug-chart-admission-{admission_id}.pdf"
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "Content-Type": "application/pdf",
    }
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
