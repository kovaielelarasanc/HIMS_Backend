# FILE: app/api/routes_mis.py
from __future__ import annotations

from datetime import date, datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.schemas.mis import (
    MisDefinitionOut,
    MisReportRunBody,
    MISFilter,
    MisReportResult,
    MisCard,
    MisChart,
    MisTable,
)
from app.services import mis_service

router = APIRouter()  # prefix will be added in app.api.router


@router.get("/definitions", response_model=List[MisDefinitionOut])
def get_mis_definitions(
    current_user: User = Depends(auth_current_user),
) -> List[MisDefinitionOut]:
    """
    List available MIS report definitions for the logged-in user (permission-aware).

    Full URL: GET /api/mis/definitions
    """
    return mis_service.list_definitions_for_user(user=current_user)


@router.post("/reports/{code}", response_model=MisReportResult)
def run_mis_report(
        code: str,
        body: MisReportRunBody,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
) -> MisReportResult:
    """
    Run a specific MIS report identified by `code`.

    - URL:  POST /api/mis/reports/{code}
    - Body: { "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD", "filters": { ... } }
    """
    if not code:
        raise HTTPException(status_code=400, detail="Report code is required")

    # Prepare MISFilter (what mis_service.run_report expects)
    filter_data = {
        "date_from": body.date_from,
        "date_to": body.date_to,
    }

    extra = body.filters or {}
    # Only allow known filter fields; treat "__ALL__" as "no filter"
    for key in [
            "department_id",
            "doctor_id",
            "unit_id",
            "context_type",
            "payment_mode",
            "patient_id",
    ]:
        if key in extra:
            val = extra[key]
            if val in ("__ALL__", "", None):
                continue
            filter_data[key] = val

    filters = MISFilter(**filter_data)

    raw = mis_service.run_report(
        db=db,
        user=current_user,
        code=code,
        filters_in=filters,
    )

    # Final date range (prefer filters_applied if set)
    applied = raw.filters_applied or filters
    date_from: date = applied.date_from or body.date_from
    date_to: date = applied.date_to or body.date_to

    # ---- Summary cards ----
    cards: List[MisCard] = []
    for key, value in (raw.summary or {}).items():
        label = key.replace("_", " ").title()
        helper = None
        tone = None

        if key in {"total_patients", "total_visits", "total_admissions"}:
            tone = "info"
        elif key in {"total_net", "total_sales_amount"}:
            tone = "success"
        elif key in {"total_balance_due"}:
            tone = "danger"

        cards.append(
            MisCard(label=label, value=value, helper=helper, tone=tone))

    # ---- Charts ----
    charts: List[MisChart] = []
    if raw.chart:
        x_key = raw.chart.x_key
        series_key = None
        if raw.chart.series:
            series_key = raw.chart.series[0].get("key")

        data = []
        if raw.rows and x_key and series_key:
            for row in raw.rows:
                label_val = row.get(x_key)
                if isinstance(label_val, (date, datetime)):
                    label_val = label_val.isoformat()
                value_val = row.get(series_key, 0) or 0
                data.append({"label": label_val, "value": value_val})

        chart_type = raw.chart.chart_type
        chart_type_front = "pie" if chart_type == "pie" else "bar"

        charts.append(
            MisChart(
                code=f"{raw.code}.primary",
                title=raw.name,
                type=chart_type_front,
                data=data,
                config={
                    "x_key": x_key,
                    "series": raw.chart.series,
                    "chart_type": raw.chart.chart_type,
                },
            ))

    # ---- Optional detailed table ----
    table: MisTable | None = None
    if raw.columns and raw.rows:
        table = MisTable(
            columns=[c.key for c in raw.columns],
            rows=raw.rows,
        )

    return MisReportResult(
        code=raw.code,
        name=raw.name,
        group=raw.group,
        description=raw.description,
        date_from=date_from,
        date_to=date_to,
        cards=cards,
        charts=charts,
        table=table,
        meta=raw.meta or {},
    )
