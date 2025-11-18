# app/services/pdf.py
from __future__ import annotations
import io, re, os
from typing import Tuple, Optional
from urllib.parse import urlparse
from app.core.config import settings

# ---------- CSS helpers (WeasyPrint-ready; xhtml2pdf gets a sanitized version) ----------

DEFAULT_PRINT_CSS = r"""
/* Base */
html, body { font-family: system-ui, -apple-system, 'Segoe UI', Roboto, Arial; font-size: 12px; line-height: 1.35; }
h1,h2,h3 { margin: 0 0 8px 0; }
table { width: 100%; border-collapse: collapse; }
th, td { vertical-align: top; }

/* Alignment utils */
.text-left   { text-align: left; }
.text-center { text-align: center; }
.text-right  { text-align: right; }
.text-justify{ text-align: justify; }

/* Floats / images */
.float-left  { float: left;  margin-right: 8px; }
.float-right { float: right; margin-left: 8px; }
.img-center  { display:block; margin-left:auto; margin-right:auto; }

/* Width utils */
.w-25 { width:25%; } .w-33 { width:33.333%; } .w-50 { width:50%; }
.w-66 { width:66.666%; } .w-75 { width:75%; } .w-100 { width:100%; }

/* Columns (WeasyPrint supports CSS columns) */
.cols-2 { column-count: 2; column-gap: 16px; }
.cols-3 { column-count: 3; column-gap: 16px; }

/* Table border presets */
.tbl-border-all    table, .tbl-border-all th, .tbl-border-all td    { border:1px solid #333; }
.tbl-border-h      th, td { border-top:1px solid #333; border-bottom:1px solid #333; }
.tbl-border-v      th, td { border-left:1px solid #333; border-right:1px solid #333; }
.tbl-border-none   table, th, td { border:none !important; }

/* Row shading */
.tr-shade > td { background:#f5f5f5; }

/* Running header/footer (WeasyPrint) */
header.tpl-header { position: running(doc-header); }
footer.tpl-footer { position: running(doc-footer); }

/* Default page + page numbers (WeasyPrint) */
@page {
  size: A4;
  margin: 18mm 16mm 18mm 16mm;
  @top-center    { content: element(doc-header); }
  @bottom-center { content: element(doc-footer) }
  @bottom-right  { content: "Page " counter(page) " of " counter(pages); font-size:10px; }
}
"""


def _sanitize_for_xhtml2pdf_css(css: str) -> str:
    """
    xhtml2pdf chokes on comments, @page/@font-face/@media, counters, 'position: running', etc.
    This leaves only safe rules so fallback PDFs don't crash.
    """
    if not css:
        return ""

    # remove comments
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    # remove @-blocks (generic)
    prev = None
    while prev != css:
        prev = css
        css = re.sub(r"@[^{}]+{[^{}]*}", "", css)

    # remove unsupported props
    css = re.sub(r"position\s*:\s*running\([^)]*\)\s*;?", "", css, flags=re.I)
    css = re.sub(r"content\s*:\s*element\([^)]*\)\s*;?", "", css, flags=re.I)
    css = re.sub(r"counter\([^)]+\)", "", css, flags=re.I)

    # tidy braces
    css = re.sub(r"}\s*}", "}", css)
    css = re.sub(r"{\s*{", "{", css)
    css = re.sub(r"^\s*}\s*", "", css)
    return css


def _strip_unsupported_css_in_html_for_xhtml2pdf(html: str) -> str:
    # merge all style blocks -> sanitize -> inject one safe block
    styles = re.findall(r"<style[^>]*>(.*?)</style>", html, flags=re.I | re.S)
    merged = "\n".join(styles)
    safe = _sanitize_for_xhtml2pdf_css(merged)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.I | re.S)
    safe_tag = f"<style>{safe}</style>"
    if re.search(r"<head[^>]*>", html, flags=re.I):
        html = re.sub(r"(<head[^>]*>)",
                      r"\1" + safe_tag,
                      html,
                      count=1,
                      flags=re.I)
    else:
        html = re.sub(r"(<body[^>]*>)",
                      r"<head>" + safe_tag + "</head>\1",
                      html,
                      count=1,
                      flags=re.I)
    return html


