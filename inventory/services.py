"""Business logic for IMS Modules 1 to 4.

Every function that changes stock runs inside a transaction and records a
StockMovement (FR-17) and, where the SRS calls for it, an audit entry.
Errors the user can fix are raised as ``ValidationError`` with a plain
message for the screen.
"""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from core.models import AuditLog, Market, Role, StorageType
from core.rules import rule, rule_int
from core.services import audit, notify, notify_roles, users_with_role

from .models import (Batch, BatchStatus, BatchStock, CustomerOffer, ExpiryAlert,
                     ExpiryScanRun, FieldDispatch, Listing, ProductRegistration,
                     StockCount, StockCountLine, StockMovement, SurplusRelease, WriteOff)

TWO = Decimal("0.01")
ACTIVE_STATUSES = [BatchStatus.IN_STOCK, BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS,
                   BatchStatus.RELEASED]


def money(value):
    return Decimal(value).quantize(TWO, rounding=ROUND_HALF_UP)


def next_number(prefix, model, field="number"):
    """Next free reference such as PO-2026-00012, using the highest numeric suffix
    (references imported from Odoo may use a different width)."""
    n = 0
    for value in model.objects.filter(**{f"{field}__startswith": prefix}).values_list(
            field, flat=True):
        tail = value[len(prefix):]
        if tail.isdigit():
            n = max(n, int(tail))
    candidate = f"{prefix}{n + 1:05d}"
    while model.objects.filter(**{field: candidate}).exists():
        n += 1
        candidate = f"{prefix}{n + 1:05d}"
    return candidate


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def sales_reps_for(product):
    """Sales Representatives responsible for a product's category (FR-25)."""
    reps = [u for u in users_with_role(Role.SALES)
            if u.categories.filter(pk=product.category_id).exists()]
    return reps or users_with_role(Role.SALES)


def listed_qty(batch):
    return batch.listings.filter(status=Listing.ACTIVE).aggregate(
        t=Sum("available_qty"))["t"] or 0


def free_qty(batch):
    """Stock that is not reserved, blocked or offered on the platform."""
    return max(0, batch.qty_available - listed_qty(batch))


def storage_compatible(storage_condition, storage_type):
    """Cold-chain stock must go to matching storage (FR-11); ambient stock
    may not go into frozen storage."""
    if storage_condition == StorageType.AMBIENT:
        return storage_type != StorageType.FROZEN
    return storage_condition == storage_type


def location_load(location):
    return location.stock.aggregate(t=Sum("quantity"))["t"] or 0


def mark_actioned(batch):
    batch.last_action_at = timezone.localdate()
    batch.save(update_fields=["last_action_at"])
    batch.alerts.filter(status=ExpiryAlert.OPEN, alert_type__in=[
        ExpiryAlert.NEAR_EXPIRY, ExpiryAlert.ESCALATION]).update(status=ExpiryAlert.ACTIONED)


def _move(batch, qty, mtype, user, reference="", from_loc=None, to_loc=None):
    return StockMovement.objects.create(batch=batch, movement_type=mtype, quantity=qty,
                                        from_location=from_loc, to_location=to_loc,
                                        reference=reference, user=user)


def _set_depleted_if_empty(batch):
    if batch.qty_on_hand <= 0 and batch.status not in (BatchStatus.EXPIRED,):
        batch.status = BatchStatus.DEPLETED


def _deduct(batch, qty, user, mtype, reference, prefer_location=None):
    """Take ``qty`` out of a batch across its locations."""
    remaining = qty
    stocks = list(batch.stock.filter(quantity__gt=0).select_related("location"))
    stocks.sort(key=lambda s: (s.location_id != getattr(prefer_location, "pk", None),
                               -s.quantity))
    for s in stocks:
        if remaining <= 0:
            break
        take = min(s.quantity, remaining)
        s.quantity -= take
        s.save(update_fields=["quantity"])
        _move(batch, -take, mtype, user, reference, from_loc=s.location)
        remaining -= take
    if remaining > 0:
        raise ValidationError(f"Not enough stock in lot {batch.lot_number}.")
    batch.recompute_on_hand()


# --------------------------------------------------------------------------
# Module 1: Inventory and batch control
# --------------------------------------------------------------------------
def remaining_shelf_days(expiry, on=None):
    return (expiry - (on or timezone.localdate())).days


def low_shelf_life_at_receipt(expiry, on=None):
    """FR-09: warn when shelf life is below the minimum receiving shelf life."""
    months = rule_int("min_receiving_months")
    return remaining_shelf_days(expiry, on) < months * 30.4


@transaction.atomic
def record_inspection(user, *, purchase_order, product, lot_number, manufacture_date,
                      expiry_date, storage_condition, quantity_received, quantity_approved,
                      quantity_rejected, rejection_reason="", market=None):
    """UC-04 / FR-07 to FR-09."""
    today = timezone.localdate()
    if expiry_date < manufacture_date or expiry_date < today:
        raise ValidationError("Expiry date cannot be earlier than the manufacture date or "
                              "the inspection date.")
    if quantity_approved + quantity_rejected != quantity_received:
        raise ValidationError("Approved plus rejected quantity must equal received quantity.")
    if quantity_rejected and not rejection_reason:
        raise ValidationError("Select a rejection reason for the rejected quantity.")
    if Batch.objects.filter(product=product, lot_number=lot_number).exists():
        raise ValidationError(f"Lot {lot_number} is already recorded for this product.")
    market = market or Market.objects.filter(is_active=True).first()
    batch = Batch.objects.create(
        product=product, lot_number=lot_number, manufacture_date=manufacture_date,
        expiry_date=expiry_date, storage_condition=storage_condition, market=market,
        purchase_order=purchase_order, quantity_received=quantity_received,
        quantity_approved=quantity_approved, quantity_rejected=quantity_rejected,
        rejection_reason=rejection_reason, inspection_date=today, inspected_by=user,
        early_monitoring=low_shelf_life_at_receipt(expiry_date),
        status=BatchStatus.INSPECTED if quantity_approved else BatchStatus.REJECTED,
    )
    if purchase_order:
        line = purchase_order.lines.filter(product=product).first()
        if line:
            line.quantity_received = F("quantity_received") + quantity_received
            line.save(update_fields=["quantity_received"])
    audit(user, AuditLog.STOCK, f"Inspection recorded for lot {lot_number}",
          f"BATCH-{batch.pk}", new=f"approved={quantity_approved} rejected={quantity_rejected}")
    if quantity_approved:
        notify_roles(Role.INVENTORY,
                     f"Batch {product.name} lot {lot_number} approved by QA and ready for "
                     f"receipt ({quantity_approved} {product.unit}).",
                     f"/stock/receipt/{batch.pk}/")
    return batch


class CapacityWarning(ValidationError):
    pass


