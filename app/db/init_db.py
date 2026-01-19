# app/db/init_db.py
from __future__ import annotations

import argparse

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.db.base_master import MasterBase
from app.db.base import Base
from app.db.session import master_engine, get_or_create_tenant_engine


def print_tables(conn):
    rows = conn.exec_driver_sql("SHOW TABLES").fetchall()
    names = [r[0] for r in rows]
    print("Existing tables:", names)
    return set(names)


def seed_permissions(db: Session) -> None:
    """
    Seed ONLY missing permission codes into a TENANT DB; safe to run multiple times.
    Supports two formats inside MODULES:
      1) ("module.name", ["view","create"])
      2) "module.name.action"   (full permission code)
    """

    MODULES = [
        # -------- CORE / ADMIN ----------
        ("departments", ["view", "create", "update", "delete"]),
        ("roles", ["view", "create", "update", "delete"]),
        ("permissions", ["view", "create", "update", "delete"]),
        ("users", ["view", "create", "update", "delete"]),
        ("doctors", ["view"]),

        # -------- PATIENTS ----------
        ("patients", ["view", "create", "update", "deactivate"]),
        ("patients.addresses", ["view", "create", "update", "delete"]),
        ("patients.consents", ["view", "create"]),
        ("patients.attachments", ["manage"]),
        ("patients.masters", ["view", "manage"]),

        # -------- OPD ----------
        ("schedules", ["manage"]),
        ("appointments", ["view", "create", "update", "cancel"]),
        ("vitals", ["create"]),
        ("visits", ["view", "create", "update"]),
        ("prescriptions", ["create", "esign"]),
        ("orders.lab", ["create", "view"]),
        ("orders.ris", ["create", "view"]),
        ("opd.queue", ["view", "manage"]),
        ("opd.followups", ["view", "manage"]),

        # -------- IPD ----------
        ("ipd", ["view", "manage", "doctor", "nursing"]),
        ("ipd.masters", ["manage"]),
        ("ipd.beds", ["view", "manage", "reserve", "release"]),
        ("ipd.bedrates", ["view", "manage"]),
        ("ipd.packages", ["view", "manage"]),
        ("ipd.nursing", ["view", "create", "update", "manage"]),
        ("ipd.newborn", [
            "view", "create", "update", "verify", "finalize", "void", "print",
            "manage"
        ]),
        ("ipd.admissions",
         ["view", "create", "update", "cancel", "transfer", "discharge"]),
        ("ipd.tracking", ["view"]),
        ("ipd.my", ["view"]),
        ("ipd.discharged", ["view"]),
        ("ipd.bedboard", ["view"]),
        ("ipd.vitals", ["view", "create", "update"]),
        ("ipd.nursing_notes", ["view", "create", "update"]),
        ("ipd.io", ["view", "create", "update"]),
        ("ipd.assessments", ["view", "create", "update"]),
        ("ipd.meds", [
            "view", "order", "update", "regenerate", "mark", "meta", "iv",
            "nurse_rows", "doctor_auth", "pdf"
        ]),
        ("ipd.discharges", [
            "view", "summary", "checklist", "medications", "queue",
            "mark_status", "push_abha", "pdf"
        ]),
        ("ipd.referrals", [
            "view", "create", "accept", "decline", "respond", "close",
            "cancel", "edit", "manage"
        ]),
        ("ipd.referrals.audit", ["view"]),
        ("ipd.transfers",
         ["view", "create", "approve", "complete", "cancel", "manage"]),
        ("ipd.dressing", ["create", "view", "update"]),
        ("ipd.icu", ["create", "view", "update"]),
        ("ipd.isolation", ["create", "view", "update", "stop"]),
        ("ipd.restraints", ["create", "view", "update", "monitor", "stop"]),
        ("ipd.transfusion", ["create", "view", "update"]),

        # -------- Inventory / Pharmacy ----------
        ("pharmacy.inventory.locations", ["view", "manage"]),
        ("pharmacy.inventory.suppliers", ["view", "manage"]),
        ("pharmacy.inventory.items", ["view", "manage"]),
        ("pharmacy.inventory.stock", ["view"]),
        ("pharmacy.inventory.alerts", ["view"]),
        ("pharmacy.inventory.po", ["view", "manage", "approve", "cancel"]),
        ("pharmacy.inventory.grn", ["view", "manage"]),
        ("pharmacy.inventory.returns", ["view", "manage"]),
        ("pharmacy.inventory", ["dispense", "view"]),
        ("pharmacy.inventory.txns", ["view"]),
        ("pharmacy.batch_picks", ["view"]),
        ("pharmacy.accounts.supplier_ledger", ["view", "manage", "export"]),
        ("pharmacy.accounts.supplier_payments", ["view", "manage", "export"]),
        ("pharmacy.accounts.supplier_invoices", ["view", "manage", "export"]),
        ("inventory.indents",
         ["view", "create", "update", "submit", "approve", "cancel"]),
        ("inventory.issues", ["view", "create", "update", "post", "cancel"]),
        ("inventory.catalog", ["view"]),
        ("inventory.locations", ["view"]),
        ("inventory.items", ["view"]),
        ("inventory.stock", ["view"]),
        ("inventory.batches", ["view"]),
        ("inventory.consume", ["create", "view"]),
        ("inventory", ["manage", "view"]),
        ("inventory.reconcile", ["create"]),

        # -------- LIS ----------
        ("lab.masters", ["view", "manage"]),
        ("lab.orders", ["create", "view"]),
        ("lab.samples", ["collect"]),
        ("lab.results", ["enter", "validate", "report"]),
        ("lab.attachments", ["add"]),
        ("lab.devices", ["view", "manage"]),
        ("lab.device_results", ["review", "import"]),
        ("lab.device_logs", ["view"]),
        ("lab.integration", ["view", "manage"]),
        ("lis.masters.departments", ["view", "create", "update", "delete"]),
        ("lis.masters.services", ["view", "create", "update", "delete"]),

        # -------- RIS ----------
        ("radiology.masters", ["view", "manage"]),
        ("radiology.orders", ["create", "view"]),
        ("radiology.schedule", ["manage"]),
        ("radiology.scan", ["update"]),
        ("radiology.report", ["create", "approve"]),
        ("radiology.attachments", ["add"]),

        # -------- OT ----------
        ("ot.masters", ["view", "create", "update", "delete", "manage"]),
        ("ot.specialities", ["view", "create", "update", "delete"]),
        ("ot.schedule",
         ["view", "create", "update", "delete", "cancel", "manage"]),
        ("ot.cases", ["view", "create", "update", "delete", "close"]),
        ("ot.pre_anaesthesia", ["view", "create", "update"]),
        ("ot.preop_checklist", ["view", "create", "update"]),
        ("ot.safety", ["view", "create", "update", "manage"]),
        ("ot.anaesthesia_record", ["view", "create", "update"]),
        ("ot.anaesthesia_vitals", ["view", "create", "update", "delete"]),
        ("ot.anaesthesia_drugs", ["view", "create", "update", "delete"]),
        ("ot.nursing_record", ["view", "create", "update"]),
        ("ot.counts", ["view", "create", "update"]),
        ("ot.implants", ["view", "create", "update", "delete"]),
        ("ot.operation_notes", ["view", "create", "update"]),
        ("ot.blood_transfusion", ["view", "create", "update", "delete"]),
        ("ot.pacu", ["view", "create", "update"]),
        ("ot.equipment_checklist", ["view", "create", "update", "delete"]),
        ("ot.cleaning_log", ["view", "create", "update", "delete"]),
        ("ot.environment_log", ["view", "create", "update", "delete"]),
        ("ot.procedures", ["view", "create", "update", "delete"]),

        # -------- Billing ----------
        ("billing", ["view", "create", "finalize"]),
        ("billing.items", ["add"]),
        ("billing.payments", ["add"]),

        # -------- EMR / Settings / MIS ----------
        ("emr", ["view", "download"]),
        ("templates", ["view", "manage"]),
        ("consents", ["view", "manage"]),
        ("mis", ["view"]),
        ("mis.collection", ["view"]),
        ("mis.accounts", ["view"]),
        ("mis.opd", ["view"]),
        ("mis.ipd", ["view"]),
        ("mis.visits", ["view"]),
        ("mis.pharmacy", ["view"]),
        ("mis.stock", ["view"]),
        ("mis.lab", ["view"]),
        ("mis.radiology", ["view"]),

        # -------- Pharmacy Rx ----------
        ("pharmacy.rx",
         ["view", "dispense", "override", "cancel", "manage", "sign",
          "print"]),
        ("pharmacy.rx_queue", ["view"]),
        ("pharmacy.sales",
         ["view", "create", "return", "finalize", "cancel", "update"]),
        ("pharmacy.billing", ["view", "create", "refund"]),
        ("pharmacy.returns", ["view", "manage"]),
        ("pharmacy.prescriptions",
         ["view", "create", "update", "sign", "cancel"]),
        ("pharmacy.dispense", ["view", "create"]),
        ("pharmacy.payments", ["view", "create"]),
        ("pharmacy.stock", ["view"]),
        ("pharmacy", ["view", "dispense"]),
        ("pharmacy.reports.schedule_medicine", ["view"]),
        ("pharmacy.reports", ["view"]),
        ("quickorder", ["radiology", "pharmacy", "laboratory", "ot", "consumables"]),

        # -------- Master / Settings ----------
        ("settings.customization", ["view", "manage"]),
        ("master.tenants", ["view", "manage"]),
        ("master.storage", ["view", "manage"]),
        ("master.migrations", ["view", "manage"]),

        # ✅ Your “single code” entries can stay as strings now:
        "billing.insurance.view",
        "billing.preauth.view",
        "billing.claims.view",
        "billing.insurance.manage",
        "billing.preauth.manage",
        "billing.claims.manage",
        "billing.invoices.split",
        "billing.refunds.create",
        "billing.claims.reject",
        "billing.claims.cancel",
        "billing.claims.reopen",
        "billing.invoice.print",
        "billing.invoice.export",
        "billing.case.statement.print",
        "billing.invoice.edit",
        "billing.invoice.recalculate",
        "billing.case.cancel",
        "billing.case.close",
        "billing.case.reopen",
        "billing.receipts.void",
        "billing.preauth.create",
        "billing.preauth.submit",
        "billing.preauth.approve",
        "billing.preauth.reject",
        "billing.preauth.cancel",
        "billing.claims.set_query",
        "billing.claims.close",
        "billing.receipt.print",
        "billing.manage",
        "billing.invoices.create",
        "masters.charge_items.view",
        "masters.charge_items.manage",
        "billing.invoice.lines.edit",
        "billing.invoice.lines.delete",
        "billing.invoice.reopen",
    ]

    from app.models.permission import Permission

    # load once (fast)
    existing = {c for (c, ) in db.query(Permission.code).all()}

    to_add = []
    seen = set()

    def add_code(full_code: str):
        full_code = (full_code or "").strip()
        if not full_code or full_code in existing or full_code in seen:
            return
        seen.add(full_code)

        if "." in full_code:
            module, action = full_code.rsplit(".", 1)
        else:
            module, action = full_code, "view"

        label = f"{module.replace('.', ' ').title()} — {action.replace('_', ' ').title()}"
        to_add.append(Permission(code=full_code, label=label, module=module))

    for item in MODULES:
        if isinstance(item, str):
            add_code(item)
            continue

        # tuple/list -> (module, actions)
        module, actions = item
        for action in actions:
            add_code(f"{module}.{action}")

    if to_add:
        db.add_all(to_add)


