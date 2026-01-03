# FILE: app/services/pharmacy_stock_alerts.py
from __future__ import annotations

from datetime import datetime, date as dt_date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Dict, Any, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func, case, and_, or_

from app.models.pharmacy_inventory import (
    InventoryLocation,
    InventoryItem,
    ItemLocationStock,
    ItemBatch,
    StockTransaction,
    PurchaseOrder,
    GRN,
    POStatus,
    GRNStatus,
)

from app.schemas.pharmacy_stock_alerts import (
    StockAlertsDashboardOut,
    StockAlertsFiltersOut,
    StockKpisOut,
    LocationStockSummaryOut,
    StockAlertOut,
    AlertType,
    AlertSeverity,
    FastMovingOut,
    MovementBucketOut,
    ProcurementPipelineOut,
    ItemBatchRowOut,
    FEFOItemSuggestionOut,
    SpikeOut,
    ReportType,
)

ZERO = Decimal("0")
QTY_EPS = Decimal("0.0001")


def _d(v) -> Decimal:
    if v is None:
        return ZERO
    try:
        return Decimal(str(v))
    except Exception:
        return ZERO


def _q(v) -> Decimal:
    # qty helper (keep precision)
    return _d(v)


def _money(v) -> Decimal:
    # money helper
    return _d(v)


def _ceil_dec(v: Decimal) -> Decimal:
    # ceiling for decimals (to whole number)
    if v <= 0:
        return ZERO
    n = v.to_integral_value(rounding="ROUND_CEILING")
    return Decimal(str(n))


# ✅ MySQL-safe ordering helpers (avoid .nullsfirst/.nullslast)
def _order_nulls_last(col):
    return (col.is_(None).asc(), col.asc())


def _order_nulls_first(col):
    return (col.is_(None).desc(), col.asc())


def _resolve_locations(db: Session, location_id: Optional[int]) -> List[InventoryLocation]:
    q = db.query(InventoryLocation).filter(
        InventoryLocation.is_active.is_(True),
        InventoryLocation.is_pharmacy.is_(True),
    )
    if location_id:
        q = q.filter(InventoryLocation.id == location_id)
    return q.order_by(InventoryLocation.name.asc()).all()


def _apply_item_filters(q, item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int]):
    if item_type:
        q = q.filter(InventoryItem.item_type == item_type)
    if schedule_code:
        q = q.filter(InventoryItem.schedule_code == schedule_code)
    if supplier_id:
        q = q.filter(InventoryItem.default_supplier_id == supplier_id)
    return q


