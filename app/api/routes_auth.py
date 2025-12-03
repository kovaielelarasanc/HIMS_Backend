# app/api/routes_auth.py
import random
from datetime import datetime, timedelta
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    Query,
    Request,
    Response,
)
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from app.api.deps import (
    get_master_db,
    get_current_user_and_tenant_from_token,
)
from app.core.config import settings
from app.core.emailer import send_email
from app.core.security import verify_password
from app.db.session import create_tenant_session
from app.models.tenant import Tenant
from app.models.user import User
from app.models.permission import Permission
from app.models.otp import OtpToken
from app.schemas.auth import RegisterAdminIn, LoginIn, OtpVerifyIn, TokenOut
from app.services.tenant_provisioning import provision_tenant_with_admin
from app.utils.jwt import create_access_refresh

router = APIRouter()


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _compute_license_dates(plan: Optional[str]):
    """
    Returns (license_start, license_end, amc_next_due) as datetimes
    based on subscription plan:
      - basic   -> 30 days
      - standard-> 6 * 30 days
      - premium -> 365 days
    """
    now = datetime.utcnow()
    plan_norm = (plan or "basic").strip().lower()

    if plan_norm == "basic":
        delta = timedelta(days=30)
    elif plan_norm == "standard":
        delta = timedelta(days=30 * 6)
    elif plan_norm == "premium":
        delta = timedelta(days=365)
    else:
        # fallback: treat unknown like monthly
        delta = timedelta(days=30)

    license_start = now
    license_end = now + delta
    amc_next_due = license_end + timedelta(days=10)

    return license_start, license_end, amc_next_due


def _load_tenant_from_refresh_payload(payload: dict,
                                      master_db: Session) -> Tenant:
    """
    Helper for /auth/refresh:
    Load Tenant using tid/tcode claims from refresh token.
    """
    tid = payload.get("tid")
    tcode = payload.get("tcode")

    if not tid and not tcode:
        raise HTTPException(status_code=401, detail="Missing tenant in token")

    q = master_db.query(Tenant)
    if tid:
        q = q.filter(Tenant.id == tid)
    if tcode:
        q = q.filter(Tenant.code == tcode)

    tenant = q.first()
    if not tenant:
        raise HTTPException(status_code=403, detail="Tenant not found")
    if not tenant.is_active:
        raise HTTPException(status_code=403, detail="Tenant inactive")
    return tenant


# ---------------------------------------------------------------------
#  Admin registration (first tenant + admin user)
# ---------------------------------------------------------------------


@router.post("/register-admin")
def register_admin(
        payload: RegisterAdminIn,
        master_db: Session = Depends(get_master_db),
):
    """
    FIRST STEP: Register tenant + first admin.
    - Create Tenant in MASTER DB
    - Create tenant DB & tables
    - Create Admin user inside tenant DB

    Also:
    - Auto-compute license_start_date (now)
    - Auto-compute license_end_date based on plan
    - Auto-compute amc_next_due = license_end_date + 10 days
    """
    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    tenant_code = (payload.tenant_code
                   or payload.tenant_name.replace(" ", "").upper())

    try:
        # Create tenant + tenant DB + admin user
        tenant = provision_tenant_with_admin(
            master_db,
            tenant_name=payload.tenant_name,
            tenant_code=tenant_code,
            hospital_address=payload.hospital_address,
            contact_person=payload.contact_person,
            contact_phone=payload.contact_phone,
            subscription_plan=payload.subscription_plan,
            amc_percent=payload.
            amc_percent,  # ⚠ can later be overridden only by NDH admin
            admin_name=payload.admin_name,
            admin_email=payload.email,
            admin_password=payload.password,
        )

        # 🔐 Commercial logic (master side, not visible to hospital client)
        # Auto-calc license dates based on plan
        lic_start, lic_end, amc_next_due = _compute_license_dates(
            payload.subscription_plan)

        tenant.license_start_date = lic_start
        tenant.license_end_date = lic_end
        tenant.amc_next_due = amc_next_due

        # NOTE: subscription_amount, amc_percent etc. SHOULD be updated
        # later from an internal NDH Admin panel / API, not by the hospital.
        # Here we only set dates; amounts stay null or default.
        master_db.commit()
        master_db.refresh(tenant)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "message":
        "Tenant created and Admin user provisioned. Proceed to login.",
        "tenant_id": tenant.id,
        "tenant_code": tenant.code,
    }