@transaction.atomic
def receive_batch(user, batch, location, allow_over_capacity=False):
    """UC-05 / FR-10 to FR-12."""
    batch = Batch.objects.select_for_update().get(pk=batch.pk)
    if batch.status != BatchStatus.INSPECTED:
        raise ValidationError("Only inspected batches awaiting receipt can be booked in.")
    if not storage_compatible(batch.storage_condition, location.storage_type):
        raise ValidationError(
            f"{batch.get_storage_condition_display()} stock cannot be stored in a "
            f"{location.get_storage_type_display()} location.")
    load = location_load(location)
    if load + batch.quantity_approved > location.capacity and not allow_over_capacity:
        raise CapacityWarning(f"Location {location.code} would exceed its capacity "
                              f"({load}/{location.capacity} units in use).")
    stock, _ = BatchStock.objects.get_or_create(batch=batch, location=location)
    stock.quantity += batch.quantity_approved
    stock.save()
    batch.recompute_on_hand()
    batch.received_at = timezone.now()
    batch.received_by = user
    batch.status = BatchStatus.IN_STOCK
    batch.save()
    _move(batch, batch.quantity_approved, StockMovement.RECEIPT, user,
          batch.purchase_order.number if batch.purchase_order else "", to_loc=location)
    evaluate_batch(batch)
    if batch.early_monitoring:
        ExpiryAlert.objects.create(batch=batch, alert_type=ExpiryAlert.LOW_SHELF_RECEIPT,
                                   days_to_expiry=batch.days_to_expiry)
    return batch


@transaction.atomic
def transfer_stock(user, batch, from_location, to_location, quantity):
    """UC-07 / FR-11, FR-13."""
    batch = Batch.objects.select_for_update().get(pk=batch.pk)
    if batch.frozen_for_count:
        raise ValidationError("This batch is frozen for an audit count until the count is "
                              "submitted.")
    if from_location == to_location:
        raise ValidationError("Source and destination must differ.")
    if not storage_compatible(batch.storage_condition, to_location.storage_type):
        raise ValidationError(
            f"{batch.get_storage_condition_display()} stock cannot be moved to a "
            f"{to_location.get_storage_type_display()} location.")
    src = BatchStock.objects.select_for_update().filter(batch=batch,
                                                        location=from_location).first()
    on_loc = src.quantity if src else 0
    unreserved = min(on_loc, batch.qty_available)
    if quantity <= 0 or quantity > unreserved:
        raise ValidationError(f"Quantity exceeds unreserved stock at the source "
                              f"({unreserved} available).")
    src.quantity -= quantity
    src.save()
    dst, _ = BatchStock.objects.get_or_create(batch=batch, location=to_location)
    dst.quantity += quantity
    dst.save()
    # Transfers leave stock on hand unchanged; the quantity moved is recorded.
    _move(batch, quantity, StockMovement.TRANSFER, user, "Transfer",
          from_loc=from_location, to_loc=to_location)
    return batch


def eligible_batches(product):
    """Batches that may be allocated: unfrozen and above the minimum sellable
    shelf life (FR-14, UC-09 step 3)."""
    min_days = rule_int("min_sellable_days")
    cutoff = timezone.localdate() + timedelta(days=min_days)
    return (Batch.objects.filter(product=product, status__in=[BatchStatus.IN_STOCK,
                                                              BatchStatus.NEAR_EXPIRY],
                                 frozen_for_count=False, expiry_date__gt=cutoff)
            .order_by("expiry_date", "id"))


def fefo_allocate(product, quantity):
    """UC-09: allocate from the earliest-expiring eligible batch first.

    Returns ``(allocation, shortfall)`` where allocation is a list of
    ``(batch, qty)``."""
    allocation, remaining = [], quantity
    for batch in eligible_batches(product):
        if remaining <= 0:
            break
        avail = free_qty(batch)
        if avail <= 0:
            continue
        take = min(avail, remaining)
        allocation.append((batch, take))
        remaining -= take
    return allocation, remaining


@transaction.atomic
def dispatch_field_order(user, product, quantity, customer=None, reference=""):
    """Dispatch outside the platform with FEFO allocation (UC-08 / UC-09)."""
    allocation, shortfall = fefo_allocate(product, quantity)
    if not allocation:
        raise ValidationError("No eligible stock is available for this product.")
    ref = reference or next_number("FD-", FieldDispatch, "reference")
    record = []
    for batch, qty in allocation:
        b = Batch.objects.select_for_update().get(pk=batch.pk)
        _deduct(b, qty, user, StockMovement.DISPATCH, ref)
        _set_depleted_if_empty(b)
        b.save()
        record.append({"batch": b.pk, "lot": b.lot_number, "qty": qty,
                       "expiry": b.expiry_date.isoformat()})
    fd = FieldDispatch.objects.create(reference=ref, customer=customer, product=product,
                                      quantity=quantity, quantity_dispatched=quantity - shortfall,
                                      dispatched_by=user, allocation=record)
    audit(user, AuditLog.STOCK, f"Field dispatch {ref}", ref,
          new=", ".join(f"{r['lot']}:{r['qty']}" for r in record))
    return fd, shortfall


@transaction.atomic
def dispatch_platform_order(user, order, picked_lots, delivery_method, delivery_date,
                            cold_chain_checked=False):
    """UC-08: dispatch a confirmed platform order from the batches named in
    its listings, deducting stock on confirmation (FR-15)."""
    from commerce.models import PlatformOrder
    from commerce.services import notify_order_status

    order = PlatformOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != PlatformOrder.CONFIRMED:
        raise ValidationError("Only confirmed orders can be dispatched.")
    lines = list(order.lines.select_related("batch__product"))
    for line in lines:
        lot = (picked_lots.get(line.pk) or "").strip()
        if lot != line.batch.lot_number:
            raise ValidationError(f"Picked lot for {line.batch.product.name} does not match "
                                  f"the allocated lot {line.batch.lot_number}.")
    if any(line.batch.is_cold_chain for line in lines) and not cold_chain_checked:
        raise ValidationError("Confirm the cold-chain packaging check before dispatch.")
    for line in lines:
        b = Batch.objects.select_for_update().get(pk=line.batch_id)
        if line.quantity > b.qty_on_hand:
            raise ValidationError(f"Cannot dispatch more than on hand for lot {b.lot_number}.")
        _deduct(b, line.quantity, user, StockMovement.DISPATCH, order.reference)
        b.qty_reserved = max(0, b.qty_reserved - line.quantity)
        _set_depleted_if_empty(b)
        b.save()
        line.picked_lot = b.lot_number
        line.save(update_fields=["picked_lot"])
    order.status = PlatformOrder.DISPATCHED
    order.dispatched_by = user
    order.dispatched_at = timezone.now()
    order.delivery_method = delivery_method
    order.delivery_date = delivery_date
    order.on_hold = False
    order.save()
    audit(user, AuditLog.ORDER, f"Order {order.reference} dispatched", order.reference)
    notify_order_status(order)
    return order


@transaction.atomic
def hold_order_for_damage(user, order, note):
    """UC-08 A2: stock found damaged at picking."""
    order.on_hold = True
    order.hold_reason = note
    order.save(update_fields=["on_hold", "hold_reason"])
    notify_roles(Role.INVENTORY, f"Order {order.reference} on hold: {note}",
                 f"/dispatch/{order.pk}/", level="warning")


