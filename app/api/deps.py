# app/api/deps.py
from __future__ import annotations

from typing import Optional, Tuple, Generator, Dict, Any, Set
import base64
import json

from fastapi import Depends, Header, HTTPException, Request
from jose import jwt, JWTError
from sqlalchemy.orm import Session, joinedload, sessionmaker
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.db.session import MasterSessionLocal, create_tenant_session
from app.models.tenant import Tenant
from app.models.user import User
from app.models.role import Role


# =========================================================
# MASTER DB
# =========================================================
def get_master_db() -> Generator[Session, None, None]:
    """
    Session for MASTER (tenant management) DB.
    """
    db = MasterSessionLocal()
    try:
        yield db
    finally:
        db.close()


# =========================================================
# AUTH HELPERS
# =========================================================
def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _decode_token(raw_token: str) -> dict:
    try:
        return jwt.decode(
            raw_token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALG],
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def _load_tenant_from_claims(payload: dict, master_db: Session) -> Tenant:
    tid = payload.get("tid")
    tcode = payload.get("tcode")

    if not tid and not tcode:
        raise HTTPException(status_code=401, detail="Missing tenant in token")

    q = master_db.query(Tenant)
    if tid:
        q = q.filter(Tenant.id == tid)
    if tcode:
        q = q.filter(Tenant.code == tcode)

    tenant: Optional[Tenant] = q.first()
    if not tenant:
        raise HTTPException(status_code=403, detail="Tenant not found")
    if not tenant.is_active:
        raise HTTPException(status_code=403, detail="Tenant inactive")
    return tenant


# =========================================================
# TENANT DB (per request)
# =========================================================
def get_db(
    authorization: Optional[str] = Header(None),
    master_db: Session = Depends(get_master_db),
) -> Generator[Session, None, None]:
    """
    TENANT DB session (per request).
    Any authenticated API using this automatically connects to correct tenant DB.
    """
    raw = _extract_bearer(authorization)
    if not raw:
        raise HTTPException(status_code=401, detail="Missing token")

    payload = _decode_token(raw)
    tenant = _load_tenant_from_claims(payload, master_db)

    db = create_tenant_session(tenant.db_uri)
    try:
        yield db
    finally:
        db.close()


def get_current_user_and_tenant_from_token(
    raw_token: str,
    master_db: Session,
) -> Tuple[User, Tenant]:
    """
    Decode token -> find Tenant -> open tenant DB -> load user (with roles+permissions).
    """
    if not raw_token:
        raise HTTPException(status_code=401, detail="Missing token")

    payload = _decode_token(raw_token)
    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    tenant = _load_tenant_from_claims(payload, master_db)

    db = create_tenant_session(tenant.db_uri)
    try:
        user: Optional[User] = (
            db.query(User)
            .options(joinedload(User.roles).joinedload(Role.permissions))
            .filter(User.email == email)
            .first()
        )

        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="User inactive")

        return user, tenant
    finally:
        db.close()


def current_user(
    authorization: Optional[str] = Header(None),
    master_db: Session = Depends(get_master_db),
) -> User:
    raw = _extract_bearer(authorization)
    user, _ = get_current_user_and_tenant_from_token(raw, master_db)
    return user


# =========================================================
# CONNECTOR TENANT DB (Analyzer Connector only)
# =========================================================
_connector_session_cache: Dict[str, sessionmaker] = {}
_connector_engine_cache: Dict[str, Engine] = {}


def _build_tenant_db_url(tenant_code: str) -> str:
    """
    Build SQLAlchemy URL for tenant DB using env/settings.

    Pattern: nabh_hims_<tenant_code>
    Example: smc001 -> nabh_hims_smc001
    """
    driver = getattr(settings, "DB_DRIVER", "pymysql")
    host = getattr(settings, "MYSQL_HOST", "localhost")
    port = getattr(settings, "MYSQL_PORT", 3306)
    user = getattr(settings, "MYSQL_USER", "root")
    password = getattr(settings, "MYSQL_PASSWORD", "")

    db_name = f"nabh_hims_{tenant_code}"

    if password:
        auth = f"{user}:{password}"
    else:
        auth = user

    return f"mysql+{driver}://{auth}@{host}:{port}/{db_name}?charset=utf8mb4"


def get_connector_tenant_db(request: Request) -> Generator[Session, None, None]:
    """
    Dependency for /api/connector/... routes.

    - Reads X-Tenant-Code header (e.g. 'smc001')
    - Builds DB URL: mysql+pymysql://.../nabh_hims_smc001
    - Returns a Session bound to that tenant DB
    """
    tenant_code = request.headers.get("X-Tenant-Code")
    if not tenant_code:
        raise HTTPException(status_code=400, detail="Missing X-Tenant-Code header")

    engine = _connector_engine_cache.get(tenant_code)
    if engine is None:
        url = _build_tenant_db_url(tenant_code)
        try:
            engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Could not open tenant database for tenant '{tenant_code}': {e}",
            )
        _connector_engine_cache[tenant_code] = engine

    SessionLocal = _connector_session_cache.get(tenant_code)
    if SessionLocal is None:
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        _connector_session_cache[tenant_code] = SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =========================================================
# MASTER / PROVIDER CONSOLE HELPERS
# =========================================================
def user_perm_codes(u: Any) -> Set[str]:
    codes: Set[str] = set()

    # direct permissions (if you ever have them)
    direct = getattr(u, "permissions", None) or []
    for p in direct:
        codes.add(p if isinstance(p, str) else getattr(p, "code", None))

    # role -> permissions (YOUR case)
    roles = getattr(u, "roles", None) or []
    for r in roles:
        perms = getattr(r, "permissions", None) or []
        for p in perms:
            codes.add(p if isinstance(p, str) else getattr(p, "code", None))

    return {c for c in codes if c}


def require_perm(u: Any, perm: str) -> None:
    # admin override
    if bool(getattr(u, "is_admin", False)) is True:
        return

    codes = user_perm_codes(u)
    if perm not in codes:
        raise HTTPException(status_code=403, detail=f"Forbidden: missing {perm}")


def require_provider_tenant(tenant: Tenant) -> None:
    """
    Provider = tenant.code == settings.PROVIDER_TENANT_CODE
    ✅ No more 500 if env not set: defaults to "NUTRYAH"
    """
    provider_code = (getattr(settings, "PROVIDER_TENANT_CODE", None) or "NUTRYAH").strip().upper()
    if (tenant.code or "").strip().upper() != provider_code:
        raise HTTPException(status_code=403, detail="Forbidden: Provider only")


def current_provider_user(
    authorization: Optional[str] = Header(None),
    master_db: Session = Depends(get_master_db),
) -> User:
    raw = _extract_bearer(authorization)
    user, tenant = get_current_user_and_tenant_from_token(raw, master_db)
    require_provider_tenant(tenant)
    return user


def current_provider_user_and_tenant(
    authorization: Optional[str] = Header(None),
    master_db: Session = Depends(get_master_db),
) -> Tuple[User, Tenant]:
    raw = _extract_bearer(authorization)
    user, tenant = get_current_user_and_tenant_from_token(raw, master_db)
    require_provider_tenant(tenant)
    return user, tenant