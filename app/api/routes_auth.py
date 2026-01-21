# FILE: app/api/routes_auth.py
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Tuple
import re
import traceback
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request, Response
from jose import jwt, JWTError
from sqlalchemy.orm import Session
import logging
from app.api.deps import get_master_db, get_current_user_and_tenant_from_token
from app.core.config import settings
from app.core.security import verify_password
from app.db.session import create_tenant_session
from app.models.tenant import Tenant
from app.models.user import User, UserSession
from app.models.permission import Permission
from app.schemas.auth import LoginIn, OtpVerifyIn, TokenOut, RegisterAdminIn
from app.utils.jwt import create_access_refresh
from app.services.otp_service import send_login_otp, send_email_verify_otp, verify_and_consume
from app.services.tenant_provisioning import provision_tenant_with_admin
router = APIRouter()

MULTI_LOGIN_BLOCK_MESSAGE = (
    "This Login ID is already active on another device. If this was not you, please contact the administrator "
    "to change your password immediately to protect your data."
)

logger = logging.getLogger(__name__)
# -------------------------
# Helpers (safe)
# -------------------------
def _derive_tenant_code(tenant_name: str, tenant_code: Optional[str]) -> str:
    """
    Same concept as your code, but safer:
    - if tenant_code provided -> use it
    - else derive from tenant_name
    - uppercase, remove spaces and non-alphanumerics
    """
    base = (tenant_code or tenant_name or "").strip()
    if not base:
        return ""
    base = base.replace(" ", "").upper()
    base = re.sub(r"[^A-Z0-9_]", "", base)
    return base.strip()


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    name, dom = email.split("@", 1)
    if len(name) <= 2:
        return f"{name[0]}***@{dom}"
    return f"{name[:2]}***@{dom}"


def _compute_license_dates(subscription_plan: Optional[str]) -> Tuple[date, date, date]:
    """
    Compute license_start_date, license_end_date, amc_next_due.

    Supports frontend values:
      - basic    -> monthly
      - standard -> 6 months
      - premium  -> 1 year

    Also supports common aliases:
      monthly, 1m, m
      quarterly, 3m, q
      halfyearly, 6m, hy
      yearly, 12m, y, annual
    """
    today = date.today()
    plan = (subscription_plan or "").strip().lower()

    # Map your UI plans to durations
    if plan in ("basic", "monthly", "1m", "m"):
        end = today + timedelta(days=30)
    elif plan in ("quarterly", "3m", "q"):
        end = today + timedelta(days=90)
    elif plan in ("standard", "halfyearly", "6m", "hy"):
        end = today + timedelta(days=182)
    elif plan in ("premium", "yearly", "12m", "y", "annual"):
        end = today + timedelta(days=365)
    else:
        # Safe default: 1 year
        end = today + timedelta(days=365)

    # Simple rule: AMC due on license end date
    amc_next_due = end
    return today, end, amc_next_due

# -------------------------
# helpers
# -------------------------
def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _tenant_by_code(master_db: Session, tenant_code: str) -> Tenant:
    t = (
        master_db.query(Tenant)
        .filter(Tenant.code == tenant_code.strip().upper(), Tenant.is_active.is_(True))
        .first()
    )
    if not t:
        raise HTTPException(status_code=404, detail="Tenant not found or inactive")
    return t


def _utcnow() -> datetime:
    # naive UTC (consistent with your DB string storage)
    return datetime.utcnow()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """
    Parse ISO strings safely and normalize to naive UTC.
    Accepts: "YYYY-MM-DDTHH:MM:SS[.ffffff]" and "...Z"
    """
    if not s:
        return None
    try:
        t = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _is_session_expired(sess: UserSession, now: datetime) -> bool:
    exp = _parse_iso(getattr(sess, "expires_at", None))
    if not exp:
        return True
    return exp <= now


