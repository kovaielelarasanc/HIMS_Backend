# app/services/pdf_opd_summary.py
from __future__ import annotations

from io import BytesIO
from datetime import datetime, date
from typing import Any, Optional, List, Tuple
from pathlib import Path
import html as _html

from sqlalchemy.orm import Session, joinedload
from fastapi import HTTPException

from app.core.config import settings
from app.models.ui_branding import UiBranding
from app.models.patient import Patient
from app.models.department import Department
from app.models.user import User
from app.models.opd import (
    Visit,
    Vitals,
    Prescription,
    PrescriptionItem,
    LabOrder,
    RadiologyOrder,
    LabTest,
    RadiologyTest,
)

from app.services.pdf_branding import brand_header_css, render_brand_header_html


# -------------------------------
# Helpers
# -------------------------------
def _safe(v: Any) -> str:
    return "" if v is None else str(v)


def _clean(v: Any) -> str:
    return (_safe(v) or "").strip()


def _present(v: Any) -> bool:
    s = (_safe(v) or "").strip()
    return bool(s) and s not in ("—", "-", "None", "null", "NULL")


def _esc(s: Any) -> str:
    return _html.escape(_safe(s), quote=True)


def _to_date(v: Any) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "")).date()
    except Exception:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except Exception:
            continue
    return None


def _fmt_date(v: Any) -> str:
    d = _to_date(v)
    return d.strftime("%d-%m-%Y") if d else ("—" if not v else str(v))


