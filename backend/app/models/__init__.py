# backend/app/models/__init__.py
from .department import Department
from .user import User, UserRole
from .role import Role, RolePermission
from .permission import Permission
from .otp import OtpToken
from .opd import OpdSchedule
from .template import DocumentTemplate, TemplateRevision, PatientConsentTemp 
__all__ = [
    "Department",
    "User",
    "UserRole",
    "Role",
    "RolePermission",
    "Permission",
    "OtpToken",
    "OpdSchedule",
    "DocumentTemplate",
    "TemplateRevision",
    "PatientConsentTemp" 
]
