"""Order exceptions after checkout: holds, line cancellation, returns,
credit notes and the exception queue (FR-90 to FR-102, UC-45 to UC-53)."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from core.models import AuditLog, Role
from core.rules import rule_int
from core.services import audit, email_buyer, notify_roles
from inventory.models import Batch, BatchStatus, BatchStock, Listing, StockMovement

from .models import (CreditNote, Invoice, OrderHold, OrderLine, PlatformOrder, ReturnLine,
                     ReturnRequest)
from .services import b2b_url, next_reference, store_encrypted, validate_upload

TWO = Decimal("0.01")


# --------------------------------------------------------------------------
# UC-45: holds
# --------------------------------------------------------------------------
@transaction.atomic
def hold_order(user, order, reason, note=""):
    """FR-90: a held order cannot be confirmed or dispatched, and its stock
    stays reserved while the hold lasts."""
    order = PlatformOrder.objects.select_for_update().get(pk=order.pk)
    if not order.can_change:
        raise ValidationError("Only orders that have not left the warehouse can be held.")
    if order.on_hold:
        raise ValidationError("This order is already on hold.")
    if reason not in dict(OrderHold.REASONS):
        raise ValidationError("Choose a reason for the hold.")
    if reason == "other" and not note:
        raise ValidationError("Describe the reason for the hold.")
    hold = OrderHold.objects.create(order=order, reason=reason, note=note, placed_by=user)
    order.on_hold = True
    order.hold_reason = str(hold)
    order.save(update_fields=["on_hold", "hold_reason"])
    audit(user, AuditLog.ORDER, f"Order {order.reference} put on hold", order.reference,
          new=str(hold))
    email_buyer(order.buyer.user, f"Order {order.reference} is on hold",
                f"Your order {order.reference} is on hold while we check: "
                f"{hold.get_reason_display().lower()}. We will contact you if we need anything."
                f"\n\n" + b2b_url(f"/orders/{order.pk}/"))
    return hold


@transaction.atomic
def release_hold(user, order, comment=""):
    """FR-91. The reservation clock restarts so the buyer is not penalised for
    time spent on hold."""
    order = PlatformOrder.objects.select_for_update().get(pk=order.pk)
    hold = order.active_hold
    if hold is None:
        raise ValidationError("This order is not on hold.")
    hold.released_by = user
    hold.released_at = timezone.now()
    hold.release_comment = comment
    hold.save()
    order.on_hold = False
    order.hold_reason = ""
    if order.status in (PlatformOrder.PLACED, PlatformOrder.AWAITING_PAYMENT):
        fresh = timezone.now() + timedelta(hours=rule_int("reservation_hold_hours"))
        if not order.reservation_expires_at or order.reservation_expires_at < fresh:
            order.reservation_expires_at = fresh
    order.save()
    audit(user, AuditLog.ORDER, f"Hold released on {order.reference}", order.reference,
          new=comment)
    email_buyer(order.buyer.user, f"Order {order.reference} is moving again",
                f"The hold on your order {order.reference} has been released.\n\n"
                + b2b_url(f"/orders/{order.pk}/"))
    return order


# --------------------------------------------------------------------------
# UC-46: cancel order lines
# --------------------------------------------------------------------------
@transaction.atomic
def cancel_lines(user, order, quantities, reason):
    """FR-92, FR-93. ``quantities`` maps OrderLine id to the quantity to cancel.

    The stock goes back to its listing, the total is recalculated, and an
    invoiced order gets a credit note for the cancelled value."""
    order = PlatformOrder.objects.select_for_update().get(pk=order.pk)
    if not order.can_change:
        raise ValidationError("Lines can only be cancelled before the order is dispatched.")
    if not reason:
        raise ValidationError("Give a reason for the cancellation.")
    lines = {line.pk: line for line in order.lines.select_for_update()}
    cancelled_value = Decimal(0)
    changed = []
    for line_id, qty in quantities.items():
        line = lines.get(int(line_id))
        if line is None or not qty:
            continue
        if qty < 0 or qty > line.active_qty:
            raise ValidationError(f"You can cancel at most {line.active_qty} of "
                                  f"{line.batch.product.name}.")
        Listing.objects.filter(pk=line.listing_id).update(
            available_qty=F("available_qty") + qty)
        Listing.objects.filter(pk=line.listing_id, status=Listing.SOLD_OUT,
                               available_qty__gt=0).update(status=Listing.ACTIVE)
        Batch.objects.filter(pk=line.batch_id).update(qty_reserved=F("qty_reserved") - qty)
        line.cancelled_qty += qty
        line.cancel_reason = reason
        line.save(update_fields=["cancelled_qty", "cancel_reason"])
        cancelled_value += line.unit_price * qty
        changed.append(f"{line.batch.product.name} × {qty}")
    if not changed:
        raise ValidationError("Enter a quantity to cancel on at least one line.")
    order.recalculate_total()
    if not any(line.active_qty for line in order.lines.all()):
        order.status = PlatformOrder.CANCELLED
        order.cancelled_at = timezone.now()
        order.cancel_reason = reason
    order.save()
    audit(user, AuditLog.ORDER, f"Lines cancelled on {order.reference}", order.reference,
          new=f"{'; '.join(changed)} – {reason}")
    invoice = Invoice.objects.filter(order=order).first()
    if invoice and cancelled_value > 0:
        issue_credit_note(user, invoice, cancelled_value, f"Cancelled before dispatch: {reason}")
    email_buyer(order.buyer.user, f"Order {order.reference} changed",
                f"These items were cancelled: {'; '.join(changed)}.\nReason: {reason}\n"
                f"New order total: {order.total:,.2f} EGP.\n\n"
                + b2b_url(f"/orders/{order.pk}/"))
    return order


# --------------------------------------------------------------------------
# UC-52: credit notes
# --------------------------------------------------------------------------
@transaction.atomic
def issue_credit_note(user, invoice, amount, reason, return_request=None):
    """FR-100: set off against an unpaid invoice, or owed as a bank refund."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    amount = Decimal(amount).quantize(TWO)
    already = invoice.credit_notes.aggregate(t=Sum("amount"))["t"] or 0
    if amount <= 0:
        raise ValidationError("The credit amount must be more than zero.")
    if amount + already > invoice.amount:
        raise ValidationError(f"Credit notes cannot exceed the invoice: at most "
                              f"{invoice.amount - already:,.2f} EGP is left.")
    if not reason:
        raise ValidationError("Give a reason for the credit note.")
    if invoice.status == Invoice.UNPAID:
        invoice.credited += amount
        if invoice.balance <= 0:
            invoice.status = Invoice.PAID
            invoice.paid_at = timezone.now()
        invoice.save()
        status = CreditNote.SETTLED
    else:
        status = CreditNote.REFUND_DUE
    note = CreditNote.objects.create(
        number=next_reference("CN-", CreditNote, "number"), invoice=invoice,
        buyer=invoice.buyer, order=invoice.order, return_request=return_request,
        amount=amount, reason=reason, status=status, issued_by=user)
    audit(user, AuditLog.APPROVAL, f"Credit note {note.number} issued", note.number,
          new=f"{amount} EGP – {note.get_status_display()} – {reason}")
    if status == CreditNote.REFUND_DUE:
        notify_roles(Role.FINANCE, f"Refund of {amount:,.2f} EGP due to "
                                   f"{invoice.buyer.company_name} ({note.number}).",
                     "/b2b/credit-notes/", level="warning")
    email_buyer(invoice.buyer.user, f"Credit note {note.number}",
                f"We issued credit note {note.number} for {amount:,.2f} EGP against invoice "
                f"{invoice.number}.\n"
                + ("It has been deducted from what you owe." if status == CreditNote.SETTLED
                   else "We will refund it to your bank account.")
                + "\n\n" + b2b_url("/account/returns/"))
    return note