# --------------------------------------------------------------------------
# Module 2: Warehouse audit
# --------------------------------------------------------------------------
def _count_scope_stock(warehouse, scope, category=None, batch_ids=None):
    qs = BatchStock.objects.filter(location__warehouse=warehouse, quantity__gt=0,
                                   batch__status__in=ACTIVE_STATUSES + [BatchStatus.EXPIRED])
    if scope == StockCount.SCOPE_CATEGORY and category:
        qs = qs.filter(batch__product__category=category)
    if scope == StockCount.SCOPE_BATCHES:
        qs = qs.filter(batch_id__in=batch_ids or [])
    return qs.select_related("batch", "location")


def count_conflicts(warehouse, scope, category=None, batch_ids=None):
    """UC-10 A1/A2: batches already under count, and batches with open dispatches."""
    from commerce.models import OrderLine, PlatformOrder

    stock = list(_count_scope_stock(warehouse, scope, category, batch_ids))
    ids = {s.batch_id for s in stock}
    overlapping = StockCountLine.objects.filter(
        batch_id__in=ids, count__status__in=[StockCount.SCHEDULED, StockCount.IN_PROGRESS,
                                             StockCount.SUBMITTED]).values_list(
        "count__number", flat=True).distinct()
    open_dispatch = set(OrderLine.objects.filter(
        batch_id__in=ids, order__status=PlatformOrder.CONFIRMED).values_list("batch_id",
                                                                              flat=True))
    return stock, list(overlapping), open_dispatch


@transaction.atomic
def schedule_count(user, *, count_type, warehouse, scope, scheduled_date, assigned_to,
                   category=None, batch_ids=None, exclude_open_dispatch=False):
    """UC-10 / FR-18, FR-19."""
    stock, overlapping, open_dispatch = count_conflicts(warehouse, scope, category, batch_ids)
    if overlapping:
        raise ValidationError("Another count already covers some of these batches: "
                              + ", ".join(sorted(set(overlapping))))
    if open_dispatch and not exclude_open_dispatch:
        lots = Batch.objects.filter(pk__in=open_dispatch).values_list("lot_number", flat=True)
        raise ValidationError("Batches in scope have open dispatches: " + ", ".join(lots)
                              + ". Reschedule, or tick 'exclude batches with open "
                                "dispatches'.")
    if exclude_open_dispatch:
        stock = [s for s in stock if s.batch_id not in open_dispatch]
    if not stock:
        raise ValidationError("No stock found in the selected scope.")
    count = StockCount.objects.create(
        number=next_number("CNT-", StockCount), count_type=count_type, warehouse=warehouse,
        scope=scope, category=category, scheduled_date=scheduled_date, assigned_to=assigned_to,
        created_by=user)
    for s in stock:
        StockCountLine.objects.create(count=count, batch=s.batch, location=s.location,
                                      system_qty=s.quantity)
    notify(assigned_to, f"Stock count {count.number} assigned to you for "
                        f"{scheduled_date:%d %b %Y}.", f"/audits/{count.pk}/count/")
    if scheduled_date <= timezone.localdate():
        start_count(count)
    return count


@transaction.atomic
def start_count(count):
    """Freeze movements for the batches under count (FR-19) and snapshot the
    system quantity."""
    if count.status != StockCount.SCHEDULED:
        return count
    for line in count.lines.select_related("batch"):
        if line.batch_id:
            Batch.objects.filter(pk=line.batch_id).update(frozen_for_count=True)
            stock = BatchStock.objects.filter(batch_id=line.batch_id,
                                              location=line.location).first()
            line.system_qty = stock.quantity if stock else 0
            line.save(update_fields=["system_qty"])
    count.status = StockCount.IN_PROGRESS
    count.save(update_fields=["status"])
    return count


def _variance(line):
    counted = 0 if line.not_found else (line.counted_qty or 0)
    line.variance_qty = counted - line.system_qty
    cost = (line.batch.product.unit_cost if line.batch_id
            else (line.extra_product.unit_cost if line.extra_product_id else Decimal(0)))
    line.variance_value = money(cost * line.variance_qty)
    tol = rule("variance_tolerance_pct") / 100
    if line.system_qty == 0:
        flagged = line.variance_qty != 0
    else:
        flagged = abs(line.variance_qty) > tol * line.system_qty
    return flagged


@transaction.atomic
def submit_count(user, count, entries, extras=()):
    """UC-11 / FR-20, FR-21.

    ``entries`` maps line id to ``{"counted": int|None, "not_found": bool,
    "observed_expiry": date|None}``. Lines inside the tolerance are posted
    straight away, so the record matches the physical count; flagged lines
    go to the Inventory Controller."""
    if count.status != StockCount.IN_PROGRESS:
        raise ValidationError("This count is not open for counting.")
    lines = list(count.lines.filter(status__in=[StockCountLine.PENDING, StockCountLine.RECOUNT])
                 .select_related("batch__product"))
    for line in lines:
        e = entries.get(line.pk, {})
        if e.get("counted") is None and not e.get("not_found"):
            raise ValidationError("Every line needs a count or must be marked Not Found.")
    for ex in extras:
        StockCountLine.objects.create(
            count=count, batch=None, location=ex["location"], system_qty=0,
            counted_qty=ex["quantity"], is_extra=True, extra_product=ex["product"],
            extra_lot=ex["lot"], extra_expiry=ex.get("expiry"))
    lines = list(count.lines.filter(status__in=[StockCountLine.PENDING, StockCountLine.RECOUNT])
                 .select_related("batch__product", "extra_product"))
    for line in lines:
        e = entries.get(line.pk, {})
        if not line.is_extra:
            line.not_found = bool(e.get("not_found"))
            line.counted_qty = None if line.not_found else e.get("counted")
            line.observed_expiry = e.get("observed_expiry")
        flagged = _variance(line)
        line.status = StockCountLine.FLAGGED if flagged else StockCountLine.WITHIN
        line.save()
        if not flagged and line.variance_qty:
            _post_adjustment(line, user, "Within tolerance")
    count.status = StockCount.SUBMITTED
    count.submitted_at = timezone.now()
    count.save()
    flagged = count.lines.filter(status=StockCountLine.FLAGGED).count()
    if flagged:
        notify_roles(Role.INVENTORY, f"Count {count.number} submitted with {flagged} flagged "
                                     f"variance(s).", f"/audits/{count.pk}/reconcile/")
    else:
        close_count(count)
        notify_roles(Role.INVENTORY, f"Count {count.number} submitted and closed with no "
                                     f"flagged variances.", f"/audits/{count.pk}/")
    return count


