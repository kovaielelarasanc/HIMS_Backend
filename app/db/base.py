from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
from app.models import (user, role, department, permission, otp, 
                        patient, opd, ipd , lis, ris, 
                        ot_master, ot, billing, common,template, 
                        ui_branding, pharmacy_inventory, pharmacy_prescription)