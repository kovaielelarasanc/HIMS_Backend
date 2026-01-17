# FILE: app/services/pdf_prescription.py
from __future__ import annotations

from datetime import datetime, date
from typing import Any, Optional, Tuple, List
from io import BytesIO
import html as _html
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics

from app.core.config import settings
from app.services.pdf_branding import brand_header_css, render_brand_header_html


# -------------------------------
# Helpers
# -------------------------------
def _g(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _safe(v: Any) -> str:
    return "" if v is None else str(v)


def _esc(v: Any) -> str:
    return _html.escape(_safe(v), quote=True)


def _present(v: Any) -> bool:
    s = (_safe(v) or "").strip()
    return bool(s) and s not in ("—", "-", "None", "null", "NULL")


def _first_present(obj: Any, keys: List[str], default: Any = "") -> Any:
    for k in keys:
        v = _g(obj, k, None)
        if _present(v):
            return v
    return default


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


def _age_years_from_dob(dob: Any, asof: Optional[date] = None) -> Optional[int]:
    d = _to_date(dob)
    if not d:
        return None
    asof = asof or date.today()
    years = asof.year - d.year - ((asof.month, asof.day) < (d.month, d.day))
    return max(0, int(years))


def _wrap(text: str, font: str, size: float, max_w: float) -> List[str]:
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


def _fmt_weight(v: Any) -> str:
    if not _present(v):
        return "—"
    s = str(v).strip()
    try:
        f = float(s)
        if f <= 0:
            return "—"
        # allow grams if tiny?
        if f < 3:  # could be kg still; keep kg
            return f"{f:g} kg"
        return f"{f:g} kg"
    except Exception:
        # if already like "12 kg"
        return s


def _join_nonempty(parts: List[str], sep: str = ", ") -> str:
    out = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in ("—", "-", "None", "null", "NULL"):
            out.append(p)
    return sep.join(out)


def _patient_address(patient: Any) -> str:
    if not patient:
        return "—"
    # Try common address shapes
    addr = _first_present(patient, ["address", "full_address", "patient_address"], "")
    if _present(addr):
        return str(addr).strip()

    line1 = _first_present(patient, ["address_line1", "address1", "line1", "street", "house", "door_no"], "")
    line2 = _first_present(patient, ["address_line2", "address2", "line2", "area", "locality"], "")
    city = _first_present(patient, ["city", "town"], "")
    district = _first_present(patient, ["district"], "")
    state = _first_present(patient, ["state"], "")
    pincode = _first_present(patient, ["pincode", "pin", "zip", "postal_code"], "")

    composed = _join_nonempty([str(line1), str(line2), str(city), str(district), str(state), str(pincode)])
    return composed if _present(composed) else "—"


def _format_allergies(val: Any) -> str:
    if not _present(val):
        return "—"
    if isinstance(val, (list, tuple, set)):
        items = [str(x).strip() for x in val if _present(x)]
        return _join_nonempty(items, sep=", ") if items else "—"
    return str(val).strip()


def _format_vitals(rx: Any) -> str:
    if not rx:
        return "—"
    # support either a dict vitals or flat fields
    vit = _g(rx, "vitals", None)
    if isinstance(vit, dict):
        bp = vit.get("bp") or ""
        sys = vit.get("bp_sys") or vit.get("systolic") or ""
        dia = vit.get("bp_dia") or vit.get("diastolic") or ""
        if not bp and _present(sys) and _present(dia):
            bp = f"{sys}/{dia}"
        temp = vit.get("temp") or vit.get("temperature") or vit.get("temp_c") or ""
        pulse = vit.get("pulse") or vit.get("pr") or ""
        spo2 = vit.get("spo2") or ""
        rr = vit.get("rr") or vit.get("resp_rate") or ""
    else:
        bp = _g(rx, "bp", "") or ""
        sys = _g(rx, "bp_sys", "") or _g(rx, "systolic", "") or ""
        dia = _g(rx, "bp_dia", "") or _g(rx, "diastolic", "") or ""
        if not bp and _present(sys) and _present(dia):
            bp = f"{sys}/{dia}"
        temp = _g(rx, "temperature", "") or _g(rx, "temp", "") or _g(rx, "temp_c", "") or ""
        pulse = _g(rx, "pulse", "") or _g(rx, "pr", "") or ""
        spo2 = _g(rx, "spo2", "") or ""
        rr = _g(rx, "rr", "") or _g(rx, "resp_rate", "") or ""

    parts = []
    if _present(bp):
        parts.append(f"BP {str(bp).strip()}")
    if _present(temp):
        parts.append(f"Temp {str(temp).strip()}")
    if _present(pulse):
        parts.append(f"Pulse {str(pulse).strip()}")
    if _present(spo2):
        parts.append(f"SpO₂ {str(spo2).strip()}")
    if _present(rr):
        parts.append(f"RR {str(rr).strip()}")
    joined = " | ".join(parts)
    return joined if _present(joined) else "—"


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


def _estimate_qty(days_raw: Any, freq: Any) -> Optional[int]:
    """
    If line doesn't explicitly contain qty/quantity, estimate using (sum of slots) * days.
    This is a rough estimate; if your model stores exact quantity, prefer that.
    """
    try:
        d = int(float(str(days_raw).strip()))
    except Exception:
        return None
    am, af, pm, night = freq_to_slots(freq)
    per_day = int(am + af + pm + night)
    if d > 0 and per_day > 0:
        return d * per_day
    return None


# -------------------------------------------------------------------
# WeasyPrint HTML
# -------------------------------------------------------------------
def _build_prescription_html(
    *,
    branding_obj: Any,
    rx: Any,
    patient: Any | None,
    doctor: Any | None,
) -> str:
    # --- Rx basics
    rx_no = _safe(_g(rx, "rx_number", _g(rx, "rx_no", "—"))) or "—"
    rx_dt = _g(rx, "rx_datetime") or _g(rx, "created_at") or _g(rx, "date") or _g(rx, "prescribed_at")
    rx_date = _fmt_date(rx_dt)

    op_uid = _safe(_g(rx, "op_uid", "")).strip()
    ip_uid = _safe(_g(rx, "ip_uid", "")).strip()

    # --- Doctor / prescriber
    d_name = (_safe(_first_present(doctor, ["full_name", "name"], "—")) or "—").strip()
    d_qual = _safe(_first_present(doctor, ["doctor_qualification", "qualification"], "")).strip()
    d_reg = _safe(_first_present(doctor, ["doctor_registration_no", "registration_no"], "")).strip()
    d_pathy = _safe(_first_present(doctor, ["doctor_registration_prefix", "pathy_prefix", "pathy"], "")).strip()
    d_dept = ""
    try:
        dept_obj = _g(doctor, "department", None)
        d_dept = _safe(_g(dept_obj, "name", "")).strip()
    except Exception:
        d_dept = ""

    reg_disp = ""
    if _present(d_reg) and _present(d_pathy):
        reg_disp = f"{d_pathy} {d_reg}".strip()
    elif _present(d_reg):
        reg_disp = d_reg

    # --- Patient
    p_name = " ".join([
        _safe(_g(patient, "prefix", "")).strip(),
        _safe(_g(patient, "first_name", "")).strip(),
        _safe(_g(patient, "last_name", "")).strip(),
    ]).strip() or (_safe(_g(patient, "full_name", "—")) or "—")

    p_uhid = _safe(_g(patient, "uhid", "—")) or "—"
    p_phone = _safe(_first_present(patient, ["phone", "mobile", "contact_no"], "—")) or "—"

    p_addr = _patient_address(patient)

    p_dob_raw = _g(patient, "dob", _g(patient, "date_of_birth", None))
    p_dob = _fmt_date(p_dob_raw)
    age_years = _age_years_from_dob(p_dob_raw)
    p_age = f"{age_years} Y" if age_years is not None else "—"
    p_gender = _safe(_g(patient, "gender", _g(patient, "sex", "—"))) or "—"

    # Weight (especially pediatric) - accept patient.weight_kg or rx.weight_kg etc.
    p_wt_raw = _first_present(patient, ["weight_kg", "weight", "wt"], None)
    if not _present(p_wt_raw):
        p_wt_raw = _first_present(rx, ["weight_kg", "weight", "wt"], None)
    p_weight = _fmt_weight(p_wt_raw)

    # Clinical info (optional)
    diagnosis = _safe(_first_present(rx, ["diagnosis", "diagnosis_text", "problem", "chief_complaint"], "")).strip()
    allergies_raw = _first_present(patient, ["drug_allergies", "allergies", "allergy"], None)
    if not _present(allergies_raw):
        allergies_raw = _first_present(rx, ["drug_allergies", "allergies", "allergy"], None)
    allergies = _format_allergies(allergies_raw)
    vitals = _format_vitals(rx)

    follow_up = _first_present(rx, ["follow_up_date", "review_date", "next_visit_date", "followup_date"], None)
    follow_up_s = _fmt_date(follow_up) if _present(follow_up) else "—"

    notes = (_safe(_g(rx, "notes", "")) or "").strip()
    advice = (_safe(_first_present(rx, ["advice", "precautions"], "")) or "").strip()

    # OP/IP combined
    opip_val = ""
    if _present(op_uid) and _present(ip_uid):
        opip_val = f"{op_uid} / {ip_uid}"
    elif _present(op_uid):
        opip_val = op_uid
    elif _present(ip_uid):
        opip_val = ip_uid

    # Age/Sex combined
    age_sex = ""
    if _present(p_age) or _present(p_gender):
        a = p_age if _present(p_age) else ""
        g = p_gender if _present(p_gender) else ""
        age_sex = (f"{a} / {g}").strip(" /")

    def _field(label: str, value: Any, *, right: bool = False) -> str:
        if not _present(value):
            return f"<div class='field empty{' right' if right else ''}'></div>"
        return (f"<div class='field{' right' if right else ''}'>"
                f"<span class='lab'>{_esc(label)}:</span>"
                f"<span class='val'>{_esc(value)}</span>"
                f"</div>")

    def _kv(label: str, value: Any) -> str:
        if not _present(value) or str(value).strip() == "—":
            return ""
        return f"<div class='kv'><span class='k'>{_esc(label)}:</span><span class='v'>{_esc(value)}</span></div>"

    # table rows
    lines = _g(rx, "lines", []) or []
    tr_html = ""
    if not lines:
        tr_html = "<tr><td colspan='8' class='empty'>No medicines</td></tr>"
    else:
        for i, ln in enumerate(lines, start=1):
            drug_raw = _safe(_g(ln, "item_name", _g(_g(ln, "item", None), "name", "—"))) or "—"
            drug = drug_raw.strip()
            # NABH: Prefer generic + CAPS; we show CAPS for safety/legibility
            drug_caps = drug.upper() if _present(drug) else "—"

            strength = _safe(_first_present(ln, ["strength", "strength_text", "mg", "dose_strength"], "")).strip()
            form = _safe(_first_present(ln, ["dosage_form", "form", "form_text"], "")).strip()

            dose = (_safe(_g(ln, "dose_text", "")) or "").strip()
            route = (_safe(_g(ln, "route", "")) or "").strip()
            timing = (_safe(_g(ln, "timing", "")) or "").strip()
            inst = (_safe(_g(ln, "instructions", "")) or "").strip()
            if not timing and inst:
                timing = inst

            days = _safe(_g(ln, "duration_days", _g(ln, "days", "—"))) or "—"
            freq = _g(ln, "frequency_code", _g(ln, "frequency", _g(ln, "freq", None)))
            am, af, pm, night = freq_to_slots(freq)

            qty_raw = _first_present(ln, ["qty", "quantity", "total_quantity", "dispense_qty", "total_qty"], None)
            if not _present(qty_raw):
                est = _estimate_qty(days, freq)
                qty_raw = est if est is not None else "—"
            qty = str(qty_raw)

            sub_parts = []
            if _present(strength) or _present(form):
                sf = _join_nonempty([strength, form], sep=" • ")
                if _present(sf):
                    sub_parts.append(sf)
            for p in [dose, route, timing]:
                if _present(p):
                    sub_parts.append(p)
            sub = " • ".join(sub_parts)

            tr_html += f"""
              <tr>
                <td class="c num">{i}</td>
                <td class="med">
                  <div class="drug">{_esc(drug_caps)}</div>
                  {f"<div class='sub'>{_esc(sub)}</div>" if sub else ""}
                </td>
                <td class="c">{am}</td>
                <td class="c">{af}</td>
                <td class="c">{pm}</td>
                <td class="c">{night}</td>
                <td class="c">{_esc(days)}</td>
                <td class="c">{_esc(qty)}</td>
              </tr>
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
      line-height: 1.25;
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

    .card {{
      margin-top: 10px;
      border: 1px solid #e5e7eb;
      background: #f8fafc;
      border-radius: 12px;
      padding: 10px;
    }}
    .card .title {{
      font-weight: 950;
      font-size: 11px;
      margin-bottom: 6px;
      letter-spacing: 0.2px;
    }}
    .kv {{
      display: grid;
      grid-template-columns: 110px 1fr;
      gap: 8px;
      padding: 2px 0;
    }}
    .kv .k {{
      font-size: 10px;
      color: #334155;
      font-weight: 900;
      white-space: nowrap;
    }}
    .kv .v {{
      font-size: 10.5px;
      color: #0f172a;
      font-weight: 800;
      word-break: break-word;
    }}

    .rxhead {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 10px;
      margin-bottom: 6px;
    }}
    .rxmark {{
      width: 34px;
      height: 34px;
      border-radius: 10px;
      background: #0b1220;
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 950;
      font-size: 16px;
    }}
    .rxtext {{
      font-weight: 950;
      font-size: 12px;
      color: #0f172a;
    }}
    .rxsub {{
      margin-top: 1px;
      font-size: 10px;
      color: #475569;
      font-weight: 800;
    }}

    .legend-line {{
      margin: 6px 0 6px 0;
      font-size: 10.5px;
      color: #0f172a;
      font-weight: 800;
    }}
    .legend-line span {{
      color: #334155;
      font-weight: 900;
    }}

    .table-wrap {{
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      overflow: hidden;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }}
    thead th {{
      background: #0b1220;
      color: #ffffff;
      font-size: 10.5px;
      padding: 8px 6px;
      border-right: 1px solid #1f2937;
    }}
    thead th:last-child {{ border-right: none; }}

    tbody td {{
      border-top: 1px solid #e5e7eb;
      padding: 8px 6px;
      vertical-align: top;
    }}
    tbody tr:nth-child(even) td {{
      background: #f8fafc;
    }}

    .c {{ text-align: center; }}
    .num {{ width: 26px; }}
    .drug {{ font-weight: 950; color: #0f172a; letter-spacing: 0.2px; }}
    .sub {{ margin-top: 3px; font-size: 10px; color: #475569; font-weight: 800; }}
    .empty {{
      text-align: left;
      color: #64748b;
      padding: 12px;
    }}

    .notes {{
      margin-top: 10px;
      border: 1px solid #e5e7eb;
      background: #f8fafc;
      border-radius: 12px;
      padding: 10px;
    }}
    .notes .label {{
      font-weight: 950;
      margin-bottom: 6px;
    }}

    .sig {{
      margin-top: 14px;
      display: grid;
      grid-template-columns: 1fr 260px;
      gap: 10px;
      align-items: end;
    }}
    .stamp {{
      border: 1px dashed #94a3b8;
      border-radius: 12px;
      padding: 10px;
      min-height: 62px;
      color: #334155;
      font-weight: 900;
      font-size: 10px;
    }}
    .sigbox {{
      border-top: 1px solid #111827;
      padding-top: 6px;
      text-align: right;
      font-weight: 950;
      font-size: 10px;
      color: #0f172a;
    }}
    .sigmeta {{
      margin-top: 2px;
      font-weight: 900;
      color: #475569;
      font-size: 9.5px;
    }}

    tr {{ page-break-inside: avoid; }}
    .notes, .card, .sig {{ page-break-inside: avoid; }}
    """

    prescriber_card = f"""
      <div class="card">
        <div class="title">Prescriber Details</div>
        {_kv("Doctor", (f"Dr. {d_name}".strip() if _present(d_name) and not d_name.lower().startswith("dr") else d_name))}
        {_kv("Qualification", d_qual)}
        {_kv("Registration No", reg_disp)}
        {_kv("Department", d_dept)}
      </div>
    """

    clinical_card_parts = []
    if _present(diagnosis):
        clinical_card_parts.append(_kv("Diagnosis", diagnosis))
    if _present(vitals) and vitals != "—":
        clinical_card_parts.append(_kv("Vitals", vitals))
    if _present(allergies) and allergies != "—":
        clinical_card_parts.append(_kv("Drug Allergies", allergies))
    clinical_card = ""
    if clinical_card_parts:
        clinical_card = f"""
          <div class="card">
            <div class="title">Clinical Information</div>
            {''.join(clinical_card_parts)}
          </div>
        """

    header_html = render_brand_header_html(branding_obj)

    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <style>{css}</style>
      </head>
      <body>
        {header_html}

        {prescriber_card}

        <div style="height:8px;"></div>

        <div class="row3">
          {_field("Rx No", rx_no)}
          {_field("OP/IP", opip_val)}
          {_field("Date", rx_date, right=True)}
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
          {_field("Address", p_addr)}
          {_field("Weight", p_weight, right=True)}
        </div>

        {clinical_card}

        <div class="rxhead">
          <div class="rxmark">℞</div>
          <div>
            <div class="rxtext">Rx / Prescription</div>
            <div class="rxsub">Use as directed. Do not self-modify doses. Fresh prescription required for changes.</div>
          </div>
        </div>

        <div class="legend-line">
          After Food <span>[AF]</span> &nbsp; | &nbsp; Before Food <span>[BF]</span>
        </div>

        <div class="rule"></div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:26px;">S.No</th>
                <th>Medicine &amp; Instructions</th>
                <th style="width:34px;">AM</th>
                <th style="width:34px;">AF</th>
                <th style="width:34px;">PM</th>
                <th style="width:44px;">Night</th>
                <th style="width:40px;">Days</th>
                <th style="width:44px;">Qty</th>
              </tr>
            </thead>
            <tbody>
              {tr_html}
            </tbody>
          </table>
        </div>

        {f"<div class='notes'><div class='label'>Advice / Notes</div><div>{_esc(advice or notes)}</div><div style='height:6px;'></div><div class='kv'><span class='k'>Follow-up:</span><span class='v'>{_esc(follow_up_s)}</span></div></div>" if (advice or notes) or (_present(follow_up_s) and follow_up_s != "—") else ""}

        <div class="sig">
          <div class="stamp">Official Stamp / Seal</div>
          <div>
            <div class="sigbox">Doctor Signature</div>
            <div class="sigmeta">
              {(_esc((f"Dr. {d_name}".strip() if _present(d_name) and not d_name.lower().startswith("dr") else d_name)) if _present(d_name) else "—")}
              {(" | Reg: " + _esc(reg_disp)) if _present(reg_disp) else ""}
              {" | Date: " + _esc(rx_date) if _present(rx_date) else ""}
            </div>
          </div>
        </div>
      </body>
    </html>
    """.strip()

    return html


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------
def build_prescription_pdf(
    *,
    branding_obj: Any,
    rx: Any,
    patient: Any | None,
    doctor: Any | None,
) -> Tuple[bytes, str]:
    try:
        from weasyprint import HTML  # type: ignore

        html = _build_prescription_html(
            branding_obj=branding_obj,
            rx=rx,
            patient=patient,
            doctor=doctor,
        )
        pdf_bytes = HTML(string=html, base_url=str(settings.STORAGE_DIR)).write_pdf()
        return pdf_bytes, "application/pdf"
    except Exception:
        return _build_prescription_pdf_reportlab(
            branding_obj=branding_obj,
            rx=rx,
            patient=patient,
            doctor=doctor,
        )


# -------------------------------------------------------------------
# ReportLab fallback (updated NABH fields + qty + stamp)
# -------------------------------------------------------------------
def _build_prescription_pdf_reportlab(
    *,
    branding_obj: Any,
    rx: Any,
    patient: Any | None,
    doctor: Any | None,
) -> Tuple[bytes, str]:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    INK = colors.HexColor("#0f172a")
    MUTED = colors.HexColor("#475569")
    SOFT = colors.HexColor("#f8fafc")
    LINE = colors.HexColor("#e5e7eb")
    BAR = colors.HexColor("#0b1220")
    UNDER = colors.HexColor("#94a3b8")

    M = 14 * mm
    content_w = W - 2 * M

    # Branding values
    org_name = (_safe(_g(branding_obj, "org_name", "NUTRYAH HIMS")).strip() or "NUTRYAH HIMS")
    org_tagline = _safe(_g(branding_obj, "org_tagline", "")).strip()
    org_addr = _safe(_g(branding_obj, "org_address", "")).strip()
    org_phone = _safe(_g(branding_obj, "org_phone", "")).strip()
    org_email = _safe(_g(branding_obj, "org_email", "")).strip()
    org_web = _safe(_g(branding_obj, "org_website", "")).strip()
    logo_path = _safe(_g(branding_obj, "logo_path", "")).strip()

    rx_no = _safe(_g(rx, "rx_number", _g(rx, "rx_no", "—"))) or "—"
    rx_dt = _g(rx, "rx_datetime") or _g(rx, "created_at") or _g(rx, "date") or _g(rx, "prescribed_at")
    rx_date = _fmt_date(rx_dt)

    op_uid = _safe(_g(rx, "op_uid", "")).strip()
    ip_uid = _safe(_g(rx, "ip_uid", "")).strip()

    # Doctor fields
    d_name = (_safe(_first_present(doctor, ["full_name", "name"], "—")) or "—").strip()
    d_qual = _safe(_first_present(doctor, ["doctor_qualification", "qualification"], "")).strip()
    d_reg = _safe(_first_present(doctor, ["doctor_registration_no", "registration_no"], "")).strip()
    d_pathy = _safe(_first_present(doctor, ["doctor_registration_prefix", "pathy_prefix", "pathy"], "")).strip()
    reg_disp = ""
    if _present(d_reg) and _present(d_pathy):
        reg_disp = f"{d_pathy} {d_reg}".strip()
    elif _present(d_reg):
        reg_disp = d_reg

    p_name = " ".join([
        _safe(_g(patient, "prefix", "")).strip(),
        _safe(_g(patient, "first_name", "")).strip(),
        _safe(_g(patient, "last_name", "")).strip(),
    ]).strip() or (_safe(_g(patient, "full_name", "—")) or "—")

    p_uhid = _safe(_g(patient, "uhid", "—")) or "—"
    p_phone = _safe(_first_present(patient, ["phone", "mobile", "contact_no"], "—")) or "—"
    p_addr = _patient_address(patient)

    p_dob_raw = _g(patient, "dob", _g(patient, "date_of_birth", None))
    p_dob = _fmt_date(p_dob_raw)
    p_age_years = _age_years_from_dob(p_dob_raw)
    p_age = f"{p_age_years} Y" if p_age_years is not None else "—"
    p_gender = _safe(_g(patient, "gender", _g(patient, "sex", "—"))) or "—"

    p_wt_raw = _first_present(patient, ["weight_kg", "weight", "wt"], None)
    if not _present(p_wt_raw):
        p_wt_raw = _first_present(rx, ["weight_kg", "weight", "wt"], None)
    p_weight = _fmt_weight(p_wt_raw)

    diagnosis = _safe(_first_present(rx, ["diagnosis", "diagnosis_text", "problem", "chief_complaint"], "")).strip()
    allergies_raw = _first_present(patient, ["drug_allergies", "allergies", "allergy"], None)
    if not _present(allergies_raw):
        allergies_raw = _first_present(rx, ["drug_allergies", "allergies", "allergy"], None)
    allergies = _format_allergies(allergies_raw)
    vitals = _format_vitals(rx)

    notes = _safe(_g(rx, "notes", "")).strip()
    advice = _safe(_first_present(rx, ["advice", "precautions"], "")).strip()

    header_h = 25 * mm

    def draw_brand_header() -> float:
        y_top = H - M
        x = M

        # Logo left
        logo_w = 58 * mm
        logo_h = 18 * mm
        if logo_path:
            try:
                abs_path = Path(settings.STORAGE_DIR).joinpath(logo_path)
                if abs_path.exists() and abs_path.is_file():
                    img = ImageReader(str(abs_path))
                    c.drawImage(img, x, y_top - logo_h, width=logo_w, height=logo_h,
                                preserveAspectRatio=True, mask="auto")
            except Exception:
                pass

        # ORG block right aligned
        xr = W - M
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 13.5)
        c.drawRightString(xr, y_top - 4.8 * mm, org_name)

        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8.6)

        yy = y_top - 9.8 * mm
        if org_tagline:
            c.drawRightString(xr, yy, org_tagline)
            yy -= 3.9 * mm
        if org_addr:
            c.setFont("Helvetica", 8.4)
            c.drawRightString(xr, yy, _wrap(org_addr, "Helvetica", 8.4, 92 * mm)[0])
            yy -= 3.7 * mm

        contact_parts = [p for p in [org_email, org_phone] if p]
        if contact_parts:
            c.setFont("Helvetica", 8.4)
            c.drawRightString(xr, yy, " | ".join(contact_parts))
            yy -= 3.7 * mm

        if org_web:
            c.setFont("Helvetica", 8.4)
            c.drawRightString(xr, yy, org_web)

        return y_top - header_h

    def _draw_underlined_field(x: float, y: float, w: float, label: str, value: Any, *, right: bool = False) -> None:
        if not _present(value):
            return
        value_s = _safe(value).strip()
        if not _present(value_s):
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

    def draw_prescriber_block(y_base: float) -> float:
        y = y_base - 5.0 * mm
        c.setFillColor(colors.HexColor("#f8fafc"))
        c.setStrokeColor(LINE)
        c.roundRect(M, y - 18 * mm, content_w, 18 * mm, 6, stroke=1, fill=1)

        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(M + 4 * mm, y - 5.2 * mm, "Prescriber Details")

        c.setFont("Helvetica-Bold", 9.2)
        c.setFillColor(INK)
        doc_line = d_name if d_name.lower().startswith("dr") else f"Dr. {d_name}"
        c.drawString(M + 4 * mm, y - 10.5 * mm, doc_line)

        c.setFont("Helvetica", 8.6)
        c.setFillColor(MUTED)
        sub_parts = []
        if _present(d_qual):
            sub_parts.append(d_qual)
        if _present(reg_disp):
            sub_parts.append(f"Reg: {reg_disp}")
        if sub_parts:
            c.drawString(M + 4 * mm, y - 14.3 * mm, " | ".join(sub_parts))

        return y - 22 * mm

    def draw_patient_details_form(y_base: float) -> float:
        y = y_base - 2.5 * mm

        col_w = (content_w - 8 * mm) / 3.0
        gap = 4 * mm

        opip_val = ""
        if _present(op_uid) and _present(ip_uid):
            opip_val = f"{op_uid} / {ip_uid}"
        elif _present(op_uid):
            opip_val = op_uid
        elif _present(ip_uid):
            opip_val = ip_uid

        _draw_underlined_field(M, y, col_w, "Rx No", rx_no)
        _draw_underlined_field(M + col_w + gap, y, col_w, "OP/IP", opip_val)
        _draw_underlined_field(W - M - col_w, y, col_w, "Date", rx_date, right=True)

        y -= 7.0 * mm

        col2_w = (content_w - 6 * mm) / 2.0
        gap2 = 6 * mm
        _draw_underlined_field(M, y, col2_w, "Patient Name", p_name)
        _draw_underlined_field(M + col2_w + gap2, y, col2_w, "UHID", p_uhid)

        y -= 7.0 * mm

        age_sex = ""
        if _present(p_age) or _present(p_gender):
            a = p_age if _present(p_age) else ""
            g = p_gender if _present(p_gender) else ""
            age_sex = (f"{a} / {g}").strip(" /")

        _draw_underlined_field(M, y, col_w, "DOB", p_dob)
        _draw_underlined_field(M + col_w + gap, y, col_w, "Age/Sex", age_sex)
        _draw_underlined_field(W - M - col_w, y, col_w, "Mobile", p_phone, right=True)

        y -= 7.0 * mm
        _draw_underlined_field(M, y, content_w - col_w - gap, "Address", p_addr)
        _draw_underlined_field(W - M - col_w, y, col_w, "Weight", p_weight, right=True)

        # Clinical info box (optional)
        y -= 10.0 * mm
        clinical_lines = []
        if _present(diagnosis):
            clinical_lines.append(("Diagnosis", diagnosis))
        if _present(vitals) and vitals != "—":
            clinical_lines.append(("Vitals", vitals))
        if _present(allergies) and allergies != "—":
            clinical_lines.append(("Drug Allergies", allergies))

        if clinical_lines:
            box_h = 18 * mm
            c.setFillColor(SOFT)
            c.setStrokeColor(LINE)
            c.roundRect(M, y - box_h, content_w, box_h, 6, stroke=1, fill=1)

            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(M + 4 * mm, y - 5.2 * mm, "Clinical Information")

            c.setFont("Helvetica-Bold", 8.6)
            c.setFillColor(MUTED)
            yy = y - 9.2 * mm
            for k, v in clinical_lines[:3]:
                line = f"{k}: {v}"
                c.drawString(M + 4 * mm, yy, _wrap(line, "Helvetica", 8.6, content_w - 8 * mm)[0])
                yy -= 3.8 * mm

            y -= (box_h + 6 * mm)

        # Rx heading
        # c.setFillColor(BAR)
        # c.roundRect(M, y - 10 * mm, 14 * mm, 10 * mm, 4, stroke=0, fill=1)
        # c.setFillColor(colors.white)
        # c.setFont("Helvetica-Bold", 12)
        # c.drawCentredString(M + 7 * mm, y - 7.2 * mm, "℞")

        # c.setFillColor(INK)
        # c.setFont("Helvetica-Bold", 10)
        # c.drawString(M + 18 * mm, y - 6.8 * mm, "Rx / Prescription")

        # y -= 14.0 * mm
        # c.setFillColor(INK)
        # c.setFont("Helvetica-Bold", 9.0)
        # c.drawString(M, y, "After Food [AF]   |   Before Food [BF]")

        # y -= 5.5 * mm
        # c.setStrokeColor(colors.HexColor("#0f172a"))
        # c.setLineWidth(0.8)
        # c.line(M, y, W - M, y)

        return y - 1 * mm

    # Table columns (added Qty)
    cols = [
        ("S.No", 9 * mm),
        ("Medicine & Instruction", content_w - (9 + 11 + 11 + 11 + 14 + 14 + 14) * mm),
        ("AM", 11 * mm),
        ("AF", 11 * mm),
        ("PM", 11 * mm),
        ("Night", 14 * mm),
        ("Days", 14 * mm),
        ("Qty", 14 * mm),
    ]
    x_positions = [M]
    for _, wcol in cols:
        x_positions.append(x_positions[-1] + wcol)

    def draw_table_header(y_top: float) -> float:
        h = 8 * mm
        c.setFillColor(BAR)
        c.setStrokeColor(BAR)
        c.rect(M, y_top - h, content_w, h, stroke=1, fill=1)

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 8.6)

        xx = M
        for title, wcol in cols:
            c.drawCentredString(xx + wcol / 2, y_top - h + 2.4 * mm, title)
            xx += wcol

        c.setStrokeColor(colors.HexColor("#1f2937"))
        c.setLineWidth(1)
        xx = M
        for _, wcol in cols[:-1]:
            xx += wcol
            c.line(xx, y_top - h, xx, y_top)

        return y_top - h

    def new_page(*, include_table_header: bool) -> float:
        div_y = draw_brand_header()
        y = draw_prescriber_block(div_y)
        y = draw_patient_details_form(y)
        if include_table_header:
            y = draw_table_header(y)
        return y

    def draw_row(y_top: float, idx: int, drug: str, sub: str, am: int, af: int,
                 pm: int, night: int, days: str, qty: str, zebra: bool) -> float:
        med_w = cols[1][1] - 4 * mm
        drug_lines = _wrap(drug, "Helvetica-Bold", 9.2, med_w)[:2]
        sub_lines = _wrap(sub, "Helvetica", 8.0, med_w)[:2] if sub else []

        pad_t = 2.0 * mm
        pad_b = 2.0 * mm
        lh1 = 4.1 * mm
        lh2 = 3.6 * mm
        row_h = pad_t + len(drug_lines) * lh1 + (len(sub_lines) * lh2) + pad_b
        row_h = max(row_h, 9.5 * mm)

        if y_top - row_h < 34 * mm:
            c.showPage()
            y_top = new_page(include_table_header=True)

        fill_bg = colors.HexColor("#f8fafc") if zebra else colors.white
        c.setFillColor(fill_bg)
        c.setStrokeColor(LINE)
        c.setLineWidth(1)
        c.rect(M, y_top - row_h, content_w, row_h, stroke=1, fill=1)

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
        draw_center(7, qty)

        return y_top - row_h

    # Render
    row_y = new_page(include_table_header=True)

    lines = _g(rx, "lines", []) or []
    if not lines:
        row_h = 10 * mm
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.rect(M, row_y - row_h, content_w, row_h, stroke=1, fill=1)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 9)
        c.drawString(M + 3 * mm, row_y - row_h / 2 - 1.5 * mm, "No medicines")
        row_y -= row_h
    else:
        for i, ln in enumerate(lines, start=1):
            drug_raw = _safe(_g(ln, "item_name", _g(_g(ln, "item", None), "name", "—"))) or "—"
            drug = (drug_raw.strip() or "—").upper()

            strength = _safe(_first_present(ln, ["strength", "strength_text", "mg", "dose_strength"], "")).strip()
            form = _safe(_first_present(ln, ["dosage_form", "form", "form_text"], "")).strip()
            dose = _safe(_g(ln, "dose_text", "")).strip()
            route = _safe(_g(ln, "route", "")).strip()
            timing = _safe(_g(ln, "timing", "")).strip()
            inst = _safe(_g(ln, "instructions", "")).strip()
            if not timing and inst:
                timing = inst

            days = _safe(_g(ln, "duration_days", _g(ln, "days", "—"))) or "—"
            freq = _g(ln, "frequency_code", _g(ln, "frequency", _g(ln, "freq", None)))
            am, af, pm, night = freq_to_slots(freq)

            qty_raw = _first_present(ln, ["qty", "quantity", "total_quantity", "dispense_qty", "total_qty"], None)
            if not _present(qty_raw):
                est = _estimate_qty(days, freq)
                qty_raw = est if est is not None else "—"
            qty = str(qty_raw)

            sub_parts = []
            if _present(strength) or _present(form):
                sf = _join_nonempty([strength, form], sep=" • ")
                if _present(sf):
                    sub_parts.append(sf)
            for p in [dose, route, timing]:
                if _present(p):
                    sub_parts.append(p)
            sub = " • ".join(sub_parts)

            row_y = draw_row(row_y, i, drug, sub, am, af, pm, night, str(days), qty, zebra=(i % 2 == 0))

    # Notes / Advice
    note_text = advice or notes
    if _present(note_text):
        note_lines = _wrap(note_text, "Helvetica", 9.0, content_w - 8 * mm)
        note_h = 6 * mm + min(len(note_lines), 6) * 4.2 * mm + 10 * mm

        if row_y - note_h < 34 * mm:
            c.showPage()
            row_y = new_page(include_table_header=False)

        y_top = row_y - 6 * mm
        c.setStrokeColor(LINE)
        c.setFillColor(SOFT)
        c.roundRect(M, y_top - note_h, content_w, note_h, 6, stroke=1, fill=1)

        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(M + 4 * mm, y_top - 6 * mm, "Advice / Notes")

        c.setFont("Helvetica", 9)
        c.setFillColor(MUTED)
        yy = y_top - 11 * mm
        for ln in note_lines[:6]:
            c.drawString(M + 4 * mm, yy, ln)
            yy -= 4.2 * mm

        row_y = y_top - note_h

    # Stamp + Signature + Date
    sig_y = 22 * mm

    # Stamp box (left bottom)
    stamp_w = 70 * mm
    stamp_h = 18 * mm
    c.setStrokeColor(UNDER)
    c.setDash(3, 2)
    c.roundRect(M, sig_y - stamp_h, stamp_w, stamp_h, 6, stroke=1, fill=0)
    c.setDash()  # reset
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 8.6)
    c.drawString(M + 4 * mm, sig_y - 6.5 * mm, "Official Stamp / Seal")

    # Signature line (right)
    c.setStrokeColor(colors.HexColor("#111827"))
    c.setLineWidth(1)
    c.line(W - 80 * mm, sig_y, W - M, sig_y)

    c.setFont("Helvetica-Bold", 8.8)
    c.setFillColor(INK)
    c.drawRightString(W - M, sig_y - 4.2 * mm, "Doctor Signature")

    c.setFont("Helvetica", 8.2)
    c.setFillColor(MUTED)
    meta = []
    if _present(d_name):
        meta.append(d_name if d_name.lower().startswith("dr") else f"Dr. {d_name}")
    if _present(reg_disp):
        meta.append(f"Reg: {reg_disp}")
    meta.append(f"Date: {rx_date}")
    c.drawRightString(W - M, sig_y - 8.4 * mm, " | ".join(meta))

    c.save()
    return buf.getvalue(), "application/pdf"
