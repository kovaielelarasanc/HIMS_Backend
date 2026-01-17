# FILE: app/pdf/billing_case_full_pdf_v2.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from io import BytesIO
from pathlib import Path
import base64
import html as _html
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, selectinload

from app.models.ui_branding import UiBranding
from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPayment,
    BillingAdvance,
    BillingAdvanceApplication,
    DocStatus,
    PayMode,
    PaymentKind,
    PaymentDirection,
    ReceiptStatus,
)

try:
    from weasyprint import HTML, CSS  # type: ignore
except Exception:  # pragma: no cover
    HTML = None
    CSS = None


def h(s: Any) -> str:
    if s is None:
        return ""
    return _html.escape(str(s), quote=True)


def d(v: Any) -> Decimal:
    try:
        return Decimal(str(v or "0"))
    except Exception:
        return Decimal("0")


def fmt_date(dt: Any) -> str:
    if not dt:
        return ""
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        return dt.strftime("%d-%b-%Y")
    try:
        return str(dt)
    except Exception:
        return ""


def fmt_dt(dt: Any) -> str:
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%d-%b-%Y %I:%M %p")
    return fmt_date(dt)


def fmt_inr(amount: Any) -> str:
    n = d(amount)
    sign = "-" if n < 0 else ""
    n = abs(n)
    s = f"{n:.2f}"
    whole, frac = s.split(".")
    if len(whole) <= 3:
        return f"{sign}{whole}.{frac}"
    last3 = whole[-3:]
    rest = whole[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return f"{sign}{','.join(parts)},{last3}.{frac}"


def amount_to_words_inr(amount: Any) -> str:
    # Indian system: crore, lakh, thousand, hundred
    n = d(amount)
    if n < 0:
        return "Minus " + amount_to_words_inr(abs(n))

    rupees = int(n)
    paise = int((n - Decimal(rupees)) * 100)

    ones = [
        "",
        "One",
        "Two",
        "Three",
        "Four",
        "Five",
        "Six",
        "Seven",
        "Eight",
        "Nine",
        "Ten",
        "Eleven",
        "Twelve",
        "Thirteen",
        "Fourteen",
        "Fifteen",
        "Sixteen",
        "Seventeen",
        "Eighteen",
        "Nineteen",
    ]
    tens = [
        "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
        "Eighty", "Ninety"
    ]

    def two_digits(x: int) -> str:
        if x < 20:
            return ones[x]
        return (tens[x // 10] + (" " + ones[x % 10] if x % 10 else "")).strip()

    def three_digits(x: int) -> str:
        if x < 100:
            return two_digits(x)
        return (ones[x // 100] + " Hundred" +
                (" " + two_digits(x % 100) if x % 100 else "")).strip()

    parts: List[str] = []
    crore = rupees // 10000000
    rupees %= 10000000
    lakh = rupees // 100000
    rupees %= 100000
    thousand = rupees // 1000
    rupees %= 1000
    hundred_block = rupees

    if crore:
        parts.append(two_digits(crore) + " Crore")
    if lakh:
        parts.append(two_digits(lakh) + " Lakh")
    if thousand:
        parts.append(two_digits(thousand) + " Thousand")
    if hundred_block:
        parts.append(three_digits(hundred_block))

    words = " ".join([p for p in parts if p]).strip() or "Zero"
    if paise:
        words = f"{words} and {two_digits(paise)} Paise"
    return f"Rupees {words} Only"


def _pick(obj: Any, fields: List[str], default: Any = None) -> Any:
    for f in fields:
        if hasattr(obj, f):
            v = getattr(obj, f, None)
            if v not in (None, "", []):
                return v
    return default


def _logo_data_uri(branding: UiBranding) -> Optional[str]:
    # tries: logo_base64 / logo_data_uri / logo_path
    v = _pick(branding, ["logo_data_uri", "logo_base64", "logo_b64"], None)
    if v:
        s = str(v)
        if s.startswith("data:image"):
            return s
        return f"data:image/png;base64,{s}"

    p = _pick(branding, ["logo_path", "logo_file", "logo"], None)
    if p:
        try:
            path = Path(str(p))
            if path.exists():
                b = path.read_bytes()
                return "data:image/png;base64," + base64.b64encode(b).decode(
                    "utf-8")
        except Exception:
            return None
    return None


def _brand_block(branding: UiBranding) -> str:
    logo = _logo_data_uri(branding)
    org_name = _pick(branding, ["org_name", "name", "hospital_name"], "") or ""
    tagline = _pick(branding, ["tagline"], "") or ""
    address = _pick(branding, ["address", "org_address"], "") or ""
    phone = _pick(branding, ["phone", "mobile", "contact_phone"], "") or ""
    email = _pick(branding, ["email", "contact_email"], "") or ""
    gstin = _pick(branding, ["gstin", "gst_no"], "") or ""

    contact_bits = []
    if phone:
        contact_bits.append(f"Phone: {h(phone)}")
    if email:
        contact_bits.append(f"Email: {h(email)}")
    contact_line = " | ".join(contact_bits)

    return f"""
    <div class="brand">
      <div class="brand-left">
        {"<img class='brand-logo' src='"+h(logo)+"' />" if logo else ""}
        <div class="brand-left-text">
          <div class="brand-name">{h(org_name)}</div>
          {"<div class='brand-tagline'>"+h(tagline)+"</div>" if tagline else ""}
        </div>
      </div>
      <div class="brand-right">
        {"<div class='brand-line'>"+h(address)+"</div>" if address else ""}
        {"<div class='brand-line'>"+contact_line+"</div>" if contact_line else ""}
        {"<div class='brand-line'>GSTIN: "+h(gstin)+"</div>" if gstin else ""}
      </div>
    </div>
    """


def _css() -> str:
    # medium header, clean patient layout, printable tables
    return """
    @page {
      size: A4;
      margin: 12mm 12mm 14mm 12mm;
      @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 9px; color: #444; }
      @bottom-left  { content: "Printed: " string(printed_at); font-size: 9px; color: #444; }
    }

    body { font-family: Arial, Helvetica, sans-serif; font-size: 11px; color:#111; }
    .printed-at { string-set: printed_at content(text); }

    .brand { display:flex; align-items:center; justify-content:space-between; gap:12px; padding: 2px 0 8px 0; border-bottom:1px solid #cfd6df; }
    .brand-left { display:flex; align-items:center; gap:10px; min-width: 55%; }
    .brand-logo { height: 52px; width:auto; object-fit:contain; }
    .brand-name { font-size: 18px; font-weight: 800; line-height: 1.1; }
    .brand-tagline { font-size: 11px; color:#444; font-weight: 600; margin-top:2px; }
    .brand-right { text-align:right; font-size: 10px; color:#333; max-width: 42%; }
    .brand-line { line-height: 1.25; }

    .title { text-align:center; font-weight:800; margin: 10px 0 8px; letter-spacing: .5px; }
    .title.big { font-size: 12px; }

    .two-col { width:100%; border-collapse:collapse; margin-top: 6px; }
    .two-col td { vertical-align: top; padding: 4px 6px; }
    .box { border:1px solid #111; border-collapse: collapse; width:100%; }
    .box td { border:1px solid #111; padding:4px 6px; }
    .kv { width:100%; border-collapse:collapse; }
    .kv td { padding:2px 0; }
    .kv .k { width: 36%; color:#111; font-weight:700; }
    .kv .v { width: 64%; color:#111; }

    .tbl { width:100%; border-collapse:collapse; margin-top: 8px; }
    .tbl th, .tbl td { border:1px solid #111; padding: 4px 6px; }
    .tbl th { font-weight:800; background:#f3f5f8; }
    .right { text-align:right; }
    .center { text-align:center; }
    .muted { color:#555; }

    .section-head {
      font-weight: 800; text-transform: uppercase;
      padding: 6px 0 3px; margin-top: 10px;
      border-top: 1px solid #111;
    }
    .group-row td { font-weight: 800; background:#fafbfd; }

    .totals { width: 100%; border-collapse: collapse; margin-top: 8px; }
    .totals td { padding: 2px 0; }
    .totals .k { text-align:right; font-weight: 800; padding-right: 10px; }
    .totals .v { text-align:right; width: 120px; }

    .page-break { page-break-before: always; }
    """


def _line_printable(line: BillingInvoiceLine) -> bool:
    mj = getattr(line, "meta_json", None) or {}
    if isinstance(mj, dict):
        if mj.get("is_void") or mj.get("void") or mj.get("voided") or mj.get(
                "is_deleted") or mj.get("deleted"):
            return False
    if d(getattr(line, "net_amount", 0)) <= 0:
        return False
    if d(getattr(line, "qty", 0)) == 0:
        return False
    return True


def _module_key(inv: BillingInvoice) -> str:
    m = (getattr(inv, "module", "") or "").upper().strip()
    if not m:
        # fallback to invoice_type
        it = str(getattr(inv, "invoice_type", "") or "").upper()
        if "PHARM" in it:
            return "PHARM"
    if m in ("RIS", ):
        return "RAD"
    return m or "MISC"


def _module_label(m: str) -> str:
    m = (m or "").upper()
    return {
        "LAB": "CLINICAL LAB CHARGES",
        "RAD": "RADIOLOGY CHARGES",
        "PHARM": "PHARMACY CHARGES",
        "ROOM": "HOSPITAL / ROOM CHARGES",
        "IPD": "HOSPITAL / IPD CHARGES",
        "OT": "OT / PROCEDURES",
        "OPD": "OPD CHARGES",
        "MISC": "OTHER CHARGES",
    }.get(m, f"{m} CHARGES")


def _group_key(line: BillingInvoiceLine) -> str:
    cc = getattr(line, "cost_center", None)
    rh = getattr(line, "revenue_head", None)
    if cc and getattr(cc, "name", None):
        return str(getattr(cc, "name"))
    if rh and getattr(rh, "name", None):
        return str(getattr(rh, "name"))
    sg = str(getattr(line, "service_group", "") or "")
    return sg or "CHARGES"


def _pharm_meta(line: BillingInvoiceLine) -> Dict[str, str]:
    mj = getattr(line, "meta_json", None) or {}
    if not isinstance(mj, dict):
        mj = {}

    def pick(*keys: str) -> str:
        for k in keys:
            v = mj.get(k)
            if v not in (None, ""):
                return str(v)
        return ""

    return {
        "batch": pick("batch_no", "batch", "batch_id"),
        "expiry": pick("expiry", "expiry_date", "exp"),
        "hsn": pick("hsn", "hsn_sac", "hsn_code"),
    }


@dataclass
class CaseTotals:
    grand_total: Decimal
    taxable_value: Decimal
    gst_total: Decimal
    round_off: Decimal
    by_module: Dict[str, Decimal]


def _load_case(db: Session, case_id: int) -> BillingCase:
    q = (db.query(BillingCase).options(
        selectinload(BillingCase.patient),
        selectinload(BillingCase.invoices).selectinload(BillingInvoice.lines),
        selectinload(BillingCase.payments).selectinload(
            BillingPayment.allocations),
        selectinload(BillingCase.advances),
    ).filter(BillingCase.id == case_id))
    case = q.first()
    if not case:
        raise ValueError("Case not found")
    return case


def _calc_totals(case: BillingCase) -> CaseTotals:
    by_mod: Dict[str, Decimal] = {}
    grand = Decimal("0")
    taxable = Decimal("0")
    gst = Decimal("0")
    ro = Decimal("0")

    invoices: List[BillingInvoice] = list(getattr(case, "invoices", []) or [])
    for inv in invoices:
        if getattr(inv, "status", None) == DocStatus.VOID:
            continue
        mkey = _module_key(inv)
        g = d(getattr(inv, "grand_total", 0))
        grand += g
        by_mod[mkey] = by_mod.get(mkey, Decimal("0")) + g

        # printable totals
        st = d(getattr(inv, "sub_total", 0))
        disc = d(getattr(inv, "discount_total", 0))
        taxable += (st - disc)
        gst += d(getattr(inv, "tax_total", 0))
        ro += d(getattr(inv, "round_off", 0))

    return CaseTotals(grand_total=grand,
                      taxable_value=taxable,
                      gst_total=gst,
                      round_off=ro,
                      by_module=by_mod)


def _payments_net(case: BillingCase) -> Dict[str, Decimal]:
    total_in = Decimal("0")
    total_out = Decimal("0")
    adv_adj = Decimal("0")

    payments: List[BillingPayment] = list(getattr(case, "payments", []) or [])
    for p in payments:
        if getattr(p, "status", None) != ReceiptStatus.ACTIVE:
            continue
        amt = d(getattr(p, "amount", 0))
        direction = getattr(p, "direction", None)
        kind = getattr(p, "kind", None)

        if direction == PaymentDirection.IN:
            # ✅ include advance adjustments in "payment received"
            if kind in (PaymentKind.RECEIPT, PaymentKind.ADVANCE_ADJUSTMENT):
                total_in += amt
            if kind == PaymentKind.ADVANCE_ADJUSTMENT:
                adv_adj += amt
        elif direction == PaymentDirection.OUT:
            total_out += amt

    net = total_in - total_out
    return {
        "received_in": total_in,
        "refund_out": total_out,
        "net_received": net,
        "advance_adjusted": adv_adj
    }


def _adv_wallet(db: Session, case: BillingCase) -> Dict[str, Decimal]:
    # wallet: advances - refunds - applied
    total_adv = Decimal("0")
    total_ref = Decimal("0")
    for a in (getattr(case, "advances", []) or []):
        et = str(getattr(a, "entry_type", "") or "")
        amt = d(getattr(a, "amount", 0))
        if "ADVANCE" in et:
            total_adv += amt
        elif "REFUND" in et:
            total_ref += amt

    applied = Decimal("0")
    try:
        rows = (db.query(BillingAdvanceApplication).filter(
            BillingAdvanceApplication.billing_case_id == case.id).all())
        applied = sum((d(r.amount) for r in rows), Decimal("0"))
    except Exception:
        applied = Decimal("0")

    available = total_adv - total_ref - applied
    if available < 0:
        available = Decimal("0")
    return {
        "advance_total": total_adv,
        "refunded_total": total_ref,
        "applied_total": applied,
        "available": available
    }


def _overview_html(db: Session, case: BillingCase, totals: CaseTotals,
                   doc_no: Optional[str], doc_date: Optional[str]) -> str:
    patient = getattr(case, "patient", None)
    p_name = _pick(patient, ["full_name", "name", "patient_name"], "") or ""
    p_uhid = _pick(patient, ["uhid", "uhid_no", "patient_id", "patient_code"],
                   "") or ""
    p_age = _pick(patient, ["age"], "") or ""
    p_gender = _pick(patient, ["gender", "sex"], "") or ""
    p_addr = _pick(patient, ["address", "full_address"], "") or ""

    bill_no = doc_no or case.case_number
    bill_dt = doc_date or fmt_date(datetime.utcnow())

    pay = _payments_net(case)
    balance = totals.grand_total - pay["net_received"]

    # module summary rows
    rows = ""
    for m, amt in sorted(totals.by_module.items(), key=lambda x: x[0]):
        rows += f"<tr><td>{h(_module_label(m))}</td><td class='right'>{fmt_inr(amt)}</td></tr>"

    # payments table rows
    pay_rows = ""
    payments: List[BillingPayment] = list(getattr(case, "payments", []) or [])
    for p in sorted(payments,
                    key=lambda x: getattr(x, "received_at", datetime.min) or
                    datetime.min):
        if getattr(p, "status", None) != ReceiptStatus.ACTIVE:
            continue
        if getattr(p, "direction", None) != PaymentDirection.IN:
            continue
        kind = getattr(p, "kind", None)
        if kind not in (PaymentKind.RECEIPT, PaymentKind.ADVANCE_ADJUSTMENT):
            continue
        label = "ADVANCE ADJ" if kind == PaymentKind.ADVANCE_ADJUSTMENT else "RECEIPT"
        pay_rows += f"""
          <tr>
            <td>{h(getattr(p, "receipt_number", "") or "")}</td>
            <td>{h(str(getattr(p, "mode", "") or ""))}</td>
            <td>{h(fmt_date(getattr(p, "received_at", None)))}</td>
            <td>{h(label)}</td>
            <td class="right">{fmt_inr(getattr(p, "amount", 0))}</td>
          </tr>
        """

    adv = _adv_wallet(db, case)

    return f"""
      <div class="printed-at">Printed: {fmt_dt(datetime.utcnow())}</div>

      <div class="title big">BILL SUMMARY</div>

      <table class="two-col">
        <tr>
          <td style="width:58%;">
            <table class="kv">
              <tr><td class="k">Patient Name</td><td class="v">: {h(p_name)}</td></tr>
              <tr><td class="k">Patient ID / UHID</td><td class="v">: {h(p_uhid)}</td></tr>
              <tr><td class="k">Age / Gender</td><td class="v">: {h(p_age)} {"/" if p_age and p_gender else ""} {h(p_gender)}</td></tr>
              <tr><td class="k">Encounter Type</td><td class="v">: {h(getattr(case, "encounter_type", ""))}</td></tr>
              <tr><td class="k">Encounter ID</td><td class="v">: {h(getattr(case, "encounter_id", ""))}</td></tr>
              <tr><td class="k">Patient Address</td><td class="v">: {h(p_addr)}</td></tr>
            </table>
          </td>
          <td style="width:42%;">
            <table class="box">
              <tr><td><b>Bill Number</b></td><td class="right"><b>{h(bill_no)}</b></td></tr>
              <tr><td><b>Bill Date</b></td><td class="right">{h(bill_dt)}</td></tr>
              <tr><td><b>Case Number</b></td><td class="right">{h(case.case_number)}</td></tr>
              <tr><td><b>Status</b></td><td class="right">{h(getattr(case, "status", ""))}</td></tr>
            </table>
          </td>
        </tr>
      </table>

      <table class="tbl">
        <thead>
          <tr><th>Particulars</th><th class="right">Total Amount</th></tr>
        </thead>
        <tbody>
          {rows or "<tr><td colspan='2' class='center muted'>No invoice totals available</td></tr>"}
        </tbody>
      </table>

      <table class="totals">
        <tr><td class="k">Taxable Value</td><td class="v">{fmt_inr(totals.taxable_value)}</td></tr>
        <tr><td class="k">GST</td><td class="v">{fmt_inr(totals.gst_total)}</td></tr>
        <tr><td class="k">Round Off</td><td class="v">{fmt_inr(totals.round_off)}</td></tr>
        <tr><td class="k">Total Bill Amount</td><td class="v"><b>{fmt_inr(totals.grand_total)}</b></td></tr>
      </table>

      <div class="muted" style="margin-top:6px;"><b>Amount in words:</b> {h(amount_to_words_inr(totals.grand_total))}</div>

      <div class="section-head">PAYMENT DETAILS</div>
      <table class="tbl">
        <thead>
          <tr><th>Receipt No</th><th>Paymode</th><th>Date</th><th>Type</th><th class="right">Amount</th></tr>
        </thead>
        <tbody>
          {pay_rows or "<tr><td colspan='5' class='center muted'>No payments</td></tr>"}
        </tbody>
      </table>

      <table class="totals">
        <tr><td class="k">Total Payment Received</td><td class="v"><b>{fmt_inr(pay["net_received"])}</b></td></tr>
        <tr><td class="k">Advance Adjusted (included)</td><td class="v">{fmt_inr(pay["advance_adjusted"])}</td></tr>
        {f"<tr><td class='k'>Refunds</td><td class='v'>{fmt_inr(pay['refund_out'])}</td></tr>" if pay["refund_out"] > 0 else ""}
        <tr><td class="k">Balance Amount</td><td class="v"><b>{fmt_inr(balance)}</b></td></tr>
      </table>

      <div class="section-head">ADVANCE SUMMARY</div>
      <table class="tbl">
        <thead>
          <tr><th>Total</th><th>Applied</th><th>Refunded</th><th class="right">Available</th></tr>
        </thead>
        <tbody>
          <tr>
            <td class="right">{fmt_inr(adv["advance_total"])}</td>
            <td class="right">{fmt_inr(adv["applied_total"])}</td>
            <td class="right">{fmt_inr(adv["refunded_total"])}</td>
            <td class="right"><b>{fmt_inr(adv["available"])}</b></td>
          </tr>
        </tbody>
      </table>
    """


def _details_html(case: BillingCase) -> str:
    invoices: List[BillingInvoice] = list(getattr(case, "invoices", []) or [])
    # remove void invoices
    invoices = [
        i for i in invoices if getattr(i, "status", None) != DocStatus.VOID
    ]
    invoices.sort(
        key=lambda i: (_module_key(i), str(getattr(i, "invoice_number", ""))))

    parts: List[str] = []
    first = True

    for inv in invoices:
        mkey = _module_key(inv)
        module_title = _module_label(mkey)
        inv_no = getattr(inv, "invoice_number", "") or ""
        inv_dt = fmt_date(
            getattr(inv, "service_date", None)
            or getattr(inv, "created_at", None))
        inv_total = fmt_inr(getattr(inv, "grand_total", 0))

        lines: List[BillingInvoiceLine] = list(getattr(inv, "lines", []) or [])
        lines = [ln for ln in lines if _line_printable(ln)]
        if not lines:
            continue

        if first:
            parts.append("<div class='page-break'></div>")
            first = False
        else:
            parts.append("<div class='page-break'></div>")

        parts.append(f"""
          <div class="title big">{h(module_title)}</div>
          <table class="box">
            <tr>
              <td><b>Invoice No</b></td><td>{h(inv_no)}</td>
              <td><b>Date</b></td><td>{h(inv_dt)}</td>
              <td><b>Status</b></td><td>{h(getattr(inv, "status", ""))}</td>
              <td class="right"><b>Total</b></td><td class="right"><b>{h(inv_total)}</b></td>
            </tr>
          </table>
        """)

        if mkey == "PHARM":
            # PHARM split-up style
            parts.append("""
              <table class="tbl">
                <thead>
                  <tr>
                    <th>Bill No</th>
                    <th>Bill Date</th>
                    <th>Item Name</th>
                    <th>Batch</th>
                    <th>Expiry</th>
                    <th>HSN</th>
                    <th class="right">Qty</th>
                    <th class="right">Amount</th>
                  </tr>
                </thead>
                <tbody>
            """)
            for ln in lines:
                meta = _pharm_meta(ln)
                parts.append(f"""
                  <tr>
                    <td>{h(inv_no)}</td>
                    <td>{h(inv_dt)}</td>
                    <td>{h(getattr(ln, "description", ""))}</td>
                    <td>{h(meta["batch"])}</td>
                    <td>{h(meta["expiry"])}</td>
                    <td>{h(meta["hsn"])}</td>
                    <td class="right">{h(getattr(ln, "qty", ""))}</td>
                    <td class="right">{fmt_inr(getattr(ln, "net_amount", 0))}</td>
                  </tr>
                """)
            parts.append("</tbody></table>")
        else:
            # group by cost center / revenue head / service group
            groups: Dict[str, List[BillingInvoiceLine]] = {}
            for ln in lines:
                gk = _group_key(ln)
                groups.setdefault(gk, []).append(ln)

            for gk in sorted(groups.keys(), key=lambda x: str(x).lower()):
                parts.append(f"""
                  <div class="section-head">{h(gk)}</div>
                  <table class="tbl">
                    <thead>
                      <tr>
                        <th>Particulars</th>
                        <th class="center">Date</th>
                        <th class="right">Quantity</th>
                        <th class="right">Total Amount</th>
                      </tr>
                    </thead>
                    <tbody>
                """)
                for ln in groups[gk]:
                    sdt = fmt_date(
                        getattr(ln, "service_date", None)
                        or getattr(inv, "service_date", None)
                        or getattr(inv, "created_at", None))
                    parts.append(f"""
                      <tr>
                        <td>{h(getattr(ln, "description", ""))}</td>
                        <td class="center">{h(sdt)}</td>
                        <td class="right">{h(getattr(ln, "qty", ""))}</td>
                        <td class="right">{fmt_inr(getattr(ln, "net_amount", 0))}</td>
                      </tr>
                    """)
                parts.append("</tbody></table>")

    return "".join(parts)


def build_full_case_html(db: Session, case_id: int, doc_no: Optional[str],
                         doc_date: Optional[str]) -> str:
    branding = db.query(UiBranding).order_by(UiBranding.id.desc()).first()
    if not branding:
        # minimal fallback
        class _B:  # type: ignore
            org_name = "NUTRYAH"
            tagline = ""
            address = ""
            phone = ""
            email = ""
            gstin = ""

        branding = _B()  # type: ignore

    case = _load_case(db, case_id)
    totals = _calc_totals(case)

    head = _brand_block(branding)
    overview = _overview_html(db, case, totals, doc_no, doc_date)
    details = _details_html(case)

    return f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <style>{_css()}</style>
      </head>
      <body>
        {head}
        {overview}
        {details}
      </body>
    </html>
    """


def render_full_case_pdf_bytes(db: Session,
                               case_id: int,
                               doc_no: Optional[str] = None,
                               doc_date: Optional[str] = None) -> bytes:
    if HTML is None:
        raise RuntimeError(
            "WeasyPrint is not installed. Please install weasyprint to render HTML PDFs."
        )
    html = build_full_case_html(db, case_id, doc_no, doc_date)
    pdf = HTML(string=html).write_pdf(stylesheets=[CSS(
        string=_css())] if CSS else None)
    return pdf
