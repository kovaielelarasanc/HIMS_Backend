# app/services/master_storage.py
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.models.tenant import Tenant

def tenant_db_usage_mb(db_name: str, master_db: Session) -> float:
    q = text("""
        SELECT IFNULL(ROUND(SUM(data_length+index_length)/1024/1024, 2), 0) AS mb
        FROM information_schema.tables
        WHERE table_schema = :db
    """)
    mb = master_db.execute(q, {"db": db_name}).scalar() or 0
    return float(mb)

def usage_by_volume(master_db: Session) -> list[dict]:
    tenants = master_db.query(Tenant).filter(Tenant.is_active == True).all()

    by_vol: dict[str, float] = {}
    for t in tenants:
        meta = t.meta or {}
        vol = (meta.get("volume_tag") or "default").strip() or "default"
        used = tenant_db_usage_mb(t.db_name, master_db)
        by_vol[vol] = by_vol.get(vol, 0.0) + used

    return [{"volume_tag": k, "used_mb": round(v, 2)} for k, v in sorted(by_vol.items())]