def _post_adjustment(line, user, note=""):
    counted = 0 if line.not_found else (line.counted_qty or 0)
    if line.is_extra:
        batch = Batch.objects.filter(product=line.extra_product, lot_number=line.extra_lot).first()
        if batch is None:
            batch = Batch.objects.create(
                product=line.extra_product, lot_number=line.extra_lot,
                manufacture_date=(line.extra_expiry or timezone.localdate()) - timedelta(days=730),
                expiry_date=line.extra_expiry or timezone.localdate() + timedelta(days=365),
                storage_condition=line.extra_product.storage_condition,
                market=line.count.warehouse.market, quantity_received=counted,
                quantity_approved=counted, status=BatchStatus.IN_STOCK,
                received_at=timezone.now(), received_by=user)
        line.batch = batch
    batch = line.batch
    stock, _ = BatchStock.objects.get_or_create(batch=batch, location=line.location)
    delta = counted - stock.quantity if line.is_extra else line.variance_qty
    stock.quantity = max(0, stock.quantity + delta)
    stock.save()
    batch.recompute_on_hand()
    _set_depleted_if_empty(batch)
    batch.save()
    _move(batch, delta, StockMovement.ADJUSTMENT, user, line.count.number,
          to_loc=line.location if delta > 0 else None,
          from_loc=line.location if delta < 0 else None)
    line.status = StockCountLine.POSTED if line.status != StockCountLine.WITHIN else line.status
    line.save()
    audit(user, AuditLog.ADJUSTMENT,
          f"Count adjustment {line.count.number} lot {batch.lot_number} {note}".strip(),
          line.count.number, old=line.system_qty, new=counted)
    count = line.count
    count.adjustment_value = money(abs(count.adjustment_value) + abs(line.variance_value))
    count.save(update_fields=["adjustment_value"])


@transaction.atomic
def reconcile_count(user, count, decisions):
    """UC-12 / FR-22, FR-23. ``decisions`` maps flagged line id to either
    ``{"action": "recount"}`` or ``{"action": "post", "reason": ReasonCode}``."""
    if count.status != StockCount.SUBMITTED:
        raise ValidationError("Only submitted counts can be reconciled.")
    threshold = rule("adjustment_threshold_egp")
    flagged = list(count.lines.filter(status=StockCountLine.FLAGGED)
                   .select_related("batch__product"))
    recounts = 0
    for line in flagged:
        d = decisions.get(line.pk)
        if not d:
            raise ValidationError("Assign a reason code or request a recount for every "
                                  "flagged variance.")
        if d["action"] == "recount":
            line.status = StockCountLine.RECOUNT
            line.counted_qty = None
            line.recount_round += 1
            line.save()
            recounts += 1
            continue
        if not d.get("reason"):
            raise ValidationError("Select a reason code for every flagged variance.")
        line.reason_code = d["reason"]
        if abs(line.variance_value) >= threshold:
            line.status = StockCountLine.AWAITING
            line.save()
            notify_roles(Role.GM, f"Stock adjustment of {abs(line.variance_value):,.2f} EGP "
                                  f"awaits your approval ({count.number}).",
                         "/approvals/", level="warning")
        else:
            line.save()
            _post_adjustment(line, user, d["reason"].label)
    if recounts:
        count.status = StockCount.IN_PROGRESS
        count.save(update_fields=["status"])
        notify(count.assigned_to, f"Blind recount requested on {count.number} "
                                  f"({recounts} line(s)).", f"/audits/{count.pk}/count/")
    else:
        maybe_close_count(count)
    return count


@transaction.atomic
def decide_adjustment(user, line, approve, comment):
    """UC-13: General Manager approves or rejects an adjustment."""
    if line.status != StockCountLine.AWAITING:
        raise ValidationError("This adjustment is not awaiting approval.")
    if not approve and not comment:
        raise ValidationError("A comment is required to reject an adjustment.")
    line.approved_by = user
    line.decision_comment = comment
    line.decided_at = timezone.now()
    count = line.count
    if approve:
        line.save()
        _post_adjustment(line, user, f"approved by GM: {comment}")
        audit(user, AuditLog.APPROVAL, f"Adjustment approved on {count.number}", count.number,
              new=comment)
        notify_roles(Role.INVENTORY, f"Adjustment on {count.number} approved.",
                     f"/audits/{count.pk}/")
        maybe_close_count(count)
    else:
        line.status = StockCountLine.RECOUNT
        line.counted_qty = None
        line.recount_round += 1
        line.save()
        count.status = StockCount.IN_PROGRESS
        count.save(update_fields=["status"])
        audit(user, AuditLog.APPROVAL, f"Adjustment rejected on {count.number}", count.number,
              new=comment)
        notify_roles(Role.INVENTORY, f"Adjustment on {count.number} rejected: recount "
                                     f"required.", f"/audits/{count.pk}/", level="warning")
        notify(count.assigned_to, f"Blind recount requested on {count.number}.",
               f"/audits/{count.pk}/count/")
    return line


def maybe_close_count(count):
    open_states = [StockCountLine.PENDING, StockCountLine.FLAGGED, StockCountLine.AWAITING,
                   StockCountLine.RECOUNT]
    if not count.lines.filter(status__in=open_states).exists():
        close_count(count)


def close_count(count):
    """Close the count, release the freeze and record accuracy (UC-12 step 6)."""
    lines = list(count.lines.all())
    within = sum(1 for line in lines if line.status == StockCountLine.WITHIN
                 and line.recount_round == 0)
    count.accuracy_rate = Decimal(within * 100 / len(lines)).quantize(TWO) if lines else None
    count.status = StockCount.CLOSED
    count.closed_at = timezone.now()
    count.save()
    batch_ids = [line.batch_id for line in lines if line.batch_id]
    still_frozen = StockCountLine.objects.filter(
        batch_id__in=batch_ids, count__status__in=[StockCount.IN_PROGRESS,
                                                   StockCount.SUBMITTED]).exclude(
        count=count).values_list("batch_id", flat=True)
    Batch.objects.filter(pk__in=batch_ids).exclude(pk__in=still_frozen).update(
        frozen_for_count=False)


# --------------------------------------------------------------------------
# Module 3: Expiry monitoring and surplus release
# --------------------------------------------------------------------------
def registration_check(product, market, batch_expiry):
    """UC-19: registered, Active and valid beyond the batch expiry."""
    reg = ProductRegistration.objects.filter(product=product, market=market).first()
    if reg is None:
        return False, f"{product.name} has no registration in {market}."
    if reg.status != ProductRegistration.ACTIVE:
        return False, f"Registration status is {reg.get_status_display()}."
    if reg.expiry_date <= batch_expiry:
        return False, (f"Registration expires on {reg.expiry_date:%d %b %Y}, before the batch "
                       f"expiry.")
    return True, "Cleared"


def evaluate_batch(batch, today=None):
    """Bring one batch's status in line with its days to expiry."""
    today = today or timezone.localdate()
    days = (batch.expiry_date - today).days
    if batch.qty_on_hand <= 0:
        return None
    if days < 0 and batch.status != BatchStatus.EXPIRED:
        batch.status = BatchStatus.EXPIRED
        batch.save(update_fields=["status"])
        return "expired"
    if (days <= rule_int("near_expiry_days") and batch.status == BatchStatus.IN_STOCK
            and batch.product.is_expiring):
        batch.status = BatchStatus.NEAR_EXPIRY
        batch.near_expiry_since = today
        batch.last_action_at = today
        batch.escalated = False
        batch.save(update_fields=["status", "near_expiry_since", "last_action_at", "escalated"])
        ExpiryAlert.objects.create(batch=batch, alert_type=ExpiryAlert.NEAR_EXPIRY,
                                   days_to_expiry=days, raised_date=today)
        link = f"/expiry/batch/{batch.pk}/"
        msg = (f"Near-expiry: {batch.product.name} lot {batch.lot_number}, {days} days to "
               f"expiry, {batch.qty_on_hand} {batch.product.unit}.")
        notify_roles(Role.INVENTORY, msg, link, level="warning")
        notify(sales_reps_for(batch.product), msg, link, level="warning")
        return "near_expiry"
    return None