def _fmt_dt(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d %b %Y, %I:%M %p")
    return _safe(v)


def _age_years_from_dob(dob: Any,
                        asof: Optional[date] = None) -> Optional[int]:
    d = _to_date(dob)
    if not d:
        return None
    asof = asof or date.today()
    years = asof.year - d.year - ((asof.month, asof.day) < (d.month, d.day))
    return max(0, int(years))


def _calc_bmi(height_cm: Any, weight_kg: Any) -> Optional[float]:
    try:
        h = float(height_cm) if height_cm is not None and str(
            height_cm).strip() != "" else None
        w = float(weight_kg) if weight_kg is not None and str(
            weight_kg).strip() != "" else None
    except Exception:
        return None

    if not h or not w or h <= 0 or w <= 0:
        return None

    m = h / 100.0
    bmi = w / (m * m)
    if bmi <= 0 or bmi > 80:
        return None
    return round(bmi, 1)


def _wrap(text: str, font: str, size: float, max_w: float, *,
          pdfmetrics) -> List[str]:
    s = (text or "").replace("\n", " ").strip()
    if not s:
        return [""]
    words = s.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if pdfmetrics.stringWidth(cand, font, size) <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def freq_to_slots(freq: Optional[str]) -> tuple[int, int, int, int]:
    if not freq:
        return (0, 0, 0, 0)
    f = str(freq).strip().upper()

    if "-" in f:
        parts = [p.strip() for p in f.split("-") if p.strip() != ""]
        nums: list[int] = []
        for p in parts:
            try:
                nums.append(int(float(p)))
            except Exception:
                nums.append(0)
        if len(nums) == 3:
            return (nums[0], nums[1], 0, nums[2])
        if len(nums) >= 4:
            return (nums[0], nums[1], nums[2], nums[3])

    mapping = {
        "OD": (1, 0, 0, 0),
        "QD": (1, 0, 0, 0),
        "BD": (1, 0, 0, 1),
        "BID": (1, 0, 0, 1),
        "TID": (1, 1, 0, 1),
        "TDS": (1, 1, 0, 1),
        "QID": (1, 1, 1, 1),
        "HS": (0, 0, 0, 1),
        "NIGHT": (0, 0, 0, 1),
    }
    return mapping.get(f, (0, 0, 0, 0))


# -------------------------------------------------------------------
# WeasyPrint HTML (extreme professional: no bg, no radius)
# -------------------------------------------------------------------
def _build_visit_summary_html(
    *,
    branding_obj: Any,
    visit: Visit,
    patient: Patient,
    dept: Department,
    doctor: User,
    vitals: Optional[Vitals],
    rx: Optional[Prescription],
    rx_items: List[PrescriptionItem],
    lab_names: List[str],
    rad_names: List[str],
) -> str:
    # ✅ OP number should be episode_id
    op_no = (_clean(getattr(visit, "episode_id", ""))
             or _clean(getattr(visit, "visit_no", ""))
             or _clean(getattr(visit, "id", "")))
    visit_date = _fmt_date(
        getattr(visit, "visit_at", None) or getattr(visit, "created_at", None))

    op_uid = _clean(
        getattr(visit, "op_uid", "") or getattr(patient, "op_uid", "")
        or getattr(patient, "op_uuid", ""))
    ip_uid = _clean(
        getattr(visit, "ip_uid", "") or getattr(patient, "ip_uid", "")
        or getattr(patient, "ip_uuid", ""))

    opip_val = ""
    if _present(op_uid) and _present(ip_uid):
        opip_val = f"{op_uid} / {ip_uid}"
    elif _present(op_uid):
        opip_val = op_uid
    elif _present(ip_uid):
        opip_val = ip_uid

    p_name = " ".join([
        _clean(getattr(patient, "prefix", "")),
        _clean(getattr(patient, "first_name", "")),
        _clean(getattr(patient, "last_name", "")),
    ]).strip() or (_clean(getattr(patient, "full_name", "")) or "—")

    p_uhid = _clean(getattr(patient, "uhid", "")) or "—"
    p_phone = _clean(getattr(patient, "phone", "")) or _clean(
        getattr(patient, "mobile", "")) or "—"

    p_dob_raw = getattr(patient, "dob", None) or getattr(
        patient, "date_of_birth", None)
    p_dob = _fmt_date(p_dob_raw)
    age_years = _age_years_from_dob(p_dob_raw)
    p_age = f"{age_years} Y" if age_years is not None else "—"
    p_gender = _clean(getattr(patient, "gender", "")) or _clean(
        getattr(patient, "sex", "")) or "—"

    age_sex = ""
    if _present(p_age) or _present(p_gender):
        a = p_age if _present(p_age) else ""
        g = p_gender if _present(p_gender) else ""
        age_sex = (f"{a} / {g}").strip(" /")

    dept_name = _clean(getattr(dept, "name", ""))
    doc_name = _clean(getattr(doctor, "name", "")) or _clean(
        getattr(doctor, "full_name", ""))

    def _field(label: str, value: Any, *, right: bool = False) -> str:
        if not _present(value):
            return f"<div class='field empty{' right' if right else ''}'></div>"
        return (f"<div class='field{' right' if right else ''}'>"
                f"<span class='lab'>{_esc(label)}:</span>"
                f"<span class='val'>{_esc(value)}</span>"
                f"</div>")

    # -----------------------------
    # Vitals grid (4 x 2) + BMI
    # -----------------------------
    vitals_html = ""
    if vitals:
        ht = getattr(vitals, "height_cm", None)
        wt = getattr(vitals, "weight_kg", None)
        bmi = _calc_bmi(ht, wt)

        temp = getattr(vitals, "temp_c", None)

        bp_val = ""
        if getattr(vitals, "bp_systolic", None):
            dia = _clean(getattr(vitals, "bp_diastolic", ""))
            bp_val = f"{getattr(vitals, 'bp_systolic')}/{dia}" if dia else f"{getattr(vitals, 'bp_systolic')}"

        pulse = getattr(vitals, "pulse", None)
        rr = getattr(vitals, "rr", None)
        spo2 = getattr(vitals, "spo2", None)

        def vtxt(val: Any, unit: str = "") -> str:
            s = _clean(val)
            if not s:
                return "—"
            return f"{s}{(' ' + unit) if unit else ''}"

        vitems = [
            ("HT", vtxt(ht, "cm")),
            ("WT", vtxt(wt, "kg")),
            ("BMI", (str(bmi) if bmi is not None else "—")),
            ("TEMP", vtxt(temp, "°C")),
            ("BP", (bp_val + " mmHg") if bp_val else "—"),
            ("PULSE", (vtxt(pulse) + " /min") if _present(pulse) else "—"),
            ("RR", (vtxt(rr) + " /min") if _present(rr) else "—"),
            ("SpO2",
             (vtxt(spo2) + " %") if _present(spo2) else "—"),  # ✅ no subscript
        ]

        row1 = vitems[:4]
        row2 = vitems[4:]

        def cell(k: str, v: str) -> str:
            return f"""
            <td class="vcell">
              <div class="vk">{_esc(k)}</div>
              <div class="vv">{_esc(v)}</div>
            </td>
            """

        vitals_html = f"""
        <div class="block" style="margin-top:10px;">
          <div class="block-title">Vitals</div>
          <table class="vgrid">
            <tr>{''.join(cell(k, v) for k, v in row1)}</tr>
            <tr>{''.join(cell(k, v) for k, v in row2)}</tr>
          </table>
        </div>
        """

    # -----------------------------
    # Orders (simple professional block)
    # -----------------------------
    orders_bits: List[str] = []
    if lab_names:
        orders_bits.append(
            f"<div class='ord'><b>Lab:</b> {_esc(', '.join(lab_names))}</div>")
    if rad_names:
        orders_bits.append(
            f"<div class='ord'><b>Radiology:</b> {_esc(', '.join(rad_names))}</div>"
        )

    orders_html = ""
    if orders_bits:
        orders_html = f"""
        <div class="block" style="margin-top:10px;">
          <div class="block-title">Orders</div>
          {''.join(orders_bits)}
        </div>
        """

    # -----------------------------
    # Clinical sections (border only)
    # -----------------------------
    def sec(title: str, value: Any) -> str:
        v = _clean(value)
        if not v:
            return ""
        return f"""
        <div class="block" style="margin-top:10px;">
          <div class="block-title">{_esc(title)}</div>
          <div class="block-body">{_esc(v).replace("\n", "<br/>")}</div>
        </div>
        """

    sections_html = "".join([
        sec("Chief Complaint", getattr(visit, "chief_complaint", "")),
        sec("Presenting Illness (HPI)", getattr(visit, "presenting_illness",
                                                "")),
        sec("Symptoms", getattr(visit, "symptoms", "")),
        sec("Review of Systems", getattr(visit, "review_of_systems", "")),
        sec("Past Medical History", getattr(visit, "medical_history", "")),
        sec("Past Surgical History", getattr(visit, "surgical_history", "")),
        sec("Medication History", getattr(visit, "medication_history", "")),
        sec("Drug Allergy", getattr(visit, "drug_allergy", "")),
        sec("Family History", getattr(visit, "family_history", "")),
        sec("Personal History", getattr(visit, "personal_history", "")),
        sec("General Examination", getattr(visit, "general_examination", "")),
        sec("Systemic Examination", getattr(visit, "systemic_examination",
                                            "")),
        sec("Local Examination", getattr(visit, "local_examination", "")),
        sec("Provisional Diagnosis", getattr(visit, "provisional_diagnosis",
                                             "")),
        sec("Differential Diagnosis",
            getattr(visit, "differential_diagnosis", "")),
        sec("Final Diagnosis", getattr(visit, "final_diagnosis", "")),
        sec("Diagnosis Codes (ICD)", getattr(visit, "diagnosis_codes", "")),
        sec("Investigations", getattr(visit, "investigations", "")),
        sec("Treatment Plan", getattr(visit, "treatment_plan", "")),
        sec("Advice / Counselling", getattr(visit, "advice", "")),
        sec("Follow-up Plan", getattr(visit, "followup_plan", "")),
        sec("Referral Notes", getattr(visit, "referral_notes", "")),
        sec("Procedure Notes", getattr(visit, "procedure_notes", "")),
        sec("Counselling Notes", getattr(visit, "counselling_notes", "")),
    ])

    # -----------------------------
    # Rx table (professional: no fills, no radius)
    # -----------------------------
    tr_html = ""
    if not rx_items:
        tr_html = "<tr><td colspan='7' class='empty'>No medicines</td></tr>"
    else:
        for i, it in enumerate(rx_items, start=1):
            drug = _clean(getattr(it, "drug_name", "")) or _clean(
                getattr(it, "medicine_name", "")) or "—"
            strength = _clean(getattr(it, "strength", ""))
            dose = _clean(
                getattr(it, "dose_text", "") or getattr(it, "dose", "")
                or getattr(it, "dosage", ""))
            route = _clean(getattr(it, "route", ""))
            timing = _clean(
                getattr(it, "timing", "") or getattr(it, "instructions", "")
                or getattr(it, "instruction", ""))
            sub_parts = [
                p for p in [strength, dose, route, timing] if _present(p)
            ]
            sub = " • ".join(sub_parts)

            days = _clean(
                getattr(it, "duration_days", "") or getattr(it, "days", ""))
            if not _present(days):
                days = "—"

            freq = getattr(it, "frequency_code", None) or getattr(
                it, "frequency", None) or getattr(it, "freq", None)
            am, af, pm, night = freq_to_slots(_safe(freq))

            tr_html += f"""
              <tr>
                <td class="c num">{i}</td>
                <td class="med">
                  <div class="drug">{_esc(drug)}</div>
                  {f"<div class='sub'>{_esc(sub)}</div>" if sub else ""}
                </td>
                <td class="c">{am}</td>
                <td class="c">{af}</td>
                <td class="c">{pm}</td>
                <td class="c">{night}</td>
                <td class="c">{_esc(days)}</td>
              </tr>
            """

    rx_notes = _clean(getattr(rx, "notes", "")) if rx else ""
    rx_notes_html = (f"""
        <div class="block" style="margin-top:10px;">
          <div class="block-title">Notes</div>
          <div class="block-body">{_esc(rx_notes).replace("\n","<br/>")}</div>
        </div>
        """ if _present(rx_notes) else "")

    rx_html = ""
    if rx_items:
        rx_html = f"""
        <div style="height:10px;"></div>
        <div class="legend-line">
          After Food <span>[AF]</span> &nbsp; | &nbsp; Before Food <span>[BF]</span>
        </div>
        <div class="rule"></div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:26px;">S.No</th>
                <th>Drug &amp; Instruction</th>
                <th style="width:38px;">AM</th>
                <th style="width:38px;">AF</th>
                <th style="width:38px;">PM</th>
                <th style="width:48px;">Night</th>
                <th style="width:44px;">Days</th>
              </tr>
            </thead>
            <tbody>
              {tr_html}
            </tbody>
          </table>
        </div>

        {rx_notes_html}
        """

    header_html = render_brand_header_html(branding_obj)

    css = f"""
    {brand_header_css()}

    @page {{
      size: A4;
      margin: 14mm 14mm 16mm 14mm;
    }}

    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      color: #0f172a;
      font-size: 11px;
      line-height: 1.28;
    }}

    .rule {{
      height: 1px;
      background: #0f172a;
      opacity: 0.35;
      margin: 6px 0 10px 0;
    }}

    .row3 {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 14px;
    }}
    .row2 {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }}

    .field {{
      min-height: 18px;
      padding: 1px 2px 2px 2px;
      border-bottom: 1px solid #94a3b8;
      display: flex;
      gap: 6px;
      align-items: flex-end;
      min-width: 0;
    }}
    .field.right {{
      justify-content: flex-end;
      text-align: right;
    }}
    .field .lab {{
      font-size: 10px;
      color: #334155;
      font-weight: 800;
      white-space: nowrap;
    }}
    .field .val {{
      font-size: 11px;
      color: #0f172a;
      font-weight: 900;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .field.empty {{
      border-bottom: 1px solid #e2e8f0;
    }}
    .field.empty .lab,
    .field.empty .val {{
      display: none;
    }}

    .legend-line {{
      margin: 8px 0 6px 0;
      font-size: 10.5px;
      color: #0f172a;
      font-weight: 800;
    }}
    .legend-line span {{
      color: #334155;
      font-weight: 900;
    }}

    /* ✅ Professional blocks: no bg, no radius */
    .block {{
      border: 1px solid #cbd5e1;
      padding: 10px;
      border-radius: 0;
      background: transparent;
      page-break-inside: avoid;
    }}
    .block-title {{
      font-weight: 900;
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      font-size: 10px;
      color: #0f172a;
    }}
    .block-body {{
      color: #334155;
      font-size: 11px;
      line-height: 1.55;
    }}
    .ord {{
      color: #334155;
      font-size: 11px;
      line-height: 1.55;
      margin-top: 2px;
    }}

    /* ✅ Vitals grid 4x2 */
    .vgrid {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }}
    .vcell {{
      border: 1px solid #cbd5e1;
      padding: 10px 6px;
      text-align: center;
      vertical-align: middle;
    }}
    .vk {{
      font-size: 9.5px;
      font-weight: 900;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: #64748b;
      margin-bottom: 4px;
    }}
    .vv {{
      font-size: 12px;
      font-weight: 900;
      color: #0f172a;
    }}

    /* ✅ Rx table: no fills, no radius */
    .table-wrap {{
      border: 1px solid #cbd5e1;
      border-radius: 0;
      overflow: hidden;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }}
    thead th {{
      background: transparent;
      color: #0f172a;
      font-size: 10px;
      padding: 7px 8px;
      border-bottom: 1.2px solid #0f172a;
      border-right: 1px solid #cbd5e1;
      text-transform: uppercase;
      letter-spacing: 0.14em;
    }}
    thead th:last-child {{ border-right: none; }}

    tbody td {{
      border-top: 1px solid #cbd5e1;
      padding: 8px 8px;
      vertical-align: top;
      background: transparent;
    }}
    tbody tr:nth-child(even) td {{ background: transparent; }}

    .c {{ text-align: center; }}
    .num {{ width: 26px; }}
    .drug {{ font-weight: 900; color: #0f172a; }}
    .sub {{ margin-top: 3px; font-size: 10px; color: #475569; }}
    .empty {{
      text-align: left;
      color: #64748b;
      padding: 12px;
    }}

    tr {{ page-break-inside: avoid; }}
    """

    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <style>{css}</style>
      </head>
      <body>
        {header_html}

        <div style="height:6px;"></div>

        <div class="row3">
          {_field("OP No", op_no)}
         
          {_field("Date", visit_date, right=True)}
        </div>

        <div style="height:6px;"></div>

        <div class="row2">
          {_field("Patient Name", p_name)}
          {_field("UHID", p_uhid)}
        </div>

        <div style="height:6px;"></div>

        <div class="row3">
          {_field("DOB", p_dob)}
          {_field("Age/Sex", age_sex)}
          {_field("Mobile", p_phone, right=True)}
        </div>

        <div style="height:6px;"></div>

        <div class="row2">
          {_field("Department", dept_name)}
          {_field("Doctor", doc_name, right=True)}
        </div>

        {vitals_html}
        {orders_html}

        {sections_html}

        {rx_html}

      </body>
    </html>
    """.strip()

    return html


# -------------------------------------------------------------------
# ReportLab fallback (mandatory) - professional: no bg, no radius + vitals grid 4x2
# -------------------------------------------------------------------
def _build_visit_summary_pdf_reportlab(
    *,
    branding_obj: Any,
    visit: Visit,
    patient: Patient,
    dept: Department,
    doctor: User,
    vitals: Optional[Vitals],
    rx: Optional[Prescription],
    rx_items: List[PrescriptionItem],
    lab_names: List[str],
    rad_names: List[str],
) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    INK = colors.HexColor("#0f172a")
    MUTED = colors.HexColor("#475569")
    LINE = colors.HexColor("#cbd5e1")
    UNDER = colors.HexColor("#94a3b8")

    M = 14 * mm
    content_w = W - 2 * M
    y_min = 18 * mm

    # Branding values
    org_name = (_clean(getattr(branding_obj, "org_name", "")) or "NUTRYAH")
    org_tagline = _clean(getattr(branding_obj, "org_tagline", ""))
    org_addr = _clean(getattr(branding_obj, "org_address", ""))
    org_phone = _clean(getattr(branding_obj, "org_phone", ""))
    org_email = _clean(getattr(branding_obj, "org_email", ""))
    org_web = _clean(getattr(branding_obj, "org_website", ""))
    logo_path = _clean(getattr(branding_obj, "logo_path", ""))

    def _logo_reader() -> Optional[ImageReader]:
        if not logo_path:
            return None
        try:
            abs_path = Path(settings.STORAGE_DIR).joinpath(logo_path)
            if abs_path.exists() and abs_path.is_file():
                return ImageReader(str(abs_path))
        except Exception:
            return None
        return None

    # Derived values (✅ OP no = episode_id)
    op_no = (_clean(getattr(visit, "episode_id", ""))
             or _clean(getattr(visit, "visit_no", ""))
             or _clean(getattr(visit, "id", "")))
    visit_date = _fmt_date(
        getattr(visit, "visit_at", None) or getattr(visit, "created_at", None))

    op_uid = _clean(
        getattr(visit, "op_uid", "") or getattr(patient, "op_uid", "")
        or getattr(patient, "op_uuid", ""))
    ip_uid = _clean(
        getattr(visit, "ip_uid", "") or getattr(patient, "ip_uid", "")
        or getattr(patient, "ip_uuid", ""))

    opip_val = ""
    if _present(op_uid) and _present(ip_uid):
        opip_val = f"{op_uid} / {ip_uid}"
    elif _present(op_uid):
        opip_val = op_uid
    elif _present(ip_uid):
        opip_val = ip_uid

    p_name = " ".join([
        _clean(getattr(patient, "prefix", "")),
        _clean(getattr(patient, "first_name", "")),
        _clean(getattr(patient, "last_name", "")),
    ]).strip() or (_clean(getattr(patient, "full_name", "")) or "—")

    p_uhid = _clean(getattr(patient, "uhid", "")) or "—"
    p_phone = _clean(getattr(patient, "phone", "")) or _clean(
        getattr(patient, "mobile", "")) or "—"

    p_dob_raw = getattr(patient, "dob", None) or getattr(
        patient, "date_of_birth", None)
    p_dob = _fmt_date(p_dob_raw)
    age_years = _age_years_from_dob(p_dob_raw)
    p_age = f"{age_years} Y" if age_years is not None else "—"
    p_gender = _clean(getattr(patient, "gender", "")) or _clean(
        getattr(patient, "sex", "")) or "—"

    age_sex = ""
    if _present(p_age) or _present(p_gender):
        a = p_age if _present(p_age) else ""
        g = p_gender if _present(p_gender) else ""
        age_sex = (f"{a} / {g}").strip(" /")

    dept_name = _clean(getattr(dept, "name", ""))
    doc_name = _clean(getattr(doctor, "name", "")) or _clean(
        getattr(doctor, "full_name", ""))

    header_h = 25 * mm

    def draw_brand_header() -> float:
        y_top = H - M
        x = M

        # Logo left
        lr = _logo_reader()
        logo_w = 58 * mm
        logo_h = 18 * mm
        if lr:
            try:
                c.drawImage(
                    lr,
                    x,
                    y_top - logo_h,
                    width=logo_w,
                    height=logo_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
            except Exception:
                pass

        # ORG block right aligned
        xr = W - M
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 13.5)
        c.drawRightString(xr, y_top - 4.8 * mm, org_name)

        yy = y_top - 9.8 * mm
        if org_tagline:
            c.setFillColor(MUTED)
            c.setFont("Helvetica-Bold", 8.6)
            c.drawRightString(xr, yy, org_tagline)
            yy -= 3.9 * mm

        if org_addr:
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 8.4)
            addr_lines = _wrap(org_addr,
                               "Helvetica",
                               8.4,
                               92 * mm,
                               pdfmetrics=pdfmetrics)[:2]
            for ln in addr_lines:
                c.drawRightString(xr, yy, ln)
                yy -= 3.7 * mm

        contact_parts = [p for p in [org_email, org_phone] if p]
        if contact_parts:
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 8.4)
            c.drawRightString(xr, yy, " | ".join(contact_parts))
            yy -= 3.7 * mm

        if org_web:
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 8.4)
            c.drawRightString(xr, yy, org_web)

        return y_top - header_h

    def _draw_underlined_field(
        x: float,
        y: float,
        w: float,
        label: str,
        value: Any,
        *,
        right: bool = False,
        draw_empty_line: bool = True,
    ) -> None:
        if not _present(value):
            if draw_empty_line:
                c.setStrokeColor(LINE)
                c.setLineWidth(0.8)
                c.line(x, y - 1.6 * mm, x + w, y - 1.6 * mm)
            return

        value_s = _clean(value)
        if not _present(value_s):
            if draw_empty_line:
                c.setStrokeColor(LINE)
                c.setLineWidth(0.8)
                c.line(x, y - 1.6 * mm, x + w, y - 1.6 * mm)
            return

        label_txt = f"{label}:"
        pad = 2.0 * mm
        x0 = x
        x1 = x + w

        lw = pdfmetrics.stringWidth(label_txt, "Helvetica-Bold", 8.6)
        vw = pdfmetrics.stringWidth(value_s, "Helvetica-Bold", 9.1)

        if right:
            start = max(x0, x1 - (lw + pad + vw))
            c.setFont("Helvetica-Bold", 8.6)
            c.setFillColor(MUTED)
            c.drawString(start, y, label_txt)

            c.setFont("Helvetica-Bold", 9.1)
            c.setFillColor(INK)
            c.drawString(start + lw + pad, y, value_s)

            ul_start = start + lw + pad
            ul_end = x1
        else:
            c.setFont("Helvetica-Bold", 8.6)
            c.setFillColor(MUTED)
            c.drawString(x0, y, label_txt)

            c.setFont("Helvetica-Bold", 9.1)
            c.setFillColor(INK)
            c.drawString(x0 + lw + pad, y, value_s)

            ul_start = x0 + lw + pad
            ul_end = x1

        c.setStrokeColor(UNDER)
        c.setLineWidth(0.8)
        c.line(ul_start, y - 1.6 * mm, ul_end, y - 1.6 * mm)

    def draw_patient_block(y_base: float) -> float:
        y = y_base - 6.0 * mm  # medium spacing from header

        col_w = (content_w - 8 * mm) / 3.0
        gap = 4 * mm

        _draw_underlined_field(M, y, col_w, "OP No", op_no)
        _draw_underlined_field(W - M - col_w,
                               y,
                               col_w,
                               "Date",
                               visit_date,
                               right=True)

        y -= 7.0 * mm
        col2_w = (content_w - 6 * mm) / 2.0
        gap2 = 6 * mm
        _draw_underlined_field(M, y, col2_w, "Patient Name", p_name)
        _draw_underlined_field(M + col2_w + gap2, y, col2_w, "UHID", p_uhid)

        y -= 7.0 * mm
        _draw_underlined_field(M, y, col_w, "DOB", p_dob)
        _draw_underlined_field(M + col_w + gap, y, col_w, "Age/Sex", age_sex)
        _draw_underlined_field(W - M - col_w,
                               y,
                               col_w,
                               "Mobile",
                               p_phone,
                               right=True)

        y -= 7.0 * mm
        _draw_underlined_field(M, y, col2_w, "Department", dept_name)
        _draw_underlined_field(M + col2_w + gap2,
                               y,
                               col2_w,
                               "Doctor",
                               doc_name,
                               right=True)

        return y - 5.0 * mm

    def ensure_space(y: float,
                     need: float,
                     *,
                     draw_patient: bool = True) -> float:
        if y - need < y_min:
            c.showPage()
            y0 = draw_brand_header()
            y1 = draw_patient_block(y0) if draw_patient else (y0 - 6 * mm)
            return y1
        return y

    def wrap_paragraph(text: str, font: str, size: float,
                       width: float) -> List[str]:
        if not _present(text):
            return []
        parts: List[str] = []
        for chunk in (text or "").splitlines():
            chunk = chunk.strip()
            if not chunk:
                continue
            parts.extend(_wrap(chunk, font, size, width,
                               pdfmetrics=pdfmetrics))
        return parts

    # ✅ Professional block (no bg, no radius)
    def draw_block(y_top: float, title: str, body_lines: List[str]) -> float:
        pad_x = 4 * mm
        pad_t = 4.5 * mm
        pad_b = 4.5 * mm
        title_h = 4.8 * mm
        line_h = 4.2 * mm

        h = pad_t + title_h + (len(body_lines) * line_h) + pad_b
        y_top = ensure_space(y_top, h + 2 * mm)

        c.setStrokeColor(LINE)
        c.setLineWidth(1)
        c.rect(M, y_top - h, content_w, h, stroke=1, fill=0)

        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.6)
        c.drawString(M + pad_x, y_top - pad_t - 1.5 * mm, title.upper())

        c.setFillColor(colors.HexColor("#334155"))
        c.setFont("Helvetica", 9)
        yy = y_top - pad_t - title_h - 1.0 * mm
        for ln in body_lines:
            c.drawString(M + pad_x, yy, ln)
            yy -= line_h

        return y_top - h - 6 * mm

    # ✅ Vitals grid 4x2 (bordered cells)
    def draw_vitals_grid(y_top: float, vit: Vitals) -> float:
        pad = 4 * mm
        title_h = 5.0 * mm
        cell_h = 14.0 * mm
        grid_h = 2 * cell_h
        h = pad + title_h + 3 * mm + grid_h + pad
        y_top = ensure_space(y_top, h + 2 * mm)

        # outer block
        c.setStrokeColor(LINE)
        c.setLineWidth(1)
        c.rect(M, y_top - h, content_w, h, stroke=1, fill=0)

        # title
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.6)
        c.drawString(M + pad, y_top - pad - 1.0 * mm, "VITALS")

        # values
        ht = getattr(vit, "height_cm", None)
        wt = getattr(vit, "weight_kg", None)
        bmi = _calc_bmi(ht, wt)
        temp = getattr(vit, "temp_c", None)

        bp_val = ""
        if getattr(vit, "bp_systolic", None):
            dia = _clean(getattr(vit, "bp_diastolic", ""))
            bp_val = f"{getattr(vit, 'bp_systolic')}/{dia}" if dia else f"{getattr(vit, 'bp_systolic')}"

        pulse = getattr(vit, "pulse", None)
        rr = getattr(vit, "rr", None)
        spo2 = getattr(vit, "spo2", None)

        def vtxt(val: Any, unit: str = "") -> str:
            s = _clean(val)
            if not s:
                return "—"
            return f"{s}{(' ' + unit) if unit else ''}"

        items = [
            ("HT", vtxt(ht, "cm")),
            ("WT", vtxt(wt, "kg")),
            ("BMI", (str(bmi) if bmi is not None else "—")),
            ("TEMP", vtxt(temp, "°C")),
            ("BP", (bp_val + " mmHg") if bp_val else "—"),
            ("PULSE", (vtxt(pulse) + " /min") if _present(pulse) else "—"),
            ("RR", (vtxt(rr) + " /min") if _present(rr) else "—"),
            ("SpO2", (vtxt(spo2) + " %") if _present(spo2) else "—"),
        ]

        x0 = M + pad
        y0 = y_top - pad - title_h - 4 * mm  # start grid top
        grid_w = content_w - 2 * pad
        cell_w = grid_w / 4.0

        # draw cells
        idx = 0
        for row in range(2):
            for col in range(4):
                k, v = items[idx]
                idx += 1
                cx = x0 + col * cell_w
                cy_top = y0 - row * cell_h

                c.setStrokeColor(LINE)
                c.setLineWidth(1)
                c.rect(cx, cy_top - cell_h, cell_w, cell_h, stroke=1, fill=0)

                c.setFillColor(colors.HexColor("#64748b"))
                c.setFont("Helvetica-Bold", 8.3)
                c.drawCentredString(cx + cell_w / 2, cy_top - 5.2 * mm, k)

                c.setFillColor(INK)
                c.setFont("Helvetica-Bold", 10.5)
                c.drawCentredString(cx + cell_w / 2, cy_top - 10.8 * mm, v)

        return y_top - h - 6 * mm

    # Rx table (professional: no bg header, no zebra)
    cols = [
        ("S.No", 9 * mm),
        ("Drug & Instruction", content_w - (9 + 11 + 11 + 11 + 14 + 14) * mm),
        ("AM", 11 * mm),
        ("AF", 11 * mm),
        ("PM", 11 * mm),
        ("Night", 14 * mm),
        ("Days", 14 * mm),
    ]
    x_positions = [M]
    for _, wcol in cols:
        x_positions.append(x_positions[-1] + wcol)

    def draw_rx_legend(y: float) -> float:
        y = ensure_space(y, 14 * mm)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.0)
        c.drawString(M, y, "After Food [AF]   |   Before Food [BF]")
        y -= 5.5 * mm
        c.setStrokeColor(INK)
        c.setLineWidth(0.8)
        c.line(M, y, W - M, y)
        return y - 6 * mm

    def draw_table_header(y_top: float) -> float:
        h = 8 * mm
        y_top = ensure_space(y_top, h + 2 * mm)

        # header area border only
        c.setStrokeColor(LINE)
        c.setLineWidth(1)
        c.rect(M, y_top - h, content_w, h, stroke=1, fill=0)

        # bottom strong line
        c.setStrokeColor(INK)
        c.setLineWidth(1.0)
        c.line(M, y_top - h, W - M, y_top - h)

        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8.6)

        xx = M
        for title, wcol in cols:
            c.drawCentredString(xx + wcol / 2, y_top - h + 2.4 * mm,
                                title.upper())
            xx += wcol

        # vertical separators
        c.setStrokeColor(LINE)
        c.setLineWidth(1)
        xx = M
        for _, wcol in cols[:-1]:
            xx += wcol
            c.line(xx, y_top - h, xx, y_top)

        return y_top - h

    def draw_rx_row(
        y_top: float,
        idx: int,
        drug: str,
        sub: str,
        am: int,
        af: int,
        pm: int,
        night: int,
        days: str,
    ) -> float:
        from reportlab.pdfbase import pdfmetrics as _pm

        med_w = cols[1][1] - 4 * mm
        drug_lines = _wrap(drug, "Helvetica-Bold", 9.2, med_w,
                           pdfmetrics=_pm)[:2]
        sub_lines = _wrap(sub, "Helvetica", 8.0, med_w,
                          pdfmetrics=_pm)[:2] if sub else []

        pad_t = 2.0 * mm
        pad_b = 2.0 * mm
        lh1 = 4.1 * mm
        lh2 = 3.6 * mm
        row_h = pad_t + len(drug_lines) * lh1 + (len(sub_lines) * lh2) + pad_b
        row_h = max(row_h, 9.5 * mm)

        if y_top - row_h < y_min:
            c.showPage()
            y0 = draw_brand_header()
            y1 = draw_patient_block(y0)
            y1 = draw_rx_legend(y1)
            y1 = draw_table_header(y1)
            y_top = y1

        c.setStrokeColor(LINE)
        c.setLineWidth(1)
        c.rect(M, y_top - row_h, content_w, row_h, stroke=1, fill=0)

        for xp in x_positions[1:-1]:
            c.line(xp, y_top - row_h, xp, y_top)

        c.setFillColor(INK)
        c.setFont("Helvetica", 9)
        c.drawCentredString((x_positions[0] + x_positions[1]) / 2,
                            y_top - row_h / 2 - 1.5 * mm, str(idx))

        x_text = x_positions[1] + 2 * mm
        y_text = y_top - pad_t - lh1 + 0.6 * mm

        c.setFont("Helvetica-Bold", 9.2)
        c.setFillColor(INK)
        for ln in drug_lines:
            c.drawString(x_text, y_text, ln)
            y_text -= lh1

        if sub_lines:
            c.setFont("Helvetica", 8.0)
            c.setFillColor(MUTED)
            for ln in sub_lines:
                c.drawString(x_text, y_text + 0.2 * mm, ln)
                y_text -= lh2

        def draw_center(col_idx: int, val: Any) -> None:
            xc = (x_positions[col_idx] + x_positions[col_idx + 1]) / 2
            c.setFont("Helvetica-Bold", 9.0)
            c.setFillColor(INK)
            c.drawCentredString(xc, y_top - row_h / 2 - 1.5 * mm, str(val))

        draw_center(2, am)
        draw_center(3, af)
        draw_center(4, pm)
        draw_center(5, night)
        draw_center(6, days)

        return y_top - row_h

    # -------------------------------
    # Render report
    # -------------------------------
    y = draw_brand_header()
    y = draw_patient_block(y)

    # Vitals grid (4x2)
    if vitals:
        y = draw_vitals_grid(y, vitals)

    # Orders block
    if lab_names or rad_names:
        order_lines: List[str] = []
        if lab_names:
            order_lines.extend(
                wrap_paragraph("Lab: " + ", ".join(lab_names), "Helvetica", 9,
                               content_w - 8 * mm))
        if rad_names:
            order_lines.extend(
                wrap_paragraph("Radiology: " + ", ".join(rad_names),
                               "Helvetica", 9, content_w - 8 * mm))
        if order_lines:
            y = draw_block(y, "Orders", order_lines)

    # Clinical sections
    sections: List[Tuple[str, str]] = [
        ("Chief Complaint", _clean(getattr(visit, "chief_complaint", ""))),
        ("Presenting Illness (HPI)",
         _clean(getattr(visit, "presenting_illness", ""))),
        ("Symptoms", _clean(getattr(visit, "symptoms", ""))),
        ("Review of Systems", _clean(getattr(visit, "review_of_systems", ""))),
        ("Past Medical History", _clean(getattr(visit, "medical_history",
                                                ""))),
        ("Past Surgical History", _clean(getattr(visit, "surgical_history",
                                                 ""))),
        ("Medication History", _clean(getattr(visit, "medication_history",
                                              ""))),
        ("Drug Allergy", _clean(getattr(visit, "drug_allergy", ""))),
        ("Family History", _clean(getattr(visit, "family_history", ""))),
        ("Personal History", _clean(getattr(visit, "personal_history", ""))),
        ("General Examination",
         _clean(getattr(visit, "general_examination", ""))),
        ("Systemic Examination",
         _clean(getattr(visit, "systemic_examination", ""))),
        ("Local Examination", _clean(getattr(visit, "local_examination", ""))),
        ("Provisional Diagnosis",
         _clean(getattr(visit, "provisional_diagnosis", ""))),
        ("Differential Diagnosis",
         _clean(getattr(visit, "differential_diagnosis", ""))),
        ("Final Diagnosis", _clean(getattr(visit, "final_diagnosis", ""))),
        ("Diagnosis Codes (ICD)", _clean(getattr(visit, "diagnosis_codes",
                                                 ""))),
        ("Investigations", _clean(getattr(visit, "investigations", ""))),
        ("Treatment Plan", _clean(getattr(visit, "treatment_plan", ""))),
        ("Advice / Counselling", _clean(getattr(visit, "advice", ""))),
        ("Follow-up Plan", _clean(getattr(visit, "followup_plan", ""))),
        ("Referral Notes", _clean(getattr(visit, "referral_notes", ""))),
        ("Procedure Notes", _clean(getattr(visit, "procedure_notes", ""))),
        ("Counselling Notes", _clean(getattr(visit, "counselling_notes", ""))),
    ]

    for title, val in sections:
        if not _present(val):
            continue
        body_lines = wrap_paragraph(val, "Helvetica", 9, content_w - 8 * mm)
        if body_lines:
            y = draw_block(y, title, body_lines)

    # Prescription table
    if rx_items:
        y = ensure_space(y, 22 * mm)
        y = draw_rx_legend(y)
        y = draw_table_header(y)

        for i, it in enumerate(rx_items, start=1):
            drug = _clean(getattr(it, "drug_name", "")) or _clean(
                getattr(it, "medicine_name", "")) or "—"
            strength = _clean(getattr(it, "strength", ""))
            dose = _clean(
                getattr(it, "dose_text", "") or getattr(it, "dose", "")
                or getattr(it, "dosage", ""))
            route = _clean(getattr(it, "route", ""))
            timing = _clean(
                getattr(it, "timing", "") or getattr(it, "instructions", "")
                or getattr(it, "instruction", ""))

            sub_parts = [
                p for p in [strength, dose, route, timing] if _present(p)
            ]
            sub = " • ".join(sub_parts)

            days = _clean(
                getattr(it, "duration_days", "")
                or getattr(it, "days", "")) or "—"
            freq = getattr(it, "frequency_code", None) or getattr(
                it, "frequency", None) or getattr(it, "freq", None)
            am, af, pm, night = freq_to_slots(_safe(freq))

            y = draw_rx_row(y, i, drug, sub, am, af, pm, night, days)

        # Rx Notes
        rx_notes = _clean(getattr(rx, "notes", "")) if rx else ""
        if _present(rx_notes):
            lines = wrap_paragraph(rx_notes, "Helvetica", 9,
                                   content_w - 8 * mm)[:12]
            if lines:
                y = draw_block(y, "Notes", lines)

    c.save()
    return buf.getvalue()


# -------------------------------------------------------------------
# Public API (WeasyPrint + mandatory fallback)
# -------------------------------------------------------------------
def build_visit_summary_pdf(db: Session, visit_id: int) -> BytesIO:
    v: Visit = (db.query(Visit).options(
        joinedload(Visit.patient),
        joinedload(Visit.department),
        joinedload(Visit.doctor),
        joinedload(Visit.appointment),
    ).get(visit_id))
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    patient: Patient = v.patient
    dept: Department = v.department
    doctor: User = v.doctor

    branding = db.query(UiBranding).order_by(UiBranding.id.desc()).first()
    if not branding:

        class _B:
            org_name = "NUTRYAH"
            org_tagline = ""
            org_address = ""
            org_email = ""
            org_phone = ""
            org_website = ""
            org_gstin = ""
            logo_path = ""

        branding = _B()

    # vitals: prefer appointment-linked; else latest by patient
    vit = None
    if getattr(v, "appointment_id", None) and hasattr(Vitals,
                                                      "appointment_id"):
        vit = (db.query(Vitals).filter(
            Vitals.appointment_id == v.appointment_id).order_by(
                Vitals.created_at.desc()).first())
    if not vit:
        vit = (db.query(Vitals).filter(
            Vitals.patient_id == v.patient_id).order_by(
                Vitals.created_at.desc()).first())

    # prescription
    rx = db.query(Prescription).filter(
        Prescription.visit_id == visit_id).first()
    rx_items: List[PrescriptionItem] = []
    if rx:
        rx_items = (db.query(PrescriptionItem).filter(
            PrescriptionItem.prescription_id == rx.id).order_by(
                PrescriptionItem.id.asc()).all())

    # orders
    lab_rows = (db.query(LabOrder, LabTest.name).join(
        LabTest, LabTest.id == LabOrder.test_id).filter(
            LabOrder.visit_id == visit_id).all())
    lab_names = [n for (_, n) in lab_rows if n]

    rad_rows = (db.query(RadiologyOrder, RadiologyTest.name).join(
        RadiologyTest, RadiologyTest.id == RadiologyOrder.test_id).filter(
            RadiologyOrder.visit_id == visit_id).all())
    rad_names = [n for (_, n) in rad_rows if n]

    # Try WeasyPrint first
    try:
        from weasyprint import HTML  # type: ignore

        html = _build_visit_summary_html(
            branding_obj=branding,
            visit=v,
            patient=patient,
            dept=dept,
            doctor=doctor,
            vitals=vit,
            rx=rx,
            rx_items=rx_items,
            lab_names=lab_names,
            rad_names=rad_names,
        )
        pdf_bytes = HTML(string=html,
                         base_url=str(settings.STORAGE_DIR)).write_pdf()
        buff = BytesIO(pdf_bytes)
        buff.seek(0)
        return buff
    except Exception:
        pdf_bytes = _build_visit_summary_pdf_reportlab(
            branding_obj=branding,
            visit=v,
            patient=patient,
            dept=dept,
            doctor=doctor,
            vitals=vit,
            rx=rx,
            rx_items=rx_items,
            lab_names=lab_names,
            rad_names=rad_names,
        )
        buff = BytesIO(pdf_bytes)
        buff.seek(0)
        return buff
