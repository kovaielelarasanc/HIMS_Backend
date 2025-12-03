from typing import Any, Dict, Optional
import traceback

from sqlalchemy.orm import Session

from app.models.error_log import ErrorLog


def log_error(
    db: Session,
    *,
    description: Optional[str] = None,
    error_source: str = "backend",  # "backend" | "frontend"
    endpoint: Optional[str] = None,
    module: Optional[str] = None,
    function: Optional[str] = None,
    http_status: Optional[int] = None,
    tenant_code: Optional[str] = None,
    request_payload: Optional[Dict[str, Any]] = None,
    response_payload: Optional[Dict[str, Any]] = None,
    stack_trace: Optional[str] = None,
) -> None:
    """
    Central helper to persist an error into MASTER error_logs.
    Safe: wraps commit errors.
    """
    try:
        log = ErrorLog(
            error_source=error_source,
            description=description,
            endpoint=endpoint,
            module=module,
            function=function,
            http_status=http_status,
            tenant_code=tenant_code,
            request_payload=request_payload,
            response_payload=response_payload,
            stack_trace=stack_trace,
        )
        db.add(log)
        db.commit()
    except Exception as e:
        # last resort – never raise from logger
        db.rollback()
        print("Failed to log error:", e)


def format_exception(exc: Exception) -> str:
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__))
