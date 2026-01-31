# FILE: app/services/pdfs/ipd_newborn_resuscitation.py
from __future__ import annotations

import io
from datetime import datetime, date
from typing import Any, Dict, Optional, List, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.utils import ImageReader


# ----------------------------
# Helpers
# ----------------------------
def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "YES" if v else "NO"
    return str(v)


def _safe_get(rec: Any, key: str, default: Any = "") -> Any:
    if rec is None:
        return default
    # SQLAlchemy model / object
    if hasattr(rec, key):
        return getattr(rec, key, default)
    # dict-like
    if isinstance(rec, dict):
        return rec.get(key, default)
    return default


def _fmt_date(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        v = v.date()
    if isinstance(v, date):
        return v.strftime("%d-%b-%Y")
    return _s(v)


def _fmt_dt(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d-%b-%Y %I:%M %p")
    return _s(v)


def _wrap(text: str, font: str, size: float, max_w: float) -> List[str]:
    t = (text or "").strip()
    if not t:
        return []
    words = t.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if stringWidth(test, font, size) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _rect_top(c: canvas.Canvas, x: float, y_top: float, w: float, h: float, stroke=1, fill=0):
    # y_top is top edge; reportlab rect uses bottom-left
    c.rect(x, y_top - h, w, h, stroke=stroke, fill=fill)


def _bar(c: canvas.Canvas, x: float, y_top: float, w: float, h: float, title: str):
    _rect_top(c, x, y_top, w, h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawCentredString(x + w / 2, y_top - (h * 0.72), title)


def _header(c: canvas.Canvas, hospital: Dict[str, Any], x: float, y_top: float, w: float, h: float):
    _rect_top(c, x, y_top, w, h, stroke=1, fill=0)

    name = hospital.get("name") or hospital.get("org_name") or "Hospital / Facility"
    tagline = hospital.get("tagline") or hospital.get("org_tagline") or ""
    addr = hospital.get("address") or hospital.get("org_address") or ""
    phone = hospital.get("phone") or hospital.get("org_phone") or ""
    website = hospital.get("website") or hospital.get("org_website") or ""
    logo_path = hospital.get("logo_path") or hospital.get("logo") or None

    # logo (optional)
    if logo_path:
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, x + 2 * mm, y_top - h + 2 * mm, 16 * mm, h - 4 * mm, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    cx = x + w / 2
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(cx, y_top - 6 * mm, name[:60])

    if tagline:
        c.setFont("Helvetica-Bold", 8.5)
        c.drawCentredString(cx, y_top - 10.5 * mm, tagline[:80])

    line1 = addr.strip()
    line2_parts = [p for p in [phone, website] if p]
    line2 = " | ".join(line2_parts)

    c.setFont("Helvetica", 8)
    if line1:
        c.drawCentredString(cx, y_top - 14.5 * mm, line1[:110])
    if line2:
        c.drawCentredString(cx, y_top - 18.5 * mm, line2[:110])


def _field(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    w: float,
    h: float,
    label: str,
    value: str,
    label_w: float = 50 * mm,
):
    _rect_top(c, x, y_top, w, h, stroke=1, fill=0)
    # split line
    if label_w and label_w < w:
        c.line(x + label_w, y_top, x + label_w, y_top - h)

    # label (supports \n)
    c.setFont("Helvetica-Bold", 8.5)
    lab_lines = (label or "").split("\n")
    for i, ln in enumerate(lab_lines[:3]):
        c.drawString(x + 2 * mm, y_top - (5 * mm + i * 3.7 * mm), ln)

    # value (wrap)
    vx = x + (label_w if label_w else 0) + 2 * mm
    vw = w - (label_w if label_w else 0) - 4 * mm
    c.setFont("Helvetica", 9)
    v_lines = _wrap(_s(value), "Helvetica", 9, vw)
    max_lines = max(1, int((h - 4 * mm) // (3.7 * mm)))
    for i, ln in enumerate(v_lines[:max_lines]):
        c.drawString(vx, y_top - (5 * mm + i * 3.7 * mm), ln)


def _tick(c: canvas.Canvas, x: float, y: float):
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y - 2.5 * mm, "✓")


def _fit_row_heights(rows: List[Tuple[str, str, float]], available_h: float, min_h: float = 8 * mm) -> List[Tuple[str, str, float]]:
    """Scale/trim row heights so total fits into available_h (keeps min_h)."""
    desired = sum(r[2] for r in rows)
    if desired <= available_h:
        return rows

    scale = available_h / desired
    scaled = [(a, b, max(min_h, rh * scale)) for (a, b, rh) in rows]

    # if still over due to min_h clamps, trim largest rows down to min_h until fits
    while sum(r[2] for r in scaled) > available_h + 0.5:  # tiny tolerance
        # find largest above min
        idx = None
        best = 0.0
        for i, r in enumerate(scaled):
            if r[2] > min_h and r[2] > best:
                best = r[2]
                idx = i
        if idx is None:
            break
        a, b, rh = scaled[idx]
        scaled[idx] = (a, b, max(min_h, rh - 1 * mm))

    return scaled


# ----------------------------
# PDF builder (2 pages form layout)
# ----------------------------
def build_pdf(rec: Any, hospital: Optional[Dict[str, Any]] = None) -> bytes:
    hospital = hospital or {}

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    page_w, page_h = A4
    margin = 10 * mm

    x0 = margin
    y0 = margin
    w = page_w - 2 * margin
    h = page_h - 2 * margin
    y_top = y0 + h

    # =========================
    # PAGE 1
    # =========================
    _rect_top(c, x0, y_top, w, h, stroke=1, fill=0)

    header_h = 22 * mm
    _header(c, hospital, x0, y_top, w, header_h)
    cur = y_top - header_h

    title_h = 8 * mm
    _bar(c, x0, cur, w, title_h, "NEWBORN RESUSCITATION CASE RECORD")
    cur -= title_h

    mother_bar_h = 7 * mm
    _bar(c, x0, cur, w, mother_bar_h, "MOTHER DETAILS")
    cur -= mother_bar_h

    mother_h = 112 * mm
    left_w = 110 * mm
    right_w = w - left_w

    _rect_top(c, x0, cur, w, mother_h, stroke=1, fill=0)
    c.line(x0 + left_w, cur, x0 + left_w, cur - mother_h)

    ly = cur
    row8 = 8 * mm
    row12 = 12 * mm

    _field(c, x0, ly, left_w, row8, "NAME", _s(_safe_get(rec, "mother_name", ""))); ly -= row8
    _field(c, x0, ly, left_w, row8, "AGE", _s(_safe_get(rec, "mother_age_years", ""))); ly -= row8
    _field(c, x0, ly, left_w, row8, "BLOOD GROUP", _s(_safe_get(rec, "mother_blood_group", ""))); ly -= row8

    gpla = f"G {_s(_safe_get(rec,'gravida',''))}   P {_s(_safe_get(rec,'para',''))}   L {_s(_safe_get(rec,'living',''))}   A {_s(_safe_get(rec,'abortion',''))}"
    _field(c, x0, ly, left_w, row8, "G P L A", gpla, label_w=26 * mm); ly -= row8

    lmp_edd = f"{_fmt_date(_safe_get(rec,'lmp_date', None))}    /    {_fmt_date(_safe_get(rec,'edd_date', None))}"
    _field(c, x0, ly, left_w, row8, "LMP / EDD", lmp_edd, label_w=28 * mm); ly -= row8

    hhh = f"HIV {_s(_safe_get(rec,'hiv_status',''))}   VDRL {_s(_safe_get(rec,'vdrl_status',''))}   HBsAg {_s(_safe_get(rec,'hbsag_status',''))}"
    _field(c, x0, ly, left_w, row8, "HIV/VDRL/HBsAg", hhh, label_w=38 * mm); ly -= row8

    _field(c, x0, ly, left_w, row8, "THYROID", _s(_safe_get(rec, "thyroid", ""))); ly -= row8
    _field(c, x0, ly, left_w, row8, "PIH", _s(_safe_get(rec, "pih", ""))); ly -= row8
    _field(c, x0, ly, left_w, row8, "GDM", _s(_safe_get(rec, "gdm", ""))); ly -= row8
    _field(c, x0, ly, left_w, row8, "FEVER", _s(_safe_get(rec, "fever", ""))); ly -= row8

    _field(c, x0, ly, left_w, row12, "OTHER ILLNESS", _s(_safe_get(rec, "other_illness", ""))); ly -= row12
    _field(c, x0, ly, left_w, row12, "DRUG INTAKE", _s(_safe_get(rec, "drug_intake", ""))); ly -= row12
    _field(c, x0, ly, left_w, row8, "ANTENATAL STEROID", _s(_safe_get(rec, "antenatal_steroid", "")), label_w=44 * mm); ly -= row8

    # Right mother
    ry = cur
    _field(c, x0 + left_w, ry, right_w, row8, "GESTATIONAL AGE", _s(_safe_get(rec, "gestational_age_weeks", "")), label_w=44 * mm); ry -= row8
    _field(c, x0 + left_w, ry, right_w, row8, "CONSANGUINITY", _s(_safe_get(rec, "consanguinity", "")), label_w=44 * mm); ry -= row8
    _field(c, x0 + left_w, ry, right_w, 20 * mm, "PREV SIBLING\nNEONATAL PERIOD", _s(_safe_get(rec, "prev_sibling_neonatal_period", "")), label_w=54 * mm); ry -= 20 * mm
    _field(c, x0 + left_w, ry, right_w, row8, "MODE OF CONCEPTION", _s(_safe_get(rec, "mode_of_conception", "")), label_w=50 * mm); ry -= row8
    _field(c, x0 + left_w, ry, right_w, row8, "FROM", _s(_safe_get(rec, "referred_from", "")), label_w=20 * mm); ry -= row8
    _field(c, x0 + left_w, ry, right_w, row8, "AMNIOTIC FLUID", _s(_safe_get(rec, "amniotic_fluid", "")), label_w=36 * mm); ry -= row8

    cur -= mother_h

    # Baby details
    baby_bar_h = 7 * mm
    _bar(c, x0, cur, w, baby_bar_h, "BABY DETAILS")
    cur -= baby_bar_h

    baby_h = 32 * mm
    _rect_top(c, x0, cur, w, baby_h, stroke=1, fill=0)
    mid = x0 + w * 0.55
    c.line(mid, cur, mid, cur - baby_h)

    row = 8 * mm
    by = cur

    dob = _fmt_date(_safe_get(rec, "date_of_birth", None))
    tob = _s(_safe_get(rec, "time_of_birth", ""))
    _field(c, x0, by, mid - x0, row, "DATE OF BIRTH", dob, label_w=40 * mm)
    _field(c, mid, by, x0 + w - mid, row, "TIME OF BIRTH", tob, label_w=40 * mm)
    by -= row

    _field(c, x0, by, mid - x0, row, "SEX", _s(_safe_get(rec, "sex", "")), label_w=20 * mm)
    _field(c, mid, by, x0 + w - mid, row, "BIRTH WEIGHT", _s(_safe_get(rec, "birth_weight_kg", "")), label_w=40 * mm)
    by -= row

    _field(c, x0, by, mid - x0, row, "MODE OF DELIVERY", _s(_safe_get(rec, "mode_of_delivery", "")), label_w=46 * mm)
    _field(c, mid, by, x0 + w - mid, row, "LENGTH", _s(_safe_get(rec, "length_cm", "")), label_w=24 * mm)
    by -= row

    cried = _safe_get(rec, "baby_cried_at_birth", None)
    cried_text = "YES" if cried is True else ("NO" if cried is False else "")
    _field(c, x0, by, mid - x0, row, "BABY CRIED AT BIRTH", cried_text, label_w=58 * mm)
    _field(c, mid, by, x0 + w - mid, row, "H.C", _s(_safe_get(rec, "head_circum_cm", "")), label_w=14 * mm)

    cur -= baby_h

    # APGAR
    apgar_h = 10 * mm
    _rect_top(c, x0, cur, w, apgar_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x0 + 2 * mm, cur - 7 * mm, "APGAR")

    ax = x0 + 30 * mm
    col_w = (w - 30 * mm) / 3
    for i, lab in enumerate(["1'", "5'", "10'"]):
        cx = ax + i * col_w
        _rect_top(c, cx, cur, col_w, apgar_h, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(cx + col_w / 2, cur - 4 * mm, lab)

    a1 = _s(_safe_get(rec, "apgar_1_min", ""))
    a5 = _s(_safe_get(rec, "apgar_5_min", ""))
    a10 = _s(_safe_get(rec, "apgar_10_min", ""))
    c.setFont("Helvetica", 10)
    c.drawCentredString(ax + col_w / 2, cur - 8.5 * mm, a1)
    c.drawCentredString(ax + col_w * 1.5, cur - 8.5 * mm, a5)
    c.drawCentredString(ax + col_w * 2.5, cur - 8.5 * mm, a10)

    cur -= apgar_h

    # Resuscitation details - Two row layout (expanded to use more space)
    res_h = 80 * mm  # Further increased height
    _rect_top(c, x0, cur, w, res_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x0 + 2 * mm, cur - 4.5 * mm, "RESUSCITATION DETAILS:")

    # First row - Free text notes (expanded)
    notes_h = 38 * mm  # Space for notes
    c.line(x0, cur - 8 * mm, x0 + w, cur - 8 * mm)  # Horizontal line after title
    c.line(x0, cur - 8 * mm - notes_h, x0 + w, cur - 8 * mm - notes_h)  # Horizontal divider
    
    notes = _s(_safe_get(rec, "resuscitation_notes", ""))
    
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x0 + 2 * mm, cur - 12 * mm, "RESUSCITATION NOTES:")
    
    if notes:
        lines = _wrap(notes, "Helvetica", 8, w - 4 * mm)
        c.setFont("Helvetica", 8)
        yy = cur - 16 * mm
        for line in lines[:9]:  # Allow up to 9 lines
            c.drawString(x0 + 2 * mm, yy, line)
            yy -= 3.2 * mm

    # Second row - Structured data (expanded)
    res = _safe_get(rec, "resuscitation", {}) or {}
    summary_parts = []
    toggle_parts = []
    
    if isinstance(res, dict):
        for k, v in res.items():
            if v is None or v == "":
                continue
            elif v is True:
                toggle_parts.append(f"{k}: YES")
            elif v is False:
                toggle_parts.append(f"{k}: NO")
            else:
                summary_parts.append(f"{k}={v}")
    
    # Combine regular data and toggle data
    all_parts = []
    if summary_parts:
        all_parts.extend(summary_parts)
    if toggle_parts:
        all_parts.extend(toggle_parts)
    
    summary = ", ".join(all_parts)
    
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x0 + 2 * mm, cur - 50 * mm, "STRUCTURED DATA:")
    
    if summary:
        lines = _wrap(summary, "Helvetica", 8, w - 4 * mm)
        c.setFont("Helvetica", 8)
        yy = cur - 54 * mm
        for line in lines[:8]:  # Allow up to 8 lines for structured data
            c.drawString(x0 + 2 * mm, yy, line)
            yy -= 3.2 * mm

    c.showPage()

    # =========================
    # PAGE 2  ✅ FIXED LAYOUT
    # =========================
    _rect_top(c, x0, y_top, w, h, stroke=1, fill=0)
    _header(c, hospital, x0, y_top, w, header_h)
    cur = y_top - header_h

    exam_h = 7 * mm
    _bar(c, x0, cur, w, exam_h, "EXAMINATION")
    cur -= exam_h

    # HR/RR/CFT/SaO2/Sugar strip
    strip_h = 10 * mm
    _rect_top(c, x0, cur, w, strip_h, stroke=1, fill=0)
    labels = [("HR", "hr"), ("RR", "rr"), ("CFT", "cft_seconds"), ("SaO2", "sao2"), ("SUGAR", "sugar_mgdl")]
    colw = w / len(labels)
    for i, (lab, key) in enumerate(labels):
        cx = x0 + i * colw
        _rect_top(c, cx, cur, colw, strip_h, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(cx + colw / 2, cur - 4 * mm, lab)
        c.setFont("Helvetica", 10)
        c.drawCentredString(cx + colw / 2, cur - 8.3 * mm, _s(_safe_get(rec, key, "")))
    cur -= strip_h

    # Head to foot
    head_bar = 7 * mm
    _bar(c, x0, cur, w, head_bar, "HEAD TO FOOT")
    cur -= head_bar

    # Left panel + vaccination (right)
    left = x0
    vac_w = 62 * mm
    gap = 4 * mm
    left_w2 = w - vac_w - gap

    # ✅ slightly taller & CNS auto-fit table so it never overflows
    panel_h = 100 * mm

    _rect_top(c, left, cur, left_w2, panel_h, stroke=1, fill=0)
    _rect_top(c, left + left_w2 + gap, cur, vac_w, panel_h, stroke=1, fill=0)

    py = cur

    # ✅ reduce a bit to give more CNS room
    cvs_h = 16 * mm
    rs_h = 16 * mm
    pa_h = 16 * mm
    row_h = 10 * mm

    _field(c, left, py, left_w2, cvs_h, "CVS", _s(_safe_get(rec, "cvs", "")), label_w=18 * mm); py -= cvs_h
    _field(c, left, py, left_w2, rs_h, "RS", _s(_safe_get(rec, "rs", "")), label_w=18 * mm); py -= rs_h

    _rect_top(c, left, py, left_w2, row_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(
        left + 2 * mm,
        py - 7 * mm,
        f"ICR: {_s(_safe_get(rec,'icr',''))}    "
        f"SCR: {_s(_safe_get(rec,'scr',''))}    "
        f"GRUNTING: {_s(_safe_get(rec,'grunting',''))}    "
        f"APNEA: {_s(_safe_get(rec,'apnea',''))}    "
        f"DOWNE'S SCORE: {_s(_safe_get(rec,'downes_score',''))}"
    )
    py -= row_h

    _field(c, left, py, left_w2, pa_h, "P/A", _s(_safe_get(rec, "pa", "")), label_w=18 * mm); py -= pa_h

    used_h = cvs_h + rs_h + row_h + pa_h
    cns_h = panel_h - used_h

    _rect_top(c, left, py, left_w2, cns_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(left + 2 * mm, py - 5 * mm, "CNS:")

    items = [
        ("CRY", _s(_safe_get(rec, "cns_cry", ""))),
        ("ACTIVITY", _s(_safe_get(rec, "cns_activity", ""))),
        ("AF", _s(_safe_get(rec, "cns_af", ""))),
        ("REFLEXES", _s(_safe_get(rec, "cns_reflexes", ""))),
        ("TONE", _s(_safe_get(rec, "cns_tone", ""))),
    ]

    # ✅ row height auto-fit INSIDE CNS box (prevents overlap into next section)
    top_pad = 10 * mm
    table_max_h = max(18 * mm, cns_h - top_pad - 2 * mm)
    t_row = table_max_h / len(items)
    t_row = min(t_row, 7.2 * mm)  # looks like your form

    table_h = t_row * len(items)

    t_x = left + 18 * mm
    t_w = 92 * mm
    t_y = py - top_pad

    _rect_top(c, t_x, t_y, t_w, table_h, stroke=1, fill=0)

    for i in range(1, len(items)):
        yline = t_y - i * t_row
        c.line(t_x, yline, t_x + t_w, yline)

    split = t_x + 40 * mm
    c.line(split, t_y, split, t_y - table_h)

    for i, (k, v) in enumerate(items):
        yy = t_y - (i + 0.72) * t_row
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(t_x + 2 * mm, yy, k)
        c.setFont("Helvetica", 9)
        c.drawString(split + 2 * mm, yy, (v or "")[:40])

    # Vaccination panel
    vx = left + left_w2 + gap
    c.setFont("Helvetica-Bold", 10)
    c.drawString(vx + 10 * mm, cur - 12 * mm, "VACCINATION :")

    vacc = _safe_get(rec, "vaccination", {}) or {}

    def _vacc_given(name: str) -> bool:
        if not isinstance(vacc, dict):
            return False
        item = vacc.get(name) or {}
        if isinstance(item, dict):
            return bool(item.get("given"))
        return False

    start_y = cur - 24 * mm
    for i, name in enumerate(["BCG", "OPV", "HepB"]):
        yy = start_y - i * 16 * mm
        c.setFont("Helvetica-Bold", 11)
        c.drawString(vx + 18 * mm, yy, name)
        if _vacc_given(name):
            _tick(c, vx + 40 * mm, yy)

    cur -= panel_h

    # ✅ Reserve footer space so Vitamin-K never overlaps danger line
    footer_h = 10 * mm
    footer_top = y0 + footer_h  # top edge of footer box
    available_for_lower = cur - footer_top

    lower_rows = [
        ("MUSCULO SKELETAL", "musculoskeletal", 12 * mm),
        ("SPINE & CRANIUM", "spine_cranium", 12 * mm),
        ("GENITALIA", "genitalia", 12 * mm),
        ("DIAGNOSIS", "diagnosis", 12 * mm),
        ("TREATMENT", "treatment", 14 * mm),
        ("O2", "oxygen", 10 * mm),
        ("WARMTH", "warmth", 10 * mm),
        ("FEED INITIATION", "feed_initiation", 12 * mm),
        ("INJ. VITAMIN K 1MG IM STAT", "vitamin_k_at", 12 * mm),
        ("OTHERS", "others", 12 * mm),
        ("VITALS MONITOR", "vitals_monitor", 12 * mm),
    ]

    # ✅ auto-fit lower section height to available space
    lower_rows = _fit_row_heights(lower_rows, available_for_lower, min_h=8 * mm)

    for label, key, rh in lower_rows:
        if key == "vitamin_k_at":
            vk_given = _safe_get(rec, "vitamin_k_given", None)
            vk_at = _safe_get(rec, "vitamin_k_at", None)
            val = ""
            if vk_given is True:
                val += "YES"
            elif vk_given is False:
                val += "NO"
            if vk_at:
                val = (val + "  " + _fmt_dt(vk_at)).strip()
        else:
            val = _s(_safe_get(rec, key, ""))

        _field(c, x0, cur, w, rh, label, val, label_w=58 * mm)
        cur -= rh

    # Footer danger signs (inside its own box)
    _rect_top(c, x0, footer_top, w, footer_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(colors.black)
    c.drawString(x0 + 2 * mm, footer_top - 7 * mm, "REPORT IF ANY DANGER SIGN : POOR FEEDING / CYANOSIS / BREATHING DIFFICULTY")

    c.showPage()
    c.save()
    return buf.getvalue()
