from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.audit import AuditLog


def log_audit(
    db: Session,
    *,
    user_id: Optional[int],
    action: str,  # "CREATE" | "UPDATE" | "DELETE"
    table_name: str,
    record_id: Any,
    old_values: Optional[Dict[str, Any]] = None,
    new_values: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """
    Persist one audit event into tenant's audit_logs table.
    """
    try:
        log = AuditLog(
            user_id=user_id,
            action=action,
            table_name=table_name,
            record_id=str(record_id),
            old_values=old_values,
            new_values=new_values,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.add(log)
        db.commit()
    except Exception as e:
        db.rollback()
        print("Failed to log audit:", e)
