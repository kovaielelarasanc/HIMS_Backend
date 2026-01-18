# FILE: app/schemas/billing_insurance.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field, root_validator

from app.models.billing import (InsurancePayerKind, InsuranceStatus,
                                PreauthStatus, ClaimStatus, CoverageFlag)

Money = Decimal


class InsuranceCaseUpsert(BaseModel):
    payer_kind: InsurancePayerKind = InsurancePayerKind.INSURANCE
    insurance_company_id: Optional[int] = None
    insurance_id: Optional[int] = None
    tpa_id: Optional[int] = None
    corporate_id: Optional[int] = None

    policy_no: Optional[str] = None
    member_id: Optional[str] = None
    plan_name: Optional[str] = None

    # optional: you can drive approved_limit from preauth approvals too
    approved_limit: Optional[Money] = None
    status: Optional[InsuranceStatus] = None
    @root_validator(pre=True)
    def _sync_insurance_id(cls, values):
        # accept insurance_id -> insurance_company_id
        if values.get("insurance_company_id") is None and values.get("insurance_id") is not None:
            values["insurance_company_id"] = values.get("insurance_id")

        # also mirror back for convenience (if UI reads insurance_id)
        if values.get("insurance_id") is None and values.get("insurance_company_id") is not None:
            values["insurance_id"] = values.get("insurance_company_id")

        return values

    class Config:
        extra = "ignore"  # ignore unknown fields safely


class InsuranceCaseOut(BaseModel):
    id: int
    billing_case_id: int
    payer_kind: InsurancePayerKind
    insurance_company_id: Optional[int]
    insurance_id: Optional[int]

    tpa_id: Optional[int]
    corporate_id: Optional[int]
    policy_no: Optional[str]
    member_id: Optional[str]
    plan_name: Optional[str]
    status: InsuranceStatus
    approved_limit: Money
    approved_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    @root_validator(pre=True)
    def _sync_out(cls, values):
        # when coming from ORM, insurance_id may be missing
        ic = values.get("insurance_company_id")
        if values.get("insurance_id") is None:
            values["insurance_id"] = ic
        if values.get("insurance_company_id") is None and values.get("insurance_id") is not None:
            values["insurance_company_id"] = values.get("insurance_id")
        return values

    class Config:
        orm_mode = True


class PreauthCreate(BaseModel):
    requested_amount: Money = Field(default=0)
    remarks: Optional[str] = None
    attachments_json: Optional[Dict[str, Any]] = None


class PreauthUpdate(BaseModel):
    requested_amount: Optional[Money] = None
    remarks: Optional[str] = None
    attachments_json: Optional[Dict[str, Any]] = None


class PreauthDecision(BaseModel):
    approved_amount: Money = Field(default=0)
    remarks: Optional[str] = None


class PreauthOut(BaseModel):
    id: int
    insurance_case_id: int
    requested_amount: Money
    approved_amount: Money
    status: PreauthStatus
    submitted_at: Optional[datetime]
    approved_at: Optional[datetime]
    remarks: Optional[str]
    attachments_json: Optional[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    # “no raw id” display (no DB change required)
    ref_no: str


class ClaimCreate(BaseModel):
    claim_amount: Money = Field(default=0)
    remarks: Optional[str] = None
    attachments_json: Optional[Dict[str, Any]] = None
    insurer_invoice_ids: Optional[List[int]] = None  # internal ids


class ClaimUpdate(BaseModel):
    claim_amount: Optional[Money] = None
    remarks: Optional[str] = None
    attachments_json: Optional[Dict[str, Any]] = None


class ClaimDecision(BaseModel):
    approved_amount: Money = Field(default=0)
    settled_amount: Money = Field(default=0)
    remarks: Optional[str] = None


class ClaimOut(BaseModel):
    id: int
    insurance_case_id: int
    claim_amount: Money
    approved_amount: Money
    settled_amount: Money
    status: ClaimStatus
    submitted_at: Optional[datetime]
    settled_at: Optional[datetime]
    remarks: Optional[str]
    attachments_json: Optional[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    ref_no: str


class InsuranceLineRow(BaseModel):
    invoice_id: int
    invoice_number: str
    module: Optional[str]
    invoice_status: str

    line_id: int
    description: str
    service_group: str
    net_amount: Money

    is_covered: CoverageFlag
    insurer_pay_amount: Money
    patient_pay_amount: Money
    requires_preauth: bool


class InsuranceLinePatch(BaseModel):
    line_id: int
    is_covered: Optional[CoverageFlag] = None
    insurer_pay_amount: Optional[Money] = None
    requires_preauth: Optional[bool] = None


class SplitRequest(BaseModel):
    invoice_ids: List[int] = Field(default_factory=list)
