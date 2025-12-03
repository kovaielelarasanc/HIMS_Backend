# app/api/deps.py
from typing import Optional, Tuple

from fastapi import Depends, Header, HTTPException
from jose import jwt, JWTError
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.db.session import MasterSessionLocal, create_tenant_session
from app.models.tenant import Tenant
from app.models.user import User
from app.models.role import Role
from app.models.permission import Permission


def get_master_db():
    """
    Session for MASTER (tenant management) DB.
    """
    db = MasterSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _decode_token(raw_token: str) -> dict:
    try:
        return jwt.decode(raw_token,
                          settings.JWT_SECRET,
                          algorithms=[settings.JWT_ALG])
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


def get_db(
        authorization: Optional[str] = Header(None),
        master_db: Session = Depends(get_master_db),
):
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
        user: Optional[User] = (db.query(User).options(
            joinedload(User.roles).joinedload(
                Role.permissions)).filter(User.email == email).first())
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="User inactive")

        # user + roles + permissions are now loaded & detached safely
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
