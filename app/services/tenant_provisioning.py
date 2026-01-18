# FILE: app/services/tenant_provisioning.py
from __future__ import annotations

import re
import random
import logging
from typing import Optional, List, Set

from sqlalchemy import text, create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.security import hash_password
from app.db.init_db import init_tenant_db
from app.db.session import master_engine, get_or_create_tenant_engine
from app.models.tenant import Tenant
from app.models.user import User

# If you have this model/table (you do in your tables list), we use it for 6-digit login_id
try:
    from app.models.user import UserLoginSeq  # type: ignore
except Exception:  # pragma: no cover
    UserLoginSeq = None  # fallback handled below

logger = logging.getLogger(__name__)

# Minimal “this looks like our tenant schema” markers
CORE_MARKER_TABLES: Set[str] = {
    "users",
    "roles",
    "permissions",
    "role_permissions",
}


def _sanitize_db_name(db_name: str) -> str:
    """
    Ensure db_name is safe for CREATE DATABASE.
    Allows letters, digits, underscore only.
    """
    s = (db_name or "").strip()
    s = s.replace("`", "")
    s = re.sub(r"[^a-zA-Z0-9_]", "_", s)
    s = s.strip("_")
    if not s:
        raise ValueError("Invalid tenant db_name")
    return s


def _create_physical_tenant_database(db_name: str) -> None:
    safe_name = _sanitize_db_name(db_name)

    stmt = text(f"CREATE DATABASE IF NOT EXISTS `{safe_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")

    try:
        with master_engine.begin() as conn:
            conn.execute(stmt)
    except SQLAlchemyError as e:
        raise RuntimeError(
            f"Failed to create tenant database '{safe_name}'. Check MySQL privileges."
        ) from e


def _tenant_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


def _unwrap_engine(maybe_engine):
    """
    Some implementations return (engine, created_bool) etc.
    We accept both.
    """
    if isinstance(maybe_engine, tuple) and maybe_engine:
        return maybe_engine[0]
    return maybe_engine


def _norm_email(s: str) -> str:
    return (s or "").strip().lower()


def _list_tables(db_uri: str) -> List[str]:
    """
    Read existing tables in tenant DB.
    """
    eng = create_engine(db_uri, pool_pre_ping=True, future=True)
    insp = inspect(eng)
    return sorted(insp.get_table_names())


def _looks_like_tenant_schema(tables: List[str]) -> bool:
    s = set(tables or [])
    return CORE_MARKER_TABLES.issubset(s)


