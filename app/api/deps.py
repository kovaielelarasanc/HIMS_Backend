# app/api/deps.py
from __future__ import annotations

from typing import Optional, Tuple, Generator, Dict, Any, Set
import re
from urllib.parse import quote_plus

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
        return jwt.decode(raw_token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
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
# DB URL BUILDERS (PROD SAFE)
# =========================================================
def _mysql_base_from_settings(
    user_key: str = "MYSQL_USER",
    pass_key: str = "MYSQL_PASSWORD",
) -> str:
    """
    Returns: mysql+<driver>://user:pass@host:port
    Password is URL-encoded (critical if password contains @, :, /, etc.)
    """
    driver = getattr(settings, "DB_DRIVER", "pymysql")
    host = getattr(settings, "MYSQL_HOST", None)
    port = getattr(settings, "MYSQL_PORT", 3306)

    user = getattr(settings, user_key, None)
    pw = getattr(settings, pass_key, "")

    if not host or not user:
        raise HTTPException(status_code=500, detail="MYSQL_HOST / MYSQL_USER not configured")

    user_q = quote_plus(str(user))
    pw_q = quote_plus(str(pw or ""))

    auth = f"{user_q}:{pw_q}" if (pw is not None and str(pw) != "") else user_q
    return f"mysql+{driver}://{auth}@{host}:{port}"


def _tenant_db_url_from_db_name(db_name: str) -> str:
    base = _mysql_base_from_settings("MYSQL_USER", "MYSQL_PASSWORD")
    return f"{base}/{db_name}?charset=utf8mb4"


def _resolve_tenant_db_uri(tenant: Tenant) -> str:
    """
    Prefer stored tenant.db_uri (if you keep it).
    If missing/blank, build from tenant.db_name and env MYSQL_*.
    """
    uri = (getattr(tenant, "db_uri", None) or "").strip()
    if uri:
        return uri

    db_name = (getattr(tenant, "db_name", None) or "").strip()
    if not db_name:
        raise HTTPException(status_code=500, detail="Tenant missing db_name/db_uri")
    return _tenant_db_url_from_db_name(db_name)


# =========================================================
# TENANT DB (per request)
# =========================================================
def get_db(
    authorization: Optional[str] = Header(None),
    master_db: Session = Depends(get_master_db),
) -> Generator[Session, None, None]:
    raw = _extract_bearer(authorization)
    if not raw:
        raise HTTPException(status_code=401, detail="Missing token")

    payload = _decode_token(raw)
    tenant = _load_tenant_from_claims(payload, master_db)

    db_uri = _resolve_tenant_db_uri(tenant)

    db = create_tenant_session(db_uri)
    try:
        yield db
    finally:
        db.close()


def get_current_user_and_tenant_from_token(raw_token: str, master_db: Session) -> Tuple[User, Tenant]:
    if not raw_token:
        raise HTTPException(status_code=401, detail="Missing token")

    payload = _decode_token(raw_token)
    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    tenant = _load_tenant_from_claims(payload, master_db)
    db_uri = _resolve_tenant_db_uri(tenant)

    db = create_tenant_session(db_uri)
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
    user, _tenant = get_current_user_and_tenant_from_token(raw, master_db)
    return user


# =========================================================
# CONNECTOR TENANT DB (Analyzer Connector only)
# =========================================================
_connector_session_cache: Dict[str, sessionmaker] = {}
_connector_engine_cache: Dict[str, Engine] = {}


def _sanitize_tenant_code(code: str) -> str:
    c = (code or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,32}", c):
        raise HTTPException(status_code=400, detail="Invalid X-Tenant-Code")
    return c


def _build_connector_db_url(tenant_code: str) -> str:
    """
    Optional separate connector creds:
      MYSQL_CONNECTOR_USER / MYSQL_CONNECTOR_PASSWORD
    Fallback:
      MYSQL_USER / MYSQL_PASSWORD
    """
    tenant_code = _sanitize_tenant_code(tenant_code)

    base = _mysql_base_from_settings(
        user_key="MYSQL_CONNECTOR_USER" if getattr(settings, "MYSQL_CONNECTOR_USER", None) else "MYSQL_USER",
        pass_key="MYSQL_CONNECTOR_PASSWORD" if getattr(settings, "MYSQL_CONNECTOR_PASSWORD", None) else "MYSQL_PASSWORD",
    )

    db_name = f"nabh_hims_{tenant_code}"
    return f"{base}/{db_name}?charset=utf8mb4"


def get_connector_tenant_db(request: Request) -> Generator[Session, None, None]:
    tenant_code = request.headers.get("X-Tenant-Code")
    if not tenant_code:
        raise HTTPException(status_code=400, detail="Missing X-Tenant-Code header")

    tenant_code = _sanitize_tenant_code(tenant_code)

    engine = _connector_engine_cache.get(tenant_code)
    if engine is None:
        url = _build_connector_db_url(tenant_code)
        engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
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

    direct = getattr(u, "permissions", None) or []
    for p in direct:
        code = p if isinstance(p, str) else getattr(p, "code", None)
        if code:
            codes.add(code)

    roles = getattr(u, "roles", None) or []
    for r in roles:
        perms = getattr(r, "permissions", None) or []
        for p in perms:
            code = p if isinstance(p, str) else getattr(p, "code", None)
            if code:
                codes.add(code)

    return codes


def require_perm(u: Any, perm: str) -> None:
    if bool(getattr(u, "is_admin", False)) is True:
        return
    if perm not in user_perm_codes(u):
        raise HTTPException(status_code=403, detail=f"Forbidden: missing {perm}")


def require_provider_tenant(tenant: Tenant) -> None:
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
