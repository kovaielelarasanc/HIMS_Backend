from typing import Any, Dict, Optional
from pydantic import BaseModel


class ClientErrorReportIn(BaseModel):
    message: Optional[str] = None

    page_url: Optional[str] = None  # which screen
    request_url: Optional[str] = None  # API URL
    request_method: Optional[str] = None
    http_status: Optional[int] = None

    request_payload: Optional[Dict[str, Any]] = None
    response_payload: Optional[Dict[str, Any]] = None

    stack_trace: Optional[str] = None
    module: Optional[str] = None
    function_name: Optional[str] = None

    tenant_code: Optional[str] = None
    user_agent: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
