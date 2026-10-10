"""Module 1: inspection, receipt, search, transfer, dispatch (UC-04 to UC-09)."""
from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from commerce.models import PlatformOrder
from core.models import BinLocation, Role
from inventory import services as inv
from inventory.models import Batch, BatchStatus, BatchStock, FieldDispatch

from ..access import role_required
from ..forms import (DispatchForm, FieldDispatchForm, InspectionForm, ReceiptForm,
                     StockSearchForm, TransferForm)


def _err(request, exc):
    for m in getattr(exc, "messages", [str(exc)]):
        messages.error(request, m)


@role_required(Role.QA)
def inspections(request):
    form = InspectionForm(request.POST or None, initial=request.GET or None)
    low_shelf = False
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if inv.low_shelf_life_at_receipt(d["expiry_date"]) and not d["confirm_low_shelf_life"]:
            low_shelf = True
            messages.warning(request, "Remaining shelf life is below the minimum receiving "
                                      "shelf life. Tick the confirmation to save; the batch "
                                      "will be marked for early monitoring.")
        else:
            try:
                batch = inv.record_inspection(
                    request.user, purchase_order=d["purchase_order"], product=d["product"],
                    lot_number=d["lot_number"].strip(), manufacture_date=d["manufacture_date"],
                    expiry_date=d["expiry_date"], storage_condition=d["storage_condition"],
                    quantity_received=d["quantity_received"],
                    quantity_approved=d["quantity_approved"],
                    quantity_rejected=d["quantity_rejected"],
                    rejection_reason=d["rejection_reason"])
                messages.success(request, f"Inspection saved for lot {batch.lot_number} "
                                          f"({batch.days_to_expiry} days shelf life). The "
                                          f"Inventory Controller has been notified.")
                return redirect("ims:inspections")
            except ValidationError as e:
                _err(request, e)
    recent = Batch.objects.filter(inspected_by__isnull=False).select_related(
        "product", "purchase_order").order_by("-id")[:15]
    return render(request, "ims/stock/inspections.html",
                  {"form": form, "recent": recent, "low_shelf": low_shelf})


@role_required(Role.INVENTORY)
def receipts(request):
    pending = Batch.objects.filter(status=BatchStatus.INSPECTED).select_related("product")
    return render(request, "ims/stock/receipts.html", {"pending": pending})


@role_required(Role.INVENTORY)
def receipt(request, pk):
    batch = get_object_or_404(Batch, pk=pk, status=BatchStatus.INSPECTED)
    form = ReceiptForm(request.POST or None)
    valid = [loc for loc in BinLocation.objects.filter(is_active=True).select_related("warehouse")
             if inv.storage_compatible(batch.storage_condition, loc.storage_type)]
    form.fields["location"].queryset = BinLocation.objects.filter(pk__in=[v.pk for v in valid])
    over_capacity = False
    if request.method == "POST" and form.is_valid():
        try:
            inv.receive_batch(request.user, batch, form.cleaned_data["location"],
                              form.cleaned_data["allow_over_capacity"])
            messages.success(request, f"Lot {batch.lot_number} booked into "
                                      f"{form.cleaned_data['location']}.")
            return redirect("ims:receipts")
        except inv.CapacityWarning as e:
            over_capacity = True
            _err(request, e)
        except ValidationError as e:
            _err(request, e)
    return render(request, "ims/stock/receipt.html",
                  {"batch": batch, "form": form, "valid": valid, "over_capacity": over_capacity})


@role_required()
def stock_search(request):
    form = StockSearchForm(request.GET or None)
    qs = Batch.objects.filter(qty_on_hand__gt=0).select_related("product__category")
    if form.is_valid():
        d = form.cleaned_data
        if d["product"]:
            qs = qs.filter(product=d["product"])
        if d["category"]:
            qs = qs.filter(product__category=d["category"])
        if d["lot_number"]:
            qs = qs.filter(lot_number__icontains=d["lot_number"])
        if d["warehouse"]:
            qs = qs.filter(stock__location__warehouse=d["warehouse"],
                           stock__quantity__gt=0).distinct()
        if d["expiry_from"]:
            qs = qs.filter(expiry_date__gte=d["expiry_from"])
        if d["expiry_to"]:
            qs = qs.filter(expiry_date__lte=d["expiry_to"])
        if d["days_band"]:
            lo, hi = d["days_band"].split("-")
            today = timezone.localdate()
            qs = qs.filter(expiry_date__gte=today + timedelta(days=int(lo)))
            if hi:
                qs = qs.filter(expiry_date__lt=today + timedelta(days=int(hi)))
        if d["status"] == "reserved":
            qs = qs.filter(qty_reserved__gt=0)
        elif d["status"]:
            qs = qs.filter(status=d["status"])
    if request.GET.get("q"):
        q = request.GET["q"]
        qs = qs.filter(Q(lot_number__icontains=q) | Q(product__name__icontains=q)
                       | Q(product__sku__icontains=q))
    return render(request, "ims/stock/search.html", {"form": form, "batches": qs[:300]})


