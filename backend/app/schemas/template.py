# app/schemas/template.py
from typing import Optional, Any
from pydantic import BaseModel, ConfigDict


class DocumentTemplateCreate(BaseModel):
    name: str
    code: str
    category: Optional[str] = "report"
    subcategory: Optional[str] = None
    description: Optional[str] = None
    html: str = ""
    css: str = ""
    placeholders: dict[str, Any] = {}
    is_active: bool = True


class DocumentTemplateUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    description: Optional[str] = None
    html: Optional[str] = None
    css: Optional[str] = None
    placeholders: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None


class DocumentTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    code: str
    category: str
    subcategory: Optional[str] = None
    description: Optional[str] = None
    html: str
    css: str
    placeholders: dict
    is_active: bool
    version: int


class ConsentCreateFromTemplate(BaseModel):
    template_id: int
    data: Optional[dict] = None
    finalize: bool = True
    signed_by: Optional[int] = None
    witness_name: Optional[str] = None


class PatientConsentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    patient_id: int
    template_id: int
    data: dict
    html_rendered: str
    pdf_path: str
    status: str
    signed_by: int | None
    witness_name: str | None
