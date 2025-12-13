# app/services/tenant_provisioning.py
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.core.security import hash_password
from app.db.init_db import init_tenant_db
from app.db.session import master_engine, get_or_create_tenant_engine
from app.models.tenant import Tenant
from app.models.user import User


def _create_physical_tenant_database(db_name: str) -> None:
    safe_name = db_name.replace("`", "").strip()
    if not safe_name:
        raise ValueError("Invalid tenant db_name")

    stmt = text(
        f"CREATE DATABASE IF NOT EXISTS `{safe_name}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    )
    with master_engine.begin() as conn:
        conn.execute(stmt)


def _tenant_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def provision_tenant_with_admin(
    master_db: Session,
    *,
    tenant_name: str,
    tenant_code: str,
    hospital_address: Optional[str],
    contact_person: str,
    contact_phone: Optional[str],
    subscription_plan: Optional[str],
    amc_percent: Optional[int],
    admin_name: str,
    admin_email: str,
    admin_password: str,
) -> Tenant:
    tenant_code = (tenant_code or "").strip().upper()
    if not tenant_code:
        raise ValueError("Tenant code is required")

    # ✅ If master tables are missing, raise a clear error
    try:
        existing = master_db.query(Tenant).filter(Tenant.code == tenant_code).first()
    except ProgrammingError as e:
        raise RuntimeError(
            "MASTER DB tables missing. Your MASTER_DATABASE_URI is pointing to the wrong DB "
            "or you haven't created master tables. Expected table: tenants."
        ) from e

    if existing:
        raise ValueError("Tenant code already exists")

    db_name = f"{settings.TENANT_DB_NAME_PREFIX}{tenant_code.lower()}"
    db_uri = settings.make_tenant_db_uri(db_name)

    _create_physical_tenant_database(db_name)

    tenant = Tenant(
        code=tenant_code,
        name=tenant_name,
        db_name=db_name,
        db_uri=db_uri,
        contact_person=contact_person,
        contact_email=admin_email,
        contact_phone=contact_phone,
        subscription_plan=subscription_plan,
        amc_percent=amc_percent,
        onboarding_status="provisioning",
        meta={"hospital_address": hospital_address},
    )

    try:
        master_db.add(tenant)
        master_db.commit()
        master_db.refresh(tenant)

        init_tenant_db(db_uri)

        eng = get_or_create_tenant_engine(db_uri)
        if isinstance(eng, tuple):
            raise RuntimeError("get_or_create_tenant_engine returned tuple. Fix app/db/session.py")

        TenantSessionLocal = _tenant_sessionmaker(eng)
        tenant_db = TenantSessionLocal()
        try:
            found = tenant_db.query(User).filter(User.email == admin_email).first()
            if not found:
                admin_user = User(
                    name=admin_name,
                    email=admin_email,
                    password_hash=hash_password(admin_password),
                    is_admin=True,
                    is_active=True,
                )
                tenant_db.add(admin_user)
                tenant_db.commit()
        except Exception:
            tenant_db.rollback()
            raise
        finally:
            tenant_db.close()

        tenant.onboarding_status = "active"
        master_db.commit()
        master_db.refresh(tenant)
        return tenant

    except Exception:
        master_db.rollback()
        raise
