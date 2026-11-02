"""Order exceptions after checkout (UC-45 to UC-53): the order page with its
actions, the exception queue, returns and credit notes."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from commerce import orders as svc
from commerce.documents import credit_note_pdf, packing_slip_pdf
from commerce.models import (CreditNote, Invoice, OrderHold, PlatformOrder, ReturnLine,
                             ReturnRequest)
from commerce.services import read_encrypted
from core.models import BinLocation, Role

from ..access import ALL_INTERNAL, role_required
from .stock import _err

HOLDERS = (Role.FINANCE, Role.WAREHOUSE, Role.COO)
EXCEPTION_VIEWERS = (Role.FINANCE, Role.WAREHOUSE, Role.QA, Role.COO, Role.GM, Role.MANAGEMENT)
RETURN_VIEWERS = EXCEPTION_VIEWERS + (Role.SALES, Role.INVENTORY)
CREDIT_VIEWERS = (Role.FINANCE, Role.GM, Role.COO, Role.MANAGEMENT)


def _pdf(content, name):
    resp = HttpResponse(content, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{name}"'
    return resp


@role_required(*ALL_INTERNAL)
def order_detail(request, pk):
    order = get_object_or_404(PlatformOrder.objects.select_related("buyer", "replacement_for"),
                              pk=pk)
    user = request.user
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "hold" and user.has_role(*HOLDERS):
                svc.hold_order(user, order, request.POST.get("reason", ""),
                               request.POST.get("note", "").strip())
                messages.success(request, "Order put on hold. The buyer has been told.")
            elif action == "release" and user.has_role(*HOLDERS):
                svc.release_hold(user, order, request.POST.get("comment", "").strip())
                messages.success(request, "Hold released.")
            elif action == "cancel_lines" and user.has_role(Role.FINANCE):
                qty = {line.pk: int(request.POST.get(f"cancel_{line.pk}") or 0)
                       for line in order.lines.all()}
                svc.cancel_lines(user, order, qty, request.POST.get("reason", "").strip())
                messages.success(request, "Lines cancelled and the stock returned to the "
                                          "listing.")
            elif action == "credit" and user.has_role(Role.FINANCE):
                try:
                    amount = Decimal(request.POST.get("amount") or "0")
                except InvalidOperation:
                    raise ValidationError("Enter the amount in EGP.") from None
                note = svc.issue_credit_note(user, order.invoice, amount,
                                             request.POST.get("reason", "").strip())
                messages.success(request, f"Credit note {note.number} issued.")
            else:
                messages.error(request, "Your role cannot do that on this order.")
            return redirect("ims:platform_order", pk=pk)
        except ValidationError as e:
            _err(request, e)
    invoice = Invoice.objects.filter(order=order).first()
    return render(request, "ims/b2b/order.html", {
        "order": order, "lines": order.lines.select_related("batch__product"),
        "payments": order.payments.all(), "invoice": invoice,
        "holds": order.holds.select_related("placed_by", "released_by"),
        "hold_reasons": OrderHold.REASONS, "returns": order.returns.all(),
        "credit_notes": order.credit_notes.all(), "replacements": order.replacements.all(),
        "can_hold": user.has_role(*HOLDERS) and order.can_change,
        "can_cancel": user.has_role(Role.FINANCE) and order.can_change,
        "can_credit": user.has_role(Role.FINANCE) and invoice is not None})


@role_required(*ALL_INTERNAL)
def packing_slip(request, pk):
    order = get_object_or_404(PlatformOrder, pk=pk)
    return _pdf(packing_slip_pdf(order), f"packing-slip-{order.reference}.pdf")


@role_required(*EXCEPTION_VIEWERS)
def exception_queue(request):
    rows = svc.exceptions()
    kinds = sorted({r["kind"] for r in rows})
    kind = request.GET.get("kind", "")
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    return render(request, "ims/b2b/exceptions.html", {"rows": rows, "kinds": kinds,
                                                       "kind": kind})


@role_required(*RETURN_VIEWERS)
def returns(request):
    status = request.GET.get("status", "open")
    qs = ReturnRequest.objects.select_related("order", "buyer")
    if status == "open":
        qs = qs.filter(status__in=[ReturnRequest.REQUESTED, ReturnRequest.APPROVED,
                                   ReturnRequest.RECEIVED])
    elif status != "all":
        qs = qs.filter(status=status)
    return render(request, "ims/b2b/returns.html", {"items": qs[:200], "status": status,
                                                    "statuses": ReturnRequest.STATUSES})


@role_required(*RETURN_VIEWERS)
def return_detail(request, pk):
    rr = get_object_or_404(ReturnRequest.objects.select_related("order", "buyer"), pk=pk)
    user = request.user
    lines = list(rr.lines.select_related("batch__product", "order_line", "location"))
    if request.method == "POST":
        try:
            if "decide" in request.POST and user.has_role(Role.FINANCE):
                approve = request.POST["decide"] == "approve"
                svc.decide_return(user, rr, approve, request.POST.get("resolution", ""),
                                  request.POST.get("comment", "").strip())
                messages.success(request, "Decision saved and the buyer told.")
            elif "receive" in request.POST and user.has_role(Role.WAREHOUSE):
                svc.receive_return(user, rr, {rl.pk: request.POST.get(f"received_{rl.pk}")
                                              for rl in lines})
                messages.success(request, "Receipt recorded. QA has been asked to inspect.")
            elif "inspect" in request.POST and user.has_role(Role.QA):
                decisions = {}
                for rl in lines:
                    loc = BinLocation.objects.filter(
                        pk=request.POST.get(f"location_{rl.pk}") or 0).first()
                    decisions[rl.pk] = (request.POST.get(f"outcome_{rl.pk}"), loc)
                svc.inspect_return(user, rr, decisions)
                messages.success(request, "Inspection saved. Stock and credit updated.")
            else:
                messages.error(request, "Your role cannot do that step.")
            return redirect("ims:return_detail", pk=pk)
        except (ValidationError, ValueError) as e:
            _err(request, e)
    for rl in lines:
        rl.locations = [b for b in BinLocation.objects.filter(is_active=True).select_related(
            "warehouse") if (b.storage_type == rl.batch.storage_condition
                             or rl.batch.storage_condition == "ambient"
                             and b.storage_type != "frozen")]
        first = rl.batch.locations().first()
        rl.default_location = first.location_id if first else None
    steps = [("Requested", True), ("Decided", rr.status != ReturnRequest.REQUESTED),
             ("Received", rr.status in (ReturnRequest.RECEIVED, ReturnRequest.COMPLETED)),
             ("Inspected", rr.status == ReturnRequest.COMPLETED)]
    return render(request, "ims/b2b/return.html", {
        "rr": rr, "lines": lines, "steps": steps, "outcomes": ReturnLine.OUTCOMES,
        "credit_notes": rr.credit_notes.all(),
        "can_decide": user.has_role(Role.FINANCE) and rr.status == ReturnRequest.REQUESTED,
        "can_receive": user.has_role(Role.WAREHOUSE) and rr.status == ReturnRequest.APPROVED,
        "can_inspect": user.has_role(Role.QA) and rr.status == ReturnRequest.RECEIVED})


@role_required(Role.FINANCE, Role.QA, Role.WAREHOUSE)
def return_photo(request, pk):
    rr = get_object_or_404(ReturnRequest, pk=pk)
    if not rr.photo:
        return redirect("ims:return_detail", pk=pk)
    resp = HttpResponse(read_encrypted(rr.photo), content_type=rr.photo_type)
    resp["Content-Disposition"] = f'inline; filename="{rr.photo_name}"'
    return resp


@role_required(*CREDIT_VIEWERS)
def credit_notes(request):
    if request.method == "POST" and request.user.has_role(Role.FINANCE):
        note = get_object_or_404(CreditNote, pk=request.POST.get("note"))
        try:
            svc.record_refund(request.user, note, request.POST.get("reference", "").strip())
            messages.success(request, f"Refund recorded for {note.number}.")
        except ValidationError as e:
            _err(request, e)
        return redirect("ims:credit_notes")
    status = request.GET.get("status", "")
    qs = CreditNote.objects.select_related("buyer", "order", "invoice", "return_request")
    if status:
        qs = qs.filter(status=status)
    return render(request, "ims/b2b/credit_notes.html", {
        "items": qs[:200], "status": status, "statuses": CreditNote.STATUSES,
        "can_refund": request.user.has_role(Role.FINANCE)})


@role_required(*CREDIT_VIEWERS)
def credit_note_document(request, pk):
    note = get_object_or_404(CreditNote, pk=pk)
    return _pdf(credit_note_pdf(note), f"{note.number}.pdf")
