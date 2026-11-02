"""Buyer side of returns and credit notes (UC-48, UC-52, UC-53)."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from commerce import orders as svc
from commerce.documents import credit_note_pdf, packing_slip_pdf
from commerce.models import CreditNote, PlatformOrder, ReturnLine
from core.rules import rule_int

from .views import _err, _order_for, buyer_required


@buyer_required()
def request_return(request, pk):
    order = _order_for(request, pk)
    if not svc.can_request_return(order):
        messages.info(request, "This order can no longer be returned online. Contact us if "
                               "something is wrong.")
        return redirect("b2b:order", pk=pk)
    lines = [line for line in order.lines.select_related("batch__product")
             if line.returnable_qty]
    if request.method == "POST":
        items = []
        try:
            for line in lines:
                qty = int(request.POST.get(f"qty_{line.pk}") or 0)
                items.append((line, qty, request.POST.get(f"reason_{line.pk}", "")))
            rr = svc.request_return(request.buyer, order, items,
                                    request.POST.get("comment", "").strip(),
                                    request.FILES.get("photo"))
            messages.success(request, f"Return {rr.number} sent. We will reply within one "
                                      f"working day.")
            return redirect("b2b:account_returns")
        except (ValidationError, ValueError) as e:
            _err(request, e)
    return render(request, "b2b/return_request.html", {
        "order": order, "lines": lines, "reasons": ReturnLine.REASONS,
        "deadline": svc.return_deadline(order), "days": rule_int("return_window_days"),
        "buyer": request.buyer, "tab": "returns"})


@buyer_required()
def account_returns(request):
    buyer = request.buyer
    return render(request, "b2b/account/returns.html", {
        "buyer": buyer, "tab": "returns",
        "returns": buyer.returns.select_related("order", "replacement_order")
                        .prefetch_related("lines__batch__product"),
        "credit_notes": buyer.credit_notes.select_related("invoice", "order")})


@buyer_required()
def packing_slip(request, pk):
    order = _order_for(request, pk)
    if order.status not in (PlatformOrder.DISPATCHED, PlatformOrder.DELIVERED):
        raise Http404
    resp = HttpResponse(packing_slip_pdf(order), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="packing-slip-{order.reference}.pdf"'
    return resp


@buyer_required()
def credit_note(request, pk):
    note = get_object_or_404(CreditNote, pk=pk, buyer=request.buyer)
    resp = HttpResponse(credit_note_pdf(note), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{note.number}.pdf"'
    return resp
