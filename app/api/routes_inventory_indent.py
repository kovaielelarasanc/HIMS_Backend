# FILE: app/api/routes_inventory_indent.py
from __future__ import annotations

from datetime import date
from typing import Optional
from sqlalchemy.exc import SQLAlchemyError
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from app.api.deps import get_db, current_user
from app.utils.resp import ok, err
from app.core.rbac import require_any

from app.models.user import User

from app.models.inv_indent_issue import InvIndent, InvIssue, InvIndentItem, InvIssueItem
from app.models.pharmacy_inventory import (
    InventoryLocation,
    InventoryItem,
    ItemLocationStock,
    ItemBatch,
)

from app.schemas.inventory_indent import (
    LocationOut, InventoryItemOut, StockOut, BatchOut,
    IndentCreateIn, IndentUpdateIn, ApproveIndentIn, CancelIn, IndentOut,
    IssueCreateFromIndentIn, IssueOut, IssueItemUpdateIn
)
import logging
from app.services.inventory_indent_service import (
    IndentError,
    create_indent,
    update_indent,
    submit_indent,
    approve_indent,
    cancel_indent,
    create_issue_from_indent,
    update_issue_item,
    post_issue,
    cancel_issue,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/inventory", tags=["inventory"])

# Permissions (same as yours)
P_LOC_VIEW = ["inventory.locations.view", "inventory.catalog.view", "inv.locations.view", "inv.catalog.view"]
P_ITEM_VIEW = ["inventory.items.view", "inventory.catalog.view", "inv.items.view", "inv.catalog.view"]
P_STOCK_VIEW = ["inventory.stock.view", "inventory.catalog.view", "inv.stock.view", "inv.catalog.view"]
P_BATCH_VIEW = ["inventory.batches.view", "inventory.stock.view", "inv.batches.view", "inv.stock.view"]

P_INDENT_VIEW = ["inventory.indents.view", "inventory.indent.view", "inv.indents.view", "inv.indent.view"]
P_INDENT_CREATE = ["inventory.indents.create", "inventory.indents.manage", "inv.indents.create", "inv.indents.manage"]
P_INDENT_UPDATE = ["inventory.indents.update", "inventory.indents.manage", "inv.indents.update", "inv.indents.manage"]
P_INDENT_SUBMIT = ["inventory.indents.submit", "inventory.indents.manage", "inv.indents.submit", "inv.indents.manage"]
P_INDENT_APPROVE = ["inventory.indents.approve", "inventory.indents.manage", "inv.indents.approve", "inv.indents.manage"]
P_INDENT_CANCEL = ["inventory.indents.cancel", "inventory.indents.manage", "inv.indents.cancel", "inv.indents.manage"]

P_ISSUE_VIEW = ["inventory.issues.view", "inventory.issue.view", "inv.issues.view", "inv.issue.view"]
P_ISSUE_CREATE = ["inventory.issues.create", "inventory.issues.manage", "inv.issues.create", "inv.issues.manage"]
P_ISSUE_UPDATE = ["inventory.issues.update", "inventory.issues.manage", "inv.issues.update", "inv.issues.manage"]
P_ISSUE_POST = ["inventory.issues.post", "inventory.issues.manage", "inv.issues.post", "inv.issues.manage"]
P_ISSUE_CANCEL = ["inventory.issues.cancel", "inventory.issues.manage", "inv.issues.cancel", "inv.issues.manage"]


def _safe_err(e: Exception):
    # Make SQL errors readable instead of full trace
    if isinstance(e, IntegrityError):
        return err("Database constraint error (duplicate/invalid reference).", 400)
    return err(str(getattr(e, "detail", e)), getattr(e, "status_code", 500))


# =========================
# CATALOG
# =========================
@router.get("/locations")
def list_locations(
    active: Optional[bool] = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        require_any(user, P_LOC_VIEW)
        q = db.query(InventoryLocation)
        if active is not None:
            q = q.filter(InventoryLocation.is_active == active)
        rows = q.order_by(InventoryLocation.name.asc()).all()
        return ok([LocationOut.model_validate(x).model_dump() for x in rows])
    except Exception as e:
        return _safe_err(e)


@router.get("/items")
def list_items(
    search: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(True),
    item_type: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        require_any(user, P_ITEM_VIEW)
        q = db.query(InventoryItem)
        if is_active is not None:
            q = q.filter(InventoryItem.is_active == is_active)
        if item_type:
            q = q.filter(InventoryItem.item_type == item_type)
        if search:
            like = f"%{search.strip()}%"
            q = q.filter((InventoryItem.name.like(like)) | (InventoryItem.code.like(like)))

        q = q.order_by(InventoryItem.name.asc())
        
        if limit:
            q = q.limit(limit)
            
        rows = q.all()
        return ok([InventoryItemOut.model_validate(x).model_dump() for x in rows])
    except Exception as e:
        return _safe_err(e)


@router.get("/stock")
def list_location_stock(
    location_id: int = Query(...),
    only_positive: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        require_any(user, P_STOCK_VIEW)
        q = db.query(ItemLocationStock).filter(ItemLocationStock.location_id == location_id)
        if only_positive:
            q = q.filter(ItemLocationStock.on_hand_qty > 0)
        rows = q.order_by(ItemLocationStock.updated_at.desc()).all()
        return ok([StockOut.model_validate(x).model_dump() for x in rows])
    except Exception as e:
        return _safe_err(e)


@router.get("/batches")
def list_batches(
    location_id: int = Query(...),
    item_id: Optional[int] = Query(None),
    only_available: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        require_any(user, P_BATCH_VIEW)

        q = db.query(ItemBatch).filter(ItemBatch.location_id == location_id)
        if item_id:
            q = q.filter(ItemBatch.item_id == item_id)
        if only_available:
            q = q.filter(ItemBatch.current_qty > 0, ItemBatch.is_active.is_(True), ItemBatch.is_saleable.is_(True))

        q = q.order_by(
            case((ItemBatch.expiry_date.is_(None), 1), else_=0),
            ItemBatch.expiry_date.asc(),
            ItemBatch.id.asc(),
        )

        rows = q.limit(1000).all()
        return ok([BatchOut.model_validate(x).model_dump() for x in rows])
    except Exception as e:
        return _safe_err(e)


# =========================
# INDENTS
# =========================
@router.get("/indents")
def list_indents(
    status: Optional[str] = Query(None),
    from_location_id: Optional[int] = Query(None),
    to_location_id: Optional[int] = Query(None),
    patient_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        require_any(user, P_INDENT_VIEW)
        q = (
            db.query(InvIndent)
            .options(
                selectinload(InvIndent.from_location),
                selectinload(InvIndent.to_location),
                selectinload(InvIndent.items).selectinload(InvIndentItem.item),
            )
        )

        if status:
            q = q.filter(InvIndent.status == status)
        if from_location_id:
            q = q.filter(InvIndent.from_location_id == from_location_id)
        if to_location_id:
            q = q.filter(InvIndent.to_location_id == to_location_id)
        if patient_id:
            q = q.filter(InvIndent.patient_id == patient_id)
        if date_from:
            q = q.filter(InvIndent.indent_date >= date_from)
        if date_to:
            q = q.filter(InvIndent.indent_date <= date_to)

        rows = q.order_by(InvIndent.created_at.desc()).limit(limit).all()
        return ok([IndentOut.model_validate(x).model_dump() for x in rows])
    except Exception as e:
        return _safe_err(e)


@router.get("/indents/{indent_id}")
def get_indent(indent_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_INDENT_VIEW)
        ind = (
            db.query(InvIndent)
            .options(
                selectinload(InvIndent.from_location),
                selectinload(InvIndent.to_location),
                selectinload(InvIndent.items).selectinload(InvIndentItem.item),
            )
            .filter(InvIndent.id == indent_id)
            .first()
        )
        if not ind:
            return err("Indent not found", 404)
        return ok(IndentOut.model_validate(ind).model_dump())
    except Exception as e:
        return _safe_err(e)


@router.post("/indents")
def create_indent_api(payload: IndentCreateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_INDENT_CREATE)
        with db.begin():
            ind = create_indent(db, payload, getattr(user, "id", None))
        ind = (
            db.query(InvIndent)
            .options(
                selectinload(InvIndent.from_location),
                selectinload(InvIndent.to_location),
                selectinload(InvIndent.items).selectinload(InvIndentItem.item),
            )
            .filter(InvIndent.id == ind.id)
            .first()
        )
        return ok(IndentOut.model_validate(ind).model_dump(), status_code=201)
    except IndentError as e:
        return err(str(e), 400)
    except Exception as e:
        return _safe_err(e)


@router.put("/indents/{indent_id}")
def update_indent_api(indent_id: int, payload: IndentUpdateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_INDENT_UPDATE)
        with db.begin():
            ind = update_indent(db, indent_id, payload, getattr(user, "id", None))
        ind = (
            db.query(InvIndent)
            .options(
                selectinload(InvIndent.from_location),
                selectinload(InvIndent.to_location),
                selectinload(InvIndent.items).selectinload(InvIndentItem.item),
            )
            .filter(InvIndent.id == ind.id)
            .first()
        )
        return ok(IndentOut.model_validate(ind).model_dump())
    except IndentError as e:
        return err(str(e), 400)
    except Exception as e:
        return _safe_err(e)


@router.post("/indents/{indent_id}/submit")
def submit_indent_api(indent_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_INDENT_SUBMIT)
        with db.begin():
            ind = submit_indent(db, indent_id, getattr(user, "id", None))
        ind = (
            db.query(InvIndent)
            .options(
                selectinload(InvIndent.from_location),
                selectinload(InvIndent.to_location),
                selectinload(InvIndent.items).selectinload(InvIndentItem.item),
            )
            .filter(InvIndent.id == ind.id)
            .first()
        )
        return ok(IndentOut.model_validate(ind).model_dump())
    except IndentError as e:
        return err(str(e), 400)
    except Exception as e:
        return _safe_err(e)


@router.post("/indents/{indent_id}/approve")
def approve_indent_api(indent_id: int, payload: ApproveIndentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_INDENT_APPROVE)
        with db.begin():
            ind = approve_indent(db, indent_id, payload, getattr(user, "id", None))
        ind = (
            db.query(InvIndent)
            .options(
                selectinload(InvIndent.from_location),
                selectinload(InvIndent.to_location),
                selectinload(InvIndent.items).selectinload(InvIndentItem.item),
            )
            .filter(InvIndent.id == ind.id)
            .first()
        )
        return ok(IndentOut.model_validate(ind).model_dump())
    except IndentError as e:
        return err(str(e), 400)
    except Exception as e:
        return _safe_err(e)


@router.post("/indents/{indent_id}/cancel")
def cancel_indent_api(indent_id: int, payload: CancelIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_INDENT_CANCEL)
        with db.begin():
            ind = cancel_indent(db, indent_id, payload.reason, getattr(user, "id", None))
        ind = (
            db.query(InvIndent)
            .options(
                selectinload(InvIndent.from_location),
                selectinload(InvIndent.to_location),
                selectinload(InvIndent.items).selectinload(InvIndentItem.item),
            )
            .filter(InvIndent.id == ind.id)
            .first()
        )
        return ok(IndentOut.model_validate(ind).model_dump())
    except IndentError as e:
        return err(str(e), 400)
    except Exception as e:
        return _safe_err(e)


# =========================
# ISSUES
# =========================
@router.get("/issues")
def list_issues(
    status: Optional[str] = Query(None),
    indent_id: Optional[int] = Query(None),
    from_location_id: Optional[int] = Query(None),
    to_location_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        require_any(user, P_ISSUE_VIEW)
        q = (
            db.query(InvIssue)
            .options(
                selectinload(InvIssue.from_location),
                selectinload(InvIssue.to_location),
                selectinload(InvIssue.items).selectinload(InvIssueItem.item),
                selectinload(InvIssue.items).selectinload(InvIssueItem.batch),
            )
        )
        if status:
            q = q.filter(InvIssue.status == status)
        if indent_id:
            q = q.filter(InvIssue.indent_id == indent_id)
        if from_location_id:
            q = q.filter(InvIssue.from_location_id == from_location_id)
        if to_location_id:
            q = q.filter(InvIssue.to_location_id == to_location_id)

        rows = q.order_by(InvIssue.created_at.desc()).limit(limit).all()
        return ok([IssueOut.model_validate(x).model_dump() for x in rows])
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Failed to list inventory issues")
        return err("Database error", 500)
    except Exception:
        logger.exception("Unhandled error in list_issues")
        return err("Internal server error", 500)


@router.get("/issues/{issue_id}")
def get_issue(issue_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_ISSUE_VIEW)
        issue = (
            db.query(InvIssue)
            .options(
                selectinload(InvIssue.from_location),
                selectinload(InvIssue.to_location),
                selectinload(InvIssue.items).selectinload(InvIssueItem.item),
                selectinload(InvIssue.items).selectinload(InvIssueItem.batch),
            )
            .filter(InvIssue.id == issue_id)
            .first()
        )
        if not issue:
            return err("Issue not found", 404)
        return ok(IssueOut.model_validate(issue).model_dump())
    except Exception as e:
        return _safe_err(e)


@router.post("/indents/{indent_id}/issues")
def create_issue_from_indent_api(indent_id: int, payload: IssueCreateFromIndentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_ISSUE_CREATE)
        with db.begin():
            issue = create_issue_from_indent(db, indent_id, payload, getattr(user, "id", None))
        issue = (
            db.query(InvIssue)
            .options(
                selectinload(InvIssue.from_location),
                selectinload(InvIssue.to_location),
                selectinload(InvIssue.items).selectinload(InvIssueItem.item),
                selectinload(InvIssue.items).selectinload(InvIssueItem.batch),
            )
            .filter(InvIssue.id == issue.id)
            .first()
        )
        return ok(IssueOut.model_validate(issue).model_dump(), status_code=201)
    except IndentError as e:
        return err(str(e), 400)
    except Exception as e:
        return _safe_err(e)


@router.put("/issue-items/{issue_item_id}")
def update_issue_item_api(issue_item_id: int, payload: IssueItemUpdateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_ISSUE_UPDATE)
        with db.begin():
            li = update_issue_item(db, issue_item_id, payload, getattr(user, "id", None))
        issue = (
            db.query(InvIssue)
            .options(
                selectinload(InvIssue.from_location),
                selectinload(InvIssue.to_location),
                selectinload(InvIssue.items).selectinload(InvIssueItem.item),
                selectinload(InvIssue.items).selectinload(InvIssueItem.batch),
            )
            .filter(InvIssue.id == li.issue_id)
            .first()
        )
        return ok(IssueOut.model_validate(issue).model_dump())
    except IndentError as e:
        return err(str(e), 400)
    except Exception as e:
        return _safe_err(e)


@router.post("/issues/{issue_id}/post")
def post_issue_api(issue_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_ISSUE_POST)
        with db.begin():
            issue = post_issue(db, issue_id, getattr(user, "id", None))

        issue = (
            db.query(InvIssue)
            .options(
                selectinload(InvIssue.from_location),
                selectinload(InvIssue.to_location),
                selectinload(InvIssue.items).selectinload(InvIssueItem.item),
                selectinload(InvIssue.items).selectinload(InvIssueItem.batch),
            )
            .filter(InvIssue.id == issue.id)
            .first()
        )
        return ok(IssueOut.model_validate(issue).model_dump())

    except IndentError as e:
        logger.exception("post_issue failed issue_id=%s user_id=%s msg=%s",
                         issue_id, getattr(user, "id", None), str(e))
        return err(str(e), 400)

    except Exception as e:
        logger.exception("Unexpected error in post_issue_api issue_id=%s", issue_id)
        return _safe_err(e)


@router.post("/issues/{issue_id}/cancel")
def cancel_issue_api(issue_id: int, payload: CancelIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        require_any(user, P_ISSUE_CANCEL)
        with db.begin():
            issue = cancel_issue(db, issue_id, payload.reason, getattr(user, "id", None))
        issue = (
            db.query(InvIssue)
            .options(
                selectinload(InvIssue.from_location),
                selectinload(InvIssue.to_location),
                selectinload(InvIssue.items).selectinload(InvIssueItem.item),
                selectinload(InvIssue.items).selectinload(InvIssueItem.batch),
            )
            .filter(InvIssue.id == issue.id)
            .first()
        )
        return ok(IssueOut.model_validate(issue).model_dump())
    except IndentError as e:
        return err(str(e), 400)
    except Exception as e:
        return _safe_err(e)
