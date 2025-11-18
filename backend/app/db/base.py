from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
from app.models import user, role, department, permission, otp, patient, opd, ipd, pharmacy, lis, ris, ot_master, ot, billing, common,template