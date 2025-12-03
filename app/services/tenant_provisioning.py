# app/services/tenant_provisioning.py
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session, Session as OrmSession

from app.core.config import settings
from app.core.security import hash_password
from app.db.init_db import init_tenant_db
from app.db.session import master_engine, get_or_create_tenant_engine
from app.models.tenant import Tenant
from app.models.user import User


def _create_physical_tenant_database(db_name: str) -> None:
    """
    CREATE DATABASE IF NOT EXISTS `<db_name>` … in MySQL.
    """
    safe_name = db_name.replace("`", "")
    stmt = text(f"CREATE DATABASE IF NOT EXISTS `{safe_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
    with master_engine.connect() as conn:
        conn.execute(stmt)


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
    """
    1. Create Tenant row in MASTER DB
    2. CREATE DATABASE for tenant
    3. Create all tenant tables + seed permissions
    4. Create first Admin user inside tenant DB
    """
    tenant_code = tenant_code.strip().upper()
    existing = master_db.query(Tenant).filter(
        Tenant.code == tenant_code).first()
    if existing:
        raise ValueError("Tenant code already exists")

    db_name = f"{settings.TENANT_DB_NAME_PREFIX}{tenant_code.lower()}"
    db_uri = settings.make_tenant_db_uri(db_name)

    # 1) Create physical DB
    _create_physical_tenant_database(db_name)

    # 2) Insert tenant in MASTER
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
        meta={
            "hospital_address": hospital_address,
        },
    )
    master_db.add(tenant)
    master_db.commit()
    master_db.refresh(tenant)

    # 3) Initialize tenant DB schema + permissions
    init_tenant_db(db_uri)

    # 4) Create initial admin user inside tenant DB
    eng = get_or_create_tenant_engine(db_uri)
    with OrmSession(eng) as tenant_db:
        admin_user = User(
            name=admin_name,
            email=admin_email,
            password_hash=hash_password(admin_password),
            is_admin=True,
            is_active=True,
        )
        tenant_db.add(admin_user)
        tenant_db.commit()

    # 5) Mark tenant as active
    tenant.onboarding_status = "active"
    master_db.commit()
    master_db.refresh(tenant)
    return tenant
