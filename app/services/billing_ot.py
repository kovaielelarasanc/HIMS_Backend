# FILE: app/services/billing_ot.py
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Tuple

from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.billing import (
    BillingCase,
    BillingInvoice,
    EncounterType,
    BillingCaseStatus,
    InvoiceType,
    PayerType,
    DocStatus,
    ServiceGroup,
)

# --- Billing engine helpers (your new engine) ---
from app.services.billing_service import (
    create_invoice,
    add_auto_line_idempotent,
    get_tariff_rate,
    BillingError,
)

# In some projects BillingStateError may not exist; keep safe
try:
    from app.services.billing_service import BillingStateError  # type: ignore
except Exception:  # noqa

    class BillingStateError(BillingError):
        pass


from app.models.ot import OtCase, OtSchedule, OtScheduleProcedure, OtProcedure
from app.models.ot_master import (
    OtTheaterMaster,
    OtSurgeryMaster,
    OtInstrumentMaster,
    OtDeviceMaster,
)

# These models may live in different file names in your project.
# Keep imports flexible to avoid ImportError.
try:
    from app.models.ot_anaesthesia import AnaesthesiaRecord, AnaesthesiaDeviceUse  # type: ignore
except Exception:  # noqa
    try:
        from app.models.ot_anaesthesia_record import AnaesthesiaRecord, AnaesthesiaDeviceUse  # type: ignore
    except Exception:  # noqa
        # final fallback if you placed them in app.models.ot
        from app.models.ot import AnaesthesiaRecord, AnaesthesiaDeviceUse  # type: ignore

try:
    from app.models.ot_counts import OtCaseInstrumentCountLine  # type: ignore
except Exception:  # noqa
    try:
        from app.models.ot_instrument_counts import OtCaseInstrumentCountLine  # type: ignore
    except Exception:  # noqa
        # if you defined it in same module
        from app.models.ot import OtCaseInstrumentCountLine  # type: ignore


def _d(x) -> Decimal:
    try:
        return Decimal(str(x or "0"))
    except Exception:
        return Decimal("0")


def _hours_between(start: datetime, end: datetime) -> Decimal:
    secs = Decimal(str((end - start).total_seconds()))
    hrs = (secs / Decimal("3600")).quantize(Decimal("0.01"))
    return hrs if hrs > 0 else Decimal("0")


def _get_billable_window(
        case: OtCase,
        sched: OtSchedule) -> Tuple[Optional[datetime], Optional[datetime]]:
    # prefer actual surgery time
    if case.actual_start_time and case.actual_end_time:
        return case.actual_start_time, case.actual_end_time

    # fallback planned slot
    if sched and sched.date and sched.planned_start_time:
        start = datetime.combine(sched.date, sched.planned_start_time)
        if sched.planned_end_time:
            end = datetime.combine(sched.date, sched.planned_end_time)
        else:
            end = start + timedelta(hours=1)
        return start, end

    return None, None


def _ensure_encounter_type_ot() -> EncounterType:
    enc = getattr(EncounterType, "OT", None)
    if enc is None:
        raise BillingError(
            "EncounterType.OT is missing. Add OT to EncounterType enum in app.models.billing."
        )
    return enc


def _ensure_service_group_ot() -> ServiceGroup:
    sg = getattr(ServiceGroup, "OT", None) or getattr(ServiceGroup, "SURGERY",
                                                      None)
    if sg is None:
        raise BillingError(
            "ServiceGroup.OT (or ServiceGroup.SURGERY) is missing. Add it in app.models.billing."
        )
    return sg