# ---------------------------------------------------------------------
#  Login (email + password -> OTP)
# ---------------------------------------------------------------------


@router.post("/login")
def login(
        payload: LoginIn,
        master_db: Session = Depends(get_master_db),
):
    """
    Login for any user of a given tenant (hospital).
    Requires tenant_code + email + password.
    Sends OTP to email.
    """
    tenant = (master_db.query(Tenant).filter(
        Tenant.code == payload.tenant_code.strip().upper(),
        Tenant.is_active.is_(True),
    ).first())
    if not tenant:
        raise HTTPException(status_code=404,
                            detail="Tenant not found or inactive")

    tenant_db = create_tenant_session(tenant.db_uri)
    try:
        user = (tenant_db.query(User).filter(
            User.email == payload.email).first())
        if not user or not verify_password(payload.password,
                                           user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # OTP generation & store in tenant DB
        otp = f"{random.randint(0, 999999):06d}"
        token = OtpToken(
            user_id=user.id,
            otp_code=otp,
            expires_at=OtpToken.expiry(10),
        )
        tenant_db.add(token)
        tenant_db.commit()
    finally:
        tenant_db.close()

    # Send OTP email
    send_email(
        to_email=payload.email,
        subject=f"{settings.PROJECT_NAME} — Your OTP",
        body=f"Your OTP is {otp}. It will expire in 10 minutes.",
    )

    return {"message": "OTP sent to email"}


# ---------------------------------------------------------------------
#  Verify OTP -> issue JWTs (access + refresh)
# ---------------------------------------------------------------------


@router.post("/verify-otp", response_model=TokenOut)
def verify_otp(
        payload: OtpVerifyIn,
        response: Response,
        master_db: Session = Depends(get_master_db),
):
    """
    Verify OTP for a user in a tenant and issue JWTs with tenant info.

    - Returns access_token + refresh_token in JSON (TokenOut)
    - ALSO sets `refresh_token` as HttpOnly cookie
      so frontend can auto-refresh without storing refresh in JS.
    """
    tenant = (master_db.query(Tenant).filter(
        Tenant.code == payload.tenant_code.strip().upper(),
        Tenant.is_active.is_(True),
    ).first())
    if not tenant:
        raise HTTPException(status_code=404,
                            detail="Tenant not found or inactive")

    tenant_db = create_tenant_session(tenant.db_uri)
    try:
        user = (tenant_db.query(User).filter(
            User.email == payload.email).first())
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        otp_row = (tenant_db.query(OtpToken).filter(
            OtpToken.user_id == user.id,
            OtpToken.otp_code == payload.otp,
            OtpToken.used.is_(False),
        ).order_by(OtpToken.id.desc()).first())
        if not otp_row:
            raise HTTPException(status_code=400, detail="Invalid OTP")

        if otp_row.expires_at < datetime.utcnow():
            raise HTTPException(status_code=400, detail="OTP expired")

        otp_row.used = True
        tenant_db.commit()
    finally:
        tenant_db.close()

    # Use payload.email instead of user.email (user is detached now)
    access, refresh = create_access_refresh(
        subject=payload.email,
        tenant_id=tenant.id,
        tenant_code=tenant.code,
    )

    # 🔐 Store refresh token in HttpOnly cookie (used by /auth/refresh)
    # NOTE: set secure=True when running behind HTTPS in production.
    response.set_cookie(
        key="refresh_token",
        value=refresh,
        httponly=True,
        secure=False,  # change to True in production with HTTPS
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )

    return {"access_token": access, "refresh_token": refresh}


# ---------------------------------------------------------------------
#  Refresh access token (using refresh_token cookie)
# ---------------------------------------------------------------------


@router.post("/refresh", response_model=TokenOut)
def refresh_token(
        request: Request,
        response: Response,
        master_db: Session = Depends(get_master_db),
):
    """
    Refresh access_token using refresh_token (HttpOnly cookie).

    - Reads refresh_token from cookies (fallback: Authorization Bearer)
    - Validates JWT (must be refresh token)
    - Loads tenant + user
    - Issues new access + refresh
    - Sets new refresh_token cookie
    """
    raw_rt = request.cookies.get("refresh_token")

    # Fallback: allow refresh token via Authorization: Bearer <rt>
    if not raw_rt:
        auth_header = request.headers.get("Authorization")
        raw_rt = _extract_bearer(auth_header)

    if not raw_rt:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    try:
        payload = jwt.decode(
            raw_rt,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALG],
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    token_type = payload.get("type")
    # If you always set "type": "refresh" in create_access_refresh, enforce it.
    if token_type and token_type != "refresh":
        raise HTTPException(status_code=401, detail="Wrong token type")

    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid refresh payload")

    # Load tenant from claims
    tenant = _load_tenant_from_refresh_payload(payload, master_db)

    # Load user from tenant DB
    tenant_db = create_tenant_session(tenant.db_uri)
    try:
        user = (tenant_db.query(User).filter(User.email == email).first())
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="User inactive")
    finally:
        tenant_db.close()

    # Create fresh access + refresh tokens
    access, new_refresh = create_access_refresh(
        subject=email,
        tenant_id=tenant.id,
        tenant_code=tenant.code,
    )

    # Rotate refresh cookie
    response.set_cookie(
        key="refresh_token",
        value=new_refresh,
        httponly=True,
        secure=False,  # True in production
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )

    return {"access_token": access, "refresh_token": new_refresh}