@transaction.atomic
def record_refund(user, note, reference):
    if note.status != CreditNote.REFUND_DUE:
        raise ValidationError("This credit note has no refund due.")
    if not reference:
        raise ValidationError("Enter the bank transfer reference of the refund.")
    note.status = CreditNote.REFUNDED
    note.refunded_at = timezone.now()
    note.refund_reference = reference
    note.save()
    audit(user, AuditLog.APPROVAL, f"Refund paid for {note.number}", note.number,
          new=reference)
    email_buyer(note.buyer.user, f"Refund for {note.number} sent",
                f"We transferred {note.amount:,.2f} EGP to your bank account "
                f"(reference {reference}).")


# --------------------------------------------------------------------------
# UC-48 to UC-51: returns
# --------------------------------------------------------------------------
def return_deadline(order):
    if not order.delivered_at:
        return None
    return order.delivered_at + timedelta(days=rule_int("return_window_days"))


def can_request_return(order):
    deadline = return_deadline(order)
    return (order.status == PlatformOrder.DELIVERED and deadline is not None
            and timezone.now() <= deadline
            and any(line.returnable_qty for line in order.lines.all()))


@transaction.atomic
def request_return(buyer, order, items, comment="", photo=None):
    """FR-95. ``items`` is a list of ``(OrderLine, quantity, reason)``."""
    if order.buyer_id != buyer.pk:
        raise ValidationError("This order belongs to another account.")
    if order.status != PlatformOrder.DELIVERED:
        raise ValidationError("Returns can be requested only for delivered orders.")
    if timezone.now() > return_deadline(order):
        raise ValidationError(f"The {rule_int('return_window_days')}-day return window for "
                              f"this order has closed.")
    items = [(line, qty, reason) for line, qty, reason in items if qty]
    if not items:
        raise ValidationError("Enter the quantity to return on at least one line.")
    for line, qty, reason in items:
        if line.order_id != order.pk:
            raise ValidationError("That item is not part of this order.")
        if qty < 0 or qty > line.returnable_qty:
            raise ValidationError(f"You can return at most {line.returnable_qty} of "
                                  f"{line.batch.product.name}.")
        if reason not in dict(ReturnLine.REASONS):
            raise ValidationError("Choose a reason for each item.")
    rr = ReturnRequest(number=next_reference("RMA-", ReturnRequest, "number"), order=order,
                       buyer=buyer, buyer_comment=comment[:500])
    if photo:
        validate_upload(photo)
        store_encrypted(rr, "photo", photo)
        rr.photo_name = photo.name[:200]
        rr.photo_type = getattr(photo, "content_type", "") or "application/octet-stream"
    rr.save()
    for line, qty, reason in items:
        ReturnLine.objects.create(request=rr, order_line=line, batch=line.batch,
                                  qty_requested=qty, reason=reason)
    audit(buyer.user, AuditLog.ORDER, f"Return {rr.number} requested", rr.number,
          new=", ".join(f"{line.batch.lot_number}×{qty}" for line, qty, _r in items))
    notify_roles(Role.FINANCE, f"Return {rr.number} requested by {buyer.company_name} on "
                               f"{order.reference}.", f"/b2b/returns/{rr.pk}/", level="warning")
    email_buyer(buyer.user, f"Return {rr.number} received",
                f"We received your return request {rr.number}. Please keep the goods until we "
                f"confirm how to send them back.\n\n" + b2b_url("/account/returns/"))
    return rr


