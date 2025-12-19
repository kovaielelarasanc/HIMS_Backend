# app/api/routes_master_migrations.py
from __future__ import annotations
from typing import Any, Dict, List, Tuple
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy import create_engine
import json
from app.core.config import settings
from app.api.deps import get_master_db, current_provider_user, require_perm
from app.models.tenant import Tenant

from app.schemas.master_migrations import PlanRequest, PlanResponse, ApplyResponse, JobDetail
from app.services.master_ddl import build_sql_for_op, is_destructive, exec_sql
from app.services.master_storage import tenant_db_usage_mb, usage_by_volume
from app.services.master_sql_guard import assert_ident, TYPE_ALLOWLIST

router = APIRouter(prefix="/master/migrations", tags=["MASTER_MIGRATIONS"])

# --------- audit ---------
def _json(v: Any) -> str:
    # Ensures datetimes, decimals, etc. won't crash json.dumps
    return json.dumps(v, default=str, ensure_ascii=False)

def audit(
    master_db: Session,
    req: Request,
    actor_id: int,
    action: str,
    resource: str,
    details: Dict[str, Any] | None = None,
):
    master_db.execute(
        text("""
            INSERT INTO master_audit_logs(actor_id, action, resource, details_json, ip, user_agent)
            VALUES(:a,:ac,:r, CAST(:d AS JSON), :ip, :ua)
        """),
        {
            "a": actor_id,
            "ac": action,
            "r": resource,
            "d": json.dumps(details or {}, ensure_ascii=False),
            "ip": req.client.host if req.client else None,
            "ua": req.headers.get("user-agent"),
        },
    )
    master_db.commit()


# --------- Admin DDL Engines (cached) ---------
_admin_engines: Dict[str, Engine] = {}

def _mysql_admin_base_url() -> str:
    """
    Uses MYSQL_ADMIN_* if set; fallback to app MYSQL_*.
    Matches your session.py pool settings.
    """
    from urllib.parse import quote_plus

    driver = getattr(settings, "DB_DRIVER", "pymysql")
    host = getattr(settings, "MYSQL_HOST", "localhost")
    port = getattr(settings, "MYSQL_PORT", 3306)

    user = getattr(settings, "MYSQL_ADMIN_USER", None) or getattr(settings, "MYSQL_USER", "root")
    pw_raw = getattr(settings, "MYSQL_ADMIN_PASSWORD", None)
    if pw_raw is None:
        pw_raw = getattr(settings, "MYSQL_PASSWORD", "")

    pw = quote_plus(pw_raw or "")
    return f"mysql+{driver}://{user}:{pw}@{host}:{port}"

def admin_engine(db: str) -> Engine:
    """
    db: 'mysql' for global db ops, or tenant db_name for tenant ops.
    """
    eng = _admin_engines.get(db)
    if eng is None:
        eng = create_engine(
            f"{_mysql_admin_base_url()}/{db}",
            pool_pre_ping=True,
            pool_recycle=280,
            pool_size=10,
            max_overflow=20,
            future=True,
        )
        _admin_engines[db] = eng
    return eng