def value_at_risk():
    total = Decimal(0)
    for b in Batch.objects.filter(status__in=[BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS,
                                              BatchStatus.RELEASED],
                                  qty_on_hand__gt=0).select_related("product"):
        total += b.product.unit_cost * b.qty_on_hand
    return money(total)


def withdraw_listing(listing, reason, user=None):
    if listing.status != Listing.ACTIVE:
        return listing
    listing.status = Listing.WITHDRAWN
    listing.withdrawn_at = timezone.now()
    listing.withdrawn_reason = reason
    listing.available_qty = 0
    listing.save()
    batch = listing.batch
    if (batch.status == BatchStatus.RELEASED
            and not batch.listings.filter(status=Listing.ACTIVE).exists()):
        batch.status = BatchStatus.NEAR_EXPIRY
        batch.last_action_at = timezone.localdate()
        batch.save(update_fields=["status", "last_action_at"])
    audit(user, AuditLog.PRICE_CHANGE, f"Listing #{listing.pk} withdrawn: {reason}",
          f"LISTING-{listing.pk}")
    return listing


@transaction.atomic
def _scan_once(today, triggered_by):
    from commerce.services import licence_checks, purge_rejected_documents

    run = ExpiryScanRun.objects.create(triggered_by=triggered_by)
    min_days = rule_int("min_sellable_days")
    esc_days = rule_int("escalation_days")
    scanned = new_ne = expired = withdrawn = escalations = 0

    # Start counts whose date has arrived.
    for c in StockCount.objects.filter(status=StockCount.SCHEDULED, scheduled_date__lte=today):
        start_count(c)

    batches = Batch.objects.filter(qty_on_hand__gt=0).exclude(
        status__in=[BatchStatus.INSPECTED, BatchStatus.REJECTED]).select_related("product")
    for b in batches:
        scanned += 1
        days = (b.expiry_date - today).days
        outcome = evaluate_batch(b, today)
        if outcome == "near_expiry":
            new_ne += 1
        if outcome == "expired":
            expired += 1
            for listing in b.listings.filter(status=Listing.ACTIVE):
                withdraw_listing(listing, "Batch expired")
                withdrawn += 1
            ExpiryAlert.objects.create(batch=b, alert_type=ExpiryAlert.EXPIRED,
                                       days_to_expiry=days, raised_date=today)
            notify_roles(Role.INVENTORY, f"Expired: {b.product.name} lot {b.lot_number} is "
                                         f"blocked from sale. Request write-off.",
                         f"/write-offs/new/?batch={b.pk}", level="danger")
            continue
        if days <= min_days:
            for listing in b.listings.filter(status=Listing.ACTIVE):
                withdraw_listing(listing, f"Shelf life at or below {min_days} days")
                withdrawn += 1
                if not b.alerts.filter(alert_type=ExpiryAlert.MIN_SHELF).exists():
                    ExpiryAlert.objects.create(batch=b, alert_type=ExpiryAlert.MIN_SHELF,
                                               days_to_expiry=days, raised_date=today)
            if b.qty_reserved:
                # UC-14 A2: confirmed orders keep their reservation; dispatch first.
                notify_roles(Role.WAREHOUSE, f"Lot {b.lot_number} has reserved stock for "
                                             f"orders and {days} days left: dispatch first.",
                             "/dispatch/", level="warning")
        if (b.status == BatchStatus.NEAR_EXPIRY and not b.escalated and b.last_action_at
                and (today - b.last_action_at).days >= esc_days):
            b.escalated = True
            b.save(update_fields=["escalated"])
            ExpiryAlert.objects.create(batch=b, alert_type=ExpiryAlert.ESCALATION,
                                       days_to_expiry=days, raised_date=today)
            notify_roles(Role.GM, f"Escalation: {b.product.name} lot {b.lot_number} has had "
                                  f"no action for {esc_days} days ({days} days to expiry).",
                         f"/expiry/batch/{b.pk}/", level="danger")
            escalations += 1

    # Registration lapses withdraw open listings (FR-34).
    for listing in Listing.objects.filter(status=Listing.ACTIVE).select_related(
            "batch__product", "market"):
        ok, reason = registration_check(listing.batch.product, listing.market,
                                        listing.batch.expiry_date)
        if not ok:
            withdraw_listing(listing, "Registration: " + reason)
            withdrawn += 1
            ExpiryAlert.objects.create(batch=listing.batch, alert_type=ExpiryAlert.REGISTRATION,
                                       days_to_expiry=listing.batch.days_to_expiry)
            notify_roles([Role.REGULATORY, Role.COO],
                         f"Listing #{listing.pk} withdrawn: {reason}", "/registrations/",
                         level="warning")
    # Registrations expiring soon (UC-20 A2).
    alert_days = rule_int("registration_alert_days")
    soon = ProductRegistration.objects.filter(status=ProductRegistration.ACTIVE,
                                              expiry_date=today + timedelta(days=alert_days))
    for reg in soon:
        notify_roles(Role.REGULATORY, f"Registration of {reg.product.name} in {reg.market} "
                                      f"expires in {alert_days} days.", "/registrations/")

    licence_checks(today)
    purge_rejected_documents(today)

    run.batches_scanned = scanned
    run.new_near_expiry = new_ne
    run.expired = expired
    run.listings_withdrawn = withdrawn
    run.escalations = escalations
    run.value_at_risk = value_at_risk()
    run.finished_at = timezone.now()
    run.succeeded = True
    run.save()
    return run


def run_expiry_scan(today=None, triggered_by="scheduler", retries=1):
    """UC-14 / FR-24 to FR-26, FR-34, FR-37, NFR-11.

    A failed scan is retried; if the retry fails too, the System Administrator
    is alerted."""
    from commerce.services import release_expired_reservations

    today = today or timezone.localdate()
    release_expired_reservations()
    last_error = None
    for _attempt in range(retries + 1):
        try:
            return _scan_once(today, triggered_by)
        except Exception as exc:  # noqa: BLE001 - reported to the administrator
            last_error = exc
    ExpiryScanRun.objects.create(triggered_by=triggered_by, succeeded=False,
                                 finished_at=timezone.now(), error=repr(last_error))
    notify_roles(Role.ADMIN, f"Daily expiry scan failed after retry: {last_error}",
                 "/scans/", level="danger")
    return None