def _reserve_replacement(user, rr):
    """FR-97: a free replacement from the same listing, or None if any line
    can no longer be covered."""
    taken = []
    for rl in rr.lines.select_related("order_line__listing"):
        listing = rl.order_line.listing
        ok = Listing.objects.filter(pk=listing.pk, status=Listing.ACTIVE,
                                    available_qty__gte=rl.qty_requested).update(
            available_qty=F("available_qty") - rl.qty_requested)
        if not ok:
            for got, qty in taken:
                Listing.objects.filter(pk=got.pk).update(available_qty=F("available_qty") + qty)
            return None
        taken.append((listing, rl.qty_requested))
    order = rr.order
    replacement = PlatformOrder.objects.create(
        reference=next_reference("KB-", PlatformOrder, "reference"), buyer=rr.buyer,
        status=PlatformOrder.CONFIRMED, total=0, delivery_address=order.delivery_address,
        cold_chain_confirmed=order.cold_chain_confirmed, replacement_for=order,
        confirmed_by=user, confirmed_at=timezone.now())
    for listing, qty in taken:
        Listing.objects.filter(pk=listing.pk, available_qty__lte=0).update(
            status=Listing.SOLD_OUT)
        Batch.objects.filter(pk=listing.batch_id).update(qty_reserved=F("qty_reserved") + qty)
        OrderLine.objects.create(order=replacement, listing=listing, batch=listing.batch,
                                 quantity=qty, unit_price=0)
    notify_roles(Role.WAREHOUSE, f"Replacement order {replacement.reference} (for "
                                 f"{order.reference}) is ready for dispatch.",
                 f"/dispatch/{replacement.pk}/")
    return replacement


