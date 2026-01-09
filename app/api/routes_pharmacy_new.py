# FILE: app/api/routes_pharmacy.py
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

# ✅ Adjust these imports to your project (common patterns)
from app.api.deps import get_db, current_user

router = APIRouter(prefix="/pharmacy", tags=["inventory_of_pharmacy"] )
# ============================================================
# Response Wrapper Helpers
# ============================================================
def ok(data: Any = None, status_code: int = 200):
    return JSONResponse({
        "status": True,
        "data": data
    },
                        status_code=status_code)


def fail(msg: str,
         code: str = "ERROR",
         fields: Optional[Dict[str, Any]] = None,
         status_code: int = 400):
    payload = {"status": False, "error": {"msg": msg, "code": code}}
    if fields:
        payload["error"]["fields"] = fields
    return JSONResponse(payload, status_code=status_code)


# ============================================================
# Permission Helper (plug into your RBAC)
# ============================================================
def _flatten_perm_codes(node: Any, out: set[str]):
    if not node:
        return
    if isinstance(node, str):
        out.add(node)
        return
    if isinstance(node, list):
        for x in node:
            _flatten_perm_codes(x, out)
        return
    if isinstance(node, dict):
        # permissions can be: {"module":[{"code":"x"}]} or {"codes":["x"]} etc.
        if "code" in node and isinstance(node["code"], str):
            out.add(node["code"])
        for v in node.values():
            _flatten_perm_codes(v, out)


def has_perm(user: Any, code: str) -> bool:
    # Admin bypass patterns
    if getattr(user, "is_admin", False) or getattr(user, "is_superuser",
                                                   False):
        return True

    # Try common fields
    perm_node = getattr(user, "modules", None) or getattr(
        user, "permissions", None) or getattr(user, "perms", None)
    s: set[str] = set()
    _flatten_perm_codes(perm_node, s)

    if "*" in s:
        return True
    return code in s


def require_perm(user: Any, code: str):
    if not has_perm(user, code):
        raise HTTPException(status_code=403, detail="Not permitted")


# ============================================================
# Routers
# ============================================================
pharmacy_router = APIRouter(prefix="/pharmacy")

masters = APIRouter(tags=["Pharmacy • Masters"])
insurance = APIRouter(tags=["Pharmacy • Insurance"])
procurement = APIRouter(tags=["Pharmacy • Procurement"])
inventory = APIRouter(tags=["Pharmacy • Inventory"])
stock_ops = APIRouter(tags=["Pharmacy • Stock Ops"])
dispense = APIRouter(tags=["Pharmacy • Dispense"])
alerts = APIRouter(tags=["Pharmacy • Alerts"])
reports = APIRouter(tags=["Pharmacy • Reports"])
audit = APIRouter(tags=["Pharmacy • Audit"])


