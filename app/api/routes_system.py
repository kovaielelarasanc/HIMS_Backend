from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_master_db
from app.schemas.system import ClientErrorReportIn
from app.services.error_logger import log_error

router = APIRouter()


@router.post("/system/client-error")
async def report_client_error(
        payload: ClientErrorReportIn,
        request: Request,
        master_db: Session = Depends(get_master_db),
):
    """
    Frontend sends error details here. Stored in MASTER error_logs with source='frontend'.
    """
    tenant_code = payload.tenant_code or request.headers.get("X-Tenant-Code")
    ua = payload.user_agent or request.headers.get("user-agent")

    log_error(
        db=master_db,
        description=payload.message or "Frontend error",
        error_source="frontend",
        endpoint=payload.page_url,
        module=payload.module,
        function=payload.function_name,
        http_status=payload.http_status,
        tenant_code=tenant_code,
        request_payload={
            "request_url": payload.request_url,
            "request_method": payload.request_method,
            "request_payload": payload.request_payload,
            "user_agent": ua,
            "extra": payload.extra,
        },
        response_payload={"response_payload": payload.response_payload},
        stack_trace=payload.stack_trace,
    )

    return {"status": "ok"}