@transaction.atomic
def decide_return(user, rr, approve, resolution="", comment=""):
    """FR-96, FR-97."""
    rr = ReturnRequest.objects.select_for_update().get(pk=rr.pk)
    if rr.status != ReturnRequest.REQUESTED:
        raise ValidationError("This return has already been decided.")
    rr.decided_by, rr.decided_at, rr.decision_comment = user, timezone.now(), comment
    if not approve:
        if not comment:
            raise ValidationError("Give the buyer a reason for the rejection.")
        rr.status = ReturnRequest.REJECTED
        rr.save()
        audit(user, AuditLog.APPROVAL, f"Return {rr.number} rejected", rr.number, new=comment)
        email_buyer(rr.buyer.user, f"Return {rr.number} not accepted", f"Reason: {comment}")
        return rr
    if resolution not in (ReturnRequest.CREDIT, ReturnRequest.REPLACEMENT):
        raise ValidationError("Choose a credit note or a replacement.")
    note = ""
    if resolution == ReturnRequest.REPLACEMENT:
        rr.replacement_order = _reserve_replacement(user, rr)
        if rr.replacement_order is None:
            resolution = ReturnRequest.CREDIT
            note = " The listing no longer has enough stock, so a credit note will be issued."
    rr.resolution = resolution
    rr.status = ReturnRequest.APPROVED
    rr.save()
    audit(user, AuditLog.APPROVAL, f"Return {rr.number} approved", rr.number,
          new=rr.get_resolution_display() + (f" ({comment})" if comment else ""))
    notify_roles(Role.WAREHOUSE, f"Return {rr.number} approved: expect the goods back from "
                                 f"{rr.buyer.company_name}.", f"/b2b/returns/{rr.pk}/")
    email_buyer(rr.buyer.user, f"Return {rr.number} approved",
                f"Your return is approved. Resolution: {rr.get_resolution_display()}."
                f"{note}\nOur driver will collect the goods, or you can bring them to the Cairo "
                f"Main Warehouse quoting {rr.number}. Keep cold-chain items refrigerated."
                + (f"\nYour replacement is order {rr.replacement_order.reference}."
                   if rr.replacement_order else "")
                + "\n\n" + b2b_url("/account/returns/"))
    return rr


