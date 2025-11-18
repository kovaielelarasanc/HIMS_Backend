from __future__ import annotations
from typing import Optional, Literal, List
from pydantic import BaseModel, ConfigDict


class InvoiceCreate(BaseModel):
    patient_id: int
    context_type: Optional[str] = None  # opd | ipd
    context_id: Optional[int] = None


class AddServiceIn(BaseModel):
    service_type: Literal["lab", "radiology", "ot", "pharmacy", "opd", "ipd"]
    service_ref_id: int
    quantity: int = 1
    tax_rate: float = 0.0


class ManualItemIn(BaseModel):
    description: str
    quantity: int = 1
    unit_price: float
    tax_rate: float = 0.0
    service_type: Optional[str] = "manual"
    service_ref_id: Optional[int] = 0  # 0 or synthetic id


class UpdateItemIn(BaseModel):
    quantity: Optional[int] = None
    unit_price: Optional[float] = None
    tax_rate: Optional[float] = None


class VoidItemIn(BaseModel):
    reason: Optional[
        str] = None  # audit note (you can store in a separate table)


class PaymentIn(BaseModel):
    amount: float
    mode: Literal["cash", "card", "upi", "credit"]
    reference_no: Optional[str] = None


class BulkAddFromUnbilledIn(BaseModel):
    uids: Optional[
        List[str]] = None  # ["lab:123","radiology:55",...]; if None => add all


# --------- Optional OUTs for FE convenience ---------
class InvoiceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    service_type: str
    service_ref_id: int
    description: str
    quantity: int
    unit_price: float
    tax_rate: float
    tax_amount: float
    line_total: float
    is_voided: bool


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    amount: float
    mode: str
    reference_no: Optional[str] = None
    paid_at: Optional[str] = None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    patient_id: int
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    status: str
    gross_total: float
    tax_total: float
    net_total: float
    amount_paid: float
    balance_due: float
    items: list[InvoiceItemOut]
    payments: list[PaymentOut]
