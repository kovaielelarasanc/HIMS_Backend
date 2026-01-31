# FILE: app/services/emr_export_pdf.py
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, A5, landscape, portrait
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from app.models.emr_all import EmrTemplateVersion, EmrRecord


# ============================================================
# Utils
# ============================================================
def _s(x: Any) -> str:
    return "" if x is None else str(x)


def _dt(x: Any) -> str:
    try:
        if not x:
            return ""
        if isinstance(x, str):
            return x
        return x.strftime("%d-%b-%Y %I:%M %p")
    except Exception:
        return _s(x)


def _date(x: Any) -> str:
    try:
        if not x:
            return ""
        if isinstance(x, str):
            return x
        return x.strftime("%d-%b-%Y")
    except Exception:
        return _s(x)


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _bool_label(v: Any) -> str:
    if v is True or v in ("true", "True", "1", 1):
        return "Yes"
    if v is False or v in ("false", "False", "0", 0):
        return "No"
    if v is None:
        return ""
    return _s(v)


def _clean_title(title: str) -> str:
    t = _strip_html(_s(title)).strip()
    if not t:
        return "Clinical Document"
    t = re.sub(r"^\s*record\s+export\s*[-–—]\s*", "", t, flags=re.IGNORECASE)
    if " · " in t:
        t = t.split(" · ", 1)[0].strip()
    return t[:120] or "Clinical Document"


def _safe_str(v: Any) -> str:
    """Return clean string (no 'None')."""
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() == "none":
        return ""
    return s


def _join_name_parts(*parts: Any) -> str:
    clean = []
    for p in parts:
        s = _safe_str(p)
        if s:
            clean.append(s)
    return " ".join(clean).strip()


# ============================================================
# Pagesize
# ============================================================
def _pagesize(paper: str = "A4", orientation: str = "portrait"):
    paper = (paper or "A4").upper().strip()
    orientation = (orientation or "portrait").lower().strip()
    base = A4
    if paper == "A3":
        base = A3
    elif paper == "A5":
        base = A5
    return landscape(base) if orientation == "landscape" else portrait(base)


# ============================================================
# Theme
# ============================================================
@dataclass
class PdfTheme:
    margin_mm: float = 14.0
    footer_h_mm: float = 10.0
    header_h_mm_default: float = 26.0
    demo_pad_mm: float = 3.0

    font: str = "Helvetica"
    font_bold: str = "Helvetica-Bold"

    title_size: int = 12
    org_size: int = 12
    small_size: int = 8
    body_size: int = 9
    label_size: int = 8

    # softer borders for a more medical-grade look
    border: Any = colors.Color(0.83, 0.83, 0.86)
    muted: Any = colors.Color(0.35, 0.35, 0.38)
    text: Any = colors.black
    section_bg: Any = colors.Color(0.965, 0.965, 0.975)
    kv_bg: Any = colors.Color(0.985, 0.985, 0.99)