def get_or_create_case_for_ot_case(db: Session, *, case_id: int,
                                   user) -> BillingCase:
    enc_ot = _ensure_encounter_type_ot()

    case = (db.query(BillingCase).filter(
        BillingCase.encounter_type == enc_ot,
        BillingCase.encounter_id == int(case_id)).first())
    if case:
        return case

    ot_case = (db.query(OtCase).options(joinedload(
        OtCase.schedule)).filter(OtCase.id == int(case_id)).first())
    if not ot_case or not ot_case.schedule or not ot_case.schedule.patient_id:
        raise BillingError("OT case not linked to schedule/patient")

    tenant_id = getattr(user, "tenant_id", None) or getattr(
        user, "hospital_id", None)

    tariff_plan_id = None
    if getattr(ot_case.schedule, "admission", None):
        tariff_plan_id = getattr(ot_case.schedule.admission, "tariff_plan_id",
                                 None)

    case = BillingCase(
        tenant_id=tenant_id,
        patient_id=int(ot_case.schedule.patient_id),
        encounter_type=enc_ot,
        encounter_id=int(case_id),
        status=BillingCaseStatus.OPEN,
        payer_mode=None,
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
        case_number="TEMP",
        tariff_plan_id=tariff_plan_id,
    )
    db.add(case)
    db.flush()
    return case


def _find_latest_active_invoice(
        db: Session, *, billing_case_id: int) -> Optional[BillingInvoice]:
    return (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(billing_case_id),
        BillingInvoice.status
        != DocStatus.VOID).order_by(BillingInvoice.id.desc()).first())


def _pick_procedures_to_bill(sched: OtSchedule) -> List[OtProcedure]:
    procs: List[OtProcedure] = []

    links = list(getattr(sched, "procedures", []) or [])
    if links:
        for link in links:
            p = getattr(link, "procedure", None)
            if p:
                procs.append(p)
    else:
        if getattr(sched, "primary_procedure", None):
            procs.append(sched.primary_procedure)

    uniq: List[OtProcedure] = []
    seen = set()
    for p in procs:
        if p.id not in seen:
            uniq.append(p)
            seen.add(p.id)
    return uniq


def _instrument_bill_qty(line: OtCaseInstrumentCountLine) -> int:
    """
    Default billing logic:
      billable_qty = initial_qty + added_qty
    If you want strict “consumed/missing” billing:
      return max(0, (initial + added) - final)
    """
    initial_ = int(getattr(line, "initial_qty", 0) or 0)
    added_ = int(getattr(line, "added_qty", 0) or 0)
    final_ = int(getattr(line, "final_qty", 0) or 0)

    # choose ONE rule:
    return max(0, initial_ + added_)            # ✅ opened/used billing
    # return max(0, (initial_ + added_) - final_)  # ✅ missing/consumed billing