@transaction.atomic
def receive_return(user, rr, received):
    """FR-98. ``received`` maps ReturnLine id to the quantity received."""
    rr = ReturnRequest.objects.select_for_update().get(pk=rr.pk)
    if rr.status != ReturnRequest.APPROVED:
        raise ValidationError("Only approved returns can be received.")
    total = 0
    for rl in rr.lines.all():
        qty = int(received.get(rl.pk) or 0)
        if qty < 0 or qty > rl.qty_requested:
            raise ValidationError(f"Received quantity for lot {rl.batch.lot_number} must be "
                                  f"between 0 and {rl.qty_requested}.")
        rl.qty_received = qty
        rl.save(update_fields=["qty_received"])
        total += qty
    if total == 0:
        raise ValidationError("Enter the quantity received on at least one line.")
    rr.status = ReturnRequest.RECEIVED
    rr.received_by, rr.received_at = user, timezone.now()
    rr.save()
    audit(user, AuditLog.STOCK, f"Return {rr.number} received", rr.number, new=f"{total} units")
    notify_roles(Role.QA, f"Returned stock {rr.number} is waiting for inspection.",
                 f"/b2b/returns/{rr.pk}/", level="warning")
    return rr


@transaction.atomic
def inspect_return(user, rr, decisions):
    """FR-99: nothing returned reaches sellable stock without QA inspection.

    ``decisions`` maps ReturnLine id to ``(outcome, BinLocation)``."""
    from inventory import services as inv

    rr = ReturnRequest.objects.select_for_update().get(pk=rr.pk)
    if rr.status != ReturnRequest.RECEIVED:
        raise ValidationError("Only received returns can be inspected.")
    credit = Decimal(0)
    for rl in rr.lines.select_related("batch__product", "order_line"):
        if not rl.qty_received:
            continue
        outcome, location = decisions.get(rl.pk, (None, None))
        if outcome not in (ReturnLine.RESTOCK, ReturnLine.WRITE_OFF):
            raise ValidationError(f"Choose restock or write off for lot {rl.batch.lot_number}.")
        batch = Batch.objects.select_for_update().get(pk=rl.batch_id)
        if outcome == ReturnLine.RESTOCK and batch.days_to_expiry < 0:
            raise ValidationError(f"Lot {batch.lot_number} has expired and must be written off.")
        if location is None:
            raise ValidationError(f"Choose a location for lot {rl.batch.lot_number}.")
        if not inv.storage_compatible(batch.storage_condition, location.storage_type):
            raise ValidationError(f"{location} cannot hold "
                                  f"{batch.get_storage_condition_display()} stock.")
        stock, _ = BatchStock.objects.get_or_create(batch=batch, location=location)
        stock.quantity += rl.qty_received
        stock.save()
        batch.recompute_on_hand()
        if batch.status == BatchStatus.DEPLETED:
            batch.status = BatchStatus.IN_STOCK
        batch.save()
        StockMovement.objects.create(batch=batch, movement_type=StockMovement.RETURN,
                                     quantity=rl.qty_received, to_location=location, user=user,
                                     reference=rr.number)
        if batch.status == BatchStatus.IN_STOCK:
            inv.evaluate_batch(batch)
        if outcome == ReturnLine.WRITE_OFF:
            reason = "temperature" if rl.reason == "cold_chain" else "damaged"
            inv.request_write_off(user, batch, rl.qty_received, reason, "destruction")
        rl.outcome, rl.location = outcome, location
        rl.save(update_fields=["outcome", "location"])
        credit += rl.order_line.unit_price * rl.qty_received
    rr.status = ReturnRequest.COMPLETED
    rr.inspected_by, rr.inspected_at = user, timezone.now()
    rr.save()
    audit(user, AuditLog.STOCK, f"Return {rr.number} inspected", rr.number,
          new=", ".join(f"{rl.batch.lot_number}:{rl.get_outcome_display()}"
                        for rl in rr.lines.all() if rl.outcome))
    invoice = Invoice.objects.filter(order=rr.order).first()
    if rr.resolution == ReturnRequest.CREDIT and invoice and credit > 0:
        left = invoice.amount - (invoice.credit_notes.aggregate(t=Sum("amount"))["t"] or 0)
        if min(credit, left) > 0:
            issue_credit_note(user, invoice, min(credit, left), f"Return {rr.number}", rr)
    email_buyer(rr.buyer.user, f"Return {rr.number} completed",
                "We received and inspected your returned goods."
                + (" A credit note has been issued." if rr.resolution == ReturnRequest.CREDIT
                   else "") + "\n\n" + b2b_url("/account/returns/"))
    return rr


