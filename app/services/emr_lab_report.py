# FILE: app/services/emr_lab_report.py
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lis import (
    LisOrder,
    LisOrderItem,
    LisResultLine,
    LisAttachment,
    LabDepartment,
    LabService,
)
from app.models.opd import LabTest
from app.models.patient import Patient
from app.models.user import User


def _patient_name(p: Patient) -> str:
    name = f"{getattr(p, 'first_name', '')} {getattr(p, 'last_name', '')}".strip(
    )
    return name or (getattr(p, "full_name", "") or "—")


def _patient_phone(p: Patient) -> str:
    return (getattr(p, "phone", "") or getattr(p, "mobile", "") or "—")


# --- reference range display (copied from LIS for EMR UI/PDF) ---
def format_reference_ranges_display(ranges: list[dict] | None) -> str:
    if not ranges:
        return "-"

    groups: dict[str, list[dict]] = {"F": [], "M": [], "ANY": []}
    for r in ranges:
        sex = (r.get("sex") or "ANY").upper().strip()
        if sex not in groups:
            sex = "ANY"
        groups[sex].append(r)

    out: list[str] = []

    def val_text(r: dict) -> str:
        low = (r.get("low") or "").strip()
        high = (r.get("high") or "").strip()
        textv = (r.get("text") or "").strip()
        if textv:
            return textv
        if low and high:
            return f"{low}-{high}"
        if low:
            return f">= {low}"
        if high:
            return f"<= {high}"
        return "-"

    def age_text(r: dict) -> str:
        age_min = r.get("age_min")
        age_max = r.get("age_max")
        age_unit = (r.get("age_unit") or "Y").strip()
        if age_min is None and age_max is None:
            return ""
        a = "" if age_min is None else str(age_min)
        b = "" if age_max is None else str(age_max)
        return f" ({a}-{b}{age_unit})"

    def add_group(title: str, items: list[dict], add_heading: bool):
        if not items:
            return
        if add_heading:
            out.append(title)
        for r in items:
            label = (r.get("label") or "Range").strip()
            out.append(f"{label}{age_text(r)}: {val_text(r)}")

    has_sex = bool(groups["F"] or groups["M"])
    if groups["F"]:
        add_group("WOMEN", groups["F"], add_heading=True)
    if groups["M"]:
        add_group("MEN", groups["M"], add_heading=True)
    if groups["ANY"]:
        add_group("GENERAL" if has_sex else "",
                  groups["ANY"],
                  add_heading=has_sex)

    out = [x for x in out if x.strip()]
    return "\n".join(out) if out else "-"


