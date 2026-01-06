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
    """
    MODULES = [
        # -------- CORE / ADMIN ----------
        ("departments", ["view", "create", "update", "delete"]),
        ("roles", ["view", "create", "update", "delete"]),
        ("permissions", ["view", "create", "update", "delete"]),
        ("users", ["view", "create", "update", "delete"]),

        # -------- PATIENTS ----------
        ("patients", ["view", "create", "update", "deactivate",]),
        ("patients.addresses", ["view", "create", "update", "delete",]),
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

        # -------------------------------------------------------------------
        # IPD – Core
        # -------------------------------------------------------------------
        ("ipd", ["view", "manage", "doctor", "nursing"]),

        # -------------------------------------------------------------------
        # IPD Masters
        # -------------------------------------------------------------------
        ("ipd.masters", ["manage"]),
        ("ipd.beds", ["view", "manage", "reserve", "release"]),
        ("ipd.bedrates", ["view", "manage"]),
        ("ipd.packages", ["view", "manage"]),

        # ✅ FIX: Your routes use ipd.nursing.manage, so add "manage" here
        ("ipd.nursing", ["view", "create", "update", "manage"]),

        # -------------------------------------------------------------------
        # ✅ NEW: IPD Newborn (Resuscitation / Examination / Vaccination / PDF)
        # Matches your router checks: ipd.newborn.view/create/update/verify/finalize/void/print/manage
        # -------------------------------------------------------------------
        ("ipd.newborn",["view", "create", "update", "verify", "finalize", "void", "print", "manage"]),

        # -------------------------------------------------------------------
        # IPD Admissions / Tracking
        # -------------------------------------------------------------------
        ("ipd.admissions", ["view", "create", "update", "cancel", "transfer", "discharge"]),
        ("ipd.tracking", ["view"]),
        ("ipd.my", ["view"]),
        ("ipd.discharged", ["view"]),
        ("ipd.bedboard", ["view"]),

        # -------------------------------------------------------------------
        # IPD Clinical – Vitals / Nursing Notes / IO / Assessments
        # -------------------------------------------------------------------
        ("ipd.vitals", ["view", "create", "update"]),
        ("ipd.nursing_notes", ["view", "create", "update"]),
        ("ipd.io", ["view", "create", "update"]),
        ("ipd.assessments", ["view", "create", "update"]),

        # -------------------------------------------------------------------
        # IPD Medications / Drug Chart
        # -------------------------------------------------------------------
        (
            "ipd.meds",
            ["view", "order", "update", "regenerate", "mark", "meta", "iv", "nurse_rows", "doctor_auth", "pdf"],
        ),

        # -------------------------------------------------------------------
        # IPD Discharge
        # -------------------------------------------------------------------
        (
            "ipd.discharges",
            ["view", "summary", "checklist", "medications", "queue", "mark_status", "push_abha", "pdf"],
        ),
        # -------------------------------------------------------------------
        # IPD Referrals (Doctor-to-Doctor / Dept referrals)
        # -------------------------------------------------------------------
        (
            "ipd.referrals",
            [
                "view",     # ipd.referrals.view
                "create",   # ipd.referrals.create
                "accept",   # ipd.referrals.accept
                "decline",  # ipd.referrals.decline
                "respond",  # ipd.referrals.respond
                "close",    # ipd.referrals.close
                "cancel",   # ipd.referrals.cancel
                "edit",     # ipd.referrals.edit (optional)
                "manage",   # admin wildcard for this module
            ],
        ),

        # Optional: if you want audit as a separate restricted permission
        ("ipd.referrals.audit", ["view"]),  # ipd.referrals.audit.view

        # -------------------------------------------------------------------
        # IPD Bed / Ward Transfers
        # -------------------------------------------------------------------
        (
            "ipd.transfers",
            [
                "view",      # ipd.transfers.view
                "create",    # ipd.transfers.create
                "approve",   # ipd.transfers.approve
                "complete",  # ipd.transfers.complete
                "cancel",    # ipd.transfers.cancel
                "manage",    # optional admin wildcard for this module
            ],
        ),

        # -------------------------------------------------------------------
        # IPD Clinical Permissions (module -> actions)
        # -------------------------------------------------------------------

        ("ipd.dressing",   ["create", "view", "update"]),
        ("ipd.icu",        ["create", "view", "update"]),
        ("ipd.isolation",  ["create", "view", "update", "stop"]),
        ("ipd.restraints", ["create", "view", "update", "monitor", "stop"]),
        ("ipd.transfusion",["create", "view", "update"]),
        
        # -------- Pharmacy Inventory ----------
        ("pharmacy.inventory.locations", ["view", "manage"]),
        ("pharmacy.inventory.suppliers", ["view", "manage"]),
        ("pharmacy.inventory.items", ["view", "manage"]),
        ("pharmacy.inventory.stock", ["view"]),
        ("pharmacy.inventory.alerts", ["view"]),
        ("pharmacy.inventory.po", ["view", "manage","approve","cancel"]),
        ("pharmacy.inventory.grn", ["view", "manage"]),
        ("pharmacy.inventory.returns", ["view", "manage"]),
        ("pharmacy.inventory", ["dispense"]),
        ("pharmacy.inventory.txns", ["view"]),
        ("pharmacy.accounts.supplier_ledger", ["view", "manage", "export"]),
        ("pharmacy.accounts.supplier_payments", ["view", "manage", "export"]),
        ("pharmacy.accounts.supplier_invoices", ["view", "manage", "export"]),
        
        # -------- LIS ----------
        ("lab.masters", ["view", "manage"]),
        ("lab.orders", ["create", "view"]),  # OP/IP lab orders
        ("lab.samples", ["collect"]),  # sample collection
        ("lab.results", ["enter", "validate", "report"]),
        ("lab.attachments", ["add"]),  # add report attachments

        # NEW: Analyzer / Device management
        # Used in routes_lis_device.py & AnalyzerDeviceMapping.jsx
        ("lab.devices", ["view", "manage"]
         ),  # list/create/update/delete devices & channels
        ("lab.device_results", ["review",
                                "import"]),  # review staging, import to LIS
        ("lab.device_logs", ["view"]),  # view raw message logs

        # LIS masters (new LIS service master screens)
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
        ("ot.masters", ["view", "create", "update", "delete","manage"]),
        ("ot.specialities", ["view", "create", "update", "delete"]),
        ("ot.schedule", ["view", "create", "update", "delete", "cancel", "manage"]),
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

        # -------- EMR / Templates / Consents ----------
        ("emr", ["view", "download"]),
        ("templates", ["view", "manage"]),
        ("consents", ["view", "manage"]),

        # -------- MIS / Analytics ----------
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
 
        # -------- Pharmacy Rx & Billing ----------
        ("pharmacy.rx", ["view", "dispense", "override", "cancel"]),
        ("pharmacy.sales", ["view", "create", "return"]),
        ("pharmacy.billing", ["view", "create", "refund"]),
        ("pharmacy.returns", ["view", "manage"]),

        # -------- Settings / Customization ----------
        ("settings.customization", ["view", "manage"]),
        ("master.tenants", ["view", "manage"]),
        ("master.storage", ["view", "manage"]),
        ("master.migrations", ["view", "manage"]),
    ]

    from app.models.permission import Permission  # tenant-level

    seen = set()
    for module, actions in MODULES:
        for action in actions:
            code = f"{module}.{action}"
            if code in seen:
                continue
            seen.add(code)
            exists = db.query(Permission).filter(
                Permission.code == code).first()
            if not exists:
                label = f"{module.replace('.', ' ').title()} — {action.title()}"
                db.add(Permission(code=code, label=label, module=module))


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