def _get_client_ip(request: Request) -> str:
    """
    Works behind Cloudflare / Nginx / reverse proxies.
    Priority:
      - CF-Connecting-IP (Cloudflare)
      - X-Forwarded-For (first IP)
      - X-Real-IP
      - request.client.host
    """
    h = request.headers
    cf = (h.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf

    xff = (h.get("x-forwarded-for") or "").strip()
    if xff:
        # may be: "client, proxy1, proxy2"
        return xff.split(",")[0].strip()

    xri = (h.get("x-real-ip") or "").strip()
    if xri:
        return xri

    return request.client.host if request.client else ""


def _device_sig(request: Request) -> Tuple[str, str, str]:
    """
    Signature for "same device" heuristic.
    If frontend sends X-Device-Id, we use it (best).
    Else fallback to (ip + user-agent).
    """
    device_id = (request.headers.get("x-device-id") or "").strip()
    ip = _get_client_ip(request)
    ua = (request.headers.get("user-agent") or "").strip()
    return device_id, ip, ua


def _cleanup_expired_sessions(db: Session, user_id: int) -> None:
    now = _utcnow()
    rows = (
        db.query(UserSession)
        .filter(UserSession.user_id == int(user_id), UserSession.revoked_at.is_(None))
        .all()
    )
    changed = False
    for s in rows:
        if _is_session_expired(s, now):
            s.revoked_at = now.isoformat()
            s.revoke_reason = "expired"
            changed = True
    if changed:
        db.commit()


def _active_sessions(db: Session, user_id: int):
    now = _utcnow()
    rows = (
        db.query(UserSession)
        .filter(UserSession.user_id == int(user_id), UserSession.revoked_at.is_(None))
        .order_by(UserSession.id.desc())
        .all()
    )
    return [s for s in rows if not _is_session_expired(s, now)]


def _same_device(s: UserSession, request: Request) -> bool:
    device_id, ip, ua = _device_sig(request)

    s_device = (getattr(s, "device_id", None) or "").strip()
    s_ip = (getattr(s, "ip", None) or "").strip()
    s_ua = (getattr(s, "user_agent", None) or "").strip()

    # Best match if device_id exists on both sides
    if device_id and s_device:
        return s_device == device_id

    # Fallback match
    return (s_ip == ip) and (s_ua == ua)


def _has_other_device_active_session(db: Session, user_id: int, request: Request) -> bool:
    sessions = _active_sessions(db, user_id)
    for s in sessions:
        if _same_device(s, request):
            continue
        return True
    return False


def _revoke_same_device_sessions(db: Session, user_id: int, request: Request, reason: str) -> None:
    now = _utcnow().isoformat()
    rows = (
        db.query(UserSession)
        .filter(UserSession.user_id == int(user_id), UserSession.revoked_at.is_(None))
        .all()
    )
    changed = False
    for s in rows:
        if _same_device(s, request):
            s.revoked_at = now
            s.revoke_reason = str(reason)
            changed = True
    if changed:
        db.commit()


def _revoke_session_by_sid(db: Session, user_id: int, sid: str, reason: str) -> None:
    now = _utcnow().isoformat()
    sess = (
        db.query(UserSession)
        .filter(UserSession.user_id == int(user_id), UserSession.session_id == str(sid))
        .first()
    )
    if sess and sess.revoked_at is None:
        sess.revoked_at = now
        sess.revoke_reason = str(reason)
        db.commit()


def _create_session(db: Session, user: User, request: Request) -> str:
    sid = str(uuid.uuid4())
    now = _utcnow().isoformat()
    expires_at = (_utcnow() + timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)).isoformat()

    device_id, ip, ua = _device_sig(request)

    sess = UserSession(
        user_id=user.id,
        session_id=sid,
        ip=ip or None,
        user_agent=ua or None,
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
        revoked_at=None,
        revoke_reason=None,
    )

    # schema-safe: only set if column exists
    if hasattr(sess, "device_id"):
        setattr(sess, "device_id", device_id or None)

    db.add(sess)
    db.commit()
    return sid


def _set_refresh_cookie(response: Response, refresh: str) -> None:
    cookie_domain = getattr(settings, "COOKIE_DOMAIN", None) or None

    response.set_cookie(
        key="refresh_token",
        value=refresh,
        httponly=True,
        secure=bool(getattr(settings, "COOKIE_SECURE", False)),
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
        domain=cookie_domain,  # optional; set ".nutryah.com" in prod if needed
    )


def _clear_refresh_cookie(response: Response) -> None:
    cookie_domain = getattr(settings, "COOKIE_DOMAIN", None) or None
    response.delete_cookie("refresh_token", path="/", domain=cookie_domain)


def _decode_token(raw: str) -> dict:
    return jwt.decode(raw, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])


def _otp_value(payload: OtpVerifyIn) -> str:
    """
    ✅ Accepts payload.otp (also aliased from otp_code), returns cleaned digits.
    """
    raw = getattr(payload, "otp", None)  # alias otp_code maps here
    if raw is None:
        return ""
    s = "".join(ch for ch in str(raw).strip() if ch.isdigit())
    return s[:6]  # keep max 6 digits



