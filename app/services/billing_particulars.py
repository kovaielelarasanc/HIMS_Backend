from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, List, Optional
import hashlib
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.core.config import settings
from app.models.billing import (
    BillingCase,
    DocStatus,
    PayerType,
    InvoiceType,
    ServiceGroup,
)
from app.models.user import User
from app.models.charge_item_master import ChargeItemMaster

from app.models.ipd import IpdWard, IpdRoom, IpdBed, IpdBedRate
from app.models.opd import LabTest, RadiologyTest, DoctorFee
from app.models.ot import OtProcedure
from app.models.ot_master import OtSurgeryMaster

from app.services.billing_service import (
    BillingError,
    BillingStateError,
    get_or_create_active_module_invoice,
    upsert_auto_line,
    get_tariff_rate,
)


# -----------------------------
# Department options (robust import + fallback)
# -----------------------------
def _get_department_model():
    try:
        from app.models.masters import Department  # type: ignore
        return Department
    except Exception:
        pass
    try:
        from app.models.department import Department  # type: ignore
        return Department
    except Exception:
        pass
    try:
        from app.models.common import Department  # type: ignore
        return Department
    except Exception:
        return None


def department_options(
    db: Session,
    *,
    search: str = "",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    limit = min(max(int(limit or 200), 1), 500)
    s = (search or "").strip().lower()

    Dept = _get_department_model()
    if Dept is not None:
        q = db.query(Dept)

        if hasattr(Dept, "is_active"):
            q = q.filter(getattr(Dept, "is_active").is_(True))

        if s:
            conds = []
            if hasattr(Dept, "name"):
                conds.append(
                    func.lower(func.coalesce(getattr(Dept, "name"),
                                             "")).like(f"%{s}%"))
            if hasattr(Dept, "code"):
                conds.append(
                    func.lower(func.coalesce(getattr(Dept, "code"),
                                             "")).like(f"%{s}%"))
            if conds:
                q = q.filter(or_(*conds))

        if hasattr(Dept, "name"):
            q = q.order_by(getattr(Dept, "name").asc())
        else:
            q = q.order_by(getattr(Dept, "id").asc())

        rows = q.limit(limit).all()
        out = []
        for d in rows:
            did = int(getattr(d, "id"))
            name = (getattr(d, "name", None) or "").strip()
            code = (getattr(d, "code", None) or "").strip()
            label = f"{name} ({code})" if name and code else (
                name or code or f"Department #{did}")
            out.append({"id": did, "label": label})
        return out

    rows = (db.query(User.department_id).filter(
        User.department_id.isnot(None)).distinct().order_by(
            User.department_id.asc()).all())
    return [{
        "id": int(did),
        "label": f"Department #{int(did)}"
    } for (did, ) in rows if did is not None]


# -----------------------------
# Particular Registry
# -----------------------------
@dataclass(frozen=True)
class ParticularMeta:
    code: str
    label: str
    module: str
    service_group: ServiceGroup
    default_invoice_type: InvoiceType
    kind: str  # BED | LAB_TEST | RAD_TEST | OT_PROC | OT_SURGERY | DOCTOR | CHARGE_ITEM | MANUAL


PARTICULARS: List[ParticularMeta] = [
    ParticularMeta("ADM", "ADMISSIONS CHARGES", "ADM", ServiceGroup.MISC,
                   InvoiceType.PATIENT, "CHARGE_ITEM"),
    ParticularMeta("ROOM", "OBSERVATION/BED CHARGES", "ROOM",
                   ServiceGroup.ROOM, InvoiceType.PATIENT, "BED"),
    ParticularMeta("LAB", "CLINICAL LAB CHARGES", "LAB", ServiceGroup.LAB,
                   InvoiceType.PATIENT, "LAB_TEST"),
    ParticularMeta("PHC", "CONSUMBLES & DISPOSABLES CHARGES", "PHC",
                   ServiceGroup.PHARM, InvoiceType.PATIENT, "MANUAL"),
    ParticularMeta("DIET", "DIETRY CHARGES", "DIET", ServiceGroup.MISC,
                   InvoiceType.PATIENT, "CHARGE_ITEM"),
    ParticularMeta("DOC", "DOCTOR CHARGES", "DOC", ServiceGroup.CONSULT,
                   InvoiceType.PATIENT, "DOCTOR"),
    ParticularMeta("PHM", "PHARMACY CHARGES", "PHM", ServiceGroup.PHARM,
                   InvoiceType.PHARMACY, "MANUAL"),
    ParticularMeta("PROC", "PROCEDURES CHARGES", "PROC", ServiceGroup.PROC,
                   InvoiceType.PATIENT, "OT_PROC"),
    ParticularMeta("SCAN", "SCAN CHARGES", "SCAN", ServiceGroup.RAD,
                   InvoiceType.PATIENT, "RAD_TEST"),
    ParticularMeta("SURG", "SURGERY CHARGES", "SURG", ServiceGroup.OT,
                   InvoiceType.PATIENT, "OT_SURGERY"),
    ParticularMeta("XRAY", "X RAY CHARGES", "XRAY", ServiceGroup.RAD,
                   InvoiceType.PATIENT, "RAD_TEST"),
    ParticularMeta("MISC", "MISCELLANEOUS CHARGES", "MISC", ServiceGroup.MISC,
                   InvoiceType.PATIENT, "CHARGE_ITEM"),
    ParticularMeta("BLOOD", "BLOOD BANK", "BLOOD", ServiceGroup.MISC,
                   InvoiceType.PATIENT, "CHARGE_ITEM"),
]

_PART_MAP = {p.code: p for p in PARTICULARS}


def list_particulars_meta() -> Dict[str, Any]:
    return {
        "items": [{
            "code": p.code,
            "label": p.label,
            "module": p.module,
            "service_group": p.service_group.value,
            "invoice_type": p.default_invoice_type.value,
            "kind": p.kind,
        } for p in PARTICULARS]
    }


def _parse_date(dt: Optional[str]) -> Optional[datetime]:
    if not dt:
        return None
    s = (dt or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if len(s) == 10:
            return datetime.fromisoformat(s + "T00:00:00")
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _to_local_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt
    tz = ZoneInfo(getattr(settings, "TIMEZONE", "Asia/Kolkata"))
    return dt.astimezone(tz).replace(tzinfo=None)


def _d(x) -> Decimal:
    if x is None:
        return Decimal("0")
    return Decimal(str(x or "0"))


def _safe_int(x) -> int:
    try:
        return int(x)
    except Exception:
        return 0


def _line_get(line: Dict[str, Any], key: str, default=None):
    return line.get(key, default) if isinstance(line, dict) else default


def _enum_str(x: Any) -> str:
    if hasattr(x, "value"):
        return str(x.value or "")
    return str(x or "")


def _case_ref(case: BillingCase) -> int:
    return int(getattr(case, "id", 0) or 0)


def _mk_idem(case: BillingCase,
             prefix: str,
             item_id: Any,
             key_dt: str,
             suffix: str = "") -> (int, str):
    # DB uniqueness is global: source_module + source_ref_id + source_line_key
    # So: source_ref_id = case.id (unique per case)
    ref = _case_ref(case)
    base = f"CASE:{ref}:{prefix}:{item_id}:{key_dt}"
    k = f"{base}:{suffix}" if suffix else base
    return ref, k[:64]


# -----------------------------
# BED: suggested rate via IpdBedRate using room.type (case-insensitive)
# -----------------------------
def _bed_rate_for_room_type(db: Session, room_type: str,
                            on_date: date) -> Decimal:
    rt = (room_type or "").strip()
    if not rt:
        return Decimal("0")

    q = (db.query(IpdBedRate).filter(
        IpdBedRate.is_active.is_(True),
        func.upper(func.coalesce(IpdBedRate.room_type, "")) == rt.upper(),
        IpdBedRate.effective_from <= on_date,
        or_(IpdBedRate.effective_to.is_(None), IpdBedRate.effective_to
            >= on_date),
    ).order_by(IpdBedRate.effective_from.desc(), IpdBedRate.id.desc()))
    row = q.first()
    return _d(getattr(row, "daily_rate", 0)) if row else Decimal("0")


def bed_options(
    db: Session,
    *,
    case: BillingCase,
    ward_id: Optional[int],
    room_id: Optional[int],
    search: str = "",
    limit: int = 80,
    on_date: Optional[date] = None,
):
    limit = min(max(int(limit or 80), 1), 200)
    s = (search or "").strip().lower()
    on_date = on_date or date.today()

    wards = db.query(IpdWard).filter(IpdWard.is_active.is_(True)).order_by(
        IpdWard.name.asc()).all()
    ward_items = [{
        "id": int(w.id),
        "label": f"{w.name} ({w.code})"
    } for w in wards]

    rooms_q = db.query(IpdRoom).filter(IpdRoom.is_active.is_(True))
    if ward_id:
        rooms_q = rooms_q.filter(IpdRoom.ward_id == int(ward_id))
    rooms = rooms_q.order_by(IpdRoom.number.asc()).all()
    room_items = [{
        "id": int(r.id),
        "label": f"{r.number} · {getattr(r, 'type', '')}"
    } for r in rooms]

    beds_q = (db.query(IpdBed, IpdRoom, IpdWard).join(
        IpdRoom, IpdBed.room_id == IpdRoom.id).join(
            IpdWard,
            IpdRoom.ward_id == IpdWard.id).filter(IpdWard.is_active.is_(True),
                                                  IpdRoom.is_active.is_(True)))

    if ward_id:
        beds_q = beds_q.filter(IpdWard.id == int(ward_id))
    if room_id:
        beds_q = beds_q.filter(IpdRoom.id == int(room_id))

    if s:
        beds_q = beds_q.filter(
            or_(
                func.lower(func.coalesce(IpdBed.code, "")).like(f"%{s}%"),
                func.lower(func.coalesce(IpdRoom.number, "")).like(f"%{s}%"),
                func.lower(func.coalesce(IpdWard.name, "")).like(f"%{s}%"),
            ))

    rows = beds_q.order_by(IpdBed.code.asc()).limit(limit).all()

    bed_items = []
    for bed, room, ward in rows:
        room_type = (getattr(room, "type", None)
                     or getattr(room, "room_type", None) or "") or ""

        suggested = Decimal("0")
        suggested_gst = Decimal("0")
        rate_source = ""

        # 1) Tariff
        try:
            if getattr(case, "tariff_plan_id", None):
                tr, tg = get_tariff_rate(
                    db,
                    tariff_plan_id=case.tariff_plan_id,
                    item_type="BED",
                    item_id=int(bed.id),
                )
                if _d(tr) > 0:
                    suggested = _d(tr)
                    suggested_gst = _d(tg)
                    rate_source = "TARIFF"
        except Exception:
            pass

        # 2) BedRate by room_type
        if suggested <= 0:
            br = _bed_rate_for_room_type(db, room_type, on_date)
            if br > 0:
                suggested = br
                rate_source = "BED_RATE"

        # 3) Room field fallback
        if suggested <= 0:
            for fld in ("daily_rate", "bed_rate", "rate_per_day",
                        "price_per_day"):
                if hasattr(room, fld):
                    v = _d(getattr(room, fld, 0))
                    if v > 0:
                        suggested = v
                        rate_source = f"ROOM.{fld}"
                        break

        bed_items.append({
            "id": int(bed.id),
            "label":
            f"{bed.code} · Ward: {ward.name if ward else '-'} · Room: {room.number if room else '-'}",
            "meta": {
                "ward_id": int(ward.id) if ward else None,
                "room_id": int(room.id) if room else None,
                "room_type": room_type,
                "state": getattr(bed, "state", None),
            },
            "suggested_rate": str(suggested),
            "gst_rate": str(suggested_gst),
            "rate_source": rate_source,
        })

    return {"wards": ward_items, "rooms": room_items, "beds": bed_items}


# -----------------------------
# LAB options
# -----------------------------
def lab_test_options(db: Session, *, search: str = "", limit: int = 80):
    limit = min(max(int(limit or 80), 1), 200)
    s = (search or "").strip().lower()

    q = db.query(LabTest)
    if s:
        q = q.filter(
            or_(
                func.lower(LabTest.code).like(f"%{s}%"),
                func.lower(LabTest.name).like(f"%{s}%")))
    rows = q.order_by(LabTest.name.asc()).limit(limit).all()

    return {
        "tests": [{
            "id": int(x.id),
            "code": x.code,
            "name": x.name,
            "price": str(_d(x.price))
        } for x in rows]
    }


# -----------------------------
# Radiology options
# -----------------------------
def radiology_test_options(db: Session,
                           *,
                           search: str = "",
                           modality: str = "",
                           limit: int = 80):
    limit = min(max(int(limit or 80), 1), 200)
    s = (search or "").strip().lower()
    mod = (modality or "").strip()

    q = db.query(RadiologyTest).filter(RadiologyTest.is_active.is_(True))
    if mod:
        q = q.filter(
            func.upper(func.coalesce(RadiologyTest.modality, "")) ==
            mod.upper())
    if s:
        q = q.filter(
            or_(
                func.lower(RadiologyTest.code).like(f"%{s}%"),
                func.lower(RadiologyTest.name).like(f"%{s}%"),
                func.lower(func.coalesce(RadiologyTest.modality,
                                         "")).like(f"%{s}%"),
            ))
    rows = q.order_by(RadiologyTest.name.asc()).limit(limit).all()

    return {
        "tests": [{
            "id": int(x.id),
            "code": x.code,
            "name": x.name,
            "modality": x.modality,
            "price": str(_d(x.price))
        } for x in rows]
    }


# -----------------------------
# OT Procedure options
# -----------------------------
def ot_procedure_options(db: Session, *, search: str = "", limit: int = 80):
    limit = min(max(int(limit or 80), 1), 200)
    s = (search or "").strip().lower()

    q = db.query(OtProcedure).filter(OtProcedure.is_active.is_(True))
    if s:
        q = q.filter(
            or_(
                func.lower(OtProcedure.code).like(f"%{s}%"),
                func.lower(OtProcedure.name).like(f"%{s}%")))
    rows = q.order_by(OtProcedure.name.asc()).limit(limit).all()

    out = []
    for p in rows:
        out.append({
            "id":
            int(p.id),
            "code":
            p.code,
            "name":
            p.name,
            "default_duration_min":
            getattr(p, "default_duration_min", None),
            "rate_per_hour":
            str(_d(getattr(p, "rate_per_hour", 0))),
            "base_cost":
            str(_d(getattr(p, "base_cost", 0))),
            "anesthesia_cost":
            str(_d(getattr(p, "anesthesia_cost", 0))),
            "surgeon_cost":
            str(_d(getattr(p, "surgeon_cost", 0))),
            "petitory_cost":
            str(_d(getattr(p, "petitory_cost", 0))),
            "asst_doctor_cost":
            str(_d(getattr(p, "asst_doctor_cost", 0))),
            "total_fixed_cost":
            str(_d(getattr(p, "total_fixed_cost", 0))),
        })
    return {"procedures": out}


# -----------------------------
# OT Surgery options
# -----------------------------
def ot_surgery_options(db: Session, *, search: str = "", limit: int = 80):
    limit = min(max(int(limit or 80), 1), 200)
    s = (search or "").strip().lower()

    q = db.query(OtSurgeryMaster).filter(OtSurgeryMaster.active.is_(True))
    if s:
        q = q.filter(
            or_(
                func.lower(OtSurgeryMaster.code).like(f"%{s}%"),
                func.lower(OtSurgeryMaster.name).like(f"%{s}%"),
            ))
    rows = q.order_by(OtSurgeryMaster.name.asc()).limit(limit).all()

    return {
        "surgeries": [{
            "id": int(x.id),
            "code": x.code,
            "name": x.name,
            "default_cost": str(_d(getattr(x, "default_cost", 0))),
            "hourly_cost": str(_d(getattr(x, "hourly_cost", 0))),
        } for x in rows]
    }


# -----------------------------
# Charge items options
# -----------------------------
def charge_item_options(db: Session,
                        *,
                        category: str,
                        search: str = "",
                        limit: int = 80):
    limit = min(max(int(limit or 80), 1), 200)
    cat = (category or "").strip().upper()
    s = (search or "").strip().lower()

    q = db.query(ChargeItemMaster).filter(
        ChargeItemMaster.is_active.is_(True),
        func.upper(ChargeItemMaster.category) == cat,
    )
    if s:
        q = q.filter(
            or_(
                func.lower(ChargeItemMaster.code).like(f"%{s}%"),
                func.lower(ChargeItemMaster.name).like(f"%{s}%")))
    rows = q.order_by(ChargeItemMaster.name.asc()).limit(limit).all()

    return {
        "items": [{
            "id": int(x.id),
            "code": x.code,
            "name": x.name,
            "price": str(_d(getattr(x, "price", 0))),
            "gst_rate": str(_d(getattr(x, "gst_rate", 0))),
        } for x in rows]
    }


# -----------------------------
# Doctor options
# -----------------------------
def doctor_fee_options(
    db: Session,
    *,
    search: str = "",
    limit: int = 80,
    department_id: Optional[int] = None,
):
    limit = min(max(int(limit or 80), 1), 200)
    s = (search or "").strip().lower()

    dept_items = department_options(db, limit=300)

    q = db.query(User).filter(User.is_active.is_(True),
                              User.is_doctor.is_(True))

    if department_id:
        q = q.filter(User.department_id == int(department_id))

    if s:
        q = q.filter(
            or_(
                func.lower(func.coalesce(User.name, "")).like(f"%{s}%"),
                func.lower(func.coalesce(User.email, "")).like(f"%{s}%"),
            ))

    doctors = q.order_by(User.name.asc(), User.id.asc()).limit(limit).all()
    ids = [int(u.id) for u in doctors]

    fee_map: Dict[int, Any] = {}
    try:
        if ids:
            fee_rows = (db.query(DoctorFee).filter(
                DoctorFee.is_active.is_(True),
                DoctorFee.doctor_user_id.in_(ids)).order_by(
                    DoctorFee.doctor_user_id.asc(),
                    getattr(DoctorFee, "id").desc()).all())
            for r in fee_rows:
                duid = int(getattr(r, "doctor_user_id", 0) or 0)
                if duid and duid not in fee_map:
                    fee_map[duid] = r
    except Exception:
        fee_map = {}

    out: List[Dict[str, Any]] = []
    for u in doctors:
        df = fee_map.get(int(u.id))
        out.append({
            "id":
            int(u.id),
            "label": (getattr(u, "full_name", None) or u.name
                      or f"Doctor #{u.id}"),
            "name":
            u.name,
            "email":
            u.email,
            "department_id":
            getattr(u, "department_id", None),
            "base_fee":
            str(_d(getattr(df, "base_fee", 0))) if df else None,
            "followup_fee":
            str(_d(getattr(df, "followup_fee", 0))) if df else None,
        })

    return {"doctors": out, "departments": dept_items}


# -----------------------------
# Options API
# -----------------------------
def get_particular_options(
    db: Session,
    *,
    case: BillingCase,
    code: str,
    ward_id: Optional[int] = None,
    room_id: Optional[int] = None,
    department_id: Optional[int] = None,
    search: str = "",
    modality: str = "",
    limit: int = 80,
    service_date_str: Optional[str] = None,
) -> Dict[str, Any]:
    p = _PART_MAP.get(code)
    if not p:
        raise BillingError(f"Invalid particular '{code}'")

    svc_dt = _to_local_naive(_parse_date(service_date_str))
    on_date = svc_dt.date() if svc_dt else date.today()

    if p.kind == "BED":
        return {
            "particular": {
                "code": p.code,
                "label": p.label,
                "kind": p.kind
            },
            "options":
            bed_options(
                db,
                case=case,
                ward_id=ward_id,
                room_id=room_id,
                search=search,
                limit=limit,
                on_date=on_date,
            ),
        }

    if p.kind == "LAB_TEST":
        return {
            "particular": {
                "code": p.code,
                "label": p.label,
                "kind": p.kind
            },
            "options": lab_test_options(db, search=search, limit=limit)
        }

    if p.kind == "RAD_TEST":
        return {
            "particular": {
                "code": p.code,
                "label": p.label,
                "kind": p.kind
            },
            "options":
            radiology_test_options(db,
                                   search=search,
                                   modality=modality,
                                   limit=limit),
        }

    if p.kind == "OT_PROC":
        return {
            "particular": {
                "code": p.code,
                "label": p.label,
                "kind": p.kind
            },
            "options": ot_procedure_options(db, search=search, limit=limit)
        }

    if p.kind == "OT_SURGERY":
        return {
            "particular": {
                "code": p.code,
                "label": p.label,
                "kind": p.kind
            },
            "options": ot_surgery_options(db, search=search, limit=limit)
        }

    if p.kind == "DOCTOR":
        return {
            "particular": {
                "code": p.code,
                "label": p.label,
                "kind": p.kind
            },
            "options":
            doctor_fee_options(db,
                               search=search,
                               limit=limit,
                               department_id=department_id),
        }

    if p.kind == "CHARGE_ITEM":
        return {
            "particular": {
                "code": p.code,
                "label": p.label,
                "kind": p.kind
            },
            "options":
            charge_item_options(db,
                                category=p.code,
                                search=search,
                                limit=limit),
        }

    return {
        "particular": {
            "code": p.code,
            "label": p.label,
            "kind": p.kind
        },
        "options": {}
    }


# -----------------------------
# Add Lines (FINAL)
# -----------------------------
def add_particular_lines(
    db: Session,
    *,
    case: BillingCase,
    user: Any,
    code: str,
    payer_type: PayerType = PayerType.PATIENT,
    payer_id: Optional[int] = None,
    invoice_type: Optional[InvoiceType] = None,
    service_date_str: Optional[str] = None,
    qty: Decimal = Decimal("1"),
    gst_rate: Decimal = Decimal("0"),
    discount_percent: Decimal = Decimal("0"),
    discount_amount: Decimal = Decimal("0"),
    item_ids: Optional[List[int]] = None,
    doctor_id: Optional[int] = None,
    unit_price: Optional[Decimal] = None,
    description: Optional[str] = None,
    ward_id: Optional[int] = None,
    room_id: Optional[int] = None,
    modality: Optional[str] = None,
    duration_min: Optional[int] = None,
    split_costs: bool = False,
    hours: Optional[Decimal] = None,
    lines: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    p = _PART_MAP.get(code)
    if not p:
        raise BillingError(f"Invalid particular '{code}'")

    st = _enum_str(getattr(case, "status", None)).upper()
    if st in ("CLOSED", "CANCELLED", "CANCELED"):
        raise BillingStateError("Cannot add items to CLOSED/CANCELLED case")

    inv_type = invoice_type or p.default_invoice_type

    inv = get_or_create_active_module_invoice(
        db,
        billing_case_id=int(case.id),
        user=user,
        module=p.module,
        invoice_type=inv_type,
        payer_type=payer_type,
        payer_id=payer_id,
    )

    if inv.status not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError(
            "Invoice is not editable (POSTED/VOID). Open a new invoice first.")

    work: List[Dict[str, Any]] = []
    if lines:
        for ln in lines:
            if isinstance(ln, dict):
                work.append(ln)
    else:
        if item_ids:
            for iid in item_ids:
                work.append({"item_id": int(iid)})
        elif doctor_id:
            work.append({"doctor_id": int(doctor_id)})
        else:
            work.append({})

    added: List[int] = []

    def eff_dt(line: Dict[str, Any]) -> Optional[datetime]:
        dt = _parse_date(_line_get(
            line, "service_date")) or _parse_date(service_date_str)
        return _to_local_naive(dt)

    def eff_qty(line: Dict[str, Any]) -> Decimal:
        return _d(_line_get(line, "qty", qty))

    def eff_gst(line: Dict[str, Any]) -> Decimal:
        v = _line_get(line, "gst_rate", None)
        return _d(v if v is not None else gst_rate)

    def eff_disc_pct(line: Dict[str, Any]) -> Decimal:
        v = _line_get(line, "discount_percent", None)
        return _d(v if v is not None else discount_percent)

    def eff_disc_amt(line: Dict[str, Any]) -> Decimal:
        v = _line_get(line, "discount_amount", None)
        return _d(v if v is not None else discount_amount)

    def eff_price_override(line: Dict[str, Any]) -> Decimal:
        v = _line_get(line, "unit_price", None)
        if v is None:
            return _d(unit_price)
        return _d(v)

    def eff_desc(line: Dict[str, Any], fallback: str) -> str:
        d1 = (_line_get(line, "description") or "").strip()
        if d1:
            return d1
        d0 = (description or "").strip()
        return d0 or fallback

    def line_key_suffix(line: Dict[str, Any]) -> str:
        return (_line_get(line, "line_key") or "").strip()

    # ---------------- BED ----------------
    if p.kind == "BED":
        for ln_in in work:
            bid = _safe_int(_line_get(ln_in, "item_id"))
            if bid <= 0:
                raise BillingError("Each bed line must have item_id")

            bed = db.get(IpdBed, bid)
            if not bed:
                raise BillingError(f"Invalid bed_id {bid}")

            room = db.get(IpdRoom, int(bed.room_id)) if getattr(
                bed, "room_id", None) else None
            ward = db.get(IpdWard, int(room.ward_id)) if room and getattr(
                room, "ward_id", None) else None
            room_type = (getattr(room, "type", None)
                         or getattr(room, "room_type", None) or "") or ""

            svc_dt = eff_dt(ln_in)
            key_dt = (svc_dt.date().isoformat()
                      if svc_dt else date.today().isoformat())

            rate = eff_price_override(ln_in)
            gst = eff_gst(ln_in)

            if rate <= 0:
                tr, tg = get_tariff_rate(db,
                                         tariff_plan_id=case.tariff_plan_id,
                                         item_type="BED",
                                         item_id=bid)
                if _d(tr) > 0:
                    rate = _d(tr)
                    if gst <= 0:
                        gst = _d(tg)

            if rate <= 0:
                rate = _bed_rate_for_room_type(
                    db, room_type, (svc_dt.date() if svc_dt else date.today()))

            if rate <= 0:
                raise BillingError(
                    f"No rate found for bed {bed.code}. Configure IpdBedRate or Tariff."
                )

            suffix = line_key_suffix(ln_in)
            source_ref_id, source_line_key = _mk_idem(case, "BED", bid, key_dt,
                                                      suffix)

            ln = upsert_auto_line(
                db,
                invoice_id=int(inv.id),
                billing_case_id=int(case.id),
                user=user,
                service_group=p.service_group,
                item_type="BED",
                item_id=bid,
                item_code=getattr(bed, "code", None),
                description=eff_desc(
                    ln_in, f"{p.label} - {getattr(bed, 'code', bid)}"),
                qty=eff_qty(ln_in),
                unit_price=rate,
                gst_rate=gst,
                discount_percent=eff_disc_pct(ln_in),
                discount_amount=eff_disc_amt(ln_in),
                source_module=p.module,
                source_ref_id=source_ref_id,
                source_line_key=source_line_key,
                service_date=svc_dt,
                meta_patch={
                    "bed": {
                        "bed_id": bid,
                        "bed_code": getattr(bed, "code", None),
                        "ward_id": int(ward.id) if ward else None,
                        "ward_name":
                        getattr(ward, "name", None) if ward else None,
                        "room_id": int(room.id) if room else None,
                        "room_number":
                        getattr(room, "number", None) if room else None,
                        "room_type": room_type,
                    }
                },
            )
            added.append(int(ln.id))

        return {"invoice_id": int(inv.id), "added_line_ids": added}

    # ---------------- LAB ----------------
    if p.kind == "LAB_TEST":
        for ln_in in work:
            tid = _safe_int(_line_get(ln_in, "item_id"))
            if tid <= 0:
                raise BillingError("Each lab line must have item_id")
            t = db.get(LabTest, tid)
            if not t:
                raise BillingError(f"Invalid lab_test_id {tid}")

            svc_dt = eff_dt(ln_in)
            key_dt = (svc_dt.date().isoformat()
                      if svc_dt else date.today().isoformat())

            rate = eff_price_override(ln_in)
            if rate <= 0:
                rate = _d(getattr(t, "price", 0))
            if rate <= 0:
                raise BillingError(f"Lab test price missing: {t.name}")

            suffix = line_key_suffix(ln_in)
            source_ref_id, source_line_key = _mk_idem(case, "LAB", tid, key_dt,
                                                      suffix)

            ln = upsert_auto_line(
                db,
                invoice_id=int(inv.id),
                billing_case_id=int(case.id),
                user=user,
                service_group=p.service_group,
                item_type="LAB_TEST",
                item_id=tid,
                item_code=getattr(t, "code", None),
                description=eff_desc(ln_in, f"{p.label} - {t.name}"),
                qty=eff_qty(ln_in),
                unit_price=rate,
                gst_rate=eff_gst(ln_in),
                discount_percent=eff_disc_pct(ln_in),
                discount_amount=eff_disc_amt(ln_in),
                source_module=p.module,
                source_ref_id=source_ref_id,
                source_line_key=source_line_key,
                service_date=svc_dt,
                meta_patch={
                    "lab": {
                        "test_id": tid,
                        "code": t.code,
                        "name": t.name
                    }
                },
            )
            added.append(int(ln.id))

        return {"invoice_id": int(inv.id), "added_line_ids": added}

    # ---------------- RAD ----------------
    if p.kind == "RAD_TEST":
        for ln_in in work:
            rid = _safe_int(_line_get(ln_in, "item_id"))
            if rid <= 0:
                raise BillingError("Each radiology line must have item_id")
            r = db.get(RadiologyTest, rid)
            if not r:
                raise BillingError(f"Invalid radiology_test_id {rid}")

            svc_dt = eff_dt(ln_in)
            key_dt = (svc_dt.date().isoformat()
                      if svc_dt else date.today().isoformat())

            rate = eff_price_override(ln_in)
            if rate <= 0:
                rate = _d(getattr(r, "price", 0))
            if rate <= 0:
                raise BillingError(f"Radiology test price missing: {r.name}")

            suffix = line_key_suffix(ln_in)
            source_ref_id, source_line_key = _mk_idem(case, "RAD", rid, key_dt,
                                                      suffix)

            ln = upsert_auto_line(
                db,
                invoice_id=int(inv.id),
                billing_case_id=int(case.id),
                user=user,
                service_group=p.service_group,
                item_type="RAD_TEST",
                item_id=rid,
                item_code=getattr(r, "code", None),
                description=eff_desc(ln_in, f"{p.label} - {r.name}"),
                qty=eff_qty(ln_in),
                unit_price=rate,
                gst_rate=eff_gst(ln_in),
                discount_percent=eff_disc_pct(ln_in),
                discount_amount=eff_disc_amt(ln_in),
                source_module=p.module,
                source_ref_id=source_ref_id,
                source_line_key=source_line_key,
                service_date=svc_dt,
                meta_patch={
                    "radiology": {
                        "test_id": rid,
                        "code": r.code,
                        "name": r.name,
                        "modality": getattr(r, "modality", None)
                    }
                },
            )
            added.append(int(ln.id))

        return {"invoice_id": int(inv.id), "added_line_ids": added}

    # ---------------- DOCTOR ----------------
    if p.kind == "DOCTOR":
        for ln_in in work:
            did = (_safe_int(_line_get(ln_in, "doctor_id"))
                   or _safe_int(_line_get(ln_in, "item_id"))
                   or _safe_int(doctor_id))
            if did <= 0:
                raise BillingError("Select a doctor")

            df = db.query(DoctorFee).filter(
                DoctorFee.doctor_user_id == did,
                DoctorFee.is_active.is_(True)).first()

            svc_dt = eff_dt(ln_in)
            key_dt = (svc_dt.date().isoformat()
                      if svc_dt else date.today().isoformat())

            rate = eff_price_override(ln_in)
            if rate <= 0 and df is not None:
                rate = _d(getattr(df, "base_fee", 0))
            if rate <= 0:
                raise BillingError(
                    "Doctor amount missing (set unit_price or configure DoctorFee)"
                )

            doc = db.get(User, did)
            doc_name = (getattr(doc, "full_name", None)
                        or getattr(doc, "name", None) or f"Doctor #{did}")

            suffix = line_key_suffix(ln_in)
            source_ref_id, source_line_key = _mk_idem(case, "DOC", did, key_dt,
                                                      suffix)

            ln = upsert_auto_line(
                db,
                invoice_id=int(inv.id),
                billing_case_id=int(case.id),
                user=user,
                service_group=p.service_group,
                item_type="DOCTOR_FEE",
                item_id=did,
                item_code=None,
                description=eff_desc(ln_in, f"{p.label} - {doc_name}"),
                qty=eff_qty(ln_in),
                unit_price=rate,
                gst_rate=eff_gst(ln_in),
                discount_percent=eff_disc_pct(ln_in),
                discount_amount=eff_disc_amt(ln_in),
                source_module=p.module,
                source_ref_id=source_ref_id,
                source_line_key=source_line_key,
                doctor_id=did,
                service_date=svc_dt,
                meta_patch={
                    "doctor": {
                        "doctor_id":
                        did,
                        "name":
                        doc_name,
                        "base_fee":
                        str(_d(getattr(df, "base_fee", 0))) if df else None,
                        "followup_fee":
                        str(_d(getattr(df, "followup_fee", 0)))
                        if df else None,
                    }
                },
            )
            added.append(int(ln.id))

        return {"invoice_id": int(inv.id), "added_line_ids": added}

    # ---------------- CHARGE ITEM ----------------
    if p.kind == "CHARGE_ITEM":
        for ln_in in work:
            iid = _safe_int(_line_get(ln_in, "item_id"))
            if iid <= 0:
                raise BillingError("Each charge item line must have item_id")

            it = db.get(ChargeItemMaster, iid)
            if not it or not getattr(it, "is_active", False):
                raise BillingError(f"Invalid charge_item_id {iid}")

            if (getattr(it, "category", "") or "").upper() != p.code:
                raise BillingError("Selected item category mismatch")

            svc_dt = eff_dt(ln_in)
            key_dt = (svc_dt.date().isoformat()
                      if svc_dt else date.today().isoformat())

            rate = eff_price_override(ln_in)
            if rate <= 0:
                rate = _d(getattr(it, "price", 0))

            gst = eff_gst(ln_in)
            if gst <= 0:
                gst = _d(getattr(it, "gst_rate", 0))

            if rate <= 0:
                raise BillingError(f"Price missing for: {it.name}")

            suffix = line_key_suffix(ln_in)
            source_ref_id, source_line_key = _mk_idem(case, "CHG", iid, key_dt,
                                                      suffix)

            ln = upsert_auto_line(
                db,
                invoice_id=int(inv.id),
                billing_case_id=int(case.id),
                user=user,
                service_group=p.service_group,
                item_type="CHARGE_ITEM",
                item_id=iid,
                item_code=getattr(it, "code", None),
                description=eff_desc(ln_in, f"{p.label} - {it.name}"),
                qty=eff_qty(ln_in),
                unit_price=rate,
                gst_rate=gst,
                discount_percent=eff_disc_pct(ln_in),
                discount_amount=eff_disc_amt(ln_in),
                source_module=p.module,
                source_ref_id=source_ref_id,
                source_line_key=source_line_key,
                service_date=svc_dt,
                meta_patch={
                    "charge_item": {
                        "id": iid,
                        "category": it.category,
                        "code": it.code,
                        "name": it.name
                    }
                },
            )
            added.append(int(ln.id))

        return {"invoice_id": int(inv.id), "added_line_ids": added}

    # ---------------- OT PROC ----------------
    if p.kind == "OT_PROC":
        for ln_in in work:
            pid = _safe_int(_line_get(ln_in, "item_id"))
            if pid <= 0:
                raise BillingError("Each procedure line must have item_id")

            proc = db.get(OtProcedure, pid)
            if not proc or not getattr(proc, "is_active", False):
                raise BillingError("Invalid procedure")

            svc_dt = eff_dt(ln_in)
            key_dt = (svc_dt.date().isoformat()
                      if svc_dt else date.today().isoformat())

            mins = _safe_int(
                _line_get(
                    ln_in, "duration_min", duration_min
                    or getattr(proc, "default_duration_min", 0)))
            split = bool(_line_get(ln_in, "split_costs", split_costs))

            rate = eff_price_override(ln_in)
            if rate <= 0:
                fixed = _d(getattr(proc, "total_fixed_cost", 0))
                if fixed > 0:
                    rate = fixed
                else:
                    hr = _d(getattr(proc, "rate_per_hour", 0))
                    if hr <= 0 or mins <= 0:
                        raise BillingError(
                            "Procedure price missing: set fixed cost or rate_per_hour + duration"
                        )
                    rate = (hr * Decimal(str(mins))) / Decimal("60")

            suffix = line_key_suffix(ln_in)
            source_ref_id, source_line_key = _mk_idem(case, "PROC", pid,
                                                      key_dt, suffix)

            if not split:
                ln = upsert_auto_line(
                    db,
                    invoice_id=int(inv.id),
                    billing_case_id=int(case.id),
                    user=user,
                    service_group=p.service_group,
                    item_type="OT_PROCEDURE",
                    item_id=pid,
                    item_code=getattr(proc, "code", None),
                    description=eff_desc(ln_in, f"{p.label} - {proc.name}"),
                    qty=eff_qty(ln_in),
                    unit_price=rate,
                    gst_rate=eff_gst(ln_in),
                    discount_percent=eff_disc_pct(ln_in),
                    discount_amount=eff_disc_amt(ln_in),
                    source_module=p.module,
                    source_ref_id=source_ref_id,
                    source_line_key=source_line_key,
                    service_date=svc_dt,
                    meta_patch={
                        "procedure": {
                            "id": pid,
                            "code": proc.code,
                            "name": proc.name,
                            "duration_min": mins
                        }
                    },
                )
                added.append(int(ln.id))
            else:
                parts = [
                    ("BASE", _d(getattr(proc, "base_cost", 0))),
                    ("ANESTHESIA", _d(getattr(proc, "anesthesia_cost", 0))),
                    ("SURGEON", _d(getattr(proc, "surgeon_cost", 0))),
                    ("PETITORY", _d(getattr(proc, "petitory_cost", 0))),
                    ("ASST", _d(getattr(proc, "asst_doctor_cost", 0))),
                ]
                any_added = False
                for tag, amt in parts:
                    if amt <= 0:
                        continue
                    # tag is part of idempotency key
                    _, k = _mk_idem(case, "PROC", pid, key_dt,
                                    f"{suffix}:{tag}" if suffix else tag)
                    ln = upsert_auto_line(
                        db,
                        invoice_id=int(inv.id),
                        billing_case_id=int(case.id),
                        user=user,
                        service_group=p.service_group,
                        item_type=f"OT_PROC_{tag}",
                        item_id=pid,
                        item_code=getattr(proc, "code", None),
                        description=
                        f"{eff_desc(ln_in, f'{p.label} - {proc.name}')} ({tag})",
                        qty=Decimal("1"),
                        unit_price=amt,
                        gst_rate=eff_gst(ln_in),
                        discount_percent=Decimal("0"),
                        discount_amount=Decimal("0"),
                        source_module=p.module,
                        source_ref_id=source_ref_id,
                        source_line_key=k,
                        service_date=svc_dt,
                        meta_patch={
                            "procedure": {
                                "id": pid,
                                "code": proc.code,
                                "name": proc.name,
                                "split": True
                            }
                        },
                    )
                    added.append(int(ln.id))
                    any_added = True
                if not any_added:
                    raise BillingError(
                        "No split costs found to add (all costs are 0)")

        return {"invoice_id": int(inv.id), "added_line_ids": added}

    # ---------------- OT SURGERY ----------------
    if p.kind == "OT_SURGERY":
        for ln_in in work:
            sid = _safe_int(_line_get(ln_in, "item_id"))
            if sid <= 0:
                raise BillingError("Each surgery line must have item_id")

            srg = db.get(OtSurgeryMaster, sid)
            if not srg or not getattr(srg, "active", False):
                raise BillingError("Invalid surgery")

            svc_dt = eff_dt(ln_in)
            key_dt = (svc_dt.date().isoformat()
                      if svc_dt else date.today().isoformat())

            rate = eff_price_override(ln_in)
            if rate <= 0:
                hrs = _d(_line_get(ln_in, "hours", hours))
                if hrs > 0 and _d(getattr(srg, "hourly_cost", 0)) > 0:
                    rate = _d(getattr(srg, "hourly_cost", 0)) * hrs
                else:
                    rate = _d(getattr(srg, "default_cost", 0))

            if rate <= 0:
                raise BillingError("Surgery price missing")

            suffix = line_key_suffix(ln_in)
            source_ref_id, source_line_key = _mk_idem(case, "SURG", sid,
                                                      key_dt, suffix)

            ln = upsert_auto_line(
                db,
                invoice_id=int(inv.id),
                billing_case_id=int(case.id),
                user=user,
                service_group=p.service_group,
                item_type="OT_SURGERY",
                item_id=sid,
                item_code=getattr(srg, "code", None),
                description=eff_desc(ln_in, f"{p.label} - {srg.name}"),
                qty=eff_qty(ln_in),
                unit_price=rate,
                gst_rate=eff_gst(ln_in),
                discount_percent=eff_disc_pct(ln_in),
                discount_amount=eff_disc_amt(ln_in),
                source_module=p.module,
                source_ref_id=source_ref_id,
                source_line_key=source_line_key,
                service_date=svc_dt,
                meta_patch={
                    "surgery": {
                        "id": sid,
                        "code": srg.code,
                        "name": srg.name
                    }
                },
            )
            added.append(int(ln.id))

        return {"invoice_id": int(inv.id), "added_line_ids": added}

    # ---------------- MANUAL fallback ----------------
    for ln_in in work:
        svc_dt = eff_dt(ln_in)
        key_dt = (svc_dt.date().isoformat()
                  if svc_dt else date.today().isoformat())

        desc = eff_desc(ln_in, p.label)
        rate = eff_price_override(ln_in)
        if rate <= 0:
            raise BillingError("Amount is required")

        # safer than desc[:20]
        h = hashlib.sha1(desc.encode("utf-8")).hexdigest()[:10]
        base_key = f"SVC:{code}:{key_dt}:{h}"
        suffix = line_key_suffix(ln_in)
        source_line_key = f"{base_key}:{suffix}" if suffix else base_key

        ln = upsert_auto_line(
            db,
            invoice_id=int(inv.id),
            billing_case_id=int(case.id),
            user=user,
            service_group=p.service_group,
            item_type="SERVICE",
            item_id=None,
            item_code=None,
            description=desc,
            qty=eff_qty(ln_in),
            unit_price=rate,
            gst_rate=eff_gst(ln_in),
            discount_percent=eff_disc_pct(ln_in),
            discount_amount=eff_disc_amt(ln_in),
            source_module=p.module,
            source_ref_id=int(inv.id),
            source_line_key=source_line_key[:64],
            service_date=svc_dt,
            meta_patch={"particular": {
                "code": p.code,
                "label": p.label
            }},
        )
        added.append(int(ln.id))

    return {"invoice_id": int(inv.id), "added_line_ids": added}