# --------------------------------------------------------------------------
# UC-47: exception queue
# --------------------------------------------------------------------------
def exceptions():
    """FR-94: one list of every order that needs a person to act."""
    now = timezone.now()
    soon = now + timedelta(hours=rule_int("exception_reservation_hours"))
    rows = []
    for o in PlatformOrder.objects.filter(on_hold=True).select_related("buyer"):
        hold = o.active_hold
        rows.append({"kind": "On hold", "level": "warn", "order": o,
                     "detail": str(hold) if hold else o.hold_reason,
                     "since": hold.placed_at if hold else o.created_at,
                     "owner": "Finance / Warehouse", "url": f"/b2b/orders/{o.pk}/"})
    for o in PlatformOrder.objects.filter(
            status__in=[PlatformOrder.PLACED, PlatformOrder.AWAITING_PAYMENT], on_hold=False,
            reservation_expires_at__lte=soon).select_related("buyer"):
        rows.append({"kind": "Reservation ending", "level": "warn", "order": o,
                     "detail": f"Stock released at {timezone.localtime(o.reservation_expires_at):%d %b %H:%M}"
                               + (" – receipt waiting" if o.status == PlatformOrder.AWAITING_PAYMENT
                                  else " – no payment yet"),
                     "since": o.created_at, "owner": "Finance",
                     "url": f"/payments/{o.pk}/" if o.status == PlatformOrder.AWAITING_PAYMENT
                     else f"/b2b/orders/{o.pk}/"})
    for inv in Invoice.objects.filter(status=Invoice.UNPAID,
                                      due_date__lt=timezone.localdate()).select_related(
            "order__buyer"):
        rows.append({"kind": "Payment overdue", "level": "bad", "order": inv.order,
                     "detail": f"Invoice {inv.number}: {inv.balance:,.2f} EGP due "
                               f"{inv.due_date:%d %b}",
                     "since": inv.issue_date, "owner": "Finance",
                     "url": f"/b2b/orders/{inv.order_id}/"})
    for o in PlatformOrder.objects.exclude(delivery_issue="").filter(
            status__in=[PlatformOrder.DISPATCHED, PlatformOrder.DELIVERED],
            dispatched_at__gte=now - timedelta(days=30)).select_related("buyer"):
        rows.append({"kind": "Delivery problem", "level": "bad", "order": o,
                     "detail": o.delivery_issue.splitlines()[-1][:120],
                     "since": o.dispatched_at, "owner": "Warehouse",
                     "url": f"/b2b/orders/{o.pk}/"})
    labels = {ReturnRequest.REQUESTED: ("Return to decide", "Finance"),
              ReturnRequest.APPROVED: ("Return to receive", "Warehouse"),
              ReturnRequest.RECEIVED: ("Return to inspect", "QA")}
    for rr in ReturnRequest.objects.filter(status__in=list(labels)).select_related(
            "order__buyer"):
        kind, owner = labels[rr.status]
        rows.append({"kind": kind, "level": "info", "order": rr.order,
                     "detail": f"{rr.number} · {rr.lines.count()} item(s) · "
                               f"{rr.value:,.2f} EGP",
                     "since": rr.requested_at, "owner": owner, "url": f"/b2b/returns/{rr.pk}/"})
    for cn in CreditNote.objects.filter(status=CreditNote.REFUND_DUE).select_related(
            "order__buyer"):
        rows.append({"kind": "Refund to pay", "level": "warn", "order": cn.order,
                     "detail": f"{cn.number}: {cn.amount:,.2f} EGP", "since": cn.issued_at,
                     "owner": "Finance", "url": f"/b2b/orders/{cn.order_id}/"})
    order = {"bad": 0, "warn": 1, "info": 2}
    return sorted(rows, key=lambda r: order[r["level"]])