# -------------------------
# routes
# -------------------------

@router.post("/register-admin")
def register_admin(
    payload: RegisterAdminIn,
    master_db: Session = Depends(get_master_db),
):
    """
    ✅ SAME CONCEPT you asked:
    - provisions tenant + admin
    - forces admin: two_fa_enabled=True, multi_login_enabled=False
    - returns login_id for popup
    - sends email verify OTP immediately
    """

    # 1) validations
    if (payload.password or "") != (payload.confirm_password or ""):
        raise HTTPException(status_code=400, detail="Passwords do not match")

    tenant_code = _derive_tenant_code(payload.tenant_name, payload.tenant_code)
    if not tenant_code:
        raise HTTPException(status_code=400, detail="Tenant code is required")

    if not (payload.tenant_name or "").strip():
        raise HTTPException(status_code=400, detail="Tenant name is required")

    if not (payload.contact_person or "").strip():
        raise HTTPException(status_code=400, detail="Contact person is required")

    if not (payload.admin_name or "").strip():
        raise HTTPException(status_code=400, detail="Admin name is required")

    pwd = (payload.password or "").strip()
    if len(pwd) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # 2) provision tenant + admin (master db)
    try:
        tenant: Tenant = provision_tenant_with_admin(
            master_db,
            tenant_name=payload.tenant_name.strip(),
            tenant_code=tenant_code,
            hospital_address=getattr(payload, "hospital_address", None),
            contact_person=payload.contact_person.strip(),
            contact_phone=(payload.contact_phone.strip() if payload.contact_phone else None),
            subscription_plan=payload.subscription_plan,
            amc_percent=int(payload.amc_percent or 30),
            admin_name=payload.admin_name.strip(),
            admin_email=str(payload.email).strip().lower(),
            admin_password=pwd,
        )

        # license fields update (same concept)
        lic_start, lic_end, amc_next_due = _compute_license_dates(payload.subscription_plan)
        tenant.license_start_date = lic_start
        tenant.license_end_date = lic_end
        tenant.amc_next_due = amc_next_due

        master_db.commit()
        master_db.refresh(tenant)

    except ValueError as e:
        master_db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        master_db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        master_db.rollback()
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Registration failed: {repr(e)}")

    # 3) enforce admin flags in TENANT DB + send OTP + return login_id for popup
    tenant_db = create_tenant_session(tenant.db_uri)
    try:
        admin_email = str(payload.email).strip().lower()

        admin = tenant_db.query(User).filter(User.email == admin_email).first()
        if not admin:
            # If your provision function stores email differently, adjust this lookup.
            raise HTTPException(status_code=500, detail="Admin user not found after provisioning")

        # ✅ FORCE your required defaults (DON'T CHANGE)
        admin.two_fa_enabled = True
        admin.multi_login_enabled = False
        admin.email_verified = False

        tenant_db.commit()
        tenant_db.refresh(admin)

        meta = send_email_verify_otp(tenant_db, admin, ttl_minutes=10)

        return {
            "message": "Tenant created and Admin user provisioned. Verify email OTP and proceed to login.",
            "tenant_id": tenant.id,
            "tenant_code": tenant.code,
            # ✅ SHOW THIS IN POPUP
            "admin_login_id": admin.login_id,
            # ✅ OTP modal trigger
            "otp_required": True,
            "purpose": "email_verify",
            "masked_email": meta.get("masked_email") or _mask_email(admin.email or ""),
        }

    finally:
        tenant_db.close()

