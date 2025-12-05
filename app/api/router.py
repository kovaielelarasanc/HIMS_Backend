# backend/app/api/router.py
from fastapi import APIRouter
from app.api import (
    # Core
    routes_auth,
    routes_admin,
    routes_users,
    routes_roles,
    routes_departments,
    routes_permissions,
    routes_masters,

    # Patients / ABHA
    routes_patients,
    routes_abha,
    routes_patient_types,

    # OPD
    routes_opd_common,
    routes_opd_schedules,
    routes_opd,
    routes_opd_clinical,

    # IPD
    routes_ipd_masters,
    routes_ipd,
    routes_pharmacy,
 
    routes_pharmacy_rx_list,
  
    routes_lis,
    routes_ris,
    
    routes_billing,
    
    routes_ot_masters,
    routes_ot_schedule_cases,
    routes_ot_clinical,
    routes_ot_admin_logs,
    
    
    routes_system,
   
    routes_files,
    routes_lis_history,

    routes_ris_history,
    routes_emr,
    routes_templates,
    routes_patient_search,
    routes_dashboard,
    routes_mis,
    routes_patient_masters,
    routes_masters_credit,
    routes_ui_branding,
    routes_inventory,
    routes_audit_logs,
    routes_lis_masters
    )

api_router = APIRouter()

# ---- Core
api_router.include_router(routes_auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(routes_admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(routes_users.router, prefix="/users", tags=["users"])
api_router.include_router(routes_roles.router, prefix="/roles", tags=["roles"])
api_router.include_router(routes_departments.router,
                          prefix="/departments",
                          tags=["departments"])
api_router.include_router(routes_permissions.router,
                          prefix="/permissions",
                          tags=["permissions"])

# ---- Patients / ABHA
api_router.include_router(routes_patients.router,
                          prefix="/patients",
                          tags=["patients"])
api_router.include_router(routes_patient_masters.router,
                          prefix="/patient-masters",
                          tags=["patient-masters"])
api_router.include_router(
    routes_patient_types.router,
    prefix="/patient-types",
    tags=["Patient Types"],
)
api_router.include_router(routes_audit_logs.router,
                          prefix="/audit-logs",
                          tags=["audit-logs"])  # 👈 this gives /api/audit-logs
api_router.include_router(routes_masters_credit.router,
                          prefix="/masters",
                          tags=["masters"])
api_router.include_router(routes_abha.router, prefix="/abha", tags=["abha"])

# ---- OPD (Avoid path collisions: see notes in each module)
api_router.include_router(routes_opd_common.router,
                          prefix="/opd",
                          tags=["opd"])
api_router.include_router(routes_opd_schedules.router,
                          prefix="/opd",
                          tags=["opd"])
api_router.include_router(routes_opd.router, prefix="/opd", tags=["opd"])
api_router.include_router(routes_opd_clinical.router,
                          prefix="/opd",
                          tags=["opd"])

# ---- IPD
api_router.include_router(routes_ipd_masters.router,
                          prefix="/ipd",
                          tags=["ipd"])
api_router.include_router(routes_ipd.router, prefix="/ipd", tags=["ipd"])


# ---- Masters
api_router.include_router(routes_masters.router,
                          prefix="/masters",
                          tags=["masters"])

# ---- LIS / RIS / OT / Billing
api_router.include_router(routes_lis.router,  prefix="/lab", tags=["LIS Orders"])
api_router.include_router(routes_lis_masters.router)


api_router.include_router(routes_ris.router)

api_router.include_router(routes_billing.router,
                          prefix="/billing",
                          tags=["billing"])



api_router.include_router(routes_ot_masters.router)
api_router.include_router(routes_ot_schedule_cases.router)
api_router.include_router(routes_ot_clinical.router)
api_router.include_router(routes_ot_admin_logs.router)


# ---- Files & History
api_router.include_router(routes_files.router, prefix="/files", tags=["Files"])
api_router.include_router(routes_lis_history.router)
api_router.include_router(routes_ris_history.router)


api_router.include_router(routes_emr.router, prefix="/emr", tags=["EMR"])

api_router.include_router(routes_templates.router,
                          prefix="/templates",
                          tags=["Templates & Consents"])
api_router.include_router(routes_patient_search.router,
                          prefix="/opd",
                          tags=["OPD common"])

api_router.include_router(routes_dashboard.router,
                          prefix="/dashboard",
                          tags=["Dashboard"])
api_router.include_router(routes_mis.router, prefix="/mis", tags=["MIS"])

api_router.include_router(routes_ui_branding.router)

api_router.include_router(routes_inventory.router)
api_router.include_router(routes_pharmacy.router)

api_router.include_router(
    routes_pharmacy_rx_list.router,
    prefix="/pharmacy",
    tags=["Pharmacy Rx"],
)


api_router.include_router(routes_system.router, tags=["system"])