# ============================================================
# ✅ FIXED NumberedCanvas (correct totals, no extra blank page)
# ============================================================
class NumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas with safe save():
    - showPage() stores state + starts next page
    - save() replays stored pages and writes PDF WITHOUT calling self.showPage()
      (because reportlab Canvas.save() calls self.showPage() if _code exists)
    """

    def __init__(self, *args, footer_cb=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states: List[Dict[str, Any]] = []
        self._footer_cb = footer_cb

    def showPage(self):
        # store state of current page
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        # store last page too (if it has content) OR ensure at least one page
        if len(self._code) or not self._saved_page_states:
            self._saved_page_states.append(dict(self.__dict__))

        total_pages = max(1, len(self._saved_page_states))

        for state in self._saved_page_states:
            self.__dict__.update(state)
            if callable(self._footer_cb):
                self._footer_cb(self, self._pageNumber, total_pages)
            canvas.Canvas.showPage(self)  # base showPage (finalize)

        # IMPORTANT: write without calling canvas.Canvas.save(self),
        # because base save() would call self.showPage() again.
        self._code = []
        self._doc.SaveToFile(self._filename, self)


# ============================================================
# Paths / images
# ============================================================
def _project_root() -> Path:
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path(os.getcwd()).resolve()

def _get_storage_dir() -> Path:
    """
    Where uploaded files live on disk.
    Priority:
      1) env STORAGE_DIR / UPLOAD_DIR / MEDIA_ROOT
      2) ./storage (cwd)
    """
    for k in ("STORAGE_DIR", "UPLOAD_DIR", "MEDIA_ROOT"):
        v = (os.getenv(k) or "").strip()
        if v:
            return Path(os.path.abspath(v))
    return Path(os.path.abspath(os.path.join(os.getcwd(), "storage")))


def _try_resolve_path(p: str) -> Optional[str]:
    v = (p or "").strip()
    if not v:
        return None

    # normalize + prevent traversal
    v = v.replace("\\", "/").lstrip("/")
    v = v.replace("..", "")
    if not v:
        return None

    # absolute path
    if os.path.isabs(v) and os.path.exists(v):
        return v

    root = _project_root()
    storage_dir = Path(os.getenv("STORAGE_DIR") or os.getenv("UPLOAD_DIR") or os.getenv("MEDIA_ROOT") or (root / "storage")).resolve()
    fname = Path(v).name

    candidates = [
        # existing patterns
        root / v,
        root / "app" / v,
        root / "app" / "static" / v,
        root / "app" / "static" / "uploads" / fname,
        root / "static" / v,
        root / "static" / "uploads" / fname,

        # ✅ NEW: storage-aware patterns (this fixes "branding/logo.png")
        storage_dir / v,                         # storage/<v>
        storage_dir / "branding" / fname,        # storage/branding/<file>
        storage_dir / "uploads" / fname,         # storage/uploads/<file>
        root / "storage" / v,                    # <root>/storage/<v>
        root / "storage" / "branding" / fname,   # <root>/storage/branding/<file>
    ]

    # keep compatibility if DB stores "static/.."
    if v.startswith("static/"):
        candidates.insert(0, root / "app" / v)

    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return str(c)
        except Exception:
            continue
    return None






def _image_from_value(value: Any) -> Optional[ImageReader]:
    """
    Supports:
    - filesystem path
    - data:image/...;base64,...
    - dict with {"data_url": "..."} or {"path": "..."} or {"url": "..."}
    """
    if not value:
        return None

    if isinstance(value, dict):
        value = value.get("data_url") or value.get("path") or value.get("url") or ""

    if isinstance(value, (bytes, bytearray)):
        try:
            return ImageReader(BytesIO(value))
        except Exception:
            return None

    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None

        if v.startswith("data:image/") and "base64," in v:
            try:
                b64 = v.split("base64,", 1)[1].strip()
                raw = base64.b64decode(b64)
                return ImageReader(BytesIO(raw))
            except Exception:
                return None

        path = _try_resolve_path(v)
        if path:
            try:
                return ImageReader(path)
            except Exception:
                return None

    return None


# ============================================================
# JSON helpers
# ============================================================
def _as_json_obj(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except Exception:
            return None
    return None


def _schema_from_template_version(v: Any) -> Optional[Dict[str, Any]]:
    for attr in (
        "schema_json",
        "normalized_schema_json",
        "template_json",
        "json_schema",
        "definition_json",
        "sections_json",
    ):
        raw = getattr(v, attr, None)
        obj = _as_json_obj(raw)
        if obj is None:
            continue

        if isinstance(obj, dict) and isinstance(obj.get("sections"), list):
            return obj

        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            if "items" in obj[0] or "label" in obj[0]:
                return {"schema_version": 1, "sections": obj}

    return None


def _extract_record_payload(r: Any) -> Dict[str, Any]:
    for attr in (
        "record_json",
        "payload_json",
        "data_json",
        "values_json",
        "content_json",
        "form_data_json",
        "payload",
        "data",
        "values",
        "content",
        "form_data",
    ):
        raw = getattr(r, attr, None)
        obj = _as_json_obj(raw)
        if isinstance(obj, dict) and obj:
            return obj
    return {}


def _extract_schema_and_data(
    *,
    record_payload: Dict[str, Any],
    template_version_schema: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    schema = None

    tpl = record_payload.get("template")
    if isinstance(tpl, dict) and isinstance(tpl.get("sections"), list):
        schema = tpl
    elif template_version_schema and isinstance(template_version_schema.get("sections"), list):
        schema = template_version_schema

    data = record_payload.get("data") if isinstance(record_payload.get("data"), dict) else None
    if not isinstance(data, dict):
        data = record_payload if isinstance(record_payload, dict) else {}

    return schema, data


# ============================================================
# Options mapping
# ============================================================
def _option_label_from_options(options: Any, value: Any) -> str:
    v = _s(value)
    if isinstance(options, list):
        for o in options:
            if isinstance(o, dict) and _s(o.get("value")) == v:
                return _s(o.get("label")) or v
    return v


def _option_label(field: Dict[str, Any], value: Any) -> str:
    options = field.get("options") or (field.get("choice") or {}).get("options") or []
    return _option_label_from_options(options, value)


def _chips_label(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join([_strip_html(_s(x)) for x in value if _s(x).strip()])
    return _strip_html(_s(value))


# ============================================================
# Visible rules
# ============================================================
def _get_value_in_scopes(scopes: List[Dict[str, Any]], key: str) -> Any:
    for sc in scopes:
        if isinstance(sc, dict) and key in sc:
            return sc.get(key)
    return None


def _visible_when(item: Dict[str, Any], scopes: List[Dict[str, Any]]) -> bool:
    rules = item.get("rules") or {}
    vw = rules.get("visible_when")
    if not vw:
        return True

    op = (_s(vw.get("op")) or "eq").lower().strip()
    field_key = vw.get("field_key")
    expected = vw.get("value")
    if not field_key:
        return True
    actual = _get_value_in_scopes(scopes, field_key)

    if op == "eq":
        return _s(actual) == _s(expected)
    if op == "ne":
        return _s(actual) != _s(expected)
    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    if op == "in":
        if isinstance(expected, list):
            return _s(actual) in [_s(x) for x in expected]
        return False
    if op == "not_in":
        if isinstance(expected, list):
            return _s(actual) not in [_s(x) for x in expected]
        return True

    return True


# ============================================================
# Drawing helpers
# ============================================================
def _draw_box(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fill=None,
    stroke=colors.lightgrey,
    lw=0.55,
):
    c.saveState()
    if fill is not None:
        c.setFillColor(fill)
        c.rect(x, y, w, h, fill=1, stroke=0)
    c.setStrokeColor(stroke)
    c.setLineWidth(lw)
    c.rect(x, y, w, h, fill=0, stroke=1)
    c.restoreState()


def _wrap(text: str, font: str, size: int, max_w: float, max_lines: Optional[int] = None) -> List[str]:
    t = _strip_html(_s(text))
    if not t:
        return [""]
    out: List[str] = []
    for line in t.split("\n"):
        line = line.rstrip()
        if not line.strip():
            out.append("")
            continue
        segs = simpleSplit(line, font, size, max_w)
        out.extend(segs if segs else [""])
    if max_lines is not None:
        return out[:max_lines]
    return out


def _draw_watermark(c: canvas.Canvas, text: str):
    if not text:
        return
    c.saveState()
    c.setFillColor(colors.Color(0.88, 0.88, 0.88))
    c.setFont("Helvetica-Bold", 44)
    # centered-ish; acceptable for A4/A5/A3
    W, H = c._pagesize
    c.translate(W * 0.35, H * 0.55)
    c.rotate(30)
    c.drawCentredString(0, 0, text)
    c.restoreState()


def _draw_section_header(c: canvas.Canvas, *, x: float, top_y: float, w: float, text: str, theme: PdfTheme) -> float:
    h = 8.2 * mm
    _draw_box(c, x, top_y - h, w, h, fill=theme.section_bg, stroke=theme.border, lw=0.55)
    c.setFont(theme.font_bold, 10)
    c.setFillColor(theme.text)
    c.drawString(x + 2.6 * mm, top_y - 5.8 * mm, _s(text)[:200])
    return h


def _measure_field_box_h(
    *,
    label: str,
    value: str,
    w: float,
    theme: PdfTheme,
    min_h: float = 10.0 * mm,
) -> float:
    pad_x = 2.6 * mm
    pad_y = 2.0 * mm
    label_lines = _wrap(label, theme.font_bold, theme.label_size, w - pad_x * 2, max_lines=2)
    value_lines = _wrap(value, theme.font, theme.body_size, w - pad_x * 2, max_lines=None)
    # protect against extremely huge accidental payloads
    if len(value_lines) > 600:
        value_lines = value_lines[:600] + ["…(truncated)"]

    h = pad_y + (len(label_lines) * 3.6 * mm) + 1.2 * mm + (len(value_lines) * 4.2 * mm) + pad_y
    return max(h, min_h)


def _draw_field_box(
    c: canvas.Canvas,
    *,
    x: float,
    top_y: float,
    w: float,
    label: str,
    value: str,
    theme: PdfTheme,
    min_h: float = 10.0 * mm,
) -> float:
    pad_x = 2.6 * mm
    pad_y = 2.0 * mm

    label_lines = _wrap(label, theme.font_bold, theme.label_size, w - pad_x * 2, max_lines=2)
    value_lines = _wrap(value, theme.font, theme.body_size, w - pad_x * 2, max_lines=None)
    if len(value_lines) > 600:
        value_lines = value_lines[:600] + ["…(truncated)"]

    h = pad_y + (len(label_lines) * 3.6 * mm) + 1.2 * mm + (len(value_lines) * 4.2 * mm) + pad_y
    h = max(h, min_h)

    _draw_box(c, x, top_y - h, w, h, fill=None, stroke=theme.border, lw=0.50)

    c.saveState()
    yy = top_y - pad_y - theme.label_size * 0.35

    c.setFillColor(theme.muted)
    c.setFont(theme.font_bold, theme.label_size)
    for ln in label_lines:
        c.drawString(x + pad_x, yy, ln[:240])
        yy -= 3.6 * mm

    yy -= 0.6 * mm
    c.setFillColor(theme.text)
    c.setFont(theme.font, theme.body_size)
    for ln in value_lines:
        if ln is None:
            continue
        c.drawString(x + pad_x, yy, ln[:340])
        yy -= 4.2 * mm

    c.restoreState()
    return h


def _draw_image_box(
    c: canvas.Canvas,
    *,
    x: float,
    top_y: float,
    w: float,
    label: str,
    value: Any,
    theme: PdfTheme,
    box_h: float = 30.0 * mm,
) -> float:
    h = box_h
    _draw_box(c, x, top_y - h, w, h, fill=None, stroke=theme.border, lw=0.50)

    pad_x = 2.6 * mm
    c.saveState()

    c.setFillColor(theme.muted)
    c.setFont(theme.font_bold, theme.label_size)
    c.drawString(x + pad_x, top_y - 4.0 * mm, _s(label)[:200])

    img = _image_from_value(value)
    if img:
        try:
            iw, ih = img.getSize()
            max_w = w - pad_x * 2
            max_h = h - 8.0 * mm
            draw_w = max_w
            draw_h = (draw_w * float(ih) / float(iw)) if iw else max_h
            if draw_h > max_h:
                draw_h = max_h
                draw_w = (draw_h * float(iw) / float(ih)) if ih else max_w
            ix = x + (w - draw_w) / 2.0
            iy = (top_y - h) + 2.0 * mm
            c.drawImage(img, ix, iy, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")
        except Exception:
            c.setFillColor(theme.text)
            c.setFont(theme.font, theme.body_size)
            c.drawString(x + pad_x, top_y - 11 * mm, "[Image could not be rendered]")
    else:
        c.setFillColor(theme.text)
        c.setFont(theme.font, theme.body_size)
        c.drawString(x + pad_x, top_y - 11 * mm, "[No image]")

    c.restoreState()
    return h


# ============================================================
# Table rendering (✅ FIX: real height + pagination support)
# ============================================================
def _table_cell_text(col_def: Dict[str, Any], vv: Any) -> str:
    if vv is None:
        return ""
    ctype = (_s(col_def.get("type")) or "").lower().strip()
    if ctype == "select":
        return _option_label_from_options(col_def.get("options") or [], vv)
    if isinstance(vv, bool):
        return _bool_label(vv)
    return _strip_html(_s(vv))


def _extract_table_rows(rows_value: Any) -> List[Dict[str, Any]]:
    if isinstance(rows_value, list):
        return [r for r in rows_value if isinstance(r, dict)]
    if isinstance(rows_value, dict):
        rv = rows_value.get("rows")
        if isinstance(rv, list):
            return [r for r in rv if isinstance(r, dict)]
    return []


def _draw_table_paged(
    c: canvas.Canvas,
    *,
    x: float,
    top_y: float,
    w: float,
    field: Dict[str, Any],
    rows_value: Any,
    theme: PdfTheme,
    content_bottom_y: float,
    new_page_cb,  # returns new content_top_y
) -> float:
    """
    Draws a table and automatically page-breaks if needed.
    Returns the new y after drawing.
    """
    label = _s(field.get("label") or "")
    cols = (field.get("table") or {}).get("columns") or []
    if not isinstance(cols, list) or not cols:
        cols = [{"key": "value", "label": "Value"}]

    rows = _extract_table_rows(rows_value)

    pad_x = 2.6 * mm
    title_h = 6.0 * mm
    header_h = 7.0 * mm
    row_h_min = 6.0 * mm
    col_pad = 2.0 * mm
    body_line_h = 4.2 * mm

    ncol = max(1, len(cols))
    col_w = w / ncol

    def row_height(row: Dict[str, Any]) -> float:
        max_lines = 1
        for col in cols:
            key = col.get("key")
            txt = _table_cell_text(col, row.get(key))
            lines = _wrap(txt, theme.font, theme.body_size, col_w - (col_pad * 2), max_lines=6)
            max_lines = max(max_lines, len(lines))
        return max(row_h_min, (max_lines * body_line_h) + 1.8 * mm)

    # If empty, create one empty row so the table box looks valid
    if not rows:
        rows = [{}]

    idx = 0
    while idx < len(rows):
        # Ensure minimum space for title+header+1row
        min_need = title_h + header_h + row_h_min + 2.0 * mm
        if top_y - min_need < content_bottom_y:
            top_y = float(new_page_cb() or top_y)

        avail = top_y - content_bottom_y
        chunk_rows: List[Dict[str, Any]] = []
        chunk_heights: List[float] = []

        used = title_h + header_h + 2.0 * mm
        while idx < len(rows):
            rh = row_height(rows[idx])
            if used + rh > avail and chunk_rows:
                break
            if used + rh > avail and not chunk_rows:
                # even one row doesn't fit: new page and retry
                top_y = float(new_page_cb() or top_y)
                avail = top_y - content_bottom_y
                used = title_h + header_h + 2.0 * mm
                continue
            chunk_rows.append(rows[idx])
            chunk_heights.append(rh)
            used += rh
            idx += 1

        chunk_h = used
        _draw_box(c, x, top_y - chunk_h, w, chunk_h, fill=None, stroke=theme.border, lw=0.50)

        c.saveState()

        # title
        c.setFont(theme.font_bold, theme.label_size)
        c.setFillColor(theme.muted)
        c.drawString(x + pad_x, top_y - 4.0 * mm, label[:200])

        # header background
        c.setFillColor(theme.section_bg)
        c.rect(x, top_y - title_h - header_h, w, header_h, fill=1, stroke=0)

        c.setStrokeColor(theme.border)
        c.setLineWidth(0.45)

        # line under title
        c.line(x, top_y - title_h, x + w, top_y - title_h)

        # header text + verticals
        c.setFont(theme.font_bold, theme.label_size)
        c.setFillColor(theme.text)

        for j, col in enumerate(cols):
            cx = x + j * col_w
            txt = _s(col.get("label") or col.get("key") or "")[:60]
            c.drawString(cx + col_pad, top_y - title_h - 4.8 * mm, txt)
            if j > 0:
                c.line(cx, top_y - chunk_h, cx, top_y - title_h)

        # rows
        c.setFont(theme.font, theme.body_size)
        c.setFillColor(theme.text)

        cur_y = top_y - title_h - header_h

        for r_i, row in enumerate(chunk_rows):
            rh = chunk_heights[r_i]
            cur_y -= rh
            # row line
            c.line(x, cur_y, x + w, cur_y)

            for j, col in enumerate(cols):
                cx = x + j * col_w
                key = col.get("key")
                txt = _table_cell_text(col, row.get(key))
                lines = _wrap(txt, theme.font, theme.body_size, col_w - (col_pad * 2), max_lines=6)
                ty = cur_y + rh - 4.5 * mm
                for ln in lines:
                    if ln.strip():
                        c.drawString(cx + col_pad, ty, ln[:120])
                    ty -= body_line_h

        c.restoreState()

        # continue below the table chunk
        top_y = top_y - chunk_h - 3.2 * mm

        # if more rows remain, move to next page (clean continuation)
        if idx < len(rows):
            top_y = float(new_page_cb() or top_y)

    return top_y


# ============================================================
# Layout pack (GRID_2 / GRID_3 / STACK)
# ============================================================
def _grid_cols_for(layout: str) -> int:
    L = (layout or "STACK").upper().strip()
    if L in ("GRID_3", "GRID3", "3COL"):
        return 3
    if L in ("GRID_2", "GRID2", "2COL"):
        return 2
    return 1


def _span_for(ui_width: str, grid_cols: int) -> int:
    uw = (ui_width or "").upper().strip()
    if uw in ("FULL", "100", "100%", "W_FULL"):
        return grid_cols
    if uw in ("HALF", "50", "50%"):
        return 2 if grid_cols >= 3 else 1
    if uw in ("THIRD", "33", "33%"):
        return 1
    return 1 if grid_cols > 1 else grid_cols


def _render_items(
    c: canvas.Canvas,
    *,
    x: float,
    y_top: float,
    w: float,
    items: List[Dict[str, Any]],
    values_scope: Dict[str, Any],
    # scopes used for visible_when (order matters: most local first)
    visible_scopes: List[Dict[str, Any]],
    container_layout: str,
    theme: PdfTheme,
    new_page_cb,  # returns new content_top_y
    content_bottom_y: float,
) -> float:
    """
    ✅ FIXES:
    - Tables are rendered with TRUE height and can page-break (no overlap).
    - Groups render as blocks (no wrong height estimates).
    - Field value wrapping is not hard-truncated (safer for clinical docs).
    """
    grid_cols = _grid_cols_for(container_layout)
    col_gap = 4.0 * mm
    row_gap = 3.2 * mm

    col_w = w if grid_cols == 1 else (w - (grid_cols - 1) * col_gap) / grid_cols
    cur_y = y_top

    def ensure_space(need_h: float):
        nonlocal cur_y
        if cur_y - need_h < content_bottom_y:
            cur_y = float(new_page_cb() or cur_y)

    # render a normal row (non-table, non-group)
    row_cells: List[Tuple[Dict[str, Any], int, float]] = []
    row_span_used = 0

    def flush_row():
        nonlocal cur_y, row_cells, row_span_used
        if not row_cells:
            return

        row_h = max(h for (_, _, h) in row_cells) if row_cells else (10.0 * mm)
        ensure_space(row_h)

        cursor_x = x
        for item, span, _h in row_cells:
            # skip invisible (already filtered, but safe)
            if not _visible_when(item, visible_scopes):
                cursor_x += (span * col_w + (span - 1) * col_gap + col_gap)
                continue

            ftype = (_s(item.get("type")) or "").lower().strip()
            label = _s(item.get("label") or item.get("key") or "")
            ui = item.get("ui") or {}
            box_w = span * col_w + (span - 1) * col_gap

            key = item.get("key")
            val = values_scope.get(key) if (isinstance(values_scope, dict) and key) else None

            if ftype == "boolean":
                vv = _bool_label(val)
                _draw_field_box(c, x=cursor_x, top_y=cur_y, w=box_w, label=label, value=vv, theme=theme)

            elif ftype in ("select", "radio", "options"):
                vv = _option_label(item, val)
                _draw_field_box(c, x=cursor_x, top_y=cur_y, w=box_w, label=label, value=vv, theme=theme)

            elif ftype == "chips":
                vv = _chips_label(val)
                _draw_field_box(c, x=cursor_x, top_y=cur_y, w=box_w, label=label, value=vv, theme=theme)

            elif ftype in ("image", "signature"):
                if isinstance(val, dict):
                    val = val.get("data_url") or val.get("path") or val.get("url")
                # allow optional height override
                box_h = float(ui.get("height_mm") or 30.0) * mm
                _draw_image_box(c, x=cursor_x, top_y=cur_y, w=box_w, label=label, value=val, theme=theme, box_h=box_h)

            else:
                vv = _strip_html(_s(val))
                _draw_field_box(c, x=cursor_x, top_y=cur_y, w=box_w, label=label, value=vv, theme=theme)

            cursor_x += box_w + col_gap

        cur_y -= row_h + row_gap
        row_cells = []
        row_span_used = 0

    for it in items:
        if not isinstance(it, dict):
            continue

        if not _visible_when(it, visible_scopes):
            continue

        kind = it.get("kind")
        ftype = (_s(it.get("type")) or "").lower().strip()

        ui = it.get("ui") or {}
        span = _span_for(_s(ui.get("width") or ""), grid_cols)
        if grid_cols == 1:
            span = 1

        # ✅ TABLES: flush row and render as a paged block (no overlaps)
        if ftype == "table":
            flush_row()
            label = _s(it.get("label") or it.get("key") or "")
            key = it.get("key")
            val = values_scope.get(key) if (isinstance(values_scope, dict) and key) else None

            # keep a little breathing room for the table title
            ensure_space(14.0 * mm)
            cur_y = _draw_table_paged(
                c,
                x=x,
                top_y=cur_y,
                w=w,
                field=it,
                rows_value=val,
                theme=theme,
                content_bottom_y=content_bottom_y,
                new_page_cb=new_page_cb,
            )
            cur_y -= 1.2 * mm
            continue

        # ✅ GROUPS: flush row and render as a block (no wrong estimates)
        if kind == "field" and ftype == "group":
            flush_row()

            label = _s(it.get("label") or it.get("key") or "")
            g_key = it.get("key") or ""
            nested_scope = {}
            if isinstance(values_scope, dict) and g_key and isinstance(values_scope.get(g_key), dict):
                nested_scope = values_scope.get(g_key) or {}

            gh = 7.0 * mm
            ensure_space(gh + 10.0 * mm)

            _draw_box(c, x, cur_y - gh, w, gh, fill=theme.section_bg, stroke=theme.border, lw=0.50)
            c.setFont(theme.font_bold, theme.label_size)
            c.setFillColor(theme.text)
            c.drawString(x + 2.6 * mm, cur_y - 4.8 * mm, label[:200])

            cur_y = cur_y - gh - 2.4 * mm

            g_layout = (it.get("group") or {}).get("layout") or "STACK"
            child_items = it.get("items") or []
            if isinstance(child_items, list) and child_items:
                # visible_when inside group can reference:
                # - nested_scope (most local)
                # - values_scope (section)
                # - then other scopes provided
                child_scopes = [nested_scope, values_scope] + [s for s in visible_scopes if s not in (nested_scope, values_scope)]
                cur_y = _render_items(
                    c,
                    x=x,
                    y_top=cur_y,
                    w=w,
                    items=child_items,
                    values_scope=nested_scope if isinstance(nested_scope, dict) else {},
                    visible_scopes=child_scopes,
                    container_layout=g_layout,
                    theme=theme,
                    new_page_cb=new_page_cb,
                    content_bottom_y=content_bottom_y,
                )

            cur_y -= 2.0 * mm
            continue

        # normal items: add to grid row
        label = _s(it.get("label") or it.get("key") or "")
        key = it.get("key")
        val = values_scope.get(key) if (isinstance(values_scope, dict) and key) else None

        # force heavy text to full-width for safety (prevents awkward multi-col huge boxes)
        is_heavy_text = ftype in ("textarea", "multiline", "richtext", "paragraph", "note")
        if is_heavy_text and grid_cols > 1:
            flush_row()
            span = 1
            # render as single full-width field (not paginated, but will page-break before it)
            vv = _strip_html(_s(val))
            h_need = _measure_field_box_h(label=label, value=vv, w=w, theme=theme)
            ensure_space(min(h_need, (cur_y - content_bottom_y) - 2.0 * mm))
            _draw_field_box(c, x=x, top_y=cur_y, w=w, label=label, value=vv, theme=theme)
            cur_y -= h_need + row_gap
            continue

        # measure height for row math
        if ftype == "boolean":
            vv = _bool_label(val)
        elif ftype in ("select", "radio", "options"):
            vv = _option_label(it, val)
        elif ftype == "chips":
            vv = _chips_label(val)
        elif ftype in ("image", "signature"):
            # fixed height
            vv = ""
        else:
            vv = _strip_html(_s(val))

        if ftype in ("image", "signature"):
            h = float((it.get("ui") or {}).get("height_mm") or 30.0) * mm
        else:
            box_w = span * col_w + (span - 1) * col_gap
            h = _measure_field_box_h(label=label, value=vv, w=box_w, theme=theme)

        if row_span_used + span > grid_cols and row_cells:
            flush_row()

        row_cells.append((it, span, h))
        row_span_used += span

        if row_span_used >= grid_cols:
            flush_row()

    flush_row()
    return cur_y


# ============================================================
# Patient demographics
# ============================================================
def _patient_demo_rows(patient: Any, encounter_type: Optional[str], encounter: Any) -> List[Tuple[str, str]]:
    # Patient name (✅ no "None")
    name = _safe_str(getattr(patient, "name", None))
    if not name:
        name = _join_name_parts(
            getattr(patient, "first_name", None),
            getattr(patient, "middle_name", None),
            getattr(patient, "last_name", None),
        )
    if not name:
        name = "—"

    uhid = _safe_str(getattr(patient, "uhid", None)) or _safe_str(getattr(patient, "patient_id", None)) or _safe_str(getattr(patient, "code", None)) or "—"

    rows: List[Tuple[str, str]] = [
        ("Patient", name),
        ("UHID", uhid),
    ]

    # Optional fields
    gender = _safe_str(getattr(patient, "gender", None))
    dob = _date(getattr(patient, "dob", None))
    phone = _safe_str(getattr(patient, "phone", None))
    ptype = _safe_str(getattr(patient, "patient_type", None))

    if gender:
        rows.append(("Gender", gender))
    if dob:
        rows.append(("DOB", dob))
    if phone:
        rows.append(("Phone", phone))
    if ptype:
        rows.append(("Patient Type", ptype))

    et = (encounter_type or "").upper().strip()
    if et:
        rows.append(("Encounter", et))

    # You can extend here safely (Encounter ID, Visit No, etc.) when available
    return [(k, _strip_html(_s(v)) if v != "—" else "—") for (k, v) in rows]


def _draw_kv_grid(c: canvas.Canvas, *, x: float, top_y: float, w: float, rows: List[Tuple[str, str]], cols: int, theme: PdfTheme) -> float:
    if not rows:
        return 0.0
    cols = max(1, min(3, int(cols)))
    cell_h = 6.2 * mm
    pad_x = 2.6 * mm
    gap = 1.8 * mm

    n = len(rows)
    n_rows = (n + cols - 1) // cols
    h = n_rows * cell_h + 2.0 * mm

    _draw_box(c, x, top_y - h, w, h, fill=theme.kv_bg, stroke=theme.border, lw=0.50)

    col_w = w / cols
    c.saveState()
    for i, (k, v) in enumerate(rows):
        r = i // cols
        cc = i % cols
        cx = x + cc * col_w
        cy_top = top_y - 1.2 * mm - r * cell_h

        c.setFont(theme.font_bold, theme.label_size)
        c.setFillColor(theme.muted)
        c.drawString(cx + pad_x, cy_top - 2.4 * mm, f"{k}:")

        c.setFont(theme.font, theme.body_size)
        c.setFillColor(theme.text)

        label_w = c.stringWidth(f"{k}: ", theme.font_bold, theme.label_size)
        vx = cx + pad_x + min(label_w + gap, col_w * 0.55)
        c.drawString(vx, cy_top - 2.4 * mm, _s(v)[:180])

    c.restoreState()
    return h


# ============================================================
# Header / footer
# ============================================================
def _footer_cb_factory(theme: PdfTheme, footer_text: str, show_page_number: bool = True):
    def _cb(c: canvas.Canvas, page_no: int, total_pages: int):
        W, _ = c._pagesize
        M = theme.margin_mm * mm
        y = theme.footer_h_mm * mm

        c.saveState()
        c.setStrokeColor(theme.border)
        c.setLineWidth(0.50)
        c.line(M, y + 2.2 * mm, W - M, y + 2.2 * mm)

        c.setFont(theme.font, theme.small_size)
        c.setFillColor(theme.muted)
        c.drawString(M, y - 0.4 * mm, _s(footer_text)[:220])

        if show_page_number:
            c.drawRightString(W - M, y - 0.4 * mm, f"Page {page_no} of {total_pages}")
        c.restoreState()

    return _cb


def _draw_header(
    c: canvas.Canvas,
    *,
    theme: PdfTheme,
    branding: Any,
    doc_title: str,
    printed_at: datetime,
    watermark: Optional[str],
):
    W, H = c._pagesize
    M = theme.margin_mm * mm

    _draw_watermark(c, watermark or "")

    header_h_mm = getattr(branding, "pdf_header_height_mm", None) if branding else None
    header_h = (float(header_h_mm) * mm) if header_h_mm else (theme.header_h_mm_default * mm)

    logo_src = None
    if branding:
        logo_src = (
            getattr(branding, "pdf_logo_path", None)
            or getattr(branding, "org_logo_path", None)
            or getattr(branding, "header_logo_path", None)
            or getattr(branding, "logo_path", None)
            or getattr(branding, "login_logo_path", None)
            or getattr(branding, "pdf_header_path", None)
        )
    logo_img = _image_from_value(logo_src) if logo_src else None

    logo_w_mm = None
    if branding:
        logo_w_mm = getattr(branding, "pdf_logo_width_mm", None) or getattr(branding, "pdf_header_logo_width_mm", None)

    # default bigger than 30mm so it’s clearly visible
    logo_w_mm = float(logo_w_mm) if logo_w_mm else 42.0
    logo_w_mm = max(18.0, min(logo_w_mm, 80.0))  # clamp safe

    logo_w = logo_w_mm * mm
    logo_x = M
    top = H - M

    # ✅ Use a small top padding so logo doesn't touch border
    pad_y = 1.0 * mm
    max_w = logo_w
    max_h = max(6 * mm, header_h - (2 * pad_y))

    if logo_img:
        try:
            iw, ih = logo_img.getSize()
            if iw and ih:
                scale = min(float(max_w) / float(iw), float(max_h) / float(ih))
                draw_w = float(iw) * scale
                draw_h = float(ih) * scale

                ix = logo_x
                iy = top - pad_y - draw_h  # ✅ TOP aligned (more “visible” than center align)

                c.drawImage(
                    logo_img,
                    ix,
                    iy,
                    width=draw_w,
                    height=draw_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
        except Exception:
            pass

    org_name = _s(getattr(branding, "org_name", "") if branding else "") or "Organisation"
    org_tagline = _s(getattr(branding, "org_tagline", "") if branding else "")
    org_address = _s(getattr(branding, "org_address", "") if branding else "")
    org_email = _s(getattr(branding, "org_email", "") if branding else "")
    org_phone = _s(getattr(branding, "org_phone", "") if branding else "")
    org_web = _s(getattr(branding, "org_website", "") if branding else "")

    right_x = M + logo_w + 6.0 * mm
    right_w = (W - M) - right_x

    c.setFillColor(theme.text)
    c.setFont(theme.font_bold, theme.org_size)
    c.drawRightString(W - M, top - 5.0 * mm, org_name[:90])

    c.setFillColor(theme.muted)
    c.setFont(theme.font, theme.small_size)

    y = top - 10.0 * mm
    if org_tagline:
        c.drawRightString(W - M, y, org_tagline[:140])
        y -= 4.0 * mm

    if org_address:
        addr_lines = _wrap(org_address, theme.font, theme.small_size, right_w, max_lines=2)
        for ln in addr_lines:
            if ln.strip():
                c.drawRightString(W - M, y, ln[:170])
                y -= 4.0 * mm

    contact_line = " | ".join([x for x in [org_email, org_phone, org_web] if x.strip()])
    if contact_line:
        c.drawRightString(W - M, y, contact_line[:170])

    # doc title & printed line (single, clean)
    c.setFillColor(theme.text)
    c.setFont(theme.font_bold, theme.title_size)
    c.drawString(M, top - header_h + 3.2 * mm, _clean_title(doc_title))

    c.setFillColor(theme.muted)
    c.setFont(theme.font, theme.small_size)
    c.drawRightString(W - M, top - header_h + 3.2 * mm, f"Printed: {_dt(printed_at)}")

    # divider
    c.setStrokeColor(theme.border)
    c.setLineWidth(0.55)
    c.line(M, top - header_h, W - M, top - header_h)



# ============================================================
# Public API
# ============================================================
def build_export_pdf_bytes(
    *,
    patient: Any,
    bundle_title: str,
    records: List[EmrRecord],
    watermark: Optional[str],
    db: Session,
    branding: Any = None,
    encounter_type: Optional[str] = None,
    encounter: Any = None,
    paper: str = "A4",
    orientation: str = "portrait",
    system_footer: str = "System-generated at print time",
) -> bytes:
    """
    ✅ FIXES INCLUDED:
      - No "None" printed in patient name
      - TRUE table heights + table pagination (no overlap/collisions)
      - Correct page numbering (no extra blank page)
      - Section-scoped data lookup (data[SECTION_CODE][field_key])
      - Clean medical-grade layout with softer borders
    """
    buff = BytesIO()
    size = _pagesize(paper, orientation)
    theme = PdfTheme()
    printed_at = datetime.now()

    show_page_number = True
    if branding is not None:
        show_page_number = bool(getattr(branding, "pdf_show_page_number", True))

    footer_cb = _footer_cb_factory(
        theme,
        footer_text=f"{system_footer} | Generated by NABH HIMS",
        show_page_number=show_page_number,
    )
    c = NumberedCanvas(buff, pagesize=size, footer_cb=footer_cb)

    W, H = size
    M = theme.margin_mm * mm
    footer_reserved = (theme.footer_h_mm * mm) + 4.5 * mm
    bottom_y = footer_reserved

    def draw_static() -> float:
        _draw_header(
            c,
            theme=theme,
            branding=branding,
            doc_title=bundle_title,
            printed_at=printed_at,
            watermark=watermark,
        )

        header_h_mm = getattr(branding, "pdf_header_height_mm", None) if branding else None
        header_h = (float(header_h_mm) * mm) if header_h_mm else (theme.header_h_mm_default * mm)

        demo_top = (H - M) - header_h - (theme.demo_pad_mm * mm)
        demo_rows = _patient_demo_rows(patient, encounter_type, encounter)
        demo_cols = 3 if len(demo_rows) >= 8 else 2

        used = _draw_kv_grid(
            c,
            x=M,
            top_y=demo_top,
            w=W - 2 * M,
            rows=demo_rows,
            cols=demo_cols,
            theme=theme,
        )
        return demo_top - used - 4.0 * mm  # content top Y

    def new_page() -> float:
        c.showPage()
        return draw_static()

    # first page
    content_top_y = draw_static()
    y = content_top_y

    if not records:
        c.setFont(theme.font, theme.body_size)
        c.setFillColor(theme.text)
        c.drawString(M, y, "No records selected.")
        c.save()
        return buff.getvalue()

    def new_page_cb() -> float:
        nonlocal content_top_y, y
        content_top_y = new_page()
        y = content_top_y
        return content_top_y

    for r in records:
        tv_schema = None
        if getattr(r, "template_version_id", None):
            v = (
                db.query(EmrTemplateVersion)
                .filter(EmrTemplateVersion.id == int(r.template_version_id))
                .one_or_none()
            )
            if v:
                tv_schema = _schema_from_template_version(v)

        payload = _extract_record_payload(r)
        schema, data = _extract_schema_and_data(
            record_payload=payload,
            template_version_schema=tv_schema,
        )

        if not schema or not isinstance(schema.get("sections"), list):
            continue

        sections = schema.get("sections") or []
        for sec in sections:
            if not isinstance(sec, dict):
                continue

            sec_code = _s(sec.get("code") or "").strip()
            sec_label = _s(sec.get("label") or sec_code or "Section").strip()
            sec_layout = _s(sec.get("layout") or "STACK").strip()
            sec_items = sec.get("items") or []
            if not isinstance(sec_items, list) or not sec_items:
                continue

            sec_values = data.get(sec_code) if (sec_code and isinstance(data, dict)) else None
            if not isinstance(sec_values, dict):
                sec_values = data if isinstance(data, dict) else {}

            # ensure space for section header
            if y < bottom_y + 14.0 * mm:
                y = new_page_cb()

            used_h = _draw_section_header(
                c,
                x=M,
                top_y=y,
                w=W - 2 * M,
                text=sec_label,
                theme=theme,
            )
            y -= (used_h + 3.2 * mm)

            # visible_when scopes:
            # - section values
            # - record root data (for rare cross-field references)
            visible_scopes = [sec_values, data] if isinstance(data, dict) else [sec_values]

            y = _render_items(
                c,
                x=M,
                y_top=y,
                w=W - 2 * M,
                items=sec_items,
                values_scope=sec_values,
                visible_scopes=visible_scopes,
                container_layout=sec_layout,
                theme=theme,
                new_page_cb=new_page_cb,
                content_bottom_y=bottom_y,
            )

            y -= 2.0 * mm

        # spacing between records
        y -= 4.0 * mm
        if y < bottom_y + 12.0 * mm:
            y = new_page_cb()

    c.save()
    return buff.getvalue()