@router.post("/login")
def login(payload: LoginIn, request: Request, response: Response, master_db: Session = Depends(get_master_db)):
    tenant = _tenant_by_code(master_db, payload.tenant_code)

    tenant_db = create_tenant_session(tenant.db_uri)
    try:
        user = tenant_db.query(User).filter(User.login_id == payload.login_id).first()
        if not user or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="User inactive")

        _cleanup_expired_sessions(tenant_db, user.id)

        # ✅ Multi-login enforcement:
        # multi_login_enabled = False -> block ONLY if another device has an active session
        # same-device relogin allowed -> revoke same-device sessions
        if user.multi_login_enabled is False:
            if _has_other_device_active_session(tenant_db, user.id, request):
                raise HTTPException(status_code=409, detail=MULTI_LOGIN_BLOCK_MESSAGE)
            _revoke_same_device_sessions(tenant_db, user.id, request, reason="relogin_same_device")

        # ✅ 2FA flow
        if user.two_fa_enabled:
            if not user.email:
                raise HTTPException(status_code=400, detail="2FA enabled but email missing. Contact admin.")

            if not user.email_verified:
                meta = send_email_verify_otp(tenant_db, user, ttl_minutes=10)
                return {
                    "otp_required": True,
                    "purpose": "email_verify",
                    "masked_email": meta.get("masked_email", ""),
                    "message": "Email verification OTP sent",
                }

            meta = send_login_otp(tenant_db, user, ttl_minutes=10)
            return {
                "otp_required": True,
                "purpose": "login",
                "masked_email": meta.get("masked_email", ""),
                "message": "Login OTP sent to registered email",
            }

        # ✅ no 2FA -> create session + tokens
        sid = _create_session(tenant_db, user, request)

        access, refresh = create_access_refresh(
            user_id=user.id,
            tenant_id=tenant.id,
            tenant_code=tenant.code,
            session_id=sid,
            token_version=user.token_version,
        )

        _set_refresh_cookie(response, refresh)
        return {"otp_required": False, "access_token": access, "refresh_token": refresh}

    finally:
        tenant_db.close()


@router.post("/verify-otp", response_model=TokenOut)
def verify_otp_route(
    payload: OtpVerifyIn,
    request: Request,
    response: Response,
    master_db: Session = Depends(get_master_db),
):
    """
    purpose=login        -> normal 2FA login
    purpose=email_verify -> first-time email verification during login
    """
    tenant = _tenant_by_code(master_db, payload.tenant_code)

    purpose = (payload.purpose or "login").strip()
    if purpose not in ("login", "email_verify"):
        raise HTTPException(status_code=400, detail="Invalid purpose")

    otp_code = _otp_value(payload)
    if len(otp_code) != 6:
        raise HTTPException(status_code=400, detail="Enter 6-digit OTP")

    tenant_db = create_tenant_session(tenant.db_uri)
    try:
        user = tenant_db.query(User).filter(User.login_id == payload.login_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="User inactive")

        _cleanup_expired_sessions(tenant_db, user.id)

        # multi-login enforcement again
        if user.multi_login_enabled is False:
            if _has_other_device_active_session(tenant_db, user.id, request):
                raise HTTPException(status_code=409, detail=MULTI_LOGIN_BLOCK_MESSAGE)
            _revoke_same_device_sessions(
                tenant_db, user.id, request, reason="relogin_same_device"
            )

        ok = verify_and_consume(
            tenant_db,
            user_id=user.id,
            purpose=purpose,
            otp_code=otp_code,
        )
        if not ok:
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")

        # ✅ If email verification OTP, mark verified
        if purpose == "email_verify":
            user.email_verified = True
            tenant_db.commit()

        sid = _create_session(tenant_db, user, request)

        access, refresh = create_access_refresh(
            user_id=user.id,
            tenant_id=tenant.id,
            tenant_code=tenant.code,
            session_id=sid,
            token_version=user.token_version,
        )

        _set_refresh_cookie(response, refresh)
        return {"access_token": access, "refresh_token": refresh}

    finally:
        tenant_db.close()


@router.post("/resend-otp")
def resend_otp(payload: dict, master_db: Session = Depends(get_master_db)):
    tenant_code = (payload.get("tenant_code") or "").strip()
    login_id = (payload.get("login_id") or "").strip()
    purpose = (payload.get("purpose") or "login").strip()

    tenant = _tenant_by_code(master_db, tenant_code)
    tenant_db = create_tenant_session(tenant.db_uri)
    try:
        user = tenant_db.query(User).filter(User.login_id == login_id).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=404, detail="User not found/inactive")
        if not user.two_fa_enabled:
            raise HTTPException(status_code=400, detail="2FA is disabled")
        if not user.email:
            raise HTTPException(status_code=400, detail="Email not set")

        if purpose == "email_verify":
            meta = send_email_verify_otp(tenant_db, user, ttl_minutes=10)
            return {"ok": True, "purpose": "email_verify", **meta}

        if not user.email_verified:
            meta = send_email_verify_otp(tenant_db, user, ttl_minutes=10)
            return {"ok": True, "purpose": "email_verify", **meta}

        meta = send_login_otp(tenant_db, user, ttl_minutes=10)
        return {"ok": True, "purpose": "login", **meta}
    finally:
        tenant_db.close()