@role_required()
def batch_detail(request, pk):
    batch = get_object_or_404(Batch.objects.select_related("product__category"), pk=pk)
    return render(request, "ims/stock/batch.html", {
        "batch": batch, "locations": batch.locations(),
        "movements": batch.movements.select_related("user", "from_location", "to_location")[:100],
        "listed": inv.listed_qty(batch), "free": inv.free_qty(batch),
        "registration": inv.registration_check(batch.product, batch.market, batch.expiry_date),
    })


@role_required(Role.WAREHOUSE, Role.INVENTORY)
def transfer(request):
    initial = {}
    if request.GET.get("batch"):
        initial["batch"] = request.GET["batch"]
    form = TransferForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            inv.transfer_stock(request.user, d["batch"], d["from_location"], d["to_location"],
                               d["quantity"])
            messages.success(request, f"Moved {d['quantity']} of lot {d['batch'].lot_number} "
                                      f"to {d['to_location']}.")
            return redirect("ims:transfer")
        except ValidationError as e:
            _err(request, e)
    stock = BatchStock.objects.filter(quantity__gt=0).select_related(
        "batch__product", "location__warehouse").order_by("batch__product__name")[:200]
    return render(request, "ims/stock/transfer.html", {"form": form, "stock": stock})


@role_required(Role.WAREHOUSE, Role.INVENTORY)
def dispatch_queue(request):
    orders = PlatformOrder.objects.filter(status=PlatformOrder.CONFIRMED).select_related(
        "buyer").prefetch_related("lines__batch__product")
    recent = PlatformOrder.objects.filter(status__in=[PlatformOrder.DISPATCHED,
                                                      PlatformOrder.DELIVERED]).select_related(
        "buyer")[:15]
    issues = PlatformOrder.objects.exclude(delivery_issue="").select_related("buyer")[:10]
    return render(request, "ims/stock/dispatch_queue.html",
                  {"orders": orders, "recent": recent, "issues": issues})


@role_required(Role.WAREHOUSE, Role.INVENTORY)
def dispatch_order(request, pk):
    order = get_object_or_404(PlatformOrder.objects.select_related("buyer"), pk=pk)
    lines = list(order.lines.select_related("batch__product"))
    for line in lines:
        line.pick_locations = line.batch.locations()
    form = DispatchForm(request.POST or None)
    if request.method == "POST" and "hold" in request.POST:
        if not request.user.has_role(Role.WAREHOUSE):
            messages.error(request, "Only the Warehouse Officer can record a shortage.")
        else:
            inv.hold_order_for_damage(request.user, order, request.POST.get("hold_note", "")
                                      or "Stock damaged at picking")
            messages.warning(request, "Order held and the Inventory Controller alerted.")
        return redirect("ims:dispatch_order", pk=pk)
    if request.method == "POST" and form.is_valid():
        if not request.user.has_role(Role.WAREHOUSE):
            messages.error(request, "Only the Warehouse Officer can dispatch orders.")
            return redirect("ims:dispatch_order", pk=pk)
        picked = {line.pk: request.POST.get(f"lot_{line.pk}", "") for line in lines}
        try:
            inv.dispatch_platform_order(request.user, order, picked,
                                        form.cleaned_data["delivery_method"],
                                        form.cleaned_data["delivery_date"],
                                        form.cleaned_data["cold_chain_checked"])
            messages.success(request, f"Order {order.reference} dispatched and the buyer "
                                      f"notified.")
            return redirect("ims:dispatch_queue")
        except ValidationError as e:
            _err(request, e)
    return render(request, "ims/stock/dispatch_order.html",
                  {"order": order, "lines": lines, "form": form,
                   "cold": any(line.batch.is_cold_chain for line in lines)})


@role_required(Role.WAREHOUSE)
def field_dispatch(request):
    form = FieldDispatchForm(request.POST or None)
    preview = shortfall = None
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if "confirm" in request.POST:
            try:
                fd, short = inv.dispatch_field_order(request.user, d["product"], d["quantity"],
                                                     d["customer"], d["reference"])
                msg = f"Dispatched {fd.quantity_dispatched} units ({fd.reference})."
                if short:
                    msg += f" Shortfall of {short} units flagged."
                    messages.warning(request, msg)
                else:
                    messages.success(request, msg)
                return redirect("ims:field_dispatch")
            except ValidationError as e:
                _err(request, e)
        else:
            preview, shortfall = inv.fefo_allocate(d["product"], d["quantity"])
    recent = FieldDispatch.objects.select_related("product", "customer")[:10]
    return render(request, "ims/stock/field_dispatch.html",
                  {"form": form, "preview": preview, "shortfall": shortfall, "recent": recent})