# ---------- MASTER DB INIT ----------


def init_master_db(fresh: bool = False) -> None:
    """
    Initialize / migrate the central Tenant Management DB.
    """
    if fresh:
        print("WARNING: Dropping ALL MASTER tables (dev only) …")
        MasterBase.metadata.drop_all(bind=master_engine)

    print("Creating master tables …")
    MasterBase.metadata.create_all(bind=master_engine)

    with master_engine.connect() as conn:
        print_tables(conn)

    print("Master DB ready.")


# ---------- TENANT DB INIT (used during provisioning) ----------


def init_tenant_db(db_uri: str, fresh: bool = False) -> None:
    """
    Create / migrate all tenant tables in a specific tenant DB and seed permissions.
    For brand new tenants we temporarily disable FOREIGN_KEY_CHECKS so
    MySQL doesn't complain about table creation order (FKs like lis_result_lines → lis_orders).
    """
    engine = get_or_create_tenant_engine(db_uri)

    if fresh:
        print(f"WARNING: Dropping ALL TENANT tables for {db_uri} (dev only) …")
        with engine.begin() as conn:
            # Disable FK checks so drop_all doesn't choke on references
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            Base.metadata.drop_all(bind=conn)
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    print(f"Creating tenant tables for {db_uri} …")
    with engine.begin() as conn:
        # ✅ Disable FK checks so create_all can create tables in any order
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        Base.metadata.create_all(bind=conn)
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

        # Optional: see what got created
        print_tables(conn)

    # Seed permissions AFTER tables are created
    try:
        with Session(engine) as db:
            seed_permissions(db)
            db.commit()
            print("Tenant permissions seeded (missing codes inserted).")
    except SQLAlchemyError as e:
        print("Tenant seeding failed:", e)
        raise


def run(fresh: bool = False) -> None:
    """
    Backward-compatible entry point — now only initializes MASTER DB.
    """
    init_master_db(fresh=fresh)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Initialize MASTER DB (create tables).")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Drop & recreate all MASTER tables (DEV ONLY).",
    )
    args = parser.parse_args()
    run(fresh=args.fresh)