@router.post("/refresh", response_model=TokenOut)
def refresh_token(request: Request, response: Response, master_db: Session = Depends(get_master_db)):
    raw = request.cookies.get("refresh_token") or _extract_bearer(request.headers.get("Authorization"))
    if not raw:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    try:
        payload = _decode_token(raw)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Wrong token type")

    tcode = payload.get("tcode")
    uid = payload.get("uid")
    sid = payload.get("sid")
    tv = payload.get("tv")

    if not tcode or not uid or not sid or tv is None:
        raise HTTPException(status_code=401, detail="Invalid refresh payload")

    tenant = _tenant_by_code(master_db, str(tcode))
    tenant_db = create_tenant_session(tenant.db_uri)

    try:
        user = tenant_db.query(User).filter(User.id == int(uid)).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found/inactive")

        if int(user.token_version) != int(tv):
            raise HTTPException(status_code=401, detail="Session expired. Please login again.")

        sess = (
            tenant_db.query(UserSession)
            .filter(UserSession.session_id == str(sid), UserSession.user_id == int(uid))
            .first()
        )
        if not sess or sess.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Session revoked. Please login again.")

        if _is_session_expired(sess, _utcnow()):
            sess.revoked_at = _utcnow().isoformat()
            sess.revoke_reason = "expired"
            tenant_db.commit()
            raise HTTPException(status_code=401, detail="Session expired. Please login again.")

        sess.last_seen_at = _utcnow().isoformat()
        tenant_db.commit()

        access, new_refresh = create_access_refresh(
            user_id=user.id,
            tenant_id=tenant.id,
            tenant_code=tenant.code,
            session_id=str(sid),
            token_version=user.token_version,
        )

        _set_refresh_cookie(response, new_refresh)
        return {"access_token": access, "refresh_token": new_refresh}

    finally:
        tenant_db.close()


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(None),
    master_db: Session = Depends(get_master_db),
):
    raw = request.cookies.get("refresh_token") or _extract_bearer(authorization)
    _clear_refresh_cookie(response)

    if not raw:
        return {"ok": True}

    try:
        payload = _decode_token(raw)
    except JWTError:
        return {"ok": True}

    if payload.get("type") != "refresh":
        return {"ok": True}

    tcode = payload.get("tcode")
    sid = payload.get("sid")
    uid = payload.get("uid")
    if not tcode or not sid or not uid:
        return {"ok": True}

    tenant = _tenant_by_code(master_db, str(tcode))
    tenant_db = create_tenant_session(tenant.db_uri)
    try:
        _revoke_session_by_sid(tenant_db, user_id=int(uid), sid=str(sid), reason="logout")
    finally:
        tenant_db.close()

    return {"ok": True}


@router.get("/me")
def me(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    master_db: Session = Depends(get_master_db),
):
    raw = _extract_bearer(authorization) or token
    user, tenant = get_current_user_and_tenant_from_token(raw, master_db)
    role_names = [r.name for r in (user.roles or [])]

    return {
        "id": user.id,
        "login_id": user.login_id,
        "name": user.name,
        "email": user.email,
        "is_admin": user.is_admin,
        "department_id": user.department_id,
        "roles": role_names,
        "tenant_id": tenant.id,
        "tenant_code": tenant.code,
        "tenant_name": tenant.name,
    }




@router.get("/me/permissions")
def my_permissions(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    master_db: Session = Depends(get_master_db),
):
    raw = _extract_bearer(authorization) or token
    user, tenant = get_current_user_and_tenant_from_token(raw, master_db)

    # ✅ Admin: return all perms in tenant
    if user.is_admin and settings.ADMIN_ALL_ACCESS:
        tenant_db = create_tenant_session(tenant.db_uri)
        try:
            rows = tenant_db.query(Permission).all()

            # ✅ LOG: only permission codes
            codes = sorted({p.code for p in rows})
            logger.info("PERMS user_id=%s tenant_id=%s codes=%s", user.id, tenant.id, codes)

            modules = {}
            for p in rows:
                modules.setdefault(p.module, []).append({"code": p.code, "label": p.label})
            return {"modules": modules}
        finally:
            tenant_db.close()

    # ✅ Non-admin: from roles
    perms = set()
    for role in (user.roles or []):
        for p in (role.permissions or []):
            perms.add((p.code, p.label, p.module))

    # ✅ LOG: only permission codes
    codes = sorted({code for code, _, _ in perms})
    logger.info("PERMS user_id=%s tenant_id=%s codes=%s", user.id, tenant.id, codes)

    modules = {}
    for code, label, module in perms:
        modules.setdefault(module, []).append({"code": code, "label": label})
    

    return {"modules": modules}