def build_emr_lab_report(db: Session, order_id: int) -> Dict[str, Any]:
    order: Optional[LisOrder] = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Lab order not found")

    patient: Optional[Patient] = db.query(Patient).get(order.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    # Order Items (tests)
    items = (db.query(LisOrderItem).filter(
        LisOrderItem.order_id == order_id).order_by(
            LisOrderItem.id.asc()).all())

    test_names: List[str] = []
    for it in items:
        test_names.append(it.test_name or "-")

    # Result Lines (panel style)
    lines: List[LisResultLine] = (db.query(LisResultLine).filter(
        LisResultLine.order_id == order_id).order_by(
            LisResultLine.department_id.asc(),
            LisResultLine.sub_department_id.asc(),
            LisResultLine.id.asc(),
        ).all())

    sections_map: Dict[Tuple[int, Optional[int]], Dict[str, Any]] = {}

    for line in lines:
        dept_id = line.department_id or 0
        sub_id = line.sub_department_id

        # resolve dept names
        dept_name = ""
        sub_name: Optional[str] = None

        if line.department:
            dept_name = line.department.name
        elif line.service and line.service.department:
            d = line.service.department
            if d.parent_id:
                parent = db.query(LabDepartment).get(d.parent_id)
                dept_name = parent.name if parent else d.name
            else:
                dept_name = d.name

        if line.sub_department:
            sub_name = line.sub_department.name
        else:
            if line.service and line.service.department and line.service.department.parent_id:
                sub_name = line.service.department.name

        key = (dept_id, sub_id)
        if key not in sections_map:
            title = (dept_name or "Department").strip()
            if sub_name:
                title = f"{title} / {sub_name}"
            sections_map[key] = {
                "key": f"{dept_id}:{sub_id}",
                "title": title,
                "rows": []
            }

        svc_obj = getattr(line, "service", None)
        rr_display = None
        if svc_obj and getattr(svc_obj, "reference_ranges", None):
            rr_display = format_reference_ranges_display(
                svc_obj.reference_ranges)

        sections_map[key]["rows"].append({
            "service_name":
            line.service_name,
            "result_value":
            line.result_value,
            "unit":
            line.unit or "-",
            "normal_range": (rr_display or line.normal_range or "-"),
            "flag":
            line.flag,
            "comments":
            line.comments,
        })

    sections = list(sections_map.values())
    sections.sort(key=lambda s: s["key"])

    # Attachments (optional)
    atts = (db.query(LisAttachment).join(
        LisOrderItem, LisAttachment.order_item_id == LisOrderItem.id).filter(
            LisOrderItem.order_id == order_id).order_by(
                LisAttachment.id.desc()).all())
    attachments_out = []
    for a in atts:
        attachments_out.append({
            "id":
            a.id,
            "item_id":
            a.order_item_id,
            "file_url":
            a.file_url,
            "note":
            a.note,
            "created_at":
            a.created_at.isoformat() if a.created_at else None,
        })

    created_at = getattr(order, "created_at", None)
    collected_at = getattr(order, "collected_at", None)
    reported_at = getattr(order, "reported_at", None)

    lab_no = f"LAB-{order.id:06d}"

    return {
        "meta": {
            "order_id": order.id,
            "lab_no": lab_no,
            "status": order.status,
            "priority": order.priority,
            "context_type": order.context_type,
            "context_id": order.context_id,
            "billing_invoice_id": getattr(order, "billing_invoice_id", None),
            "billing_status": getattr(order, "billing_status", None),
            "created_at": created_at.isoformat() if created_at else None,
            "collected_at": collected_at.isoformat() if collected_at else None,
            "reported_at": reported_at.isoformat() if reported_at else None,
            "tests": test_names,
        },
        "patient": {
            "id":
            patient.id,
            "uhid":
            getattr(patient, "uhid", "") or "—",
            "name":
            _patient_name(patient),
            "phone":
            _patient_phone(patient),
            "dob":
            getattr(patient, "dob", None)
            or getattr(patient, "date_of_birth", None),
            "gender":
            getattr(patient, "gender", "") or getattr(patient, "sex", ""),
        },
        "sections": sections,
        "attachments": attachments_out,
    }


def build_emr_lab_report_object_for_pdf(
        db: Session,
        order_id: int) -> tuple[Any, Any, str, Any, Optional[str]]:
    """
    Returns (report_obj, patient_obj, lab_no, order_date, collected_by_name)
    for build_lab_report_pdf_bytes()
    """
    data = build_emr_lab_report(db, order_id)
    meta = data["meta"]
    patient = data["patient"]

    # build report sections in the same shape as LIS PDF expects (attr-based)
    sec_objs = []
    for sec in (data.get("sections") or []):
        rows = []
        for r in (sec.get("rows") or []):
            rows.append(
                SimpleNamespace(
                    service_name=r.get("service_name"),
                    result_value=r.get("result_value"),
                    unit=r.get("unit"),
                    normal_range=r.get("normal_range"),
                    flag=r.get("flag"),
                    comments=r.get("comments"),
                ))
        # split title into dept/sub in a safe way
        title = (sec.get("title") or "Department")
        if " / " in title:
            dept_name, sub_name = title.split(" / ", 1)
        else:
            dept_name, sub_name = title, None

        sec_objs.append(
            SimpleNamespace(
                department_id=None,
                department_name=dept_name,
                sub_department_id=None,
                sub_department_name=sub_name,
                rows=rows,
            ))

    # Age text quick
    age_text = None
    dob = patient.get("dob", None)
    if dob:
        try:
            if isinstance(dob, str):
                dt = datetime.fromisoformat(dob.replace("Z", "+00:00")).date()
            else:
                dt = dob
            today = datetime.utcnow().date()
            years = today.year - dt.year
            age_text = f"{years} Years"
        except Exception:
            age_text = None

    report_obj = SimpleNamespace(
        order_id=meta.get("order_id"),
        lab_no=meta.get("lab_no"),
        patient_id=patient.get("id"),
        patient_uhid=patient.get("uhid"),
        patient_name=patient.get("name"),
        patient_gender=patient.get("gender"),
        patient_dob=patient.get("dob"),
        patient_age_text=age_text,
        patient_type=(meta.get("context_type") or "").upper() or None,
        bill_no=None,
        received_on=meta.get("collected_at"),
        reported_on=meta.get("reported_at"),
        referred_by=None,
        sections=sec_objs,
    )

    patient_obj = db.query(Patient).get(patient["id"])
    lab_no = meta.get("lab_no") or f"LAB-{order_id:06d}"
    order = db.query(LisOrder).get(order_id)
    order_date = getattr(order, "created_at", None)

    collected_by_name = None
    # If you later add collected_by, you can resolve it here safely.
    return report_obj, patient_obj, lab_no, order_date, collected_by_name