# ============================================================
# 1) MASTERS
# ============================================================
@masters.get("/uoms")
def list_uoms(db: Session = Depends(get_db),
              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.uoms.view")
    return ok([])


@masters.post("/uoms", status_code=201)
def create_uom(payload: Dict[str, Any],
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.uoms.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@masters.get("/uoms/{uom_id}")
def get_uom(uom_id: int,
            db: Session = Depends(get_db),
            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.uoms.view")
    return ok({"id": uom_id})


@masters.put("/uoms/{uom_id}")
def update_uom(uom_id: int,
               payload: Dict[str, Any],
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.uoms.manage")
    return ok({"updated": True, "id": uom_id, "payload": payload})


@masters.delete("/uoms/{uom_id}")
def delete_uom(uom_id: int,
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.uoms.manage")
    return ok({"deleted": True, "id": uom_id})


# Categories
@masters.get("/categories")
def list_categories(db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.categories.view")
    return ok([])


@masters.post("/categories", status_code=201)
def create_category(payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.categories.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@masters.get("/categories/{category_id}")
def get_category(category_id: int,
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.categories.view")
    return ok({"id": category_id})


@masters.put("/categories/{category_id}")
def update_category(category_id: int,
                    payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.categories.manage")
    return ok({"updated": True, "id": category_id, "payload": payload})


@masters.delete("/categories/{category_id}")
def delete_category(category_id: int,
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.categories.manage")
    return ok({"deleted": True, "id": category_id})


# Manufacturers
@masters.get("/manufacturers")
def list_manufacturers(db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.manufacturers.view")
    return ok([])


@masters.post("/manufacturers", status_code=201)
def create_manufacturer(payload: Dict[str, Any],
                        db: Session = Depends(get_db),
                        user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.manufacturers.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@masters.get("/manufacturers/{manufacturer_id}")
def get_manufacturer(manufacturer_id: int,
                     db: Session = Depends(get_db),
                     user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.manufacturers.view")
    return ok({"id": manufacturer_id})


@masters.put("/manufacturers/{manufacturer_id}")
def update_manufacturer(manufacturer_id: int,
                        payload: Dict[str, Any],
                        db: Session = Depends(get_db),
                        user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.manufacturers.manage")
    return ok({"updated": True, "id": manufacturer_id, "payload": payload})


@masters.delete("/manufacturers/{manufacturer_id}")
def delete_manufacturer(manufacturer_id: int,
                        db: Session = Depends(get_db),
                        user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.manufacturers.manage")
    return ok({"deleted": True, "id": manufacturer_id})


# Tax codes
@masters.get("/tax-codes")
def list_tax_codes(db: Session = Depends(get_db),
                   user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.tax.view")
    return ok([])


@masters.post("/tax-codes", status_code=201)
def create_tax_code(payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.tax.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@masters.get("/tax-codes/{tax_id}")
def get_tax_code(tax_id: int,
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.tax.view")
    return ok({"id": tax_id})


@masters.put("/tax-codes/{tax_id}")
def update_tax_code(tax_id: int,
                    payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.tax.manage")
    return ok({"updated": True, "id": tax_id, "payload": payload})


@masters.delete("/tax-codes/{tax_id}")
def delete_tax_code(tax_id: int,
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.tax.manage")
    return ok({"deleted": True, "id": tax_id})


# Suppliers
@masters.get("/suppliers")
def list_suppliers(db: Session = Depends(get_db),
                   user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.suppliers.view")
    return ok([])


@masters.post("/suppliers", status_code=201)
def create_supplier(payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.suppliers.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@masters.get("/suppliers/{supplier_id}")
def get_supplier(supplier_id: int,
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.suppliers.view")
    return ok({"id": supplier_id})


@masters.put("/suppliers/{supplier_id}")
def update_supplier(supplier_id: int,
                    payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.suppliers.manage")
    return ok({"updated": True, "id": supplier_id, "payload": payload})


@masters.delete("/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int,
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.suppliers.manage")
    return ok({"deleted": True, "id": supplier_id})


# Stores
@masters.get("/stores")
def list_stores(db: Session = Depends(get_db),
                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stores.view")
    return ok([])


@masters.post("/stores", status_code=201)
def create_store(payload: Dict[str, Any],
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stores.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@masters.get("/stores/{store_id}")
def get_store(store_id: int,
              db: Session = Depends(get_db),
              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stores.view")
    return ok({"id": store_id})


@masters.put("/stores/{store_id}")
def update_store(store_id: int,
                 payload: Dict[str, Any],
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stores.manage")
    return ok({"updated": True, "id": store_id, "payload": payload})


@masters.delete("/stores/{store_id}")
def delete_store(store_id: int,
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stores.manage")
    return ok({"deleted": True, "id": store_id})


# Items
@masters.get("/items")
def list_items(
        q: Optional[str] = Query(default=None),
        category_id: Optional[int] = Query(default=None),
        is_active: Optional[bool] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.items.view")
    return ok({
        "items": [],
        "limit": limit,
        "offset": offset,
        "q": q,
        "category_id": category_id,
        "is_active": is_active
    })


@masters.post("/items", status_code=201)
def create_item(payload: Dict[str, Any],
                db: Session = Depends(get_db),
                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@masters.get("/items/{item_id}")
def get_item(item_id: int,
             db: Session = Depends(get_db),
             user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.view")
    return ok({"id": item_id})


@masters.put("/items/{item_id}")
def update_item(item_id: int,
                payload: Dict[str, Any],
                db: Session = Depends(get_db),
                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.manage")
    return ok({"updated": True, "id": item_id, "payload": payload})


@masters.delete("/items/{item_id}")
def delete_item(item_id: int,
                db: Session = Depends(get_db),
                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.manage")
    return ok({"deleted": True, "id": item_id})


# Item UOM conversions
@masters.get("/items/{item_id}/uom-conversions")
def list_item_uom_conversions(item_id: int,
                              db: Session = Depends(get_db),
                              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.view")
    return ok([])


@masters.post("/items/{item_id}/uom-conversions", status_code=201)
def create_item_uom_conversion(item_id: int,
                               payload: Dict[str, Any],
                               db: Session = Depends(get_db),
                               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.manage")
    return ok({
        "created": True,
        "item_id": item_id,
        "payload": payload
    },
              status_code=201)


@masters.put("/uom-conversions/{conv_id}")
def update_item_uom_conversion(conv_id: int,
                               payload: Dict[str, Any],
                               db: Session = Depends(get_db),
                               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.manage")
    return ok({"updated": True, "conv_id": conv_id, "payload": payload})


@masters.delete("/uom-conversions/{conv_id}")
def delete_item_uom_conversion(conv_id: int,
                               db: Session = Depends(get_db),
                               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.manage")
    return ok({"deleted": True, "conv_id": conv_id})


# Store item settings (reorder/expiry rules)
@masters.get("/stores/{store_id}/item-settings")
def list_item_store_settings(
        store_id: int,
        item_id: Optional[int] = Query(default=None),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.items.view")
    return ok([])


@masters.post("/stores/{store_id}/item-settings", status_code=201)
def create_item_store_setting(store_id: int,
                              payload: Dict[str, Any],
                              db: Session = Depends(get_db),
                              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.manage")
    return ok({
        "created": True,
        "store_id": store_id,
        "payload": payload
    },
              status_code=201)


@masters.put("/item-settings/{setting_id}")
def update_item_store_setting(setting_id: int,
                              payload: Dict[str, Any],
                              db: Session = Depends(get_db),
                              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.manage")
    return ok({"updated": True, "setting_id": setting_id, "payload": payload})


@masters.delete("/item-settings/{setting_id}")
def delete_item_store_setting(setting_id: int,
                              db: Session = Depends(get_db),
                              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.items.manage")
    return ok({"deleted": True, "setting_id": setting_id})


# ============================================================
# 2) INSURANCE / CONTRACT
# ============================================================
@insurance.get("/insurance/payers")
def list_payers(db: Session = Depends(get_db),
                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.view")
    return ok([])


@insurance.post("/insurance/payers", status_code=201)
def create_payer(payload: Dict[str, Any],
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@insurance.put("/insurance/payers/{payer_id}")
def update_payer(payer_id: int,
                 payload: Dict[str, Any],
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"updated": True, "payer_id": payer_id, "payload": payload})


@insurance.delete("/insurance/payers/{payer_id}")
def delete_payer(payer_id: int,
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"deleted": True, "payer_id": payer_id})


@insurance.get("/insurance/plans")
def list_plans(payer_id: Optional[int] = Query(default=None),
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.view")
    return ok([])


@insurance.post("/insurance/plans", status_code=201)
def create_plan(payload: Dict[str, Any],
                db: Session = Depends(get_db),
                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@insurance.put("/insurance/plans/{plan_id}")
def update_plan(plan_id: int,
                payload: Dict[str, Any],
                db: Session = Depends(get_db),
                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"updated": True, "plan_id": plan_id, "payload": payload})


@insurance.delete("/insurance/plans/{plan_id}")
def delete_plan(plan_id: int,
                db: Session = Depends(get_db),
                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"deleted": True, "plan_id": plan_id})


@insurance.get("/insurance/coverage-rules")
def list_coverage_rules(payer_id: Optional[int] = Query(default=None),
                        plan_id: Optional[int] = Query(default=None),
                        db: Session = Depends(get_db),
                        user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.view")
    return ok([])


@insurance.post("/insurance/coverage-rules", status_code=201)
def create_coverage_rule(payload: Dict[str, Any],
                         db: Session = Depends(get_db),
                         user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@insurance.put("/insurance/coverage-rules/{rule_id}")
def update_coverage_rule(rule_id: int,
                         payload: Dict[str, Any],
                         db: Session = Depends(get_db),
                         user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"updated": True, "rule_id": rule_id, "payload": payload})


@insurance.delete("/insurance/coverage-rules/{rule_id}")
def delete_coverage_rule(rule_id: int,
                         db: Session = Depends(get_db),
                         user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"deleted": True, "rule_id": rule_id})


@insurance.get("/insurance/contract-prices")
def list_contract_prices(payer_id: Optional[int] = Query(default=None),
                         plan_id: Optional[int] = Query(default=None),
                         item_id: Optional[int] = Query(default=None),
                         db: Session = Depends(get_db),
                         user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.view")
    return ok([])


@insurance.post("/insurance/contract-prices", status_code=201)
def create_contract_price(payload: Dict[str, Any],
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"created": True, "payload": payload}, status_code=201)


@insurance.put("/insurance/contract-prices/{cp_id}")
def update_contract_price(cp_id: int,
                          payload: Dict[str, Any],
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"updated": True, "cp_id": cp_id, "payload": payload})


@insurance.delete("/insurance/contract-prices/{cp_id}")
def delete_contract_price(cp_id: int,
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.manage")
    return ok({"deleted": True, "cp_id": cp_id})


@insurance.post("/insurance/evaluate")
def evaluate_pricing(payload: Dict[str, Any],
                     db: Session = Depends(get_db),
                     user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.insurance.view")
    # TODO: implement coverage + contract pricing + caps + copay
    return ok({"evaluated": True, "result": [], "payload": payload})


# ============================================================
# 3) PROCUREMENT - PO
# ============================================================
@procurement.get("/purchase-orders")
def list_purchase_orders(
        status: Optional[str] = Query(default=None),
        supplier_id: Optional[int] = Query(default=None),
        store_id: Optional[int] = Query(default=None),
        from_dt: Optional[date] = Query(default=None, alias="from"),
        to_dt: Optional[date] = Query(default=None, alias="to"),
        q: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.po.view")
    return ok({
        "items": [],
        "filters": {
            "status": status,
            "supplier_id": supplier_id,
            "store_id": store_id,
            "from": from_dt,
            "to": to_dt,
            "q": q
        },
        "limit": limit,
        "offset": offset
    })


@procurement.post("/purchase-orders", status_code=201)
def create_purchase_order(payload: Dict[str, Any],
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.po.create")
    return ok({"created": True, "payload": payload}, status_code=201)


@procurement.get("/purchase-orders/{po_id}")
def get_purchase_order(po_id: int,
                       db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.po.view")
    return ok({"id": po_id})


@procurement.put("/purchase-orders/{po_id}")
def update_purchase_order(po_id: int,
                          payload: Dict[str, Any],
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.po.update")
    return ok({"updated": True, "id": po_id, "payload": payload})


@procurement.delete("/purchase-orders/{po_id}")
def delete_purchase_order(po_id: int,
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.po.manage")
    return ok({"deleted": True, "id": po_id})


@procurement.post("/purchase-orders/{po_id}/submit")
def submit_purchase_order(po_id: int,
                          payload: Dict[str, Any] = None,
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.po.create")
    return ok({"submitted": True, "id": po_id, "payload": payload or {}})


@procurement.post("/purchase-orders/{po_id}/approve")
def approve_purchase_order(po_id: int,
                           payload: Dict[str, Any] = None,
                           db: Session = Depends(get_db),
                           user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.po.approve")
    return ok({"approved": True, "id": po_id, "payload": payload or {}})


@procurement.post("/purchase-orders/{po_id}/cancel")
def cancel_purchase_order(po_id: int,
                          payload: Dict[str, Any],
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.po.manage")
    return ok({"cancelled": True, "id": po_id, "payload": payload})


@procurement.post("/purchase-orders/{po_id}/reopen")
def reopen_purchase_order(po_id: int,
                          payload: Dict[str, Any] = None,
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.po.manage")
    return ok({"reopened": True, "id": po_id, "payload": payload or {}})


@procurement.post("/purchase-orders/{po_id}/revise")
def revise_purchase_order(po_id: int,
                          payload: Dict[str, Any] = None,
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.po.manage")
    # TODO: clone PO with revision_no+1
    return ok({"revised": True, "id": po_id, "payload": payload or {}})


# ============================================================
# 4) PROCUREMENT - GRN
# ============================================================
@procurement.get("/grns")
def list_grns(
        status: Optional[str] = Query(default=None),
        supplier_id: Optional[int] = Query(default=None),
        store_id: Optional[int] = Query(default=None),
        po_id: Optional[int] = Query(default=None),
        from_dt: Optional[date] = Query(default=None, alias="from"),
        to_dt: Optional[date] = Query(default=None, alias="to"),
        q: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.grn.view")
    return ok({
        "items": [],
        "filters": {
            "status": status,
            "supplier_id": supplier_id,
            "store_id": store_id,
            "po_id": po_id,
            "from": from_dt,
            "to": to_dt,
            "q": q
        },
        "limit": limit,
        "offset": offset
    })


@procurement.post("/grns", status_code=201)
def create_grn(payload: Dict[str, Any],
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.create")
    return ok({"created": True, "payload": payload}, status_code=201)


@procurement.get("/grns/{grn_id}")
def get_grn(grn_id: int,
            db: Session = Depends(get_db),
            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.view")
    return ok({"id": grn_id})


@procurement.put("/grns/{grn_id}")
def update_grn(grn_id: int,
               payload: Dict[str, Any],
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.update")
    return ok({"updated": True, "id": grn_id, "payload": payload})


@procurement.delete("/grns/{grn_id}")
def delete_grn(grn_id: int,
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.manage")
    return ok({"deleted": True, "id": grn_id})


@procurement.post("/grns/{grn_id}/submit")
def submit_grn(grn_id: int,
               payload: Dict[str, Any] = None,
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.create")
    return ok({"submitted": True, "id": grn_id, "payload": payload or {}})


@procurement.post("/grns/{grn_id}/approve")
def approve_grn(grn_id: int,
                payload: Dict[str, Any] = None,
                db: Session = Depends(get_db),
                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.approve")
    return ok({"approved": True, "id": grn_id, "payload": payload or {}})


@procurement.post("/grns/{grn_id}/cancel")
def cancel_grn(grn_id: int,
               payload: Dict[str, Any],
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.manage")
    return ok({"cancelled": True, "id": grn_id, "payload": payload})


@procurement.post("/grns/{grn_id}/recalculate")
def recalc_grn(grn_id: int,
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.update")
    # TODO: compute totals + landed cost preview
    return ok({"recalculated": True, "id": grn_id})


@procurement.get("/grns/{grn_id}/po-variance")
def grn_po_variance(grn_id: int,
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.view")
    return ok({"id": grn_id, "variance": []})


@procurement.post("/grns/{grn_id}/post")
def post_grn(grn_id: int,
             payload: Dict[str, Any] = None,
             db: Session = Depends(get_db),
             user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.post")
    # TODO: transaction -> create/update batches -> ledger IN -> balances -> audit
    return ok({"posted": True, "id": grn_id, "payload": payload or {}})


@procurement.get("/grns/{grn_id}/print")
def print_grn(grn_id: int,
              db: Session = Depends(get_db),
              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.grn.view")
    # TODO: return PDF url / bytes
    return ok({"id": grn_id, "print": "TODO"})


# ============================================================
# 5) PROCUREMENT - Purchase Invoice
# ============================================================
@procurement.get("/purchase-invoices")
def list_purchase_invoices(
        status: Optional[str] = Query(default=None),
        supplier_id: Optional[int] = Query(default=None),
        store_id: Optional[int] = Query(default=None),
        grn_id: Optional[int] = Query(default=None),
        from_dt: Optional[date] = Query(default=None, alias="from"),
        to_dt: Optional[date] = Query(default=None, alias="to"),
        q: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.invoice.view")
    return ok({
        "items": [],
        "filters": {
            "status": status,
            "supplier_id": supplier_id,
            "store_id": store_id,
            "grn_id": grn_id,
            "from": from_dt,
            "to": to_dt,
            "q": q
        },
        "limit": limit,
        "offset": offset
    })


@procurement.post("/purchase-invoices", status_code=201)
def create_purchase_invoice(payload: Dict[str, Any],
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.invoice.create")
    return ok({"created": True, "payload": payload}, status_code=201)


@procurement.get("/purchase-invoices/{inv_id}")
def get_purchase_invoice(inv_id: int,
                         db: Session = Depends(get_db),
                         user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.invoice.view")
    return ok({"id": inv_id})


@procurement.put("/purchase-invoices/{inv_id}")
def update_purchase_invoice(inv_id: int,
                            payload: Dict[str, Any],
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.invoice.update")
    return ok({"updated": True, "id": inv_id, "payload": payload})


@procurement.delete("/purchase-invoices/{inv_id}")
def delete_purchase_invoice(inv_id: int,
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.invoice.manage")
    return ok({"deleted": True, "id": inv_id})


@procurement.post("/purchase-invoices/{inv_id}/submit")
def submit_purchase_invoice(inv_id: int,
                            payload: Dict[str, Any] = None,
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.invoice.create")
    return ok({"submitted": True, "id": inv_id, "payload": payload or {}})


@procurement.post("/purchase-invoices/{inv_id}/approve")
def approve_purchase_invoice(inv_id: int,
                             payload: Dict[str, Any] = None,
                             db: Session = Depends(get_db),
                             user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.invoice.approve")
    return ok({"approved": True, "id": inv_id, "payload": payload or {}})


@procurement.post("/purchase-invoices/{inv_id}/cancel")
def cancel_purchase_invoice(inv_id: int,
                            payload: Dict[str, Any],
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.invoice.manage")
    return ok({"cancelled": True, "id": inv_id, "payload": payload})


@procurement.post("/purchase-invoices/{inv_id}/post")
def post_purchase_invoice(inv_id: int,
                          payload: Dict[str, Any] = None,
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.invoice.post")
    # TODO: reconcile GRN vs invoice, apply cost corrections, audit
    return ok({"posted": True, "id": inv_id, "payload": payload or {}})


# ============================================================
# 6) INVENTORY READ - Batches, Balances, Ledger, FEFO
# ============================================================
@inventory.get("/batches")
def list_batches(
        item_id: Optional[int] = Query(default=None),
        batch_no: Optional[str] = Query(default=None),
        expiry_from: Optional[date] = Query(default=None),
        expiry_to: Optional[date] = Query(default=None),
        is_quarantined: Optional[bool] = Query(default=None),
        is_recalled: Optional[bool] = Query(default=None),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.stock.view")
    return ok([])


@inventory.get("/batches/{batch_id}")
def get_batch(batch_id: int,
              db: Session = Depends(get_db),
              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.view")
    return ok({"id": batch_id})


@inventory.post("/batches/{batch_id}/quarantine")
def quarantine_batch(batch_id: int,
                     payload: Dict[str, Any],
                     db: Session = Depends(get_db),
                     user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.manage")
    return ok({"quarantined": True, "batch_id": batch_id, "payload": payload})


@inventory.post("/batches/{batch_id}/recall")
def recall_batch(batch_id: int,
                 payload: Dict[str, Any],
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.manage")
    return ok({"recalled": True, "batch_id": batch_id, "payload": payload})


@inventory.post("/batches/{batch_id}/release")
def release_batch(batch_id: int,
                  payload: Dict[str, Any] = None,
                  db: Session = Depends(get_db),
                  user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.manage")
    return ok({
        "released": True,
        "batch_id": batch_id,
        "payload": payload or {}
    })


@inventory.get("/stock/balances")
def list_stock_balances(
        store_id: Optional[int] = Query(default=None),
        item_id: Optional[int] = Query(default=None),
        q: Optional[str] = Query(default=None),
        expiry_within_days: Optional[int] = Query(default=None, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.stock.view")
    return ok({"items": [], "limit": limit, "offset": offset})


@inventory.get("/stock/summary")
def stock_summary(
        store_id: Optional[int] = Query(default=None),
        q: Optional[str] = Query(default=None),
        below: Optional[str] = Query(default=None),  # reorder|min|max
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.stock.view")
    return ok({"items": [], "limit": limit, "offset": offset, "below": below})


@inventory.get("/stock/ledger")
def list_stock_ledger(
        store_id: Optional[int] = Query(default=None),
        item_id: Optional[int] = Query(default=None),
        batch_id: Optional[int] = Query(default=None),
        reason: Optional[str] = Query(default=None),
        from_dt: Optional[datetime] = Query(default=None, alias="from"),
        to_dt: Optional[datetime] = Query(default=None, alias="to"),
        limit: int = Query(default=200, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.stock.ledger")
    return ok({"items": [], "limit": limit, "offset": offset})


@inventory.get("/stock/ledger/{ledger_id}")
def get_stock_ledger_row(ledger_id: int,
                         db: Session = Depends(get_db),
                         user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.ledger")
    return ok({"id": ledger_id})


@inventory.get("/stock/fefo")
def fefo_suggest(store_id: int = Query(...),
                 item_id: int = Query(...),
                 qty_base: float = Query(..., gt=0),
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.view")
    # TODO: compute FEFO pick plan
    return ok({
        "store_id": store_id,
        "item_id": item_id,
        "qty_base": qty_base,
        "plan": []
    })


# ============================================================
# 7) STOCK OPS - Adjustments
# ============================================================
@stock_ops.get("/stock-adjustments")
def list_stock_adjustments(
        status: Optional[str] = Query(default=None),
        store_id: Optional[int] = Query(default=None),
        from_dt: Optional[date] = Query(default=None, alias="from"),
        to_dt: Optional[date] = Query(default=None, alias="to"),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.stock.adjust.view")
    return ok([])


@stock_ops.post("/stock-adjustments", status_code=201)
def create_stock_adjustment(payload: Dict[str, Any],
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.adjust")
    return ok({"created": True, "payload": payload}, status_code=201)


@stock_ops.get("/stock-adjustments/{adj_id}")
def get_stock_adjustment(adj_id: int,
                         db: Session = Depends(get_db),
                         user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.adjust.view")
    return ok({"id": adj_id})


@stock_ops.put("/stock-adjustments/{adj_id}")
def update_stock_adjustment(adj_id: int,
                            payload: Dict[str, Any],
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.adjust")
    return ok({"updated": True, "id": adj_id, "payload": payload})


@stock_ops.post("/stock-adjustments/{adj_id}/submit")
def submit_stock_adjustment(adj_id: int,
                            payload: Dict[str, Any] = None,
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.adjust")
    return ok({"submitted": True, "id": adj_id, "payload": payload or {}})


@stock_ops.post("/stock-adjustments/{adj_id}/approve")
def approve_stock_adjustment(adj_id: int,
                             payload: Dict[str, Any] = None,
                             db: Session = Depends(get_db),
                             user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.adjust.approve")
    return ok({"approved": True, "id": adj_id, "payload": payload or {}})


@stock_ops.post("/stock-adjustments/{adj_id}/cancel")
def cancel_stock_adjustment(adj_id: int,
                            payload: Dict[str, Any],
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.adjust")
    return ok({"cancelled": True, "id": adj_id, "payload": payload})


@stock_ops.post("/stock-adjustments/{adj_id}/post")
def post_stock_adjustment(adj_id: int,
                          payload: Dict[str, Any] = None,
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.adjust.post")
    # TODO: ledger +/- and balances in transaction
    return ok({"posted": True, "id": adj_id, "payload": payload or {}})


# ============================================================
# 8) STOCK OPS - Transfers (Issue/Receive)
# ============================================================
@stock_ops.get("/stock-transfers")
def list_stock_transfers(
        status: Optional[str] = Query(default=None),
        from_store_id: Optional[int] = Query(default=None),
        to_store_id: Optional[int] = Query(default=None),
        from_dt: Optional[date] = Query(default=None, alias="from"),
        to_dt: Optional[date] = Query(default=None, alias="to"),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.stock.transfer.view")
    return ok([])


@stock_ops.post("/stock-transfers", status_code=201)
def create_stock_transfer(payload: Dict[str, Any],
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.transfer")
    return ok({"created": True, "payload": payload}, status_code=201)


@stock_ops.get("/stock-transfers/{transfer_id}")
def get_stock_transfer(transfer_id: int,
                       db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.transfer.view")
    return ok({"id": transfer_id})


@stock_ops.put("/stock-transfers/{transfer_id}")
def update_stock_transfer(transfer_id: int,
                          payload: Dict[str, Any],
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.transfer")
    return ok({"updated": True, "id": transfer_id, "payload": payload})


@stock_ops.post("/stock-transfers/{transfer_id}/submit")
def submit_stock_transfer(transfer_id: int,
                          payload: Dict[str, Any] = None,
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.transfer")
    return ok({"submitted": True, "id": transfer_id, "payload": payload or {}})


@stock_ops.post("/stock-transfers/{transfer_id}/approve")
def approve_stock_transfer(transfer_id: int,
                           payload: Dict[str, Any] = None,
                           db: Session = Depends(get_db),
                           user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.transfer.approve")
    return ok({"approved": True, "id": transfer_id, "payload": payload or {}})


@stock_ops.post("/stock-transfers/{transfer_id}/cancel")
def cancel_stock_transfer(transfer_id: int,
                          payload: Dict[str, Any],
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.transfer")
    return ok({"cancelled": True, "id": transfer_id, "payload": payload})


@stock_ops.post("/stock-transfers/{transfer_id}/issue")
def issue_stock_transfer(transfer_id: int,
                         payload: Dict[str, Any] = None,
                         db: Session = Depends(get_db),
                         user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.transfer.issue")
    # TODO: ledger OUT from from_store
    return ok({"issued": True, "id": transfer_id, "payload": payload or {}})


@stock_ops.post("/stock-transfers/{transfer_id}/receive")
def receive_stock_transfer(transfer_id: int,
                           payload: Dict[str, Any] = None,
                           db: Session = Depends(get_db),
                           user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.transfer.receive")
    # TODO: ledger IN to to_store
    return ok({"received": True, "id": transfer_id, "payload": payload or {}})


# ============================================================
# 9) STOCK OPS - Stock Count (Freeze/Post variance)
# ============================================================
@stock_ops.get("/stock-counts")
def list_stock_counts(
        status: Optional[str] = Query(default=None),
        store_id: Optional[int] = Query(default=None),
        from_dt: Optional[date] = Query(default=None, alias="from"),
        to_dt: Optional[date] = Query(default=None, alias="to"),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.stock.count.view")
    return ok([])


@stock_ops.post("/stock-counts", status_code=201)
def create_stock_count(payload: Dict[str, Any],
                       db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.count")
    return ok({"created": True, "payload": payload}, status_code=201)


@stock_ops.get("/stock-counts/{count_id}")
def get_stock_count(count_id: int,
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.count.view")
    return ok({"id": count_id})


@stock_ops.put("/stock-counts/{count_id}")
def update_stock_count(count_id: int,
                       payload: Dict[str, Any],
                       db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.count")
    return ok({"updated": True, "id": count_id, "payload": payload})


@stock_ops.post("/stock-counts/{count_id}/freeze")
def freeze_stock_count(count_id: int,
                       payload: Dict[str, Any] = None,
                       db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.count")
    return ok({"frozen": True, "id": count_id, "payload": payload or {}})


@stock_ops.post("/stock-counts/{count_id}/unfreeze")
def unfreeze_stock_count(count_id: int,
                         payload: Dict[str, Any] = None,
                         db: Session = Depends(get_db),
                         user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.count")
    return ok({"unfrozen": True, "id": count_id, "payload": payload or {}})


@stock_ops.post("/stock-counts/{count_id}/submit")
def submit_stock_count(count_id: int,
                       payload: Dict[str, Any] = None,
                       db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.count")
    return ok({"submitted": True, "id": count_id, "payload": payload or {}})


@stock_ops.post("/stock-counts/{count_id}/approve")
def approve_stock_count(count_id: int,
                        payload: Dict[str, Any] = None,
                        db: Session = Depends(get_db),
                        user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.count.approve")
    return ok({"approved": True, "id": count_id, "payload": payload or {}})


@stock_ops.post("/stock-counts/{count_id}/cancel")
def cancel_stock_count(count_id: int,
                       payload: Dict[str, Any],
                       db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.count")
    return ok({"cancelled": True, "id": count_id, "payload": payload})


@stock_ops.post("/stock-counts/{count_id}/post")
def post_stock_count(count_id: int,
                     payload: Dict[str, Any] = None,
                     db: Session = Depends(get_db),
                     user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.stock.count.post")
    # TODO: ledger variance + balances
    return ok({"posted": True, "id": count_id, "payload": payload or {}})


# ============================================================
# 10) DISPENSE (OP/IP/ER/OT) + Verify + Return
# ============================================================
@dispense.get("/dispenses")
def list_dispenses(
        status: Optional[str] = Query(default=None),
        type: Optional[str] = Query(default=None),
        store_id: Optional[int] = Query(default=None),
        patient_id: Optional[int] = Query(default=None),
        admission_id: Optional[int] = Query(default=None),
        encounter_id: Optional[int] = Query(default=None),
        from_dt: Optional[date] = Query(default=None, alias="from"),
        to_dt: Optional[date] = Query(default=None, alias="to"),
        q: Optional[str] = Query(default=None),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.dispense.view")
    return ok([])


@dispense.post("/dispenses", status_code=201)
def create_dispense(payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.create")
    return ok({"created": True, "payload": payload}, status_code=201)


@dispense.get("/dispenses/{dispense_id}")
def get_dispense(dispense_id: int,
                 db: Session = Depends(get_db),
                 user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.view")
    return ok({"id": dispense_id})


@dispense.put("/dispenses/{dispense_id}")
def update_dispense(dispense_id: int,
                    payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.update")
    return ok({"updated": True, "id": dispense_id, "payload": payload})


@dispense.post("/dispenses/{dispense_id}/submit")
def submit_dispense(dispense_id: int,
                    payload: Dict[str, Any] = None,
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.create")
    return ok({"submitted": True, "id": dispense_id, "payload": payload or {}})


@dispense.post("/dispenses/{dispense_id}/approve")
def approve_dispense(dispense_id: int,
                     payload: Dict[str, Any] = None,
                     db: Session = Depends(get_db),
                     user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.approve")
    return ok({"approved": True, "id": dispense_id, "payload": payload or {}})


@dispense.post("/dispenses/{dispense_id}/cancel")
def cancel_dispense(dispense_id: int,
                    payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.cancel")
    return ok({"cancelled": True, "id": dispense_id, "payload": payload})


@dispense.post("/dispenses/{dispense_id}/post")
def post_dispense(dispense_id: int,
                  payload: Dict[str, Any] = None,
                  db: Session = Depends(get_db),
                  user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.post")
    # TODO: FEFO pick -> validate -> insurance split -> ledger OUT -> balances -> audit
    return ok({"posted": True, "id": dispense_id, "payload": payload or {}})


@dispense.post("/dispenses/{dispense_id}/verify")
def verify_dispense(dispense_id: int,
                    payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.verify")
    return ok({"verified": True, "id": dispense_id, "payload": payload})


@dispense.get("/dispenses/{dispense_id}/fefo-suggest")
def dispense_fefo_suggest(dispense_id: int,
                          item_id: int = Query(...),
                          qty_base: float = Query(..., gt=0),
                          db: Session = Depends(get_db),
                          user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.view")
    return ok({
        "dispense_id": dispense_id,
        "item_id": item_id,
        "qty_base": qty_base,
        "plan": []
    })


@dispense.post("/dispenses/{dispense_id}/return")
def return_dispense(dispense_id: int,
                    payload: Dict[str, Any],
                    db: Session = Depends(get_db),
                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.dispense.return")
    # TODO: ledger IN with reason DISPENSE_RETURN_IN
    return ok({"returned": True, "id": dispense_id, "payload": payload})


# ============================================================
# 11) ALERTS
# ============================================================
@alerts.get("/alerts")
def list_alerts(
        store_id: Optional[int] = Query(default=None),
        type: Optional[str] = Query(default=None),
        severity: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: Any = Depends(current_user),
):
    require_perm(user, "pharmacy.alerts.view")
    return ok({"items": [], "limit": limit, "offset": offset})


@alerts.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int,
              payload: Dict[str, Any] = None,
              db: Session = Depends(get_db),
              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.alerts.manage")
    return ok({"acked": True, "id": alert_id, "payload": payload or {}})


@alerts.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int,
                  payload: Dict[str, Any] = None,
                  db: Session = Depends(get_db),
                  user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.alerts.manage")
    return ok({"resolved": True, "id": alert_id, "payload": payload or {}})


# ============================================================
# 12) REPORTS
# ============================================================
@reports.get("/reports/stock-ledger")
def report_stock_ledger(store_id: Optional[int] = None,
                        item_id: Optional[int] = None,
                        from_dt: Optional[date] = Query(default=None,
                                                        alias="from"),
                        to_dt: Optional[date] = Query(default=None,
                                                      alias="to"),
                        format: str = "json",
                        db: Session = Depends(get_db),
                        user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.reports.view")
    return ok({"format": format, "rows": []})


@reports.get("/reports/stock-valuation")
def report_stock_valuation(store_id: Optional[int] = None,
                           as_of: Optional[date] = None,
                           format: str = "json",
                           db: Session = Depends(get_db),
                           user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.reports.view")
    return ok({"format": format, "as_of": as_of, "rows": []})


@reports.get("/reports/expiry")
def report_expiry(store_id: Optional[int] = None,
                  within_days: int = 90,
                  format: str = "json",
                  db: Session = Depends(get_db),
                  user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.reports.view")
    return ok({"within_days": within_days, "format": format, "rows": []})


@reports.get("/reports/grn-vs-invoice-mismatch")
def report_grn_invoice_mismatch(from_dt: Optional[date] = Query(default=None,
                                                                alias="from"),
                                to_dt: Optional[date] = Query(default=None,
                                                              alias="to"),
                                format: str = "json",
                                db: Session = Depends(get_db),
                                user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.reports.view")
    return ok({"format": format, "rows": []})


@reports.get("/reports/consumption")
def report_consumption(store_id: Optional[int] = None,
                       type: Optional[str] = None,
                       from_dt: Optional[date] = Query(default=None,
                                                       alias="from"),
                       to_dt: Optional[date] = Query(default=None, alias="to"),
                       format: str = "json",
                       db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.reports.view")
    return ok({"format": format, "rows": []})


@reports.get("/reports/insurance-claim-register")
def report_insurance_claim_register(payer_id: Optional[int] = None,
                                    plan_id: Optional[int] = None,
                                    from_dt: Optional[date] = Query(
                                        default=None, alias="from"),
                                    to_dt: Optional[date] = Query(default=None,
                                                                  alias="to"),
                                    format: str = "json",
                                    db: Session = Depends(get_db),
                                    user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.reports.view")
    return ok({"format": format, "rows": []})


@reports.get("/reports/controlled-drugs")
def report_controlled_drugs(from_dt: Optional[date] = Query(default=None,
                                                            alias="from"),
                            to_dt: Optional[date] = Query(default=None,
                                                          alias="to"),
                            item_id: Optional[int] = None,
                            format: str = "json",
                            db: Session = Depends(get_db),
                            user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.reports.view")
    return ok({"format": format, "rows": []})


@reports.get("/reports/slow-moving")
def report_slow_moving(store_id: Optional[int] = None,
                       days: int = 90,
                       format: str = "json",
                       db: Session = Depends(get_db),
                       user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.reports.view")
    return ok({"days": days, "format": format, "rows": []})


@reports.get("/reports/dead-stock")
def report_dead_stock(store_id: Optional[int] = None,
                      days: int = 180,
                      format: str = "json",
                      db: Session = Depends(get_db),
                      user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.reports.view")
    return ok({"days": days, "format": format, "rows": []})


# ============================================================
# 13) AUDIT
# ============================================================
@audit.get("/audit")
def list_audit(entity_type: Optional[str] = None,
               entity_id: Optional[int] = None,
               from_dt: Optional[datetime] = Query(default=None, alias="from"),
               to_dt: Optional[datetime] = Query(default=None, alias="to"),
               actor_user_id: Optional[int] = None,
               db: Session = Depends(get_db),
               user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.audit.view")
    return ok([])


@audit.get("/audit/{audit_id}")
def get_audit(audit_id: int,
              db: Session = Depends(get_db),
              user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.audit.view")
    return ok({"id": audit_id})


@audit.get("/timeline")
def timeline(store_id: Optional[int] = None,
             patient_id: Optional[int] = None,
             admission_id: Optional[int] = None,
             from_dt: Optional[datetime] = Query(default=None, alias="from"),
             to_dt: Optional[datetime] = Query(default=None, alias="to"),
             db: Session = Depends(get_db),
             user: Any = Depends(current_user)):
    require_perm(user, "pharmacy.audit.view")
    return ok([])


# ============================================================
# Register routers
# ============================================================
pharmacy_router.include_router(masters)
pharmacy_router.include_router(insurance)
pharmacy_router.include_router(procurement)
pharmacy_router.include_router(inventory)
pharmacy_router.include_router(stock_ops)
pharmacy_router.include_router(dispense)
pharmacy_router.include_router(alerts)
pharmacy_router.include_router(reports)
pharmacy_router.include_router(audit)

router.include_router(pharmacy_router)