def _stock_base_query(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int]):
    q = (
        db.query(
            ItemLocationStock.location_id.label("location_id"),
            InventoryLocation.name.label("location_name"),
            ItemLocationStock.item_id.label("item_id"),
            InventoryItem.code.label("code"),
            InventoryItem.name.label("name"),
            InventoryItem.item_type.label("item_type"),
            InventoryItem.schedule_code.label("schedule_code"),
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemLocationStock.on_hand_qty.label("on_hand_qty"),
            InventoryItem.reorder_level.label("reorder_level"),
            InventoryItem.max_level.label("max_level"),
            ItemLocationStock.last_unit_cost.label("last_unit_cost"),
            ItemLocationStock.last_mrp.label("last_mrp"),
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            InventoryLocation.is_active.is_(True),
            InventoryLocation.is_pharmacy.is_(True),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    return q


# ----------------------------
# Batch valuation base (mandatory batch-wise prices)
# ----------------------------
def _batch_valuation_query(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int]):
    q = (
        db.query(
            ItemBatch.location_id.label("location_id"),
            InventoryLocation.name.label("location_name"),
            ItemBatch.item_id.label("item_id"),
            InventoryItem.code.label("item_code"),
            InventoryItem.name.label("item_name"),
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemBatch.id.label("batch_id"),
            ItemBatch.batch_no.label("batch_no"),
            ItemBatch.expiry_date.label("expiry_date"),
            ItemBatch.current_qty.label("qty"),
            ItemBatch.unit_cost.label("unit_cost"),
            ItemBatch.mrp.label("mrp"),
            (ItemBatch.current_qty * ItemBatch.unit_cost).label("value_purchase"),
            (ItemBatch.current_qty * ItemBatch.mrp).label("value_mrp"),
            ItemBatch.is_saleable.label("is_saleable"),
            ItemBatch.status.label("status"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemBatch.location_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.current_qty > 0,
            InventoryItem.is_active.is_(True),
            InventoryLocation.is_active.is_(True),
            InventoryLocation.is_pharmacy.is_(True),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    return q


def get_dashboard(
    db: Session,
    location_id: Optional[int] = None,
    item_type: Optional[str] = None,
    schedule_code: Optional[str] = None,
    supplier_id: Optional[int] = None,
    days_near_expiry: Optional[int] = None,
    non_moving_days: int = 60,
    fast_moving_days: int = 30,
    consumption_days: int = 30,
    lead_time_days: int = 7,
    high_value_expiry_threshold: Decimal = Decimal("0"),
    preview_limit: int = 25,
) -> StockAlertsDashboardOut:
    now = datetime.utcnow()
    today = dt_date.today()

    locations = _resolve_locations(db, location_id)
    loc_ids = [l.id for l in locations]

    if not loc_ids:
        filters = StockAlertsFiltersOut(
            location_id=location_id,
            item_type=item_type,
            schedule_code=schedule_code,
            supplier_id=supplier_id,
            days_near_expiry=int(days_near_expiry or 90),
            non_moving_days=non_moving_days,
            fast_moving_days=fast_moving_days,
            lead_time_days=lead_time_days,
            consumption_days=consumption_days,
            high_value_expiry_threshold=_money(high_value_expiry_threshold),
        )
        return StockAlertsDashboardOut(
            as_of=now,
            filters=filters,
            kpis=StockKpisOut(),
            locations=[],
            alerts_preview=[],
            movement_today=[],
            movement_week=[],
            spikes=[],
            pipeline=ProcurementPipelineOut(),
            fefo_next_to_dispense=[],
        )

    # default near expiry days
    if days_near_expiry is None:
        if len(locations) == 1:
            days_near_expiry = int(locations[0].expiry_alert_days or 90)
        else:
            days_near_expiry = 90

    filters = StockAlertsFiltersOut(
        location_id=location_id,
        item_type=item_type,
        schedule_code=schedule_code,
        supplier_id=supplier_id,
        days_near_expiry=int(days_near_expiry),
        non_moving_days=non_moving_days,
        fast_moving_days=fast_moving_days,
        lead_time_days=lead_time_days,
        consumption_days=consumption_days,
        high_value_expiry_threshold=_money(high_value_expiry_threshold),
    )

    # ----------------------------
    # Master counts (items)
    # ----------------------------
    items_q = db.query(InventoryItem).filter(InventoryItem.is_active.is_(True))
    items_q = _apply_item_filters(items_q, item_type, schedule_code, supplier_id)

    total_items_count = int(db.query(func.count(InventoryItem.id)).scalar() or 0)
    active_items_count = int(items_q.with_entities(func.count(InventoryItem.id)).scalar() or 0)
    inactive_items_count = int(
        db.query(func.count(InventoryItem.id))
        .filter(InventoryItem.is_active.is_(False))
        .scalar()
        or 0
    )

    # ----------------------------
    # KPI stock base (ItemLocationStock for low/out/over)
    # ----------------------------
    stock_sq = _stock_base_query(db, loc_ids, item_type, schedule_code, supplier_id).subquery()

    kpi_row = db.query(
        func.count(func.distinct(stock_sq.c.location_id)).label("locations_count"),
        func.count(func.distinct(stock_sq.c.item_id)).label("active_items_count"),
        func.coalesce(
            func.sum(
                case(
                    (and_(stock_sq.c.reorder_level > 0, stock_sq.c.on_hand_qty > 0, stock_sq.c.on_hand_qty <= stock_sq.c.reorder_level), 1),
                    else_=0,
                )
            ),
            0,
        ).label("low_stock_count"),
        func.coalesce(func.sum(case((stock_sq.c.on_hand_qty <= 0, 1), else_=0)), 0).label("out_of_stock_count"),
        func.coalesce(
            func.sum(case((and_(stock_sq.c.max_level > 0, stock_sq.c.on_hand_qty > stock_sq.c.max_level), 1), else_=0)),
            0,
        ).label("over_stock_count"),
    ).one()

    # ----------------------------
    # Valuation must be batch-wise (different batch + different prices)
    # ----------------------------
    val_sq = _batch_valuation_query(db, loc_ids, item_type, schedule_code, supplier_id).subquery()
    val_row = db.query(
        func.coalesce(func.sum(val_sq.c.value_purchase), 0).label("stock_value_purchase"),
        func.coalesce(func.sum(val_sq.c.value_mrp), 0).label("stock_value_mrp"),
    ).one()

    # Expired valuation
    expired_row = db.query(
        func.coalesce(func.sum(case((and_(val_sq.c.expiry_date.isnot(None), val_sq.c.expiry_date < today), val_sq.c.value_purchase), else_=0)), 0).label("expired_value_purchase"),
        func.coalesce(func.sum(case((and_(val_sq.c.expiry_date.isnot(None), val_sq.c.expiry_date < today), val_sq.c.value_mrp), else_=0)), 0).label("expired_value_mrp"),
        func.coalesce(func.sum(case((and_(val_sq.c.expiry_date.isnot(None), val_sq.c.expiry_date < today), 1), else_=0)), 0).label("expired_count"),
    ).one()

    kpis = StockKpisOut(
        total_items_count=int(total_items_count),
        active_items_count=int(active_items_count),
        inactive_items_count=int(inactive_items_count),
        locations_count=int(kpi_row.locations_count or 0),
        stock_value_purchase=_money(val_row.stock_value_purchase),
        stock_value_mrp=_money(val_row.stock_value_mrp),
        low_stock_count=int(kpi_row.low_stock_count or 0),
        out_of_stock_count=int(kpi_row.out_of_stock_count or 0),
        over_stock_count=int(kpi_row.over_stock_count or 0),
        expired_count=int(expired_row.expired_count or 0),
        expired_value_purchase=_money(expired_row.expired_value_purchase),
        expired_value_mrp=_money(expired_row.expired_value_mrp),
    )

    # ----------------------------
    # Expiry buckets (batch-wise)
    # ----------------------------
    days_to_exp = func.datediff(ItemBatch.expiry_date, today)
    expiry_base = (
        db.query(
            func.coalesce(func.sum(case((and_(ItemBatch.expiry_date.isnot(None), days_to_exp >= 0, days_to_exp <= 7), 1), else_=0)), 0).label("d7"),
            func.coalesce(func.sum(case((and_(ItemBatch.expiry_date.isnot(None), days_to_exp >= 0, days_to_exp <= 30), 1), else_=0)), 0).label("d30"),
            func.coalesce(func.sum(case((and_(ItemBatch.expiry_date.isnot(None), days_to_exp >= 0, days_to_exp <= 60), 1), else_=0)), 0).label("d60"),
            func.coalesce(func.sum(case((and_(ItemBatch.expiry_date.isnot(None), days_to_exp >= 0, days_to_exp <= 90), 1), else_=0)), 0).label("d90"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            InventoryItem.is_active.is_(True),
        )
    )
    expiry_base = _apply_item_filters(expiry_base, item_type, schedule_code, supplier_id)
    exp_row = expiry_base.one()

    kpis.near_expiry_7 = int(exp_row.d7 or 0)
    kpis.near_expiry_30 = int(exp_row.d30 or 0)
    kpis.near_expiry_60 = int(exp_row.d60 or 0)
    kpis.near_expiry_90 = int(exp_row.d90 or 0)

    # ----------------------------
    # Non-moving counts 30/60/90 (item-level) based on last txn
    # ----------------------------
    last_txn_sq = (
        db.query(
            StockTransaction.location_id.label("location_id"),
            StockTransaction.item_id.label("item_id"),
            func.max(StockTransaction.txn_time).label("last_txn_time"),
        )
        .filter(StockTransaction.location_id.in_(loc_ids))
        .group_by(StockTransaction.location_id, StockTransaction.item_id)
        .subquery()
    )
    days_since_last = func.datediff(today, func.date(last_txn_sq.c.last_txn_time))

    nm_row = (
        db.query(
            func.coalesce(func.sum(case((or_(last_txn_sq.c.last_txn_time.is_(None), days_since_last >= 30), 1), else_=0)), 0).label("nm30"),
            func.coalesce(func.sum(case((or_(last_txn_sq.c.last_txn_time.is_(None), days_since_last >= 60), 1), else_=0)), 0).label("nm60"),
            func.coalesce(func.sum(case((or_(last_txn_sq.c.last_txn_time.is_(None), days_since_last >= 90), 1), else_=0)), 0).label("nm90"),
        )
        .select_from(ItemLocationStock)
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .outerjoin(
            last_txn_sq,
            and_(
                last_txn_sq.c.location_id == ItemLocationStock.location_id,
                last_txn_sq.c.item_id == ItemLocationStock.item_id,
            ),
        )
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            ItemLocationStock.on_hand_qty > 0,
        )
    )
    nm_row = _apply_item_filters(nm_row, item_type, schedule_code, supplier_id).one()

    kpis.non_moving_30_count = int(nm_row.nm30 or 0)
    kpis.non_moving_60_count = int(nm_row.nm60 or 0)
    kpis.non_moving_90_count = int(nm_row.nm90 or 0)

    # ----------------------------
    # Fast moving (top outflow)
    # ----------------------------
    since_dt = datetime.utcnow() - timedelta(days=fast_moving_days)
    fast_q = (
        db.query(
            StockTransaction.item_id.label("item_id"),
            InventoryItem.code.label("code"),
            InventoryItem.name.label("name"),
            func.coalesce(func.sum(-StockTransaction.quantity_change), 0).label("out_qty"),
        )
        .join(InventoryItem, InventoryItem.id == StockTransaction.item_id)
        .filter(
            StockTransaction.location_id.in_(loc_ids),
            StockTransaction.txn_time >= since_dt,
            StockTransaction.quantity_change < 0,
            StockTransaction.txn_type.in_(["DISPENSE", "SALE", "ISSUE"]),
            InventoryItem.is_active.is_(True),
        )
        .group_by(StockTransaction.item_id, InventoryItem.code, InventoryItem.name)
    )
    fast_q = _apply_item_filters(fast_q, item_type, schedule_code, supplier_id)
    fast_q = fast_q.order_by(func.sum(-StockTransaction.quantity_change).desc()).limit(10)

    fast_items = fast_q.all()
    kpis.fast_moving_top = [
        FastMovingOut(item_id=int(r.item_id), code=str(r.code), name=str(r.name), out_qty=_q(r.out_qty))
        for r in fast_items
    ]

    # ----------------------------
    # Location summaries (stock + valuation + expiry risk)
    # ----------------------------
    loc_rows = (
        db.query(
            InventoryLocation.id.label("location_id"),
            InventoryLocation.name.label("location_name"),
            func.coalesce(func.sum(case((ItemLocationStock.on_hand_qty > 0, 1), else_=0)), 0).label("items_with_stock"),
            func.coalesce(
                func.sum(case((and_(InventoryItem.reorder_level > 0, ItemLocationStock.on_hand_qty > 0, ItemLocationStock.on_hand_qty <= InventoryItem.reorder_level), 1), else_=0)),
                0,
            ).label("low_stock_count"),
            func.coalesce(func.sum(case((ItemLocationStock.on_hand_qty <= 0, 1), else_=0)), 0).label("out_of_stock_count"),
        )
        .join(ItemLocationStock, ItemLocationStock.location_id == InventoryLocation.id)
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .filter(
            InventoryLocation.id.in_(loc_ids),
            InventoryLocation.is_active.is_(True),
            InventoryLocation.is_pharmacy.is_(True),
            InventoryItem.is_active.is_(True),
        )
    )
    loc_rows = _apply_item_filters(loc_rows, item_type, schedule_code, supplier_id)
    loc_rows = loc_rows.group_by(InventoryLocation.id, InventoryLocation.name).order_by(InventoryLocation.name.asc()).all()

    # valuation per location (batch-wise)
    val_loc_rows = (
        db.query(
            ItemBatch.location_id.label("location_id"),
            func.coalesce(func.sum(ItemBatch.current_qty * ItemBatch.unit_cost), 0).label("stock_value_purchase"),
            func.coalesce(func.sum(ItemBatch.current_qty * ItemBatch.mrp), 0).label("stock_value_mrp"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.current_qty > 0,
            InventoryItem.is_active.is_(True),
        )
    )
    val_loc_rows = _apply_item_filters(val_loc_rows, item_type, schedule_code, supplier_id)
    val_loc_map = {int(r.location_id): ( _money(r.stock_value_purchase), _money(r.stock_value_mrp) )
                   for r in val_loc_rows.group_by(ItemBatch.location_id).all()}

    # expiry risk per location (use location.expiry_alert_days)
    exp_risk_rows = (
        db.query(
            ItemBatch.location_id.label("location_id"),
            func.count().label("expiry_risk_count"),
        )
        .join(InventoryLocation, InventoryLocation.id == ItemBatch.location_id)
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            func.datediff(ItemBatch.expiry_date, today) >= 0,
            func.datediff(ItemBatch.expiry_date, today) <= InventoryLocation.expiry_alert_days,
            InventoryItem.is_active.is_(True),
        )
    )
    exp_risk_rows = _apply_item_filters(exp_risk_rows, item_type, schedule_code, supplier_id)
    exp_risk_map = {int(r.location_id): int(r.expiry_risk_count or 0) for r in exp_risk_rows.group_by(ItemBatch.location_id).all()}

    loc_summaries: List[LocationStockSummaryOut] = []
    for r in loc_rows:
        loc_id = int(r.location_id)
        vpu, vmrp = val_loc_map.get(loc_id, (ZERO, ZERO))
        loc_summaries.append(
            LocationStockSummaryOut(
                location_id=loc_id,
                location_name=str(r.location_name),
                items_with_stock=int(r.items_with_stock or 0),
                low_stock_count=int(r.low_stock_count or 0),
                out_of_stock_count=int(r.out_of_stock_count or 0),
                expiry_risk_count=exp_risk_map.get(loc_id, 0),
                stock_value_purchase=vpu,
                stock_value_mrp=vmrp,
            )
        )

    # ----------------------------
    # Smart-alert counts (quick)
    # ----------------------------
    kpis.reorder_count = _count_reorder_items(db, loc_ids, item_type, schedule_code, supplier_id, consumption_days, lead_time_days)
    kpis.batch_risk_count = _count_batch_risk(db, loc_ids, item_type, schedule_code, supplier_id, non_moving_days=max(non_moving_days, 30))
    kpis.negative_stock_count = _count_negative_or_mismatch(db, loc_ids, item_type, schedule_code, supplier_id)
    kpis.high_value_expiry_count = _count_high_value_expiry(db, loc_ids, item_type, schedule_code, supplier_id, int(days_near_expiry), _money(high_value_expiry_threshold))
    kpis.controlled_drug_count = _count_controlled_stock(db, loc_ids, item_type, schedule_code, supplier_id)

    # ----------------------------
    # Alerts Preview (actionable + batch-wise)
    # ----------------------------
    alerts_preview: List[StockAlertOut] = []
    per = max(preview_limit // 6, 1)

    alerts_preview.extend(_preview_out_of_stock(db, loc_ids, item_type, schedule_code, supplier_id, per, include_batches=True))
    alerts_preview.extend(_preview_low_stock(db, loc_ids, item_type, schedule_code, supplier_id, per, include_batches=True))
    alerts_preview.extend(_preview_reorder(db, loc_ids, item_type, schedule_code, supplier_id, per, consumption_days, lead_time_days))
    alerts_preview.extend(_preview_near_expiry(db, loc_ids, item_type, schedule_code, supplier_id, int(days_near_expiry), per))
    alerts_preview.extend(_preview_expired(db, loc_ids, item_type, schedule_code, supplier_id, per))
    alerts_preview.extend(_preview_batch_risk(db, loc_ids, item_type, schedule_code, supplier_id, non_moving_days=max(non_moving_days, 30), limit=per))
    alerts_preview.extend(_preview_negative_stock(db, loc_ids, item_type, schedule_code, supplier_id, limit=max(per // 2, 1)))
    alerts_preview.extend(_preview_high_value_expiry(db, loc_ids, item_type, schedule_code, supplier_id, int(days_near_expiry), _money(high_value_expiry_threshold), limit=max(per // 2, 1)))
    alerts_preview.extend(_preview_fefo_risk(db, loc_ids, item_type, schedule_code, supplier_id, days_window=30, limit=max(per // 2, 1)))
    alerts_preview.extend(_preview_controlled_drugs(db, loc_ids, item_type, schedule_code, supplier_id, limit=max(per // 2, 1)))

    # ----------------------------
    # Movement Today + Week + Spikes
    # ----------------------------
    start_today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    start_week = start_today - timedelta(days=7)

    movement_today = _movement_bucket(db, loc_ids, start_today, item_type, schedule_code, supplier_id)
    movement_week = _movement_bucket(db, loc_ids, start_week, item_type, schedule_code, supplier_id)

    spikes = _compute_spikes(db, loc_ids, start_today, start_week, item_type, schedule_code, supplier_id)

    # ----------------------------
    # Pipeline
    # ----------------------------
    pipeline = _pipeline_summary(db, loc_ids)

    # ----------------------------
    # FEFO next-to-dispense (show for top fast-moving items)
    # ----------------------------
    fefo_next = _fefo_suggestions_for_items(
        db=db,
        loc_ids=loc_ids,
        item_ids=[int(x.item_id) for x in fast_items],
        item_type=item_type,
        schedule_code=schedule_code,
        supplier_id=supplier_id,
        per_item_batches=3,
    )

    # ----------------------------
    # FEFO compliance % (approx)
    # ----------------------------
    kpis.fefo_compliance_pct = _fefo_compliance_pct_approx(db, loc_ids, item_type, schedule_code, supplier_id, days_window=30)

    return StockAlertsDashboardOut(
        as_of=now,
        filters=filters,
        kpis=kpis,
        locations=loc_summaries,
        alerts_preview=alerts_preview[:preview_limit],
        movement_today=movement_today,
        movement_week=movement_week,
        spikes=spikes,
        pipeline=pipeline,
        fefo_next_to_dispense=fefo_next,
    )


# ----------------------------
# Batch-wise visibility (same medicine different batch+price)
# ----------------------------
def list_item_batches(db: Session, item_id: int, location_id: Optional[int] = None) -> List[ItemBatchRowOut]:
    q = (
        db.query(
            ItemBatch.id.label("batch_id"),
            ItemBatch.batch_no,
            ItemBatch.expiry_date,
            ItemBatch.current_qty,
            ItemBatch.unit_cost,
            ItemBatch.mrp,
            ItemBatch.tax_percent,
            ItemBatch.is_saleable,
            ItemBatch.status,
        )
        .filter(
            ItemBatch.item_id == item_id,
            ItemBatch.is_active.is_(True),
        )
    )
    if location_id:
        q = q.filter(ItemBatch.location_id == location_id)

    q = q.order_by(*_order_nulls_last(ItemBatch.expiry_date), ItemBatch.batch_no.asc())

    rows = q.all()
    return [
        ItemBatchRowOut(
            batch_id=int(r.batch_id),
            batch_no=str(r.batch_no),
            expiry_date=r.expiry_date,
            current_qty=_q(r.current_qty),
            unit_cost=_money(r.unit_cost),
            mrp=_money(r.mrp),
            tax_percent=_money(r.tax_percent),
            is_saleable=bool(r.is_saleable),
            status=str(r.status),
        )
        for r in rows
    ]


# ----------------------------
# Alerts list builder (supports all types)
# ----------------------------
def list_alerts(
    db: Session,
    alert_type: AlertType,
    location_id: Optional[int] = None,
    item_type: Optional[str] = None,
    schedule_code: Optional[str] = None,
    supplier_id: Optional[int] = None,
    days_near_expiry: int = 90,
    non_moving_days: int = 60,
    consumption_days: int = 30,
    lead_time_days: int = 7,
    high_value_expiry_threshold: Decimal = Decimal("0"),
    include_batches: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> List[StockAlertOut]:
    locs = _resolve_locations(db, location_id)
    loc_ids = [l.id for l in locs]
    if not loc_ids:
        return []

    take = int(limit + offset)

    if alert_type == AlertType.OUT_OF_STOCK:
        rows = _preview_out_of_stock(db, loc_ids, item_type, schedule_code, supplier_id, take, include_batches=include_batches)
        return rows[offset:offset + limit]

    if alert_type == AlertType.LOW_STOCK:
        rows = _preview_low_stock(db, loc_ids, item_type, schedule_code, supplier_id, take, include_batches=include_batches)
        return rows[offset:offset + limit]

    if alert_type == AlertType.REORDER:
        rows = _preview_reorder(db, loc_ids, item_type, schedule_code, supplier_id, take, consumption_days, lead_time_days)
        return rows[offset:offset + limit]

    if alert_type == AlertType.NEAR_EXPIRY:
        rows = _preview_near_expiry(db, loc_ids, item_type, schedule_code, supplier_id, days_near_expiry, take)
        return rows[offset:offset + limit]

    if alert_type == AlertType.EXPIRED:
        rows = _preview_expired(db, loc_ids, item_type, schedule_code, supplier_id, take)
        return rows[offset:offset + limit]

    if alert_type == AlertType.NON_MOVING:
        rows = _preview_non_moving(db, loc_ids, item_type, schedule_code, supplier_id, non_moving_days, take)
        return rows[offset:offset + limit]

    if alert_type == AlertType.BATCH_RISK:
        rows = _preview_batch_risk(db, loc_ids, item_type, schedule_code, supplier_id, non_moving_days=max(non_moving_days, 30), limit=take)
        return rows[offset:offset + limit]

    if alert_type == AlertType.NEGATIVE_STOCK:
        rows = _preview_negative_stock(db, loc_ids, item_type, schedule_code, supplier_id, limit=take)
        return rows[offset:offset + limit]

    if alert_type == AlertType.HIGH_VALUE_EXPIRY:
        rows = _preview_high_value_expiry(db, loc_ids, item_type, schedule_code, supplier_id, days_near_expiry, _money(high_value_expiry_threshold), limit=take)
        return rows[offset:offset + limit]

    if alert_type == AlertType.CONTROLLED_DRUG:
        rows = _preview_controlled_drugs(db, loc_ids, item_type, schedule_code, supplier_id, limit=take)
        return rows[offset:offset + limit]

    if alert_type == AlertType.FEFO_RISK:
        rows = _preview_fefo_risk(db, loc_ids, item_type, schedule_code, supplier_id, days_window=30, limit=take)
        return rows[offset:offset + limit]

    if alert_type == AlertType.OVER_STOCK:
        rows = _preview_over_stock(db, loc_ids, item_type, schedule_code, supplier_id, take)
        return rows[offset:offset + limit]

    return []


# ----------------------------
# Export flatteners
# ----------------------------
def flatten_alert_for_export(a: StockAlertOut) -> Dict[str, Any]:
    return {
        "Type": a.type.value,
        "Severity": a.severity.value,
        "Location": a.location_name or "",
        "Item Code": a.item_code or "",
        "Item Name": a.item_name or "",
        "On Hand Qty": str(a.on_hand_qty or ""),
        "Reorder Level": str(a.reorder_level or ""),
        "Max Level": str(a.max_level or ""),
        "Avg Daily Consumption": str(a.avg_daily_consumption or ""),
        "Lead Time Days": a.lead_time_days if a.lead_time_days is not None else "",
        "Reorder Point": str(a.reorder_point or ""),
        "Suggested Reorder Qty": str(a.suggested_reorder_qty or ""),
        "Days Of Stock Remaining": str(a.days_of_stock_remaining or ""),
        "Predicted Stockout Date": a.predicted_stockout_date.isoformat() if a.predicted_stockout_date else "",
        "Batch No": a.batch_no or "",
        "Expiry Date": a.expiry_date.isoformat() if a.expiry_date else "",
        "Days To Expiry": a.days_to_expiry if a.days_to_expiry is not None else "",
        "Unit Cost": str(a.unit_cost or ""),
        "MRP": str(a.mrp or ""),
        "Value Risk (Purchase)": str(a.value_risk_purchase or ""),
        "Value Risk (MRP)": str(a.value_risk_mrp or ""),
        "Message": a.message,
        "Suggested Action": a.suggested_action or "",
    }


# ----------------------------
# Reports (batch-wise mandatory)
# ----------------------------
def build_report_rows(
    db: Session,
    report_type: ReportType,
    location_id: Optional[int] = None,
    item_type: Optional[str] = None,
    schedule_code: Optional[str] = None,
    supplier_id: Optional[int] = None,
    days_near_expiry: int = 90,
    non_moving_days: int = 60,
    consumption_days: int = 30,
    lead_time_days: int = 7,
    limit: int = 20000,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    locs = _resolve_locations(db, location_id)
    loc_ids = [l.id for l in locs]
    if not loc_ids:
        return []

    today = dt_date.today()

    # LOW_STOCK / OUT_OF_STOCK: export batch-wise for the affected items
    if report_type in (ReportType.LOW_STOCK, ReportType.OUT_OF_STOCK):
        # select affected item+location first (fast)
        base = _stock_base_query(db, loc_ids, item_type, schedule_code, supplier_id)
        if report_type == ReportType.LOW_STOCK:
            base = base.filter(InventoryItem.reorder_level > 0, ItemLocationStock.on_hand_qty > 0, ItemLocationStock.on_hand_qty <= InventoryItem.reorder_level)
        else:
            base = base.filter(ItemLocationStock.on_hand_qty <= 0)

        affected = base.with_entities(
            ItemLocationStock.location_id.label("location_id"),
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code.label("item_code"),
            InventoryItem.name.label("item_name"),
            InventoryItem.reorder_level.label("reorder_level"),
            InventoryItem.max_level.label("max_level"),
            ItemLocationStock.on_hand_qty.label("on_hand_qty"),
        ).order_by(InventoryLocation.name.asc(), InventoryItem.name.asc()).offset(offset).limit(limit).all()

        if not affected:
            return []

        # now fetch batches for those items+locations
        key_pairs = {(int(r.location_id), int(r.item_id)) for r in affected}
        rows: List[Dict[str, Any]] = []
        for loc_id, item_id in key_pairs:
            batches = (
                db.query(
                    ItemBatch.batch_no,
                    ItemBatch.expiry_date,
                    ItemBatch.current_qty,
                    ItemBatch.unit_cost,
                    ItemBatch.mrp,
                    (ItemBatch.current_qty * ItemBatch.unit_cost).label("value_purchase"),
                    (ItemBatch.current_qty * ItemBatch.mrp).label("value_mrp"),
                    ItemBatch.status,
                    ItemBatch.is_saleable,
                )
                .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
                .filter(
                    ItemBatch.location_id == loc_id,
                    ItemBatch.item_id == item_id,
                    ItemBatch.is_active.is_(True),
                )
                .order_by(*_order_nulls_last(ItemBatch.expiry_date), ItemBatch.batch_no.asc())
                .all()
            )
            # header data
            hdr = next((x for x in affected if int(x.location_id) == loc_id and int(x.item_id) == item_id), None)
            for b in batches or [None]:
                rows.append(
                    {
                        "Report": report_type.value,
                        "Location": str(hdr.location_name) if hdr else "",
                        "Item Code": str(hdr.item_code) if hdr else "",
                        "Item Name": str(hdr.item_name) if hdr else "",
                        "On Hand Qty": str(_q(hdr.on_hand_qty) if hdr else ""),
                        "Reorder Level": str(_q(hdr.reorder_level) if hdr else ""),
                        "Max Level": str(_q(hdr.max_level) if hdr else ""),
                        "Batch No": "" if b is None else str(b.batch_no),
                        "Expiry Date": "" if (b is None or b.expiry_date is None) else b.expiry_date.isoformat(),
                        "Batch Qty": "" if b is None else str(_q(b.current_qty)),
                        "Unit Cost": "" if b is None else str(_money(b.unit_cost)),
                        "MRP": "" if b is None else str(_money(b.mrp)),
                        "Value (Purchase)": "" if b is None else str(_money(b.value_purchase)),
                        "Value (MRP)": "" if b is None else str(_money(b.value_mrp)),
                        "Saleable": "" if b is None else ("YES" if bool(b.is_saleable) else "NO"),
                        "Status": "" if b is None else str(b.status),
                    }
                )
        return rows

    # NEAR_EXPIRY / EXPIRED / VALUATION are inherently batch-wise
    if report_type in (ReportType.NEAR_EXPIRY, ReportType.EXPIRED, ReportType.VALUATION):
        base = _batch_valuation_query(db, loc_ids, item_type, schedule_code, supplier_id)

        if report_type == ReportType.NEAR_EXPIRY:
            dte = func.datediff(ItemBatch.expiry_date, today)
            base = base.filter(
                ItemBatch.expiry_date.isnot(None),
                dte >= 0,
                dte <= int(days_near_expiry),
                ItemBatch.is_saleable.is_(True),
            ).order_by(dte.asc(), InventoryItem.name.asc(), *_order_nulls_last(ItemBatch.expiry_date))
        elif report_type == ReportType.EXPIRED:
            base = base.filter(ItemBatch.expiry_date.isnot(None), ItemBatch.expiry_date < today).order_by(ItemBatch.expiry_date.asc(), InventoryItem.name.asc())
        else:
            base = base.order_by(InventoryLocation.name.asc(), InventoryItem.name.asc(), *_order_nulls_last(ItemBatch.expiry_date), ItemBatch.batch_no.asc())

        base = base.offset(offset).limit(limit)
        rows = base.all()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "Report": report_type.value,
                    "Location": str(r.location_name),
                    "Item Code": str(r.item_code),
                    "Item Name": str(r.item_name),
                    "Batch No": str(r.batch_no),
                    "Expiry Date": "" if r.expiry_date is None else r.expiry_date.isoformat(),
                    "Qty": str(_q(r.qty)),
                    "Unit Cost": str(_money(r.unit_cost)),
                    "MRP": str(_money(r.mrp)),
                    "Value (Purchase)": str(_money(r.value_purchase)),
                    "Value (MRP)": str(_money(r.value_mrp)),
                    "Status": str(r.status),
                    "Saleable": "YES" if bool(r.is_saleable) else "NO",
                }
            )
        return out

    # NON_MOVING: item-level + include batches for that item+location (batch-wise requirement)
    if report_type == ReportType.NON_MOVING:
        last_txn_sq = (
            db.query(
                StockTransaction.location_id.label("location_id"),
                StockTransaction.item_id.label("item_id"),
                func.max(StockTransaction.txn_time).label("last_txn_time"),
            )
            .filter(StockTransaction.location_id.in_(loc_ids))
            .group_by(StockTransaction.location_id, StockTransaction.item_id)
            .subquery()
        )
        days_since_last = func.datediff(today, func.date(last_txn_sq.c.last_txn_time))

        base = (
            db.query(
                ItemLocationStock.location_id,
                InventoryLocation.name.label("location_name"),
                InventoryItem.id.label("item_id"),
                InventoryItem.code.label("item_code"),
                InventoryItem.name.label("item_name"),
                ItemLocationStock.on_hand_qty,
                last_txn_sq.c.last_txn_time,
                days_since_last.label("days_since_last"),
            )
            .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
            .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
            .outerjoin(
                last_txn_sq,
                and_(
                    last_txn_sq.c.location_id == ItemLocationStock.location_id,
                    last_txn_sq.c.item_id == ItemLocationStock.item_id,
                ),
            )
            .filter(
                ItemLocationStock.location_id.in_(loc_ids),
                InventoryItem.is_active.is_(True),
                ItemLocationStock.on_hand_qty > 0,
                or_(last_txn_sq.c.last_txn_time.is_(None), days_since_last >= int(non_moving_days)),
            )
        )
        base = _apply_item_filters(base, item_type, schedule_code, supplier_id)
        base = base.order_by(*_order_nulls_first(last_txn_sq.c.last_txn_time), InventoryItem.name.asc()).offset(offset).limit(limit)
        items = base.all()

        out: List[Dict[str, Any]] = []
        for it in items:
            batches = (
                db.query(
                    ItemBatch.batch_no,
                    ItemBatch.expiry_date,
                    ItemBatch.current_qty,
                    ItemBatch.unit_cost,
                    ItemBatch.mrp,
                    (ItemBatch.current_qty * ItemBatch.unit_cost).label("value_purchase"),
                    (ItemBatch.current_qty * ItemBatch.mrp).label("value_mrp"),
                    ItemBatch.status,
                )
                .filter(
                    ItemBatch.location_id == int(it.location_id),
                    ItemBatch.item_id == int(it.item_id),
                    ItemBatch.is_active.is_(True),
                    ItemBatch.current_qty > 0,
                )
                .order_by(*_order_nulls_last(ItemBatch.expiry_date), ItemBatch.batch_no.asc())
                .all()
            )
            if not batches:
                out.append(
                    {
                        "Report": report_type.value,
                        "Location": str(it.location_name),
                        "Item Code": str(it.item_code),
                        "Item Name": str(it.item_name),
                        "On Hand Qty": str(_q(it.on_hand_qty)),
                        "Last Txn": "" if it.last_txn_time is None else str(it.last_txn_time),
                        "Days Since Last": int(it.days_since_last or 9999),
                        "Batch No": "",
                        "Expiry Date": "",
                        "Batch Qty": "",
                        "Unit Cost": "",
                        "MRP": "",
                        "Value (Purchase)": "",
                        "Value (MRP)": "",
                        "Status": "",
                    }
                )
                continue

            for b in batches:
                out.append(
                    {
                        "Report": report_type.value,
                        "Location": str(it.location_name),
                        "Item Code": str(it.item_code),
                        "Item Name": str(it.item_name),
                        "On Hand Qty": str(_q(it.on_hand_qty)),
                        "Last Txn": "" if it.last_txn_time is None else str(it.last_txn_time),
                        "Days Since Last": int(it.days_since_last or 9999),
                        "Batch No": str(b.batch_no),
                        "Expiry Date": "" if b.expiry_date is None else b.expiry_date.isoformat(),
                        "Batch Qty": str(_q(b.current_qty)),
                        "Unit Cost": str(_money(b.unit_cost)),
                        "MRP": str(_money(b.mrp)),
                        "Value (Purchase)": str(_money(b.value_purchase)),
                        "Value (MRP)": str(_money(b.value_mrp)),
                        "Status": str(b.status),
                    }
                )
        return out

    return []


# ============================================================
# PREVIEW / ALERT QUERIES (all MySQL-safe)
# ============================================================
def _fetch_item_batches_small(db: Session, item_id: int, location_id: int, per_item_batches: int = 3) -> List[ItemBatchRowOut]:
    q = (
        db.query(
            ItemBatch.id.label("batch_id"),
            ItemBatch.batch_no,
            ItemBatch.expiry_date,
            ItemBatch.current_qty,
            ItemBatch.unit_cost,
            ItemBatch.mrp,
            ItemBatch.tax_percent,
            ItemBatch.is_saleable,
            ItemBatch.status,
        )
        .filter(
            ItemBatch.item_id == item_id,
            ItemBatch.location_id == location_id,
            ItemBatch.is_active.is_(True),
            ItemBatch.current_qty > 0,
        )
        .order_by(*_order_nulls_last(ItemBatch.expiry_date), ItemBatch.batch_no.asc())
        .limit(max(int(per_item_batches), 1))
    )
    return [
        ItemBatchRowOut(
            batch_id=int(r.batch_id),
            batch_no=str(r.batch_no),
            expiry_date=r.expiry_date,
            current_qty=_q(r.current_qty),
            unit_cost=_money(r.unit_cost),
            mrp=_money(r.mrp),
            tax_percent=_money(r.tax_percent),
            is_saleable=bool(r.is_saleable),
            status=str(r.status),
        )
        for r in q.all()
    ]


def _preview_out_of_stock(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], limit: int, include_batches: bool = False):
    q = (
        db.query(
            ItemLocationStock.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemLocationStock.on_hand_qty,
            InventoryItem.reorder_level,
            InventoryItem.max_level,
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            ItemLocationStock.on_hand_qty <= 0,
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(InventoryLocation.name.asc(), InventoryItem.name.asc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        batches = _fetch_item_batches_small(db, int(r.item_id), int(r.location_id), 3) if include_batches else None
        out.append(
            StockAlertOut(
                type=AlertType.OUT_OF_STOCK,
                severity=AlertSeverity.CRIT,
                message="Out of stock",
                suggested_action="Create reorder / draft PO or transfer from another store",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                on_hand_qty=_q(r.on_hand_qty),
                reorder_level=_q(r.reorder_level),
                max_level=_q(r.max_level),
                batch_rows=batches,
            )
        )
    return out


def _preview_low_stock(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], limit: int, include_batches: bool = False):
    gap = (InventoryItem.reorder_level - ItemLocationStock.on_hand_qty)

    q = (
        db.query(
            ItemLocationStock.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemLocationStock.on_hand_qty,
            InventoryItem.reorder_level,
            InventoryItem.max_level,
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            InventoryItem.reorder_level > 0,
            ItemLocationStock.on_hand_qty > 0,
            ItemLocationStock.on_hand_qty <= InventoryItem.reorder_level,
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(gap.desc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        batches = _fetch_item_batches_small(db, int(r.item_id), int(r.location_id), 3) if include_batches else None
        out.append(
            StockAlertOut(
                type=AlertType.LOW_STOCK,
                severity=AlertSeverity.WARN,
                message="Low stock (below reorder level)",
                suggested_action="Create reorder / PO draft",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                on_hand_qty=_q(r.on_hand_qty),
                reorder_level=_q(r.reorder_level),
                max_level=_q(r.max_level),
                batch_rows=batches,
            )
        )
    return out


def _preview_over_stock(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], limit: int):
    q = (
        db.query(
            ItemLocationStock.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemLocationStock.on_hand_qty,
            InventoryItem.reorder_level,
            InventoryItem.max_level,
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            InventoryItem.max_level > 0,
            ItemLocationStock.on_hand_qty > InventoryItem.max_level,
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by((ItemLocationStock.on_hand_qty - InventoryItem.max_level).desc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        out.append(
            StockAlertOut(
                type=AlertType.OVER_STOCK,
                severity=AlertSeverity.INFO,
                message="Over stock (above max level)",
                suggested_action="Transfer / reduce reorder / review max level",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                on_hand_qty=_q(r.on_hand_qty),
                reorder_level=_q(r.reorder_level),
                max_level=_q(r.max_level),
            )
        )
    return out


def _preview_near_expiry(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], days_near_expiry: int, limit: int):
    today = dt_date.today()
    dte = func.datediff(ItemBatch.expiry_date, today)

    q = (
        db.query(
            ItemBatch.id.label("batch_id"),
            ItemBatch.batch_no,
            ItemBatch.expiry_date,
            ItemBatch.current_qty,
            ItemBatch.unit_cost,
            ItemBatch.mrp,
            ItemBatch.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            dte.label("days_to_expiry"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemBatch.location_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            dte >= 0,
            dte <= int(days_near_expiry),
            InventoryItem.is_active.is_(True),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(dte.asc(), InventoryItem.name.asc(), ItemBatch.batch_no.asc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        days = int(r.days_to_expiry or 0)
        sev = AlertSeverity.CRIT if days <= 7 else (AlertSeverity.WARN if days <= 30 else AlertSeverity.INFO)
        qty = _q(r.current_qty)
        uc = _money(r.unit_cost)
        mrp = _money(r.mrp)

        out.append(
            StockAlertOut(
                type=AlertType.NEAR_EXPIRY,
                severity=sev,
                message=f"Near expiry in {days} day(s)",
                suggested_action="Transfer / return to supplier / FEFO plan",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                batch_id=int(r.batch_id),
                batch_no=str(r.batch_no),
                expiry_date=r.expiry_date,
                days_to_expiry=days,
                on_hand_qty=qty,
                unit_cost=uc,
                mrp=mrp,
                value_risk_purchase=qty * uc,
                value_risk_mrp=qty * mrp,
            )
        )
    return out


def _preview_expired(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], limit: int):
    today = dt_date.today()

    q = (
        db.query(
            ItemBatch.id.label("batch_id"),
            ItemBatch.batch_no,
            ItemBatch.expiry_date,
            ItemBatch.current_qty,
            ItemBatch.unit_cost,
            ItemBatch.mrp,
            ItemBatch.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemBatch.location_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            ItemBatch.expiry_date < today,
            InventoryItem.is_active.is_(True),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(ItemBatch.expiry_date.asc(), InventoryItem.name.asc(), ItemBatch.batch_no.asc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        qty = _q(r.current_qty)
        uc = _money(r.unit_cost)
        mrp = _money(r.mrp)

        out.append(
            StockAlertOut(
                type=AlertType.EXPIRED,
                severity=AlertSeverity.CRIT,
                message="Expired batch in stock",
                suggested_action="Quarantine + write-off / return (if allowed)",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                batch_id=int(r.batch_id),
                batch_no=str(r.batch_no),
                expiry_date=r.expiry_date,
                on_hand_qty=qty,
                unit_cost=uc,
                mrp=mrp,
                value_risk_purchase=qty * uc,
                value_risk_mrp=qty * mrp,
            )
        )
    return out


def _preview_non_moving(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], non_moving_days: int, limit: int):
    today = dt_date.today()

    last_txn_sq = (
        db.query(
            StockTransaction.location_id.label("location_id"),
            StockTransaction.item_id.label("item_id"),
            func.max(StockTransaction.txn_time).label("last_txn_time"),
        )
        .filter(StockTransaction.location_id.in_(loc_ids))
        .group_by(StockTransaction.location_id, StockTransaction.item_id)
        .subquery()
    )

    days_since_last = func.datediff(today, func.date(last_txn_sq.c.last_txn_time))

    q = (
        db.query(
            ItemLocationStock.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemLocationStock.on_hand_qty,
            last_txn_sq.c.last_txn_time,
            days_since_last.label("days_since_last"),
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .outerjoin(
            last_txn_sq,
            and_(
                last_txn_sq.c.location_id == ItemLocationStock.location_id,
                last_txn_sq.c.item_id == ItemLocationStock.item_id,
            ),
        )
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            ItemLocationStock.on_hand_qty > 0,
            or_(last_txn_sq.c.last_txn_time.is_(None), days_since_last >= int(non_moving_days)),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(*_order_nulls_first(last_txn_sq.c.last_txn_time), InventoryItem.name.asc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        d = int(r.days_since_last or 9999) if r.last_txn_time else 9999
        out.append(
            StockAlertOut(
                type=AlertType.NON_MOVING,
                severity=AlertSeverity.WARN,
                message=f"Non-moving stock (no transaction for {d} day(s))",
                suggested_action="Review usage / transfer / reduce reorder / clear dead stock",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                on_hand_qty=_q(r.on_hand_qty),
            )
        )
    return out


# ----------------------------
# REORDER (consumption × lead time) + predictive
# ----------------------------
def _avg_daily_consumption_sq(db: Session, loc_ids: List[int], since_dt: datetime):
    # only OUT transactions (dispense/sale/issue)
    return (
        db.query(
            StockTransaction.location_id.label("location_id"),
            StockTransaction.item_id.label("item_id"),
            func.coalesce(func.sum(-StockTransaction.quantity_change), 0).label("out_qty"),
        )
        .filter(
            StockTransaction.location_id.in_(loc_ids),
            StockTransaction.txn_time >= since_dt,
            StockTransaction.quantity_change < 0,
            StockTransaction.txn_type.in_(["DISPENSE", "SALE", "ISSUE"]),
        )
        .group_by(StockTransaction.location_id, StockTransaction.item_id)
        .subquery()
    )


def _preview_reorder(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], limit: int, consumption_days: int, lead_time_days: int):
    today = dt_date.today()
    since_dt = datetime.utcnow() - timedelta(days=int(consumption_days))

    cons_sq = _avg_daily_consumption_sq(db, loc_ids, since_dt)

    q = (
        db.query(
            ItemLocationStock.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemLocationStock.on_hand_qty,
            InventoryItem.reorder_level,
            InventoryItem.max_level,
            func.coalesce(cons_sq.c.out_qty, 0).label("out_qty"),
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .outerjoin(
            cons_sq,
            and_(cons_sq.c.location_id == ItemLocationStock.location_id, cons_sq.c.item_id == ItemLocationStock.item_id),
        )
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)

    # We compute reorder_point in Python (safe + flexible)
    # Sort by (low days of stock) first.
    rows = q.all()

    enriched: List[Tuple[Decimal, StockAlertOut]] = []
    for r in rows:
        on_hand = _q(r.on_hand_qty)
        reorder_level = _q(r.reorder_level)
        max_level = _q(r.max_level)
        out_qty = _q(r.out_qty)

        avg_daily = (out_qty / Decimal(str(consumption_days))) if consumption_days > 0 else ZERO
        consumption_reorder_point = (avg_daily * Decimal(str(lead_time_days))) if avg_daily > 0 else ZERO
        reorder_point = reorder_level if reorder_level > 0 else consumption_reorder_point
        reorder_point = reorder_point if reorder_point > 0 else consumption_reorder_point

        if reorder_point <= 0:
            continue

        if on_hand > reorder_point:
            continue

        # days remaining
        days_left = (on_hand / avg_daily) if avg_daily > 0 else None
        pred_date = None
        if days_left is not None:
            # ceil to days
            days_int = int(_ceil_dec(days_left))
            pred_date = today + timedelta(days=days_int)

        # suggested reorder qty: try to refill up to max_level, else 2x reorder_point
        target = max_level if max_level > 0 else (reorder_point * Decimal("2"))
        suggested_qty = (target - on_hand) if target > on_hand else ZERO

        sev = AlertSeverity.CRIT if (days_left is not None and days_left <= Decimal("3")) else AlertSeverity.WARN

        enriched.append(
            (
                days_left if (days_left is not None) else Decimal("999999"),
                StockAlertOut(
                    type=AlertType.REORDER,
                    severity=sev,
                    message="Reorder suggested (based on consumption × lead time / reorder level)",
                    suggested_action="Create reorder basket / draft PO",
                    location_id=int(r.location_id),
                    location_name=str(r.location_name),
                    supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                    item_id=int(r.item_id),
                    item_code=str(r.code),
                    item_name=str(r.name),
                    on_hand_qty=on_hand,
                    reorder_level=reorder_level,
                    max_level=max_level,
                    avg_daily_consumption=avg_daily,
                    lead_time_days=int(lead_time_days),
                    reorder_point=reorder_point,
                    suggested_reorder_qty=suggested_qty,
                    days_of_stock_remaining=days_left.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if days_left is not None else None,
                    predicted_stockout_date=pred_date,
                    batch_rows=_fetch_item_batches_small(db, int(r.item_id), int(r.location_id), 3),
                ),
            )
        )

    enriched.sort(key=lambda x: x[0])
    return [a for _, a in enriched[: max(int(limit), 1)]]


# ----------------------------
# BATCH_RISK (batch exists but no sale/issue since X days)
# ----------------------------
def _preview_batch_risk(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], non_moving_days: int, limit: int):
    today = dt_date.today()

    last_batch_txn_sq = (
        db.query(
            StockTransaction.batch_id.label("batch_id"),
            func.max(StockTransaction.txn_time).label("last_txn_time"),
        )
        .filter(
            StockTransaction.location_id.in_(loc_ids),
            StockTransaction.txn_type.in_(["DISPENSE", "SALE", "ISSUE"]),
            StockTransaction.batch_id.isnot(None),
        )
        .group_by(StockTransaction.batch_id)
        .subquery()
    )

    days_since = func.datediff(today, func.date(last_batch_txn_sq.c.last_txn_time))

    q = (
        db.query(
            ItemBatch.id.label("batch_id"),
            ItemBatch.batch_no,
            ItemBatch.expiry_date,
            ItemBatch.current_qty,
            ItemBatch.unit_cost,
            ItemBatch.mrp,
            ItemBatch.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            last_batch_txn_sq.c.last_txn_time,
            days_since.label("days_since_last"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemBatch.location_id)
        .outerjoin(last_batch_txn_sq, last_batch_txn_sq.c.batch_id == ItemBatch.id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.current_qty > 0,
            ItemBatch.is_saleable.is_(True),
            InventoryItem.is_active.is_(True),
            or_(last_batch_txn_sq.c.last_txn_time.is_(None), days_since >= int(non_moving_days)),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(*_order_nulls_first(last_batch_txn_sq.c.last_txn_time), InventoryItem.name.asc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        d = int(r.days_since_last or 9999) if r.last_txn_time else 9999
        qty = _q(r.current_qty)
        uc = _money(r.unit_cost)
        mrp = _money(r.mrp)

        out.append(
            StockAlertOut(
                type=AlertType.BATCH_RISK,
                severity=AlertSeverity.WARN,
                message=f"Batch risk: no sale/issue for {d} day(s)",
                suggested_action="Review demand / transfer / adjust procurement",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                batch_id=int(r.batch_id),
                batch_no=str(r.batch_no),
                expiry_date=r.expiry_date,
                on_hand_qty=qty,
                unit_cost=uc,
                mrp=mrp,
                value_risk_purchase=qty * uc,
                value_risk_mrp=qty * mrp,
            )
        )
    return out


# ----------------------------
# NEGATIVE / mismatch stock
# ----------------------------
def _preview_negative_stock(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], limit: int):
    # Negative batches
    q1 = (
        db.query(
            ItemBatch.id.label("batch_id"),
            ItemBatch.batch_no,
            ItemBatch.expiry_date,
            ItemBatch.current_qty,
            ItemBatch.unit_cost,
            ItemBatch.mrp,
            ItemBatch.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemBatch.location_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.current_qty < 0,
            InventoryItem.is_active.is_(True),
        )
    )
    q1 = _apply_item_filters(q1, item_type, schedule_code, supplier_id)

    # Negative item-location stock
    q2 = (
        db.query(
            ItemLocationStock.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemLocationStock.on_hand_qty,
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            ItemLocationStock.on_hand_qty < 0,
        )
    )
    q2 = _apply_item_filters(q2, item_type, schedule_code, supplier_id)

    out: List[StockAlertOut] = []

    for r in q1.order_by(InventoryLocation.name.asc(), InventoryItem.name.asc()).limit(max(int(limit), 1)).all():
        qty = _q(r.current_qty)
        out.append(
            StockAlertOut(
                type=AlertType.NEGATIVE_STOCK,
                severity=AlertSeverity.CRIT,
                message="Negative batch stock (data mismatch)",
                suggested_action="Audit + correct adjustment entry / investigate transactions",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                batch_id=int(r.batch_id),
                batch_no=str(r.batch_no),
                expiry_date=r.expiry_date,
                on_hand_qty=qty,
                unit_cost=_money(r.unit_cost),
                mrp=_money(r.mrp),
            )
        )

    remaining = max(int(limit) - len(out), 0)
    if remaining > 0:
        for r in q2.order_by(InventoryLocation.name.asc(), InventoryItem.name.asc()).limit(remaining).all():
            out.append(
                StockAlertOut(
                    type=AlertType.NEGATIVE_STOCK,
                    severity=AlertSeverity.CRIT,
                    message="Negative on-hand stock (data mismatch)",
                    suggested_action="Audit + correct adjustment entry / investigate transactions",
                    location_id=int(r.location_id),
                    location_name=str(r.location_name),
                    supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                    item_id=int(r.item_id),
                    item_code=str(r.code),
                    item_name=str(r.name),
                    on_hand_qty=_q(r.on_hand_qty),
                    batch_rows=_fetch_item_batches_small(db, int(r.item_id), int(r.location_id), 3),
                )
            )

    # Also include mismatch between sum(batch_qty) vs on_hand_qty (small sample)
    if remaining > 0:
        mism = _find_stock_mismatch(db, loc_ids, item_type, schedule_code, supplier_id, limit=max(int(limit) // 2, 1))
        out.extend(mism)

    return out[:max(int(limit), 1)]


def _find_stock_mismatch(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], limit: int):
    # sum(batch_qty) per item+location
    batch_sum_sq = (
        db.query(
            ItemBatch.location_id.label("location_id"),
            ItemBatch.item_id.label("item_id"),
            func.coalesce(func.sum(ItemBatch.current_qty), 0).label("batch_qty_sum"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            InventoryItem.is_active.is_(True),
        )
    )
    batch_sum_sq = _apply_item_filters(batch_sum_sq, item_type, schedule_code, supplier_id)
    batch_sum_sq = batch_sum_sq.group_by(ItemBatch.location_id, ItemBatch.item_id).subquery()

    q = (
        db.query(
            ItemLocationStock.location_id,
            InventoryLocation.name.label("location_name"),
            ItemLocationStock.item_id,
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemLocationStock.on_hand_qty,
            batch_sum_sq.c.batch_qty_sum,
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .join(batch_sum_sq, and_(batch_sum_sq.c.location_id == ItemLocationStock.location_id, batch_sum_sq.c.item_id == ItemLocationStock.item_id))
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            func.abs(ItemLocationStock.on_hand_qty - batch_sum_sq.c.batch_qty_sum) > QTY_EPS,
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(func.abs(ItemLocationStock.on_hand_qty - batch_sum_sq.c.batch_qty_sum).desc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        out.append(
            StockAlertOut(
                type=AlertType.NEGATIVE_STOCK,
                severity=AlertSeverity.CRIT,
                message=f"Stock mismatch: on-hand {_q(r.on_hand_qty)} vs batch-sum {_q(r.batch_qty_sum)}",
                suggested_action="Recompute stock / audit transactions / fix batch mapping",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                on_hand_qty=_q(r.on_hand_qty),
                batch_rows=_fetch_item_batches_small(db, int(r.item_id), int(r.location_id), 5),
            )
        )
    return out


# ----------------------------
# High-value expiry risk
# ----------------------------
def _preview_high_value_expiry(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], days_near_expiry: int, threshold: Decimal, limit: int):
    if threshold is None or _money(threshold) <= 0:
        return []

    today = dt_date.today()
    dte = func.datediff(ItemBatch.expiry_date, today)
    val_purchase = (ItemBatch.current_qty * ItemBatch.unit_cost)
    val_mrp = (ItemBatch.current_qty * ItemBatch.mrp)

    q = (
        db.query(
            ItemBatch.id.label("batch_id"),
            ItemBatch.batch_no,
            ItemBatch.expiry_date,
            ItemBatch.current_qty,
            ItemBatch.unit_cost,
            ItemBatch.mrp,
            ItemBatch.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            dte.label("days_to_expiry"),
            val_purchase.label("value_purchase"),
            val_mrp.label("value_mrp"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemBatch.location_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            dte >= 0,
            dte <= int(days_near_expiry),
            or_(val_purchase >= threshold, val_mrp >= threshold),
            InventoryItem.is_active.is_(True),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(func.greatest(val_purchase, val_mrp).desc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        qty = _q(r.current_qty)
        uc = _money(r.unit_cost)
        mrp = _money(r.mrp)
        out.append(
            StockAlertOut(
                type=AlertType.HIGH_VALUE_EXPIRY,
                severity=AlertSeverity.WARN,
                message="High-value near-expiry stock",
                suggested_action="Transfer / return to supplier / prioritize dispensing (FEFO)",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                batch_id=int(r.batch_id),
                batch_no=str(r.batch_no),
                expiry_date=r.expiry_date,
                days_to_expiry=int(r.days_to_expiry or 0),
                on_hand_qty=qty,
                unit_cost=uc,
                mrp=mrp,
                value_risk_purchase=qty * uc,
                value_risk_mrp=qty * mrp,
            )
        )
    return out


# ----------------------------
# Controlled / high-risk drug alerts
# ----------------------------
def _preview_controlled_drugs(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], limit: int):
    # controlled definition (simple, adjustable)
    controlled_cond = or_(
        InventoryItem.schedule_code.in_(["H", "H1", "X"]),
        InventoryItem.lasa_flag.is_(True),
        InventoryItem.prescription_status == "SCHEDULED",
    )

    q = (
        db.query(
            ItemLocationStock.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            ItemLocationStock.on_hand_qty,
            InventoryItem.reorder_level,
            InventoryItem.max_level,
            InventoryItem.schedule_code.label("schedule_code"),
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            controlled_cond,
            ItemLocationStock.on_hand_qty > 0,
        )
    )
    # keep user filters, but note schedule_code filter may hide controlled drugs intentionally
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(InventoryLocation.name.asc(), InventoryItem.name.asc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        out.append(
            StockAlertOut(
                type=AlertType.CONTROLLED_DRUG,
                severity=AlertSeverity.INFO,
                message=f"Controlled / high-risk item (Schedule {str(r.schedule_code or '').strip() or '—'})",
                suggested_action="Ensure restricted dispensing + strict stock audit",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                on_hand_qty=_q(r.on_hand_qty),
                reorder_level=_q(r.reorder_level),
                max_level=_q(r.max_level),
                batch_rows=_fetch_item_batches_small(db, int(r.item_id), int(r.location_id), 3),
            )
        )
    return out


# ----------------------------
# FEFO risk (dispensed from later expiry while earlier exists)
# ----------------------------
def _preview_fefo_risk(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], days_window: int, limit: int):
    since_dt = datetime.utcnow() - timedelta(days=int(days_window))

    earliest_sq = (
        db.query(
            ItemBatch.location_id.label("location_id"),
            ItemBatch.item_id.label("item_id"),
            func.min(ItemBatch.expiry_date).label("earliest_expiry"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            InventoryItem.is_active.is_(True),
        )
        .group_by(ItemBatch.location_id, ItemBatch.item_id)
        .subquery()
    )

    used_sq = (
        db.query(
            StockTransaction.location_id.label("location_id"),
            StockTransaction.item_id.label("item_id"),
            func.max(ItemBatch.expiry_date).label("used_expiry"),
        )
        .join(ItemBatch, ItemBatch.id == StockTransaction.batch_id)
        .join(InventoryItem, InventoryItem.id == StockTransaction.item_id)
        .filter(
            StockTransaction.location_id.in_(loc_ids),
            StockTransaction.txn_time >= since_dt,
            StockTransaction.txn_type.in_(["DISPENSE", "SALE", "ISSUE"]),
            ItemBatch.expiry_date.isnot(None),
            InventoryItem.is_active.is_(True),
        )
        .group_by(StockTransaction.location_id, StockTransaction.item_id)
        .subquery()
    )

    q = (
        db.query(
            earliest_sq.c.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.default_supplier_id.label("supplier_id"),
            earliest_sq.c.earliest_expiry,
            used_sq.c.used_expiry,
        )
        .join(InventoryItem, InventoryItem.id == earliest_sq.c.item_id)
        .join(InventoryLocation, InventoryLocation.id == earliest_sq.c.location_id)
        .join(
            used_sq,
            and_(used_sq.c.location_id == earliest_sq.c.location_id, used_sq.c.item_id == earliest_sq.c.item_id),
        )
        .filter(used_sq.c.used_expiry > earliest_sq.c.earliest_expiry)
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    q = q.order_by(InventoryLocation.name.asc(), InventoryItem.name.asc()).limit(max(int(limit), 1))

    out: List[StockAlertOut] = []
    for r in q.all():
        out.append(
            StockAlertOut(
                type=AlertType.FEFO_RISK,
                severity=AlertSeverity.WARN,
                message="FEFO risk: dispensed from later-expiry batch while earlier-expiry batch exists",
                suggested_action="Dispense earliest expiry batch first (FEFO)",
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                supplier_id=int(r.supplier_id) if r.supplier_id is not None else None,
                item_id=int(r.item_id),
                item_code=str(r.code),
                item_name=str(r.name),
                batch_rows=_fetch_item_batches_small(db, int(r.item_id), int(r.location_id), 5),
            )
        )
    return out


# ============================================================
# Movement helpers (today/week) + spike detection
# ============================================================
def _movement_bucket(db: Session, loc_ids: List[int], start_dt: datetime, item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int]) -> List[MovementBucketOut]:
    q = (
        db.query(
            StockTransaction.txn_type.label("txn_type"),
            func.coalesce(func.sum(StockTransaction.quantity_change), 0).label("qty_sum"),
        )
        .join(InventoryItem, InventoryItem.id == StockTransaction.item_id)
        .filter(
            StockTransaction.location_id.in_(loc_ids),
            StockTransaction.txn_time >= start_dt,
            InventoryItem.is_active.is_(True),
        )
        .group_by(StockTransaction.txn_type)
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    rows = q.all()

    purchase_in = ZERO
    dispense_out = ZERO
    returns_in = ZERO
    returns_out = ZERO
    adjustment = ZERO

    for r in rows:
        t = (r.txn_type or "").upper()
        qty = _q(r.qty_sum)

        if t == "GRN":
            purchase_in += qty if qty > 0 else ZERO
        elif t in ("DISPENSE", "SALE", "ISSUE"):
            dispense_out += (-qty) if qty < 0 else ZERO
        elif "RETURN" in t:
            if qty > 0:
                returns_in += qty
            elif qty < 0:
                returns_out += (-qty)
        elif "ADJUST" in t:
            adjustment += qty

    return [
        MovementBucketOut(key="PURCHASE_IN", qty=purchase_in),
        MovementBucketOut(key="DISPENSE_OUT", qty=dispense_out),
        MovementBucketOut(key="RETURNS_IN", qty=returns_in),
        MovementBucketOut(key="RETURNS_OUT", qty=returns_out),
        MovementBucketOut(key="ADJUSTMENT", qty=adjustment),
    ]


def _compute_spikes(db: Session, loc_ids: List[int], start_today: datetime, start_week: datetime, item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int]) -> List[SpikeOut]:
    # Compare today's dispense_out vs avg daily dispense_out over last 7 days.
    today_out = _daily_outflow(db, loc_ids, start_today, start_today + timedelta(days=1), item_type, schedule_code, supplier_id)
    week_out = _daily_outflow(db, loc_ids, start_week, start_today, item_type, schedule_code, supplier_id)

    avg = (week_out / Decimal("7")) if week_out > 0 else ZERO
    if avg <= 0:
        return []

    ratio = (today_out / avg) if avg > 0 else ZERO
    if ratio < Decimal("2"):
        return []  # only flag strong spikes (>=2x)

    return [
        SpikeOut(
            metric="DISPENSE_OUT",
            today=today_out,
            avg_last_7_days=avg.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            ratio=ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
    ]


def _daily_outflow(db: Session, loc_ids: List[int], start_dt: datetime, end_dt: datetime, item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int]) -> Decimal:
    q = (
        db.query(func.coalesce(func.sum(-StockTransaction.quantity_change), 0))
        .join(InventoryItem, InventoryItem.id == StockTransaction.item_id)
        .filter(
            StockTransaction.location_id.in_(loc_ids),
            StockTransaction.txn_time >= start_dt,
            StockTransaction.txn_time < end_dt,
            StockTransaction.quantity_change < 0,
            StockTransaction.txn_type.in_(["DISPENSE", "SALE", "ISSUE"]),
            InventoryItem.is_active.is_(True),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    return _q(q.scalar())


# ============================================================
# Pipeline
# ============================================================
def _pipeline_summary(db: Session, loc_ids: List[int]) -> ProcurementPipelineOut:
    today = dt_date.today()

    po_counts = dict(
        db.query(PurchaseOrder.status, func.count())
        .filter(PurchaseOrder.location_id.in_(loc_ids))
        .group_by(PurchaseOrder.status)
        .all()
    )

    overdue_po = (
        db.query(func.count())
        .filter(
            PurchaseOrder.location_id.in_(loc_ids),
            PurchaseOrder.expected_date.isnot(None),
            PurchaseOrder.expected_date < today,
            PurchaseOrder.status.in_([POStatus.APPROVED, POStatus.SENT, POStatus.PARTIALLY_RECEIVED]),
        )
        .scalar()
        or 0
    )

    grn_counts = dict(
        db.query(GRN.status, func.count())
        .filter(GRN.location_id.in_(loc_ids))
        .group_by(GRN.status)
        .all()
    )

    grn_posted_today = (
        db.query(func.count())
        .filter(
            GRN.location_id.in_(loc_ids),
            GRN.status == GRNStatus.POSTED,
            GRN.posted_at.isnot(None),
            func.date(GRN.posted_at) == today,
        )
        .scalar()
        or 0
    )

    return ProcurementPipelineOut(
        po_draft=int(po_counts.get(POStatus.DRAFT, 0) or 0),
        po_approved=int(po_counts.get(POStatus.APPROVED, 0) or 0),
        po_sent=int(po_counts.get(POStatus.SENT, 0) or 0),
        po_partially_received=int(po_counts.get(POStatus.PARTIALLY_RECEIVED, 0) or 0),
        po_completed=int(po_counts.get(POStatus.COMPLETED, 0) or 0),
        po_overdue=int(overdue_po),
        grn_draft=int(grn_counts.get(GRNStatus.DRAFT, 0) or 0),
        grn_posted=int(grn_counts.get(GRNStatus.POSTED, 0) or 0),
        grn_cancelled=int(grn_counts.get(GRNStatus.CANCELLED, 0) or 0),
        grn_posted_today=int(grn_posted_today),
        qc_pending=0,
        stock_not_updated=0,
    )


# ============================================================
# FEFO suggestions
# ============================================================
def _fefo_suggestions_for_items(
    db: Session,
    loc_ids: List[int],
    item_ids: List[int],
    item_type: Optional[str],
    schedule_code: Optional[str],
    supplier_id: Optional[int],
    per_item_batches: int = 3,
) -> List[FEFOItemSuggestionOut]:
    if not item_ids:
        return []

    # Pull minimal item+location info first (for naming)
    base = (
        db.query(
            ItemLocationStock.location_id,
            InventoryLocation.name.label("location_name"),
            InventoryItem.id.label("item_id"),
            InventoryItem.code.label("item_code"),
            InventoryItem.name.label("item_name"),
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(InventoryLocation, InventoryLocation.id == ItemLocationStock.location_id)
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            ItemLocationStock.item_id.in_(item_ids),
            InventoryItem.is_active.is_(True),
        )
    )
    base = _apply_item_filters(base, item_type, schedule_code, supplier_id)
    rows = base.order_by(InventoryLocation.name.asc(), InventoryItem.name.asc()).all()

    out: List[FEFOItemSuggestionOut] = []
    for r in rows:
        batches = (
            db.query(
                ItemBatch.id.label("batch_id"),
                ItemBatch.batch_no,
                ItemBatch.expiry_date,
                ItemBatch.current_qty,
                ItemBatch.unit_cost,
                ItemBatch.mrp,
                ItemBatch.tax_percent,
                ItemBatch.is_saleable,
                ItemBatch.status,
            )
            .filter(
                ItemBatch.location_id == int(r.location_id),
                ItemBatch.item_id == int(r.item_id),
                ItemBatch.is_active.is_(True),
                ItemBatch.is_saleable.is_(True),
                ItemBatch.current_qty > 0,
            )
            .order_by(*_order_nulls_last(ItemBatch.expiry_date), ItemBatch.batch_no.asc())
            .limit(max(int(per_item_batches), 1))
            .all()
        )
        out.append(
            FEFOItemSuggestionOut(
                location_id=int(r.location_id),
                location_name=str(r.location_name),
                item_id=int(r.item_id),
                item_code=str(r.item_code),
                item_name=str(r.item_name),
                batches=[
                    ItemBatchRowOut(
                        batch_id=int(b.batch_id),
                        batch_no=str(b.batch_no),
                        expiry_date=b.expiry_date,
                        current_qty=_q(b.current_qty),
                        unit_cost=_money(b.unit_cost),
                        mrp=_money(b.mrp),
                        tax_percent=_money(b.tax_percent),
                        is_saleable=bool(b.is_saleable),
                        status=str(b.status),
                    )
                    for b in batches
                ],
            )
        )
    return out


def _fefo_compliance_pct_approx(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], days_window: int = 30) -> Optional[Decimal]:
    # Approx:
    # - "dispensed items" count: distinct item_id with a dispense/sale/issue in window
    # - "risky items" count: distinct item_id where used_expiry > earliest_expiry (same logic as FEFO_RISK)
    since_dt = datetime.utcnow() - timedelta(days=int(days_window))

    disp_q = (
        db.query(func.count(func.distinct(StockTransaction.item_id)))
        .join(InventoryItem, InventoryItem.id == StockTransaction.item_id)
        .filter(
            StockTransaction.location_id.in_(loc_ids),
            StockTransaction.txn_time >= since_dt,
            StockTransaction.txn_type.in_(["DISPENSE", "SALE", "ISSUE"]),
            InventoryItem.is_active.is_(True),
        )
    )
    disp_q = _apply_item_filters(disp_q, item_type, schedule_code, supplier_id)
    disp_cnt = int(disp_q.scalar() or 0)
    if disp_cnt <= 0:
        return None

    earliest_sq = (
        db.query(
            ItemBatch.location_id.label("location_id"),
            ItemBatch.item_id.label("item_id"),
            func.min(ItemBatch.expiry_date).label("earliest_expiry"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            InventoryItem.is_active.is_(True),
        )
        .group_by(ItemBatch.location_id, ItemBatch.item_id)
        .subquery()
    )

    used_sq = (
        db.query(
            StockTransaction.location_id.label("location_id"),
            StockTransaction.item_id.label("item_id"),
            func.max(ItemBatch.expiry_date).label("used_expiry"),
        )
        .join(ItemBatch, ItemBatch.id == StockTransaction.batch_id)
        .join(InventoryItem, InventoryItem.id == StockTransaction.item_id)
        .filter(
            StockTransaction.location_id.in_(loc_ids),
            StockTransaction.txn_time >= since_dt,
            StockTransaction.txn_type.in_(["DISPENSE", "SALE", "ISSUE"]),
            ItemBatch.expiry_date.isnot(None),
            InventoryItem.is_active.is_(True),
        )
        .group_by(StockTransaction.location_id, StockTransaction.item_id)
        .subquery()
    )

    risky_q = (
        db.query(func.count(func.distinct(used_sq.c.item_id)))
        .select_from(used_sq)
        .join(
            earliest_sq,
            and_(earliest_sq.c.location_id == used_sq.c.location_id, earliest_sq.c.item_id == used_sq.c.item_id),
        )
        .join(InventoryItem, InventoryItem.id == used_sq.c.item_id)
        .filter(used_sq.c.used_expiry > earliest_sq.c.earliest_expiry)
    )
    risky_q = _apply_item_filters(risky_q, item_type, schedule_code, supplier_id)
    risky_cnt = int(risky_q.scalar() or 0)

    ok_cnt = max(disp_cnt - risky_cnt, 0)
    pct = (Decimal(str(ok_cnt)) / Decimal(str(disp_cnt))) * Decimal("100")
    return pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ============================================================
# KPI counts for smart alerts
# ============================================================
def _count_reorder_items(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], consumption_days: int, lead_time_days: int) -> int:
    since_dt = datetime.utcnow() - timedelta(days=int(consumption_days))
    cons_sq = _avg_daily_consumption_sq(db, loc_ids, since_dt)

    q = (
        db.query(
            ItemLocationStock.location_id,
            ItemLocationStock.item_id,
            ItemLocationStock.on_hand_qty,
            InventoryItem.reorder_level,
            func.coalesce(cons_sq.c.out_qty, 0).label("out_qty"),
        )
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .outerjoin(cons_sq, and_(cons_sq.c.location_id == ItemLocationStock.location_id, cons_sq.c.item_id == ItemLocationStock.item_id))
        .filter(ItemLocationStock.location_id.in_(loc_ids), InventoryItem.is_active.is_(True))
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)

    cnt = 0
    rows = q.all()
    for r in rows:
        on_hand = _q(r.on_hand_qty)
        reorder_level = _q(r.reorder_level)
        out_qty = _q(r.out_qty)
        avg_daily = (out_qty / Decimal(str(consumption_days))) if consumption_days > 0 else ZERO
        rp = reorder_level if reorder_level > 0 else (avg_daily * Decimal(str(lead_time_days)))
        if rp > 0 and on_hand <= rp:
            cnt += 1
    return cnt


def _count_batch_risk(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], non_moving_days: int) -> int:
    today = dt_date.today()
    last_batch_txn_sq = (
        db.query(
            StockTransaction.batch_id.label("batch_id"),
            func.max(StockTransaction.txn_time).label("last_txn_time"),
        )
        .filter(
            StockTransaction.location_id.in_(loc_ids),
            StockTransaction.txn_type.in_(["DISPENSE", "SALE", "ISSUE"]),
            StockTransaction.batch_id.isnot(None),
        )
        .group_by(StockTransaction.batch_id)
        .subquery()
    )
    days_since = func.datediff(today, func.date(last_batch_txn_sq.c.last_txn_time))

    q = (
        db.query(func.count(ItemBatch.id))
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .outerjoin(last_batch_txn_sq, last_batch_txn_sq.c.batch_id == ItemBatch.id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.current_qty > 0,
            InventoryItem.is_active.is_(True),
            or_(last_batch_txn_sq.c.last_txn_time.is_(None), days_since >= int(non_moving_days)),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    return int(q.scalar() or 0)


def _count_negative_or_mismatch(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int]) -> int:
    q1 = (
        db.query(func.count(ItemBatch.id))
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.current_qty < 0,
            InventoryItem.is_active.is_(True),
        )
    )
    q1 = _apply_item_filters(q1, item_type, schedule_code, supplier_id)
    neg_batches = int(q1.scalar() or 0)

    q2 = (
        db.query(func.count(ItemLocationStock.id))
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            ItemLocationStock.on_hand_qty < 0,
            InventoryItem.is_active.is_(True),
        )
    )
    q2 = _apply_item_filters(q2, item_type, schedule_code, supplier_id)
    neg_stock = int(q2.scalar() or 0)

    # mismatch count (distinct item-location with abs diff > eps)
    batch_sum_sq = (
        db.query(
            ItemBatch.location_id.label("location_id"),
            ItemBatch.item_id.label("item_id"),
            func.coalesce(func.sum(ItemBatch.current_qty), 0).label("batch_qty_sum"),
        )
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .filter(ItemBatch.location_id.in_(loc_ids), ItemBatch.is_active.is_(True), InventoryItem.is_active.is_(True))
    )
    batch_sum_sq = _apply_item_filters(batch_sum_sq, item_type, schedule_code, supplier_id)
    batch_sum_sq = batch_sum_sq.group_by(ItemBatch.location_id, ItemBatch.item_id).subquery()

    q3 = (
        db.query(func.count())
        .select_from(ItemLocationStock)
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .join(batch_sum_sq, and_(batch_sum_sq.c.location_id == ItemLocationStock.location_id, batch_sum_sq.c.item_id == ItemLocationStock.item_id))
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            func.abs(ItemLocationStock.on_hand_qty - batch_sum_sq.c.batch_qty_sum) > QTY_EPS,
        )
    )
    q3 = _apply_item_filters(q3, item_type, schedule_code, supplier_id)
    mism = int(q3.scalar() or 0)

    return neg_batches + neg_stock + mism


def _count_high_value_expiry(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int], days_near_expiry: int, threshold: Decimal) -> int:
    if threshold is None or _money(threshold) <= 0:
        return 0

    today = dt_date.today()
    dte = func.datediff(ItemBatch.expiry_date, today)
    val_purchase = (ItemBatch.current_qty * ItemBatch.unit_cost)
    val_mrp = (ItemBatch.current_qty * ItemBatch.mrp)

    q = (
        db.query(func.count(ItemBatch.id))
        .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
        .filter(
            ItemBatch.location_id.in_(loc_ids),
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            dte >= 0,
            dte <= int(days_near_expiry),
            InventoryItem.is_active.is_(True),
            or_(val_purchase >= threshold, val_mrp >= threshold),
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    return int(q.scalar() or 0)


def _count_controlled_stock(db: Session, loc_ids: List[int], item_type: Optional[str], schedule_code: Optional[str], supplier_id: Optional[int]) -> int:
    controlled_cond = or_(
        InventoryItem.schedule_code.in_(["H", "H1", "X"]),
        InventoryItem.lasa_flag.is_(True),
        InventoryItem.prescription_status == "SCHEDULED",
    )
    q = (
        db.query(func.count(ItemLocationStock.id))
        .join(InventoryItem, InventoryItem.id == ItemLocationStock.item_id)
        .filter(
            ItemLocationStock.location_id.in_(loc_ids),
            InventoryItem.is_active.is_(True),
            controlled_cond,
            ItemLocationStock.on_hand_qty > 0,
        )
    )
    q = _apply_item_filters(q, item_type, schedule_code, supplier_id)
    return int(q.scalar() or 0)