def _compat_html_for_xhtml2pdf(html: str) -> str:
    """
    Minimal translation layer so float-right/img-center survive in xhtml2pdf.
    Wrap images having those classes into a text-aligned container.
    """

    def _wrap_img(m):
        tag = m.group(0)
        cls = (m.group('cls') or '').lower()
        # strip class to avoid double effects
        clean = re.sub(r'\sclass="[^"]*"', "", tag, flags=re.I)
        if 'float-right' in cls:
            return f'<div style="text-align:right">{clean}</div>'
        if 'img-center' in cls:
            return f'<div style="text-align:center">{clean}</div>'
        if 'float-left' in cls:
            return f'<div style="text-align:left">{clean}</div>'
        return tag

    html = re.sub(
        r'<img\b(?P<attrs>[^>]*?)\sclass="(?P<cls>[^"]*(?:float-right|img-center|float-left)[^"]*)"(?:[^>]*)>',
        _wrap_img,
        html,
        flags=re.I,
    )
    return html


def _inject_base_href(html: str, base_url: Optional[str]) -> str:
    if not base_url:
        return html
    base = f"<base href='{base_url.rstrip('/')}/'>"
    if "<head" in html:
        return re.sub(r"(<head[^>]*>)",
                      r"\1" + base,
                      html,
                      count=1,
                      flags=re.I)
    return f"<!doctype html><head>{base}</head><body>{html}</body>"


def render_html(body_html: str,
                css_text: Optional[str],
                context: dict,
                base_url: Optional[str] = None) -> str:
    """
    Build full HTML: base href + CSS + body.
    """
    css = (css_text or "") + "\n" + DEFAULT_PRINT_CSS
    html = f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body>{body_html}</body></html>"
    return _inject_base_href(html, base_url or settings.SITE_URL)


# ---------- File resolver for xhtml2pdf ----------
def _xhtml2pdf_link_callback(uri: str, rel: str) -> str:
    if not uri:
        return uri
    parsed = urlparse(uri)
    if parsed.scheme in ("http", "https"):
        return uri

    media_prefix = settings.MEDIA_URL.rstrip("/")
    if uri.startswith(media_prefix + "/"):
        rel_path = uri[len(media_prefix) + 1:]
        return os.path.join(os.path.abspath(settings.STORAGE_DIR),
                            rel_path.replace("/", os.sep))
    if uri.startswith("/media/"):
        rel_path = uri[7:]
        return os.path.join(os.path.abspath(settings.STORAGE_DIR),
                            rel_path.replace("/", os.sep))
    return uri


# ---------- Main PDF generator ----------
def generate_pdf(full_html: str,
                 base_url: Optional[str] = None,
                 prefer: Optional[str] = None) -> Tuple[bytes, str]:
    """
    Try WeasyPrint (best). Fallback to xhtml2pdf (sanitized + compat).
    'prefer' can force a path: "weasyprint" or "xhtml2pdf".
    """
    # force weasy
    if prefer == "weasyprint":
        from weasyprint import HTML
        html_for_weasy = _inject_base_href(full_html, base_url
                                           or settings.SITE_URL)
        pdf = HTML(string=html_for_weasy,
                   base_url=base_url or settings.SITE_URL).write_pdf()
        return pdf, "weasyprint"

    # default: try weasy, then fallback
    if prefer != "xhtml2pdf":
        try:
            from weasyprint import HTML
            html_for_weasy = _inject_base_href(full_html, base_url
                                               or settings.SITE_URL)
            pdf = HTML(string=html_for_weasy,
                       base_url=base_url or settings.SITE_URL).write_pdf()
            return pdf, "weasyprint"
        except Exception:
            pass

    # fallback: xhtml2pdf
    from xhtml2pdf import pisa
    out = io.BytesIO()
    html_for_pisa = _strip_unsupported_css_in_html_for_xhtml2pdf(full_html)
    html_for_pisa = _compat_html_for_xhtml2pdf(html_for_pisa)
    status = pisa.CreatePDF(html_for_pisa,
                            dest=out,
                            link_callback=_xhtml2pdf_link_callback)
    if status.err:
        raise RuntimeError("PDF rendering failed (xhtml2pdf)")
    return out.getvalue(), "xhtml2pdf"