def create_ot_invoice_items_for_case(db: Session, *, case_id: int, user):
    """
    ✅ NEW BILLING ENGINE OT billing (idempotent):
      - BillingCase(encounter_type=OT, encounter_id=case_id)
      - One BillingInvoice for that case (draft/approved; never mutate posted)
      - Lines:
         1) OT Theater hourly cost (hours * theater.cost_per_hour) [tariff override supported]
         2) OT Procedure:
             - If tariff exists for OT_PROC -> one fixed line
             - Else if total_fixed_cost > 0 -> split lines
             - Else fallback hourly (hours * rate_per_hour)
         3) Anaesthesia devices used (qty * device.cost) [tariff override OT_DEVICE]
         4) Instruments usage (initial+added) * cost_per_qty [tariff override OT_INSTRUMENT]
    """
    sg_ot = _ensure_service_group_ot()

    ot_case = (
        db.query(OtCase).options(
            joinedload(OtCase.schedule).joinedload(OtSchedule.theater),
            joinedload(OtCase.schedule).joinedload(
                OtSchedule.primary_procedure),
            selectinload(OtCase.schedule).selectinload(
                OtSchedule.procedures).joinedload(
                    OtScheduleProcedure.procedure),
            joinedload(OtCase.schedule).joinedload(OtSchedule.admission),
            joinedload(OtCase.anaesthesia_record),  # may exist
        ).filter(OtCase.id == int(case_id)).first())
    if not ot_case:
        raise BillingError("OT case not found")

    sched = ot_case.schedule
    if not sched or not sched.patient_id:
        raise BillingError("OT case not linked to schedule/patient")

    start, end = _get_billable_window(ot_case, sched)
    if not start or not end or end <= start:
        raise BillingError("OT timings missing/invalid, cannot bill")

    hours = _hours_between(start, end)
    if hours <= 0:
        raise BillingError("OT duration is zero, cannot bill")

    # 1) case
    bcase = get_or_create_case_for_ot_case(db, case_id=ot_case.id, user=user)

    # 2) invoice
    inv = _find_latest_active_invoice(db, billing_case_id=bcase.id)
    if not inv:
        inv = create_invoice(
            db,
            billing_case_id=bcase.id,
            user=user,
            invoice_type=InvoiceType.PATIENT,
            payer_type=PayerType.PATIENT,
            payer_id=None,
            encounter_type="OT",
        )

    # do not mutate posted invoice
    if inv.status == DocStatus.POSTED:
        return inv

    if inv.status not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError(
            "Can add OT lines only in DRAFT/APPROVED invoice")

    tariff_plan_id = getattr(bcase, "tariff_plan_id", None)

    # -------------------------------------------------
    # A) THEATER HOURLY CHARGES
    # -------------------------------------------------
    theater: OtTheaterMaster | None = getattr(sched, "theater", None)
    if theater and getattr(theater, "is_active", True):
        rate, gst = get_tariff_rate(
            db,
            tariff_plan_id=tariff_plan_id,
            item_type="OT_THEATER",
            item_id=int(theater.id),
        )
        unit_price = rate if _d(rate) > 0 else _d(
            getattr(theater, "cost_per_hour", 0))
        if unit_price > 0:
            add_auto_line_idempotent(
                db,
                invoice_id=inv.id,
                billing_case_id=bcase.id,
                user=user,
                service_group=sg_ot,
                item_type="OT_THEATER",
                item_id=int(theater.id),
                description=
                f"OT Theater Charges — {getattr(theater, 'name', 'Theater')} ({float(hours):.2f} hr)",
                qty=hours,
                unit_price=unit_price,
                gst_rate=_d(gst),
                source_module="OT",
                source_ref_id=int(ot_case.id),
                source_line_key=f"THEATER:{int(theater.id)}",
                doctor_id=getattr(sched, "surgeon_user_id", None),
                is_manual=False,
            )

    # -------------------------------------------------
    # B) PROCEDURE CHARGES
    # -------------------------------------------------
    procedures = _pick_procedures_to_bill(sched)

    # fallback to SurgeryMaster if no procedures linked
    surgery_master: OtSurgeryMaster | None = None
    if not procedures and (sched.procedure_name or "").strip():
        surgery_master = (db.query(OtSurgeryMaster).filter(
            OtSurgeryMaster.active.is_(True),
            OtSurgeryMaster.name == (sched.procedure_name
                                     or "").strip()).first())

    if procedures:
        for proc in procedures:
            # tariff override: fixed package
            rate, gst = get_tariff_rate(
                db,
                tariff_plan_id=tariff_plan_id,
                item_type="OT_PROC",
                item_id=int(proc.id),
            )

            if _d(rate) > 0:
                add_auto_line_idempotent(
                    db,
                    invoice_id=inv.id,
                    billing_case_id=bcase.id,
                    user=user,
                    service_group=sg_ot,
                    item_type="OT_PROC",
                    item_id=int(proc.id),
                    description=f"OT Procedure Package — {proc.name}",
                    qty=Decimal("1"),
                    unit_price=_d(rate),
                    gst_rate=_d(gst),
                    source_module="OT",
                    source_ref_id=int(ot_case.id),
                    source_line_key=f"PROC:{int(proc.id)}:PKG",
                    doctor_id=getattr(sched, "surgeon_user_id", None),
                    is_manual=False,
                )
                continue

            # split fixed cost (preferred if present)
            total_fixed = _d(getattr(proc, "total_fixed_cost", 0))
            if total_fixed <= 0:
                total_fixed = (_d(getattr(proc, "base_cost", 0)) +
                               _d(getattr(proc, "anesthesia_cost", 0)) +
                               _d(getattr(proc, "surgeon_cost", 0)) +
                               _d(getattr(proc, "petitory_cost", 0)) +
                               _d(getattr(proc, "asst_doctor_cost", 0)))

            if total_fixed > 0:
                parts = [
                    ("BASE", "Base OT Cost", _d(getattr(proc, "base_cost",
                                                        0))),
                    ("ANES", "Anaesthesia Charges",
                     _d(getattr(proc, "anesthesia_cost", 0))),
                    ("SURG", "Surgeon Charges",
                     _d(getattr(proc, "surgeon_cost", 0))),
                    ("PET", "Petitory Charges",
                     _d(getattr(proc, "petitory_cost", 0))),
                    ("ASST", "Assistant Doctor Charges",
                     _d(getattr(proc, "asst_doctor_cost", 0))),
                ]
                for code, label, amt in parts:
                    if amt <= 0:
                        continue
                    add_auto_line_idempotent(
                        db,
                        invoice_id=inv.id,
                        billing_case_id=bcase.id,
                        user=user,
                        service_group=sg_ot,
                        item_type="OT_PROC_COMPONENT",
                        item_id=int(proc.id),
                        description=f"{label} — {proc.name}",
                        qty=Decimal("1"),
                        unit_price=amt,
                        gst_rate=Decimal("0"),
                        source_module="OT",
                        source_ref_id=int(ot_case.id),
                        source_line_key=f"PROC:{int(proc.id)}:{code}",
                        doctor_id=getattr(sched, "surgeon_user_id", None),
                        is_manual=False,
                    )
                continue

            # fallback hourly
            rate_per_hour = _d(getattr(proc, "rate_per_hour", 0))
            if rate_per_hour > 0:
                add_auto_line_idempotent(
                    db,
                    invoice_id=inv.id,
                    billing_case_id=bcase.id,
                    user=user,
                    service_group=sg_ot,
                    item_type="OT_PROC_HOURLY",
                    item_id=int(proc.id),
                    description=
                    f"OT Procedure (Hourly) — {proc.name} ({float(hours):.2f} hr)",
                    qty=hours,
                    unit_price=rate_per_hour,
                    gst_rate=Decimal("0"),
                    source_module="OT",
                    source_ref_id=int(ot_case.id),
                    source_line_key=f"PROC:{int(proc.id)}:HOURS",
                    doctor_id=getattr(sched, "surgeon_user_id", None),
                    is_manual=False,
                )

    elif surgery_master:
        pkg = _d(getattr(surgery_master, "default_cost", 0))
        hourly = _d(getattr(surgery_master, "hourly_cost", 0))
        if pkg > 0:
            add_auto_line_idempotent(
                db,
                invoice_id=inv.id,
                billing_case_id=bcase.id,
                user=user,
                service_group=sg_ot,
                item_type="OT_SURGERY_MASTER",
                item_id=int(surgery_master.id),
                description=f"OT Surgery Package — {surgery_master.name}",
                qty=Decimal("1"),
                unit_price=pkg,
                gst_rate=Decimal("0"),
                source_module="OT",
                source_ref_id=int(ot_case.id),
                source_line_key=f"SM:{int(surgery_master.id)}:PKG",
                doctor_id=getattr(sched, "surgeon_user_id", None),
                is_manual=False,
            )
        if hourly > 0:
            add_auto_line_idempotent(
                db,
                invoice_id=inv.id,
                billing_case_id=bcase.id,
                user=user,
                service_group=sg_ot,
                item_type="OT_SURGERY_MASTER_HOURLY",
                item_id=int(surgery_master.id),
                description=
                f"OT Surgery (Hourly) — {surgery_master.name} ({float(hours):.2f} hr)",
                qty=hours,
                unit_price=hourly,
                gst_rate=Decimal("0"),
                source_module="OT",
                source_ref_id=int(ot_case.id),
                source_line_key=f"SM:{int(surgery_master.id)}:HOURS",
                doctor_id=getattr(sched, "surgeon_user_id", None),
                is_manual=False,
            )

    # -------------------------------------------------
    # C) ANAESTHESIA DEVICES USED (AnaesthesiaDeviceUse)
    # -------------------------------------------------
    anaes = (db.query(AnaesthesiaRecord).options(
        selectinload(AnaesthesiaRecord.devices).joinedload(
            AnaesthesiaDeviceUse.device)).filter(
                AnaesthesiaRecord.case_id == int(ot_case.id)).first())

    if anaes and getattr(anaes, "devices", None):
        for use in (anaes.devices or []):
            dev: OtDeviceMaster | None = getattr(use, "device", None)
            dev_id = getattr(use, "device_id", None)

            if not dev_id:
                continue

            qty_i = int(getattr(use, "qty", 1) or 1)
            if qty_i <= 0:
                continue

            # tariff override for device
            rate, gst = get_tariff_rate(
                db,
                tariff_plan_id=tariff_plan_id,
                item_type="OT_DEVICE",
                item_id=int(dev_id),
            )

            unit_price = _d(rate) if _d(rate) > 0 else _d(
                getattr(dev, "cost", 0))
            if unit_price <= 0:
                continue

            dev_name = (getattr(dev, "name", None) or "OT Device")
            cat = (getattr(dev, "category", None) or "").strip()
            label = f"{dev_name}" + (f" ({cat})" if cat else "")

            add_auto_line_idempotent(
                db,
                invoice_id=inv.id,
                billing_case_id=bcase.id,
                user=user,
                service_group=sg_ot,
                item_type="OT_DEVICE",
                item_id=int(dev_id),
                description=f"Anaesthesia Device — {label}",
                qty=Decimal(str(qty_i)),
                unit_price=unit_price,
                gst_rate=_d(gst),
                source_module="OT",
                source_ref_id=int(anaes.id),
                source_line_key=f"ANES_DEV:{int(dev_id)}",
                doctor_id=getattr(anaes, "anaesthetist_user_id", None),
                is_manual=False,
            )

    # -------------------------------------------------
    # D) INSTRUMENT USAGE (OtCaseInstrumentCountLine)
    # -------------------------------------------------
    inst_lines = (
        db.query(OtCaseInstrumentCountLine).options(
            joinedload(OtCaseInstrumentCountLine.instrument)
        )  # if relationship exists
        .filter(OtCaseInstrumentCountLine.case_id == int(ot_case.id)).all())

    for ln in (inst_lines or []):
        inst_id = getattr(ln, "instrument_id", None)
        if not inst_id:
            # if instrument not mapped to master, you can skip or bill later as manual
            continue

        qty_i = _instrument_bill_qty(ln)
        if qty_i <= 0:
            continue

        # load instrument master (in case relationship isn't defined)
        inst: OtInstrumentMaster | None = getattr(ln, "instrument", None)
        if inst is None:
            inst = db.get(OtInstrumentMaster, int(inst_id))

        # tariff override for instrument
        rate, gst = get_tariff_rate(
            db,
            tariff_plan_id=tariff_plan_id,
            item_type="OT_INSTRUMENT",
            item_id=int(inst_id),
        )

        unit_price = _d(rate) if _d(rate) > 0 else _d(
            getattr(inst, "cost_per_qty", 0))
        if unit_price <= 0:
            continue

        name = (getattr(inst, "name", None)
                or getattr(ln, "instrument_name", None)
                or "Instrument").strip()
        uom = (getattr(inst, "uom", None) or getattr(ln, "uom", None)
               or "Nos").strip()

        add_auto_line_idempotent(
            db,
            invoice_id=inv.id,
            billing_case_id=bcase.id,
            user=user,
            service_group=sg_ot,
            item_type="OT_INSTRUMENT",
            item_id=int(inst_id),
            description=f"OT Instrument Usage — {name} ({uom})",
            qty=Decimal(str(qty_i)),
            unit_price=unit_price,
            gst_rate=_d(gst),
            source_module="OT",
            source_ref_id=int(ot_case.id),
            source_line_key=f"INST:{int(inst_id)}",
            doctor_id=getattr(sched, "surgeon_user_id", None),
            is_manual=False,
        )

    db.flush()
    return inv