# ---------------------------------------------------------------------
#  /me  (current user + tenant)
# ---------------------------------------------------------------------


@router.get("/me")
def me(
        authorization: Optional[str] = Header(None),
        token: Optional[str] = Query(None),
        master_db: Session = Depends(get_master_db),
):
    """
    Return current user + tenant info.
    """
    raw = _extract_bearer(authorization) or token
    user, tenant = get_current_user_and_tenant_from_token(raw, master_db)
    role_names = [r.name for r in (user.roles or [])]

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "is_admin": user.is_admin,
        "department_id": user.department_id,
        "roles": role_names,
        "tenant_id": tenant.id,
        "tenant_code": tenant.code,
        "tenant_name": tenant.name,
    }


# ---------------------------------------------------------------------
#  /me/permissions  (RBAC)
# ---------------------------------------------------------------------


@router.get("/me/permissions")
def my_permissions(
        authorization: Optional[str] = Header(None),
        token: Optional[str] = Query(None),
        master_db: Session = Depends(get_master_db),
):
    raw = _extract_bearer(authorization) or token
    user, tenant = get_current_user_and_tenant_from_token(raw, master_db)

    # Admin: full access
    if user.is_admin and settings.ADMIN_ALL_ACCESS:
        tenant_db = create_tenant_session(tenant.db_uri)
        try:
            rows = tenant_db.query(Permission).all()
            modules = {}
            for p in rows:
                modules.setdefault(p.module, []).append({
                    "code": p.code,
                    "label": p.label
                })
            return {"modules": modules}
        finally:
            tenant_db.close()

    # Role-based permissions
    perms = set()
    for role in (user.roles or []):
        for p in (role.permissions or []):
            perms.add((p.code, p.label, p.module))

    modules = {}
    for code, label, module in perms:
        modules.setdefault(module, []).append({"code": code, "label": label})

    return {"modules": modules}
