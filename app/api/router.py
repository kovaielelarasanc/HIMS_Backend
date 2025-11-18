# backend/app/api/router.py
from fastapi import APIRouter
from app.api import (
    # Core
    routes_auth, routes_admin, routes_users, routes_roles, routes_departments,
    routes_permissions,routes_masters,

    # Patients / ABHA
    routes_patients, routes_abha,

    # OPD
    routes_opd_common, routes_opd_schedules, routes_opd, routes_opd_clinical,

    # IPD
    routes_ipd_masters, routes_ipd,

    # Pharmacy
    routes_pharmacy_dispense, routes_pharmacy_inventory, routes_pharmacy_procurement,
    routes_pharmacy_masters, routes_pharmacy_reports, routes_pharmacy_rx,

    # LIS / RIS / OT / Billing
    routes_lis, routes_ris, routes_ot, routes_billing, routes_ot_masters,

    # Files & History
    routes_files, routes_lis_history, routes_ot_history, routes_ris_history, routes_emr, routes_templates, routes_patient_search
)

api_router = APIRouter()

# ---- Core
api_router.include_router(routes_auth.router,        prefix="/auth",        tags=["auth"])
api_router.include_router(routes_admin.router,       prefix="/admin",       tags=["admin"])
api_router.include_router(routes_users.router,       prefix="/users",       tags=["users"])
api_router.include_router(routes_roles.router,       prefix="/roles",       tags=["roles"])
api_router.include_router(routes_departments.router, prefix="/departments", tags=["departments"])
api_router.include_router(routes_permissions.router, prefix="/permissions", tags=["permissions"])

# ---- Patients / ABHA
api_router.include_router(routes_patients.router,    prefix="/patients",    tags=["patients"])
api_router.include_router(routes_abha.router,        prefix="/abha",        tags=["abha"])

# ---- OPD (Avoid path collisions: see notes in each module)
api_router.include_router(routes_opd_common.router,    prefix="/opd", tags=["opd"])
api_router.include_router(routes_opd_schedules.router, prefix="/opd", tags=["opd"])
api_router.include_router(routes_opd.router,           prefix="/opd", tags=["opd"])
api_router.include_router(routes_opd_clinical.router,  prefix="/opd", tags=["opd"])

# ---- IPD
api_router.include_router(routes_ipd_masters.router,   prefix="/ipd", tags=["ipd"])
api_router.include_router(routes_ipd.router,           prefix="/ipd", tags=["ipd"])

# ---- Pharmacy
api_router.include_router(routes_pharmacy_masters.router,     prefix="/pharmacy", tags=["Pharmacy Masters"])
api_router.include_router(routes_pharmacy_procurement.router, prefix="/pharmacy", tags=["Pharmacy Procurement"])
api_router.include_router(routes_pharmacy_inventory.router,   prefix="/pharmacy", tags=["Pharmacy Inventory"])
api_router.include_router(routes_pharmacy_dispense.router,    prefix="/pharmacy", tags=["Pharmacy Dispense"])
api_router.include_router(routes_pharmacy_reports.router,     prefix="/pharmacy", tags=["Pharmacy Alerts/Reports"])
api_router.include_router(routes_pharmacy_rx.router,          prefix="/pharmacy", tags=["Pharmacy Prescriptions"])

# ---- Masters
api_router.include_router(routes_masters.router,       prefix="/masters", tags=["masters"])

# ---- LIS / RIS / OT / Billing
api_router.include_router(routes_lis.router)
api_router.include_router(routes_ris.router)
api_router.include_router(routes_ot.router)
api_router.include_router(routes_billing.router)
api_router.include_router(routes_ot_masters.router)

# ---- Files & History
api_router.include_router(routes_files.router,         prefix="/files", tags=["Files"])
api_router.include_router(routes_lis_history.router)
api_router.include_router(routes_ris_history.router)
api_router.include_router(routes_ot_history.router)

api_router.include_router(routes_emr.router, prefix="/emr", tags=["EMR"])

api_router.include_router(routes_templates.router, prefix="/templates", tags=["Templates & Consents"])
api_router.include_router(routes_patient_search.router, prefix="/opd", tags=["OPD common"] )