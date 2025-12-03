# app/db/base.py
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """All tenant-level tables (users, patients, OP, IP, etc.) inherit from this."""
    pass


# Import all tenant models so metadata is complete for create_all()
from app.models import (  # noqa: F401
    user,
    role,
    department,
    permission,
    otp,
    patient,
    opd,
    ipd,
    lis,
    ris,
    ot_master,
    ot,
    billing,
    common,
    template,
    ui_branding,
    pharmacy_inventory,
    pharmacy_prescription,
    audit,
)
