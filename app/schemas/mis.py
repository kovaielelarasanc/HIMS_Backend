# FILE: app/schemas/mis.py
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict

# ---------------------------------------------------------------------------
# Core filter / column / chart config (internal shape)
# ---------------------------------------------------------------------------


class MISFilter(BaseModel):
    """
    Generic MIS filter set.

    Frontend can send any combination of these.
    Date fields are inclusive ranges (converted to datetime in service).
    """

    date_from: Optional[date] = Field(default=None)
    date_to: Optional[date] = Field(default=None)

    department_id: Optional[int] = None
    doctor_id: Optional[int] = None
    unit_id: Optional[int] = None  # ward/unit
    context_type: Optional[Literal["opd", "ipd", "all"]] = "all"
    payment_mode: Optional[str] = None  # cash/card/upi/credit
    patient_id: Optional[int] = None


class MISColumn(BaseModel):
    key: str
    label: str
    type: Literal["string", "number", "date", "datetime", "enum"] = "string"
    align: Literal["left", "right", "center"] = "left"


class MISChartConfig(BaseModel):
    chart_type: Literal["bar", "pie", "line", "column", "area"] = "bar"
    x_key: Optional[str] = None
    series: List[Dict[str, Any]] = Field(default_factory=list)


class MISRawReportResult(BaseModel):
    """
    Internal result structure used by mis_service.

    This is column/row + a single chart + summary dictionary.
    routes_mis.py will adapt this into the frontend-friendly MisReportResult
    with cards/charts/table.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    code: str
    name: str
    group: str
    description: str

    filters_applied: MISFilter
    summary: Dict[str, Any] = Field(default_factory=dict)
    columns: List[MISColumn] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    chart: Optional[MISChartConfig] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public shapes exposed to the frontend (definitions + results)
# ---------------------------------------------------------------------------


class MisFilterDef(BaseModel):
    key: str
    label: str
    type: str = "text"  # "text" | "select" | "date" etc.
    required: bool = False
    options: Optional[List[Dict[str, Any]]] = None  # [{value, label}, ...]


class MisDefinitionOut(BaseModel):
    code: str
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    filters: List[MisFilterDef] = Field(default_factory=list)


class MisReportRunBody(BaseModel):
    date_from: date
    date_to: date
    filters: Dict[str, Any] = Field(default_factory=dict)


class MisCard(BaseModel):
    label: str
    value: Any
    helper: Optional[str] = None
    tone: Optional[str] = None  # "success" | "danger" | "info" etc.


class MisChart(BaseModel):
    code: str
    title: str
    type: str  # "bar" | "pie"
    data: List[Dict[str, Any]]
    config: Dict[str, Any] = Field(default_factory=dict)


class MisTable(BaseModel):
    columns: List[str]
    rows: List[Dict[str, Any]]


class MisReportResult(BaseModel):
    """
    Final MIS report result sent to the frontend.

    This matches what the React MIS.jsx page expects:
    - cards: array of { label, value, helper?, tone? }
    - charts: array of { code, title, type, data[], config }
    - table: { columns[], rows[] } | null
    """

    code: str
    name: str
    group: str
    description: str
    date_from: date
    date_to: date

    cards: List[MisCard] = Field(default_factory=list)
    charts: List[MisChart] = Field(default_factory=list)
    table: Optional[MisTable] = None
    meta: Dict[str, Any] = Field(default_factory=dict)
