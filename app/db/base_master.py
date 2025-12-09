# app/db/base_master.py
from sqlalchemy.orm import DeclarativeBase


class MasterBase(DeclarativeBase):
    """All central / master tables (tenants, global configs) inherit from this."""
    pass

from app.models import tenant
from app.models import error_log 