def avg_monthly_sales(product, months=12, region_ids=None):
    from planning.models import SalesRecord

    qs = SalesRecord.objects.filter(product=product)
    if region_ids:
        qs = qs.filter(region_id__in=region_ids)
    latest = qs.order_by("-period").values_list("period", flat=True).first()
    if not latest:
        return Decimal(0)
    idx = latest.year * 12 + latest.month - 1 - (months - 1)
    start = date(idx // 12, idx % 12 + 1, 1)
    total = qs.filter(period__gte=start).aggregate(t=Sum("quantity"))["t"] or 0
    return (Decimal(total) / months).quantize(TWO)


def projected_unsold(batch):
    """FR-28: quantity projected to be left unsold at expiry.

    Demand for the product is shared by all its batches in FEFO order, so
    earlier-expiring batches consume demand first."""
    rate = avg_monthly_sales(batch.product) / Decimal("30.4")  # per day
    accepted = batch.offers.filter(outcome=CustomerOffer.ACCEPTED).aggregate(
        t=Sum("qty_offered"))["t"] or 0
    earlier = (Batch.objects.filter(product=batch.product, qty_on_hand__gt=0,
                                    status__in=ACTIVE_STATUSES,
                                    expiry_date__lt=batch.expiry_date)
               .aggregate(t=Sum("qty_on_hand"))["t"] or 0)
    sellable_days = max(0, batch.days_to_expiry - rule_int("min_sellable_days"))
    capacity = rate * sellable_days - earlier
    sold = max(Decimal(0), capacity)
    return max(0, int(batch.qty_on_hand - accepted - sold))


@transaction.atomic
def record_offer(user, batch, customer, qty, outcome):
    """UC-15 step 4 / FR-27."""
    if qty <= 0 or qty > batch.qty_on_hand:
        raise ValidationError("Offered quantity must be between 1 and the quantity on hand.")
    offer = CustomerOffer.objects.create(batch=batch, customer=customer, qty_offered=qty,
                                         outcome=outcome, offered_by=user)
    mark_actioned(batch)
    return offer


def keep_under_watch(user, batch):
    batch.keep_under_watch = True
    batch.save(update_fields=["keep_under_watch"])
    mark_actioned(batch)


@transaction.atomic
def flag_surplus(user, batch, quantity, reason, comment=""):
    """UC-16 / FR-29."""
    batch = Batch.objects.select_for_update().get(pk=batch.pk)
    if not batch.product.is_expiring:
        raise ValidationError("Non-expiring products are not listed on the clearance channel.")
    if batch.status not in (BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS, BatchStatus.RELEASED):
        raise ValidationError("Only Near-Expiry batches can be flagged as surplus.")
    pending = batch.releases.filter(status=SurplusRelease.PENDING).aggregate(
        t=Sum("surplus_qty"))["t"] or 0
    available = free_qty(batch) - pending
    if quantity <= 0 or quantity > available:
        raise ValidationError(f"Surplus quantity exceeds unreserved stock ({available} "
                              f"available).")
    release = SurplusRelease.objects.create(batch=batch, flagged_by=user, flag_reason=reason,
                                            comment=comment, surplus_qty=quantity)
    if batch.status == BatchStatus.NEAR_EXPIRY:
        batch.status = BatchStatus.SURPLUS
        batch.save(update_fields=["status"])
    mark_actioned(batch)
    notify_roles(Role.COO, f"Surplus flagged: {batch.product.name} lot {batch.lot_number}, "
                           f"{quantity} {batch.product.unit}. Release decision required.",
                 f"/releases/{release.pk}/")
    return release


def price_band(original_price):
    lo = money(original_price * rule("price_band_min_pct") / 100)
    hi = money(original_price * rule("price_band_max_pct") / 100)
    return lo, hi


def proposed_clearance_price(original_price):
    """FR-31: proposed at the band ceiling (70% by default)."""
    return price_band(original_price)[1]


def validate_price(original_price, price):
    lo, hi = price_band(original_price)
    if price < lo or price > hi:
        raise ValidationError(f"Clearance price must be between {lo:,.2f} and {hi:,.2f} EGP "
                              f"({rule('price_band_min_pct'):.0f}% to "
                              f"{rule('price_band_max_pct'):.0f}% of the original price).")


def listing_blockers(batch, market):
    """Checks before publishing (UC-18 steps 1-2)."""
    ok, reason = registration_check(batch.product, market, batch.expiry_date)
    if not ok:
        return "registration", reason
    if batch.days_to_expiry <= rule_int("min_sellable_days"):
        return "shelf_life", (f"Remaining shelf life is {batch.days_to_expiry} days, at or "
                              f"below the minimum sellable shelf life.")
    return None, ""


@transaction.atomic
def approve_release(user, release, release_qty, clearance_price, comment=""):
    """UC-17 / FR-30, FR-31, then UC-18 publish (FR-32, FR-33)."""
    release = SurplusRelease.objects.select_for_update().get(pk=release.pk)
    if release.status != SurplusRelease.PENDING:
        raise ValidationError("This release request has already been decided.")
    batch = Batch.objects.select_for_update().get(pk=release.batch_id)
    validate_price(batch.product.original_price, clearance_price)
    if release_qty <= 0 or release_qty > release.surplus_qty:
        raise ValidationError("Release quantity must be between 1 and the surplus quantity.")
    if release_qty > free_qty(batch):
        raise ValidationError(f"Only {free_qty(batch)} units are unreserved.")
    kind, reason = listing_blockers(batch, batch.market)
    if kind == "registration":
        notify_roles([Role.REGULATORY, Role.COO], f"Listing blocked for {batch.product.name} "
                                                  f"lot {batch.lot_number}: {reason}",
                     "/registrations/", level="danger")
        raise ValidationError("Listing blocked: " + reason)
    if kind == "shelf_life":
        notify_roles(Role.INVENTORY, f"Lot {batch.lot_number} cannot be listed ({reason}); "
                                     f"review for write-off.",
                     f"/write-offs/new/?batch={batch.pk}", level="warning")
        raise ValidationError("Listing blocked: " + reason)
    release.status = SurplusRelease.APPROVED
    release.decided_by = user
    release.decided_at = timezone.now()
    release.release_qty = release_qty
    release.clearance_price = clearance_price
    release.decision_comment = comment
    release.save()
    listing = Listing.objects.create(release=release, batch=batch, market=batch.market,
                                     listed_qty=release_qty, available_qty=release_qty,
                                     clearance_price=clearance_price,
                                     original_price=batch.product.original_price)
    batch.status = BatchStatus.RELEASED
    batch.save(update_fields=["status"])
    mark_actioned(batch)
    audit(user, AuditLog.APPROVAL, f"Release approved for lot {batch.lot_number}",
          f"RELEASE-{release.pk}", new=f"qty={release_qty} price={clearance_price}")
    audit(user, AuditLog.PRICE_CHANGE, f"Clearance price set for lot {batch.lot_number}",
          f"LISTING-{listing.pk}", old=batch.product.original_price, new=clearance_price)
    _alert_buyers(listing)
    notify(release.flagged_by, f"Release approved: {batch.product.name} lot "
                               f"{batch.lot_number} is listed at {clearance_price:,.2f} EGP.",
           f"/expiry/batch/{batch.pk}/")
    return listing


def _alert_buyers(listing):
    """UC-33 A1: email buyers who asked to hear about new listings."""
    from commerce.models import Buyer
    from commerce.services import b2b_url
    from core.services import email_buyer

    buyers = Buyer.objects.filter(status=Buyer.VERIFIED, listing_alerts=True,
                                  market=listing.market,
                                  categories=listing.batch.product.category_id)
    for buyer in buyers.select_related("user"):
        email_buyer(buyer.user, f"New clearance listing: {listing.batch.product.name}",
                    f"{listing.batch.product.name}, lot {listing.batch.lot_number}, "
                    f"{listing.available_qty} {listing.batch.product.unit} at "
                    f"{listing.clearance_price:,.2f} EGP.\n\n"
                    + b2b_url(f"/catalogue/{listing.pk}/"))


@transaction.atomic
def reject_release(user, release, comment):
    """UC-17 A2."""
    if not comment:
        raise ValidationError("A comment is required to reject a release.")
    if release.status != SurplusRelease.PENDING:
        raise ValidationError("This release request has already been decided.")
    release.status = SurplusRelease.REJECTED
    release.decided_by = user
    release.decided_at = timezone.now()
    release.decision_comment = comment
    release.save()
    batch = release.batch
    if (batch.status == BatchStatus.SURPLUS
            and not batch.releases.filter(status=SurplusRelease.PENDING).exists()):
        batch.status = BatchStatus.NEAR_EXPIRY
        batch.save(update_fields=["status"])
    mark_actioned(batch)
    audit(user, AuditLog.APPROVAL, f"Release rejected for lot {batch.lot_number}",
          f"RELEASE-{release.pk}", new=comment)
    notify(release.flagged_by, f"Release rejected for {batch.product.name} lot "
                               f"{batch.lot_number}: {comment}", f"/expiry/batch/{batch.pk}/")


@transaction.atomic
def reprice_listing(user, listing, new_price):
    """UC-40 / FR-80: within the band, without affecting confirmed orders."""
    if listing.status != Listing.ACTIVE:
        raise ValidationError("Only active listings can be repriced.")
    validate_price(listing.original_price, new_price)
    old = listing.clearance_price
    listing.clearance_price = new_price
    listing.save(update_fields=["clearance_price"])
    audit(user, AuditLog.PRICE_CHANGE, f"Listing #{listing.pk} repriced",
          f"LISTING-{listing.pk}", old=old, new=new_price)


@transaction.atomic
def change_listing_quantity(user, listing, new_listed_qty):
    """UC-40: the listed quantity cannot fall below what is already ordered."""
    listing = Listing.objects.select_for_update().get(pk=listing.pk)
    ordered = listing.listed_qty - listing.available_qty
    if new_listed_qty < ordered:
        raise ValidationError(f"Quantity cannot be below the {ordered} units already ordered.")
    extra = new_listed_qty - listing.listed_qty
    if extra > free_qty(listing.batch):
        raise ValidationError(f"Only {free_qty(listing.batch)} more units are unreserved.")
    old = listing.listed_qty
    listing.listed_qty = new_listed_qty
    listing.available_qty = new_listed_qty - ordered
    listing.status = Listing.ACTIVE if listing.available_qty > 0 else Listing.SOLD_OUT
    listing.save()
    audit(user, AuditLog.PRICE_CHANGE, f"Listing #{listing.pk} quantity changed",
          f"LISTING-{listing.pk}", old=old, new=new_listed_qty)


def withdraw_listing_by_user(user, listing, reason="Withdrawn by Management"):
    """UC-40 A1."""
    withdraw_listing(listing, reason, user)
    notify_roles(Role.INVENTORY, f"Listing for lot {listing.batch.lot_number} withdrawn; the "
                                 f"batch is back to Near-Expiry.",
                 f"/expiry/batch/{listing.batch_id}/")


# --------------------------------------------------------------------------
# Module 4: Disposal and write-off
# --------------------------------------------------------------------------
@transaction.atomic
def request_write_off(user, batch, quantity, reason, disposal_method):
    """UC-21 / FR-36, FR-37."""
    batch = Batch.objects.select_for_update().get(pk=batch.pk)
    blockable = batch.qty_on_hand - batch.qty_reserved - batch.blocked_for_write_off
    if quantity <= 0 or quantity > blockable:
        raise ValidationError(f"Quantity must be between 1 and {blockable} (unreserved, not "
                              f"already blocked).")
    wo = WriteOff.objects.create(batch=batch, quantity=quantity, reason=reason,
                                 disposal_method=disposal_method, requested_by=user)
    batch.blocked_for_write_off += quantity
    batch.save(update_fields=["blocked_for_write_off"])
    # Blocked stock can no longer be offered online (FR-37).
    for listing in batch.listings.filter(status=Listing.ACTIVE):
        if free_qty(batch) < 0:
            withdraw_listing(listing, "Write-off requested", user)
    mark_actioned(batch)
    notify_roles([Role.GM, Role.QA], f"Write-off request {wo} for {batch.product.name} lot "
                                     f"{batch.lot_number} ({quantity} units) awaits approval.",
                 f"/write-offs/{wo.pk}/")
    return wo


@transaction.atomic
def approve_write_off(user, wo):
    """UC-22 / FR-38: both the General Manager and the QA Officer approve."""
    wo = WriteOff.objects.select_for_update().get(pk=wo.pk)
    if wo.status != WriteOff.PENDING:
        raise ValidationError("This write-off is not pending approval.")
    acted = False
    if user.has_role(Role.GM) and not wo.gm_approved_by:
        wo.gm_approved_by, wo.gm_approved_at, acted = user, timezone.now(), True
    elif user.has_role(Role.QA) and not wo.qa_approved_by:
        wo.qa_approved_by, wo.qa_approved_at, acted = user, timezone.now(), True
    if not acted:
        raise ValidationError("You have no outstanding approval on this request.")
    wo.save()
    audit(user, AuditLog.APPROVAL, f"Write-off {wo} approved", str(wo))
    if wo.gm_approved_by and wo.qa_approved_by:
        _post_write_off(user, wo)
    return wo


def _post_write_off(user, wo):
    batch = Batch.objects.select_for_update().get(pk=wo.batch_id)
    _deduct(batch, wo.quantity, user, StockMovement.WRITE_OFF, str(wo))
    batch.blocked_for_write_off = max(0, batch.blocked_for_write_off - wo.quantity)
    _set_depleted_if_empty(batch)
    batch.save()
    wo.write_off_value = money(batch.product.unit_cost * wo.quantity)
    wo.status = WriteOff.APPROVED
    wo.posted_at = timezone.now()
    if batch.purchase_order and batch.purchase_order.plan_id:
        wo.purchase_plan_id = batch.purchase_order.plan_id
    wo.save()
    audit(user, AuditLog.WRITE_OFF, f"Write-off {wo} posted", str(wo),
          new=f"{wo.quantity} units, {wo.write_off_value} EGP")
    notify(wo.requested_by, f"Write-off {wo} posted. Record disposal once complete.",
           f"/write-offs/{wo.pk}/")


@transaction.atomic
def reject_write_off(user, wo, comment):
    """UC-22 A1: the quantity stays blocked until resubmitted or released."""
    if not comment:
        raise ValidationError("A comment is required to reject a write-off.")
    if wo.status != WriteOff.PENDING:
        raise ValidationError("This write-off is not pending approval.")
    wo.status = WriteOff.REJECTED
    wo.rejection_comment = comment
    wo.save()
    audit(user, AuditLog.APPROVAL, f"Write-off {wo} rejected", str(wo), new=comment)
    notify(wo.requested_by, f"Write-off {wo} rejected: {comment}", f"/write-offs/{wo.pk}/",
           level="warning")


@transaction.atomic
def resubmit_write_off(user, wo):
    if wo.status != WriteOff.REJECTED:
        raise ValidationError("Only rejected requests can be resubmitted.")
    wo.status = WriteOff.PENDING
    wo.gm_approved_by = wo.qa_approved_by = None
    wo.gm_approved_at = wo.qa_approved_at = None
    wo.save()
    notify_roles([Role.GM, Role.QA], f"Write-off {wo} resubmitted for approval.",
                 f"/write-offs/{wo.pk}/")


@transaction.atomic
def cancel_write_off(user, wo):
    if wo.status != WriteOff.REJECTED:
        raise ValidationError("Only rejected requests can be cancelled.")
    batch = Batch.objects.select_for_update().get(pk=wo.batch_id)
    batch.blocked_for_write_off = max(0, batch.blocked_for_write_off - wo.quantity)
    batch.save(update_fields=["blocked_for_write_off"])
    wo.status = WriteOff.CANCELLED
    wo.save(update_fields=["status"])


@transaction.atomic
def record_disposal(user, wo, disposal_date, disposal_cost, certificate_ref):
    """UC-22 step 6 / FR-39."""
    if wo.status != WriteOff.APPROVED:
        raise ValidationError("Disposal can only be recorded for a posted write-off.")
    wo.disposal_date = disposal_date
    wo.disposal_cost = disposal_cost
    wo.certificate_ref = certificate_ref
    wo.status = WriteOff.DISPOSED
    wo.save()
    audit(user, AuditLog.WRITE_OFF, f"Disposal recorded for {wo}", str(wo),
          new=f"cost={disposal_cost} cert={certificate_ref}")


def customers_who_bought(product, months=12):
    """UC-15 step 3: customers and regions that bought the product recently."""
    from planning.models import SalesRecord

    latest = SalesRecord.objects.order_by("-period").values_list("period", flat=True).first()
    if not latest:
        return []
    since = latest - timedelta(days=int(30.4 * months))
    return list(SalesRecord.objects.filter(product=product, period__gte=since)
                .values("customer_id", "customer_name", "region__name")
                .annotate(qty=Sum("quantity")).order_by("-qty")[:15])


def batches_for_user(user, qs):
    """Restrict lists to a Sales Representative's categories (UC-15, UC-27 A2)."""
    if user.has_role(Role.SALES) and not user.has_role(
            Role.INVENTORY, Role.GM, Role.MANAGEMENT, Role.COO, Role.ADMIN):
        cats = list(user.categories.values_list("pk", flat=True))
        if cats:
            qs = qs.filter(product__category_id__in=cats)
    return qs



# --------------------------------------------------------------------------
# Purchase orders
# --------------------------------------------------------------------------
@transaction.atomic
def create_purchase_order(user, supplier, lines, expected_date=None, plan=None, number=""):
    """Create a purchase order. ``lines`` is a list of ``(product, quantity)``."""
    from .models import PurchaseOrder, PurchaseOrderLine

    lines = [(p, q) for p, q in lines if p and q and q > 0]
    if not lines:
        raise ValidationError("Add at least one product with a quantity.")
    if number and PurchaseOrder.objects.filter(number=number).exists():
        raise ValidationError(f"Purchase order {number} already exists.")
    po = PurchaseOrder.objects.create(
        number=number or next_number(f"PO-{timezone.localdate():%Y}-", PurchaseOrder),
        supplier=supplier, expected_date=expected_date, plan=plan)
    for product, qty in lines:
        line = PurchaseOrderLine.objects.filter(order=po, product=product).first()
        if line:
            line.quantity += qty
            line.save()
        else:
            PurchaseOrderLine.objects.create(order=po, product=product, quantity=qty)
    audit(user, AuditLog.STOCK, f"Purchase order {po.number} created", po.number,
          new=", ".join(f"{p.sku}×{q}" for p, q in lines))
    notify_roles(Role.QA, f"Purchase order {po.number} from {supplier} expected "
                          f"{expected_date or 'soon'}.", f"/purchase-orders/{po.pk}/")
    return po


@transaction.atomic
def purchase_orders_from_plan(user, plan):
    """One purchase order per supplier for the approved lines of a plan (FR-40)."""
    from planning.models import PurchasePlan, PurchasePlanLine

    if plan.status != PurchasePlan.APPROVED:
        raise ValidationError("Only approved plans can be turned into purchase orders.")
    if plan.purchase_orders.exists():
        raise ValidationError("Purchase orders already exist for this plan.")
    by_supplier = {}
    for line in plan.lines.filter(status=PurchasePlanLine.APPROVED, final_qty__gt=0) \
            .select_related("product__supplier"):
        if line.product.supplier is None:
            raise ValidationError(f"{line.product.name} has no supplier; set one first.")
        by_supplier.setdefault(line.product.supplier, []).append((line.product, line.final_qty))
    if not by_supplier:
        raise ValidationError("The plan has no approved lines with a quantity.")
    today = timezone.localdate()
    pos = []
    for supplier, lines in by_supplier.items():
        lead = max(p.lead_time_days for p, _q in lines)
        pos.append(create_purchase_order(user, supplier, lines,
                                         expected_date=today + timedelta(days=lead), plan=plan))
    return pos


@transaction.atomic
def close_purchase_order(user, po):
    from .models import PurchaseOrder

    po.status = PurchaseOrder.CLOSED
    po.save(update_fields=["status"])
    audit(user, AuditLog.STOCK, f"Purchase order {po.number} closed", po.number)


# --------------------------------------------------------------------------
# Post to B2B (one entry point for UC-16 to UC-18)
# --------------------------------------------------------------------------
def b2b_candidates():
    """Near-expiry batches and whether each can be posted to the platform.

    Returns ``(eligible, blocked)`` lists of ``(batch, free_qty, reason)``."""
    eligible, blocked = [], []
    qs = (Batch.objects.filter(status__in=[BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS,
                                           BatchStatus.RELEASED], qty_on_hand__gt=0)
          .select_related("product__category", "market").order_by("expiry_date"))
    for b in qs:
        pending = b.releases.filter(status=SurplusRelease.PENDING).aggregate(
            t=Sum("surplus_qty"))["t"] or 0
        free = free_qty(b) - pending
        if not b.product.is_expiring:
            blocked.append((b, free, "Non-expiring products are not listed"))
            continue
        kind, reason = listing_blockers(b, b.market)
        if kind:
            blocked.append((b, free, reason))
        elif free <= 0:
            blocked.append((b, free, "All stock is already listed, reserved or requested"))
        else:
            eligible.append((b, free, ""))
    return eligible, blocked


@transaction.atomic
def post_to_b2b(user, batch, quantity, reason, comment="", price=None):
    """COO: flag and publish in one step. Sales: send a release request to the COO."""
    release = flag_surplus(user, batch, quantity, reason, comment)
    if user.has_role(Role.COO):
        if price is None:
            price = proposed_clearance_price(batch.product.original_price)
        return release, approve_release(user, release, quantity, price,
                                        "Posted directly by the COO")
    return release, None
