# FILE: app/services/billing_case_service.py
from __future__ import annotations

from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.billing import BillingCase, BillingCaseStatus, PayerMode
from app.services.id_gen import next_billing_case_number


def get_or_create_billing_case_for_encounter(
    db: Session,
    *,
   
    patient_id: int,
    encounter_type: str,  # "OP"|"IP"...
    encounter_id: int,
    user_id: int,
    org_code: str = "SMC",
) -> BillingCase:
    # idempotent: if already exists, return it
    existing = (db.query(BillingCase).filter(
        BillingCase.encounter_type == encounter_type,
        BillingCase.encounter_id == encounter_id,
    ).first())
    if existing:
        return existing

    # create with REAL unique number (NO TEMP)
    case_no = next_billing_case_number(
        db,
      
        encounter_type=encounter_type,
        org_code=org_code,
    )

    case = BillingCase(
        patient_id=patient_id,
        encounter_type=encounter_type,
        encounter_id=encounter_id,
        case_number=case_no,
        status=BillingCaseStatus.OPEN,
        payer_mode=PayerMode.SELF,
        created_by=user_id,
        updated_by=user_id,
    )

    db.add(case)

    try:
        db.flush()
        return case
    except IntegrityError:
        # If two requests raced, one may have created it.
        db.rollback()
        existing = (db.query(BillingCase).filter(
            BillingCase.encounter_type == encounter_type,
            BillingCase.encounter_id == encounter_id,
        ).first())
        if existing:
            return existing
        raise