def _generate_login_id(tenant_db: Session) -> str:
    """
    Prefer user_login_seq for guaranteed unique 6-digit IDs.
    Fallback to random unique if seq table/model not available.
    """
    if UserLoginSeq is not None:
        try:
            seq = UserLoginSeq()
            tenant_db.add(seq)
            tenant_db.flush()  # seq.id available without commit
            return f"{int(seq.id):06d}"
        except Exception:
            tenant_db.rollback()

    # fallback: random 6-digit unique
    for _ in range(50):
        cand = f"{random.randint(0, 999999):06d}"
        exists = tenant_db.query(User).filter(User.login_id == cand).first()
        if not exists:
            return cand

    raise RuntimeError("Failed to generate unique 6-digit login_id")


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

    if not (tenant_name or "").strip():
        raise ValueError("Tenant name is required")

    admin_email = _norm_email(admin_email)
    if not admin_email:
        raise ValueError("Admin email is required")

    # ✅ If master tables missing / wrong MASTER_DATABASE_URI, raise clearly
    try:
        existing = master_db.query(Tenant).filter(
            Tenant.code == tenant_code).first()
    except ProgrammingError as e:
        raise RuntimeError(
            "MASTER DB tables missing or wrong MASTER_DATABASE_URI. Expected table: tenants."
        ) from e

    if existing:
        raise ValueError("Tenant code already exists")

    # Build tenant db name + uri
    db_name = f"{settings.TENANT_DB_NAME_PREFIX}{tenant_code.lower()}"
    db_name = _sanitize_db_name(db_name)
    db_uri = settings.make_tenant_db_uri(db_name)

    # 1) Create physical database
    _create_physical_tenant_database(db_name)

    # ✅ 1.1) Detect if tenant DB already has tables (retry-safe)
    tables_before = _list_tables(db_uri)
    already_initialized = bool(tables_before) and _looks_like_tenant_schema(
        tables_before)

    if tables_before and not already_initialized:
        # DB exists but not our schema → refuse to touch
        raise ValueError(
            f"Tenant database '{db_name}' already contains tables but does not look like a valid tenant schema. "
            f"Refusing to provision into this DB.")

    # 2) Create tenant row in master DB (provisioning)
    tenant = Tenant(
        code=tenant_code,
        name=tenant_name.strip(),
        db_name=db_name,
        db_uri=db_uri,
        contact_person=contact_person.strip(),
        contact_email=admin_email,
        contact_phone=(contact_phone.strip() if contact_phone else None),
        subscription_plan=(subscription_plan.strip()
                           if subscription_plan else None),
        amc_percent=amc_percent,
        onboarding_status="provisioning",
        meta={"hospital_address": hospital_address}
        if hospital_address else {"hospital_address": None},
    )

    master_db.add(tenant)
    try:
        master_db.commit()
        master_db.refresh(tenant)
    except IntegrityError as e:
        master_db.rollback()
        raise ValueError("Tenant code already exists") from e
    except SQLAlchemyError as e:
        master_db.rollback()
        raise RuntimeError("Failed to create tenant in master DB") from e

    # 3) Init tenant schema + seed (ONLY if empty)
    try:
        if already_initialized:
            logger.info(
                "Tenant DB '%s' already initialized; skipping init_tenant_db()",
                db_name)
        else:
            init_tenant_db(db_uri)
    except ValueError as e:
        # ✅ If init_tenant_db throws ValueError because tables already exist,
        # treat it as success only if schema looks correct.
        tables_now = _list_tables(db_uri)
        if tables_now and _looks_like_tenant_schema(tables_now):
            logger.warning(
                "init_tenant_db() raised ValueError but schema exists; continuing. err=%s",
                str(e))
        else:
            # mark failed
            try:
                tenant.onboarding_status = "failed"
                if hasattr(tenant, "meta") and isinstance(
                        getattr(tenant, "meta", None), dict):
                    tenant.meta[
                        "provision_error"] = f"init_tenant_db ValueError: {repr(e)}"
                master_db.commit()
            except Exception:
                master_db.rollback()
            raise
    except Exception as e:
        # mark failed (do not hide root issue)
        try:
            tenant.onboarding_status = "failed"
            if hasattr(tenant, "meta") and isinstance(
                    getattr(tenant, "meta", None), dict):
                tenant.meta["provision_error"] = repr(e)
            master_db.commit()
        except Exception:
            master_db.rollback()
        raise

    # 4) Create admin in tenant DB (with YOUR concept)
    eng = _unwrap_engine(get_or_create_tenant_engine(db_uri))
    if eng is None or not hasattr(eng, "connect"):
        # engine not valid
        try:
            tenant.onboarding_status = "failed"
            if hasattr(tenant, "meta") and isinstance(
                    getattr(tenant, "meta", None), dict):
                tenant.meta[
                    "provision_error"] = "Invalid tenant engine from get_or_create_tenant_engine"
            master_db.commit()
        except Exception:
            master_db.rollback()
        raise RuntimeError(
            "Invalid tenant engine. Fix app/db/session.py get_or_create_tenant_engine()."
        )

    TenantSessionLocal = _tenant_sessionmaker(eng)
    tenant_db = TenantSessionLocal()
    try:
        # If already exists, do not duplicate
        found = tenant_db.query(User).filter(User.email == admin_email).first()

        if not found:
            login_id = _generate_login_id(tenant_db)

            admin_user = User(
                login_id=login_id,  # ✅ 6-digit
                name=admin_name.strip(),
                email=admin_email,
                email_verified=False,  # ✅ must verify
                password_hash=hash_password(admin_password),
                is_admin=True,
                is_active=True,
                is_doctor=False,
                department_id=None,
                token_version=1,
                # ✅ your required defaults
                two_fa_enabled=True,  # ✅ ALWAYS true
                multi_login_enabled=False,  # ✅ ALWAYS false
            )

            tenant_db.add(admin_user)
            tenant_db.commit()
        else:
            # Ensure your defaults are enforced even if user exists
            changed = False
            if getattr(found, "two_fa_enabled", None) is not True:
                found.two_fa_enabled = True
                changed = True
            if getattr(found, "multi_login_enabled", None) is not False:
                found.multi_login_enabled = False
                changed = True
            if getattr(found, "is_admin", None) is not True:
                found.is_admin = True
                changed = True
            if getattr(found, "is_active", None) is not True:
                found.is_active = True
                changed = True
            if getattr(found, "email_verified", None) is not False:
                found.email_verified = False
                changed = True
            if changed:
                tenant_db.commit()

    except Exception as e:
        tenant_db.rollback()
        # mark failed in master
        try:
            tenant.onboarding_status = "failed"
            if hasattr(tenant, "meta") and isinstance(
                    getattr(tenant, "meta", None), dict):
                tenant.meta[
                    "provision_error"] = f"admin create error: {repr(e)}"
            master_db.commit()
        except Exception:
            master_db.rollback()
        raise
    finally:
        tenant_db.close()

    # 5) Mark tenant active
    try:
        tenant.onboarding_status = "active"
        if hasattr(tenant, "is_active"):
            tenant.is_active = True
        master_db.commit()
        master_db.refresh(tenant)
    except Exception:
        master_db.rollback()
        return tenant

    return tenant
