from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.utils.resp import ok
from app.core.rbac import require_any
from app.models.user import User

from app.schemas.inventory_consumption import (
    EligibleItemOut,
    PatientConsumeIn,
    PatientConsumeOut,
    ConsumptionListRowOut,
    BulkReconcileIn,
    BulkReconcileOut,
    EncounterType,   # ✅ make sure this is an Enum with OP/IP/OT/ER
)

from app.services.inventory_consumption_service import (
    list_eligible_items,
    post_patient_consumption,
    list_patient_consumptions,
    post_bulk_reconcile,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("/consumption-items", response_model=list[EligibleItemOut])
def get_consumption_items(
    location_id: int = Query(...),
    patient_id: Optional[int] = Query(None),
    encounter_type: Optional[EncounterType] = Query(None),   # ✅ Enum so .value is valid
    encounter_id: Optional[int] = Query(None),
    q: str = Query("", max_length=100),
    limit: int = Query(250, ge=1, le=500),  # ✅ fixes your 422 when frontend sends 250
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_any(user, ["inventory.view", "inventory.consume.view", "inventory.manage"])

    rows = list_eligible_items(
        db,
        location_id=location_id,
        patient_id=patient_id,
        encounter_type=encounter_type.value if encounter_type else None,
        encounter_id=encounter_id,
        q=q,
        limit=limit,
    )
    return rows


@router.post("/consumptions/patient", response_model=PatientConsumeOut)
def create_patient_consumption(
    payload: PatientConsumeIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_any(user, ["inventory.consume.create", "inventory.manage"])

    data = post_patient_consumption(
        db,
        user_id=user.id,
        location_id=payload.location_id,
        patient_id=payload.patient_id,
        encounter_type=payload.encounter_type.value if getattr(payload, "encounter_type", None) else None,
        encounter_id=getattr(payload, "encounter_id", None),
        visit_id=payload.visit_id,
        doctor_id=payload.doctor_id,
        notes=payload.notes,
        items=[x.model_dump() for x in payload.items],
    )
    return data


@router.get("/consumptions/patient", response_model=list[ConsumptionListRowOut])
def list_patient_consumptions_api(
    location_id: Optional[int] = Query(None),
    patient_id: Optional[int] = Query(None),
    encounter_type: Optional[EncounterType] = Query(None),
    encounter_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_any(user, ["inventory.consume.view", "inventory.manage"])

    rows = list_patient_consumptions(
        db,
        location_id=location_id,
        patient_id=patient_id,
        encounter_type=encounter_type.value if encounter_type else None,
        encounter_id=encounter_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return rows


@router.post("/consumptions/reconcile", response_model=BulkReconcileOut)
def reconcile_bulk_consumption(
    payload: BulkReconcileIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_any(user, ["inventory.reconcile.create", "inventory.manage"])

    data = post_bulk_reconcile(
        db,
        user_id=user.id,
        location_id=payload.location_id,
        on_date=payload.on_date or date.today(),
        notes=payload.notes,
        lines=[x.model_dump() for x in payload.lines],
    )
    return data