# --------- helpers ---------
def _split_ops(ops: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    global_ops = []
    tenant_ops = []
    for op in ops:
        if op.get("op") in ("create_database", "drop_database"):
            global_ops.append(op)
        else:
            tenant_ops.append(op)
    return global_ops, tenant_ops


def _validate_ops_or_400(ops: List[Dict[str, Any]]):
    """
    Validate required fields for each operation.
    Raise HTTPException(400) with a clear message (instead of 500 ValueError).
    """
    for i, op in enumerate(ops, start=1):
        kind = (op.get("op") or "").strip()
        if not kind:
            raise HTTPException(400, f"Operation #{i}: missing 'op'")

        # db ops
        if kind in ("create_database", "drop_database"):
            dbn = (op.get("db_name") or "").strip()
            if not dbn:
                raise HTTPException(400, f"Operation #{i} ({kind}): db_name is required")
            continue

        # table-based ops
        tb = (op.get("table") or "").strip()
        if not tb:
            raise HTTPException(400, f"Operation #{i} ({kind}): table is required")

        # validate identifiers (your assert_ident raises ValueError; convert to 400)
        try:
            assert_ident(tb, "table")
        except Exception as e:
            raise HTTPException(400, f"Operation #{i} ({kind}): {e}")

        if kind in ("add_column", "modify_column"):
            col = op.get("column") or {}
            name = (col.get("name") or "").strip()
            ctype = (col.get("type") or "").strip()
            if not name:
                raise HTTPException(400, f"Operation #{i} ({kind}): column.name is required")
            if not ctype:
                raise HTTPException(400, f"Operation #{i} ({kind}): column.type is required")
            try:
                assert_ident(name, "column")
            except Exception as e:
                raise HTTPException(400, f"Operation #{i} ({kind}): {e}")

        if kind == "drop_column":
            cn = (op.get("column_name") or "").strip()
            if not cn:
                raise HTTPException(400, f"Operation #{i} ({kind}): column_name is required")
            try:
                assert_ident(cn, "column")
            except Exception as e:
                raise HTTPException(400, f"Operation #{i} ({kind}): {e}")

        if kind == "rename_column":
            old = (op.get("old_column_name") or "").strip()
            new = (op.get("new_column_name") or "").strip()
            if not old or not new:
                raise HTTPException(400, f"Operation #{i} ({kind}): old_column_name and new_column_name are required")
            try:
                assert_ident(old, "column")
                assert_ident(new, "column")
            except Exception as e:
                raise HTTPException(400, f"Operation #{i} ({kind}): {e}")

        if kind == "create_table":
            cols = op.get("columns") or []
            if not isinstance(cols, list) or not cols:
                raise HTTPException(400, f"Operation #{i} ({kind}): columns[] is required")
            for ci, c in enumerate(cols, start=1):
                nm = (c.get("name") or "").strip()
                tp = (c.get("type") or "").strip()
                if not nm:
                    raise HTTPException(400, f"Operation #{i} ({kind}): columns[{ci}].name is required")
                if not tp:
                    raise HTTPException(400, f"Operation #{i} ({kind}): columns[{ci}].type is required")
                try:
                    assert_ident(nm, "column")
                except Exception as e:
                    raise HTTPException(400, f"Operation #{i} ({kind}): columns[{ci}] {e}")

# ==============================
# TENANTS + STORAGE + VOLUMES
# ==============================

@router.get("/tenants")
def list_tenants(master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.tenants.view")
    tenants = master_db.query(Tenant).order_by(Tenant.id.desc()).all()
    # Never return db_uri to UI
    return {"items": [{
        "id": t.id,
        "name": t.name,
        "code": t.code,
        "db_name": t.db_name,
        "subscription_plan": t.subscription_plan,
        "is_active": t.is_active,
        "created_at": t.created_at,
        "volume_tag": (t.meta or {}).get("volume_tag", "default"),
    } for t in tenants]}

@router.patch("/tenants/{tenant_id}/volume")
def set_volume(tenant_id: int, payload: Dict[str, Any], master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.storage.manage")
    t = master_db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    vol = (payload.get("volume_tag") or "default").strip()[:64] or "default"
    meta = t.meta or {}
    meta["volume_tag"] = vol
    t.meta = meta
    master_db.commit()
    return {"ok": True, "tenant_id": tenant_id, "volume_tag": vol}

@router.get("/tenants/{tenant_id}/storage")
def tenant_storage(tenant_id: int, master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.storage.view")
    t = master_db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    mb = tenant_db_usage_mb(t.db_name, master_db)
    return {"tenant_id": tenant_id, "db_name": t.db_name, "used_mb": mb}

@router.get("/storage/volumes")
def volumes_storage(master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.storage.view")
    return {"items": usage_by_volume(master_db)}

@router.get("/schema/types")
def allowed_types(u: Any = Depends(current_provider_user)):
    require_perm(u, "master.migrations.view")
    return {"items": TYPE_ALLOWLIST}

# ==============================
# SCHEMA INTROSPECTION (UI needs)
# ==============================

@router.get("/tenants/{tenant_id}/schema/tables")
def list_tables(tenant_id: int, master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.migrations.view")
    t = master_db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    rows = master_db.execute(text("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = :db
        ORDER BY table_name
    """), {"db": t.db_name}).scalars().all()
    return {"tenant_id": tenant_id, "db_name": t.db_name, "tables": list(rows)}

@router.get("/tenants/{tenant_id}/schema/tables/{table}/columns")
def list_columns(tenant_id: int, table: str, master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.migrations.view")
    table = assert_ident(table, "table")
    t = master_db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        raise HTTPException(404, "Tenant not found")

    rows = master_db.execute(text("""
      SELECT column_name, column_type, is_nullable, column_default, column_comment, ordinal_position
      FROM information_schema.columns
      WHERE table_schema=:db AND table_name=:tb
      ORDER BY ordinal_position
    """), {"db": t.db_name, "tb": table}).mappings().all()

    return {"tenant_id": tenant_id, "db_name": t.db_name, "table": table, "columns": list(rows)}

# ==============================
# PLAN / APPLY / JOBS
# ==============================

@router.post("/plan", response_model=PlanResponse)
def plan(req: Request, payload: PlanRequest, master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.migrations.manage")

    destructive_detected = any(is_destructive(op.op) for op in payload.ops)
    if destructive_detected and not payload.allow_destructive:
        raise HTTPException(400, "Destructive ops detected. Enable allow_destructive to proceed.")
    if destructive_detected and payload.allow_destructive and (payload.confirm_phrase or "").strip().upper() != "I UNDERSTAND":
        raise HTTPException(400, 'For destructive ops you must set confirm_phrase="I UNDERSTAND"')

    if payload.apply_all:
        tenants = master_db.query(Tenant).filter(Tenant.is_active == True).all()
    else:
        if not payload.tenant_ids:
            raise HTTPException(400, "tenant_ids required when apply_all=false")
        tenants = master_db.query(Tenant).filter(Tenant.id.in_(payload.tenant_ids), Tenant.is_active == True).all()

    if not tenants:
        raise HTTPException(400, "No tenants selected")

    ops_norm = [op.model_dump() for op in payload.ops]
    _validate_ops_or_400(ops_norm)
    global_ops, tenant_ops = _split_ops(ops_norm)

    global_sql: List[str] = []
    for op in global_ops:
        try:
            global_sql.extend(build_sql_for_op(op, db_name="mysql"))
        except ValueError as e:
            raise HTTPException(400, str(e))

    targets = []
    for t in tenants:
        sql_list: List[str] = []
        for op in tenant_ops:
            try:
                sql_list.extend(build_sql_for_op(op, db_name=t.db_name))
            except ValueError as e:
                raise HTTPException(400, str(e))

        targets.append({"tenant_id": t.id, "db_name": t.db_name, "sql": sql_list})

    audit(master_db, req, int(getattr(u, "id")), "MIGRATION_PLAN", "master/migrations/plan", {
        "name": payload.name,
        "apply_all": payload.apply_all,
        "tenant_count": len(targets),
        "dry_run": payload.dry_run,
        "allow_destructive": payload.allow_destructive,
        "ops_count": len(ops_norm),
    })

    return PlanResponse(destructive_detected=destructive_detected, global_sql=global_sql, targets=targets)

@router.post("/apply", response_model=ApplyResponse)
def apply(req: Request, payload: PlanRequest, master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.migrations.manage")

    destructive_detected = any(is_destructive(op.op) for op in payload.ops)
    if destructive_detected and not payload.allow_destructive:
        raise HTTPException(400, "Destructive ops detected. Enable allow_destructive to proceed.")
    if destructive_detected and payload.allow_destructive and (payload.confirm_phrase or "").strip().upper() != "I UNDERSTAND":
        raise HTTPException(400, 'For destructive ops you must set confirm_phrase="I UNDERSTAND"')

    # idempotency
    if payload.client_request_id:
        exists = master_db.execute(
            text("SELECT id FROM master_migration_jobs WHERE client_request_id=:c"),
            {"c": payload.client_request_id},
        ).scalar()
        if exists:
            return ApplyResponse(job_id=int(exists))

    if payload.apply_all:
        tenants = master_db.query(Tenant).filter(Tenant.is_active == True).all()
    else:
        if not payload.tenant_ids:
            raise HTTPException(400, "tenant_ids required when apply_all=false")
        tenants = master_db.query(Tenant).filter(Tenant.id.in_(payload.tenant_ids), Tenant.is_active == True).all()
    if not tenants:
        raise HTTPException(400, "No tenants selected")

    ops_norm = [op.model_dump() for op in payload.ops]
    _validate_ops_or_400(ops_norm)
    global_ops, tenant_ops = _split_ops(ops_norm)

    # Save job
    master_db.execute(
        text("""
            INSERT INTO master_migration_jobs
            (name, description, created_by, ops_json, apply_all, allow_destructive, dry_run, status, client_request_id)
            VALUES(:n,:d,:by, CAST(:ops AS JSON), :aa, :ad, :dr, 'RUNNING', :cid)
        """),
        {
            "n": payload.name,
            "d": payload.description,
            "by": int(getattr(u, "id")),
            "ops": json.dumps(ops_norm, ensure_ascii=False),
            "aa": 1 if payload.apply_all else 0,
            "ad": 1 if payload.allow_destructive else 0,
            "dr": 1 if payload.dry_run else 0,
            "cid": payload.client_request_id,
        },
    )

    job_id = int(master_db.execute(text("SELECT LAST_INSERT_ID()")).scalar())

    for t in tenants:
        master_db.execute(
            text("""INSERT INTO master_migration_job_targets(job_id, tenant_id, status)
                    VALUES(:j,:tid,'PENDING')"""),
            {"j": job_id, "tid": t.id},
        )
    master_db.commit()

    audit(master_db, req, int(getattr(u, "id")), "MIGRATION_APPLY", "master/migrations/apply", {
        "job_id": job_id,
        "apply_all": payload.apply_all,
        "tenant_count": len(tenants),
        "dry_run": payload.dry_run,
        "allow_destructive": payload.allow_destructive,
    })

    # ---- Execute global ops ONCE ----
    global_sql: List[str] = []
    try:
        for op in global_ops:
            for sql in build_sql_for_op(op, db_name="mysql"):
                global_sql.append(sql)
                if not payload.dry_run:
                    exec_sql(admin_engine("mysql"), sql)
    except Exception as e:
        master_db.execute(text("UPDATE master_migration_jobs SET status='FAILED' WHERE id=:id"), {"id": job_id})
        master_db.commit()
        raise HTTPException(500, f"Global migration failed: {e}")

    # ---- Execute tenant ops per tenant (sequential, safest) ----
    for t in tenants:
        cancel = master_db.execute(text("SELECT cancel_requested FROM master_migration_jobs WHERE id=:id"), {"id": job_id}).scalar()
        if cancel:
            master_db.execute(text("UPDATE master_migration_jobs SET status='CANCELLED' WHERE id=:id"), {"id": job_id})
            master_db.execute(text("""
                UPDATE master_migration_job_targets
                SET status='SKIPPED', finished_at=NOW(), error='Cancelled'
                WHERE job_id=:j AND status='PENDING'
            """), {"j": job_id})
            master_db.commit()
            break

        master_db.execute(text("""
            UPDATE master_migration_job_targets SET status='RUNNING', started_at=NOW()
            WHERE job_id=:j AND tenant_id=:t
        """), {"j": job_id, "t": t.id})
        master_db.commit()

        executed_sql: List[str] = []
        try:
            for op in tenant_ops:
                for sql in build_sql_for_op(op, db_name=t.db_name):
                    executed_sql.append(sql)
                    if not payload.dry_run:
                        exec_sql(admin_engine(t.db_name), sql)

            master_db.execute(text("""
                UPDATE master_migration_job_targets
                SET status='DONE', finished_at=NOW(), executed_sql=:sql
                WHERE job_id=:j AND tenant_id=:t
            """), {"j": job_id, "t": t.id, "sql": "\n".join(executed_sql)})
            master_db.commit()
        except Exception as e:
            master_db.execute(text("""
                UPDATE master_migration_job_targets
                SET status='FAILED', finished_at=NOW(), error=:err, executed_sql=:sql
                WHERE job_id=:j AND tenant_id=:t
            """), {"j": job_id, "t": t.id, "err": str(e), "sql": "\n".join(executed_sql)})
            master_db.execute(text("UPDATE master_migration_jobs SET status='FAILED' WHERE id=:id"), {"id": job_id})
            master_db.commit()
            break

    st = master_db.execute(text("SELECT status FROM master_migration_jobs WHERE id=:id"), {"id": job_id}).scalar()
    if st == "RUNNING":
        master_db.execute(text("UPDATE master_migration_jobs SET status='DONE' WHERE id=:id"), {"id": job_id})
        master_db.commit()

    return ApplyResponse(job_id=job_id)

@router.get("/jobs")
def jobs(master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.migrations.view")
    rows = master_db.execute(text("""
        SELECT id, name, status, created_at
        FROM master_migration_jobs
        ORDER BY id DESC
        LIMIT 50
    """)).mappings().all()
    return {"items": list(rows)}

@router.get("/jobs/{job_id}", response_model=JobDetail)
def job_detail(job_id: int, master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.migrations.view")
    job = master_db.execute(text("""
        SELECT id, name, description, status, created_at, apply_all, allow_destructive, dry_run
        FROM master_migration_jobs WHERE id=:id
    """), {"id": job_id}).mappings().first()
    if not job:
        raise HTTPException(404, "Job not found")
    tg = master_db.execute(text("""
        SELECT tenant_id, status, started_at, finished_at, error
        FROM master_migration_job_targets
        WHERE job_id=:id
        ORDER BY tenant_id
    """), {"id": job_id}).mappings().all()
    return {"job": job, "targets": list(tg)}

@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: int, req: Request, master_db: Session = Depends(get_master_db), u: Any = Depends(current_provider_user)):
    require_perm(u, "master.migrations.manage")
    master_db.execute(text("UPDATE master_migration_jobs SET cancel_requested=1 WHERE id=:id"), {"id": job_id})
    master_db.commit()
    audit(master_db, req, int(getattr(u, "id")), "MIGRATION_CANCEL", f"master/migrations/jobs/{job_id}/cancel", {"job_id": job_id})
    return {"ok": True}
