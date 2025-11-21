# backend/app/db/init_db.py
from __future__ import annotations

import argparse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import engine
from app.db.base import Base

# Import all models so metadata is complete
from app.models import (  # noqa: F401
    Department, User, UserRole, Role, RolePermission, Permission, OtpToken,
    patient, opd, ipd, pharmacy, common, lis, ris, ot_master, ot, billing,
    template,payer
)


def print_tables(conn):
    rows = conn.exec_driver_sql("SHOW TABLES").fetchall()
    names = [r[0] for r in rows]
    print("Existing tables:", names)
    return set(names)


def seed_permissions(db: Session) -> None:
    """
    Seed ONLY missing permission codes; safe to run multiple times.
    """
    MODULES = [
        ("departments", ["view", "create", "update", "delete"]),
        ("roles", ["view", "create", "update", "delete"]),
        ("permissions", ["view", "create", "update", "delete"]),
        ("users", ["view", "create", "update", "delete"]),

        # -------- PATIENTS ----------
        ("patients", [
            "view",
            "create",
            "update",
            "deactivate",
            "addresses.view",
            "addresses.create",
            "addresses.update",
            "addresses.delete",
            "consents.view",
            "consents.create",
            "attachments.manage",
        ]),
        # NEW: Patient masters (payer / TPA / credit plan / doctor list access)
        (
            "patients.masters",
            [
                "view",  # can list doctors, payers, tpas, credit plans, ref sources
                "manage",  # can create / update / deactivate payers, tpas, plans
            ]),

        # -------- OPD ----------
        ("schedules", ["manage"]),
        ("appointments", ["view", "create", "update", "cancel"]),
        ("vitals", ["create"]),
        ("visits", ["view", "create", "update"]),
        ("prescriptions", ["create", "esign"]),
        ("orders.lab", ["create", "view"]),
        ("orders.ris", ["create", "view"]),

        # -------- IPD ----------
        ("ipd", ["view", "manage", "nursing", "doctor"]),
        ("ipd.masters", ["manage"]),
        ("ipd.packages", ["manage"]),
        ("ipd.tracking", ["view"]),
        ("ipd.my", ["view"]),
        ("ipd.discharged", ["view"]),
        ("ipd.bedboard", ["view"]),

        # -------- Pharmacy ----------
        ("pharmacy", ["view"]),
        ("pharmacy.masters", ["manage"]),
        ("pharmacy.procure", ["manage"]),
        ("pharmacy.inventory", ["view", "manage"]),
        ("pharmacy.dispense", ["create"]),

        # -------- LIS ----------
        ("lab.masters", ["view", "manage"]),
        ("lab.orders", ["create", "view"]),
        ("lab.samples", ["collect"]),
        ("lab.results", ["enter", "validate", "report"]),
        ("lab.attachments", ["add"]),

        # -------- RIS ----------
        ("radiology.masters", ["view", "manage"]),
        ("radiology.orders", ["create", "view"]),
        ("radiology.schedule", ["manage"]),
        ("radiology.scan", ["update"]),
        ("radiology.report", ["create", "approve"]),
        ("radiology.attachments", ["add"]),

        # -------- OT ----------
        ("ot.masters", ["view", "manage"]),
        ("ot.cases", ["view", "update", "create"]),

        # -------- Billing ----------
        ("billing", ["view", "create", "finalize"]),
        ("billing.items", ["add"]),
        ("billing.payments", ["add"]),
        ("emr", ["view", "download"]),
        ("templates", ["view", "manage"]),
        ("consents", ["view", "manage"]),

        # -------- MIS / Analytics ----------
        # General MIS screen access
        ("mis", ["view"]),

        # Collections / Accounts
        ("mis.collection", ["view"]
         ),  # daily summary, date-wise collection, etc.
        ("mis.accounts", ["view"]),  # income by dept / consultant / service

        # OPD / IPD / Visits
        ("mis.opd", ["view"]),  # OPD MIS
        ("mis.ipd", ["view"]),  # IPD MIS
        ("mis.visits", ["view"]),  # Combined

        # Pharmacy / Stock
        ("mis.pharmacy", ["view"]),  # pharmacy sales, top drugs
        ("mis.stock", ["view"]),  # stock analytics

        # Lab / Radiology
        ("mis.lab", ["view"]),  # test orders, TAT
        ("mis.radiology", ["view"]),  # radiology orders, TAT
    ]

    from app.models.permission import Permission
    seen = set()
    for module, actions in MODULES:
        for action in actions:
            code = f"{module}.{action}"
            if code in seen:
                continue
            seen.add(code)
            if not db.query(Permission).filter(
                    Permission.code == code).first():
                label = f"{module.replace('.', ' ').title()} — {action.title()}"
                db.add(Permission(code=code, label=label, module=module))


def run(fresh: bool = False) -> None:
    if fresh:
        print("WARNING: Dropping ALL tables (dev only) …")
        Base.metadata.drop_all(bind=engine)

    print("Creating all missing tables …")
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        print_tables(conn)

    try:
        with Session(engine) as db:
            seed_permissions(db)
            db.commit()
            print("Permissions seeded (missing codes inserted).")
    except SQLAlchemyError as e:
        print("Seeding failed:", e)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Initialize DB (create tables, seed permissions).")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Drop & recreate all tables (DEV ONLY).",
    )
    args = parser.parse_args()
    run(fresh=args.fresh)
