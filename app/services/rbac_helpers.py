# app/services/rbac_helpers.py
from __future__ import annotations

from typing import Iterable, List, Optional, Set

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.role import Role, RolePermission
from app.models.permission import Permission
from app.models.user import User

# -----------------------------
# Role names (canonical)
# -----------------------------
ROLE_ADMIN = "Admin"
ROLE_DOCTOR = "Doctor"
ROLE_STAFF = "Staff"


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _get_or_create_role(db: Session, name: str, desc: str = "") -> Role:
    role = db.query(Role).filter(func.lower(Role.name) == _norm(name)).first()
    if not role:
        role = Role(name=name.strip(), description=desc or "")
        db.add(role)
        db.flush()  # ensures role.id is available
    return role


def ensure_default_roles_exist(db: Session) -> dict[str, Role]:
    """
    Ensures Admin/Doctor/Staff roles exist in tenant DB.
    Returns mapping by canonical name.
    """
    admin = _get_or_create_role(db, ROLE_ADMIN, "System administrator")
    doctor = _get_or_create_role(db, ROLE_DOCTOR, "Doctor role")
    staff = _get_or_create_role(db, ROLE_STAFF, "Default staff role")
    return {ROLE_ADMIN: admin, ROLE_DOCTOR: doctor, ROLE_STAFF: staff}


def _role_permission_ids(db: Session, role_id: int) -> Set[int]:
    return {
        pid
        for (pid, ) in db.query(RolePermission.permission_id).filter(
            RolePermission.role_id == role_id).all()
    }


def _permission_ids_by_codes(db: Session, codes: Iterable[str]) -> Set[int]:
    codes = [c for c in {(_norm(x)) for x in codes} if c]
    if not codes:
        return set()
    rows = db.query(Permission.id, Permission.code).all()
    # Compare in python to avoid DB collation issues across installs
    wanted = set(codes)
    return {pid for (pid, code) in rows if _norm(code) in wanted}


def ensure_admin_has_all_permissions(db: Session) -> None:
    """
    Ensures the Admin role exists and contains ALL permissions in the tenant DB.
    Safe to run multiple times. Does NOT commit.
    """
    roles = ensure_default_roles_exist(db)
    admin_role = roles[ROLE_ADMIN]

    all_perm_ids = {pid for (pid, ) in db.query(Permission.id).all()}
    if not all_perm_ids:
        return

    existing = _role_permission_ids(db, admin_role.id)
    missing = list(all_perm_ids - existing)
    if missing:
        db.bulk_save_objects([
            RolePermission(role_id=admin_role.id, permission_id=pid)
            for pid in missing
        ])


# -----------------------------
# Optional: Minimal templates
# -----------------------------
# ✅ Keep this OFF by default (safer)
AUTO_SEED_MIN_PERMS_FOR_EMPTY_ROLES = False

# Minimal safe baseline if a role has ZERO permissions (only used if flag is True)
DOCTOR_MIN_PERMS = [
    "patients.view",
    "appointments.view",
    "appointments.update",
    "vitals.create",
    "visits.view",
    "visits.create",
    "visits.update",
    "prescriptions.create",
    "orders.lab.create",
    "orders.lab.view",
    "orders.ris.create",
    "orders.ris.view",
    "opd.queue.view",
]

STAFF_MIN_PERMS = [
    "patients.view",
    "appointments.view",
    "opd.queue.view",
]


def _seed_min_permissions_if_role_empty(db: Session, role: Role,
                                        perm_codes: List[str]) -> None:
    """
    If a role has ZERO permissions (common in old tenants), optionally attach a small baseline.
    Controlled by AUTO_SEED_MIN_PERMS_FOR_EMPTY_ROLES.
    """
    if not AUTO_SEED_MIN_PERMS_FOR_EMPTY_ROLES:
        return

    existing = _role_permission_ids(db, role.id)
    if len(existing) > 0:
        return  # role already configured; don't modify

    pids = _permission_ids_by_codes(db, perm_codes)
    if not pids:
        return

    db.bulk_save_objects(
        [RolePermission(role_id=role.id, permission_id=pid) for pid in pids])


def ensure_user_has_atleast_one_role(db: Session, u: User) -> None:
    """
    Ensures user has at least one role:
      - Admin user -> Admin role
      - Doctor user -> Doctor role
      - else -> Staff role

    Also ensures default roles exist.
    Optionally seeds minimal perms for Doctor/Staff if those roles are empty (flag controlled).
    Does NOT commit.
    """
    roles = ensure_default_roles_exist(db)

    # Optional: fix old tenant roles that have zero permissions
    _seed_min_permissions_if_role_empty(db, roles[ROLE_DOCTOR],
                                        DOCTOR_MIN_PERMS)
    _seed_min_permissions_if_role_empty(db, roles[ROLE_STAFF], STAFF_MIN_PERMS)

    # Relationship may lazy-load; touching it is fine
    if getattr(u, "roles", None) and len(u.roles) > 0:
        return

    if bool(getattr(u, "is_admin", False)) is True:
        u.roles = [roles[ROLE_ADMIN]]
    elif bool(getattr(u, "is_doctor", False)) is True:
        u.roles = [roles[ROLE_DOCTOR]]
    else:
        u.roles = [roles[ROLE_STAFF]]


def repair_all_users_missing_roles(db: Session) -> int:
    """
    One-time utility you can run in a script:
    Assigns default role to any user without roles.
    Returns number of repaired users. Does NOT commit.
    """
    # This query pattern avoids relying on relationship loading
    user_ids = [
        uid
        for (uid,
             ) in db.execute("SELECT u.id FROM users u "
                             "LEFT JOIN user_roles ur ON ur.user_id = u.id "
                             "WHERE ur.user_id IS NULL").fetchall()
    ]

    if not user_ids:
        return 0

    users = db.query(User).filter(User.id.in_(user_ids)).all()
    for u in users:
        ensure_user_has_atleast_one_role(db, u)

    # Keep admin always synced too
    ensure_admin_has_all_permissions(db)

    return len(users)
