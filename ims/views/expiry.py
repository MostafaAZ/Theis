"""Module 3: expiry monitoring, surplus release, listings, registrations
(UC-14 to UC-20, UC-40)."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import Role
from core.rules import rule
from inventory import services as inv
from inventory.models import (Batch, BatchStatus, ExpiryScanRun, Listing, ProductRegistration,
                              SurplusRelease)

from ..access import EXPIRY_VIEW, role_required
from ..forms import (ListingManageForm, OfferForm, RegistrationForm, ReleaseDecisionForm,
                     SurplusForm)
from .stock import _err


@role_required(*EXPIRY_VIEW)
def near_expiry(request):
    """UC-15 step 2 / FR-28."""
    qs = Batch.objects.filter(status__in=[BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS,
                                          BatchStatus.RELEASED],
                              qty_on_hand__gt=0).select_related("product__category")
    qs = inv.batches_for_user(request.user, qs)
    rows = []
    for b in qs:
        rows.append({"batch": b, "avg": inv.avg_monthly_sales(b.product),
                     "unsold": inv.projected_unsold(b), "value": b.cost_value})
    return render(request, "ims/expiry/list.html", {"rows": rows})


@role_required(*EXPIRY_VIEW)
def batch_review(request, pk):
    """UC-15 and UC-16."""
    batch = get_object_or_404(Batch.objects.select_related("product__category"), pk=pk)
    is_sales = request.user.has_role(Role.SALES)
    offer_form = OfferForm(request.POST if "offer" in request.POST else None)
    surplus_form = SurplusForm(request.POST if "surplus" in request.POST else None)
    if request.method == "POST":
        if not is_sales and ("offer" in request.POST or "surplus" in request.POST):
            messages.error(request, "Only the Sales Representative records offers and surplus "
                                    "flags.")
            return redirect("ims:batch_review", pk=pk)
        try:
            if "offer" in request.POST and offer_form.is_valid():
                d = offer_form.cleaned_data
                inv.record_offer(request.user, batch, d["customer"], d["qty_offered"],
                                 d["outcome"])
                messages.success(request, "Offer recorded.")
                return redirect("ims:batch_review", pk=pk)
            if "surplus" in request.POST and surplus_form.is_valid():
                d = surplus_form.cleaned_data
                inv.flag_surplus(request.user, batch, d["surplus_quantity"], d["reason"],
                                 d["comment"])
                messages.success(request, "Batch flagged as surplus; the COO has been "
                                          "notified.")
                return redirect("ims:batch_review", pk=pk)
            if "watch" in request.POST:
                inv.keep_under_watch(request.user, batch)
                messages.success(request, "Batch marked Keep Under Watch.")
                return redirect("ims:batch_review", pk=pk)
        except ValidationError as e:
            _err(request, e)
    if not offer_form.is_bound:
        offer_form.initial = {"qty_offered": inv.free_qty(batch)}
    if not surplus_form.is_bound:
        surplus_form.initial = {"surplus_quantity": inv.projected_unsold(batch) or None}
    return render(request, "ims/expiry/batch.html", {
        "batch": batch, "is_sales": is_sales, "offer_form": offer_form,
        "surplus_form": surplus_form, "offers": batch.offers.select_related("customer",
                                                                            "offered_by"),
        "releases": batch.releases.select_related("flagged_by", "decided_by"),
        "buyers": inv.customers_who_bought(batch.product),
        "avg": inv.avg_monthly_sales(batch.product), "unsold": inv.projected_unsold(batch),
        "free": inv.free_qty(batch), "alerts": batch.alerts.all()[:10]})


@role_required(Role.COO, Role.MANAGEMENT)
def releases(request):
    pending = SurplusRelease.objects.filter(status=SurplusRelease.PENDING).select_related(
        "batch__product", "flagged_by")
    decided = SurplusRelease.objects.exclude(status=SurplusRelease.PENDING).select_related(
        "batch__product", "decided_by")[:20]
    return render(request, "ims/expiry/releases.html", {"pending": pending, "decided": decided})


@role_required(Role.COO, Role.MANAGEMENT)
def release_decide(request, pk):
    """UC-17."""
    release = get_object_or_404(SurplusRelease.objects.select_related("batch__product"), pk=pk)
    batch = release.batch
    lo, hi = inv.price_band(batch.product.original_price)
    form = ReleaseDecisionForm(request.POST or None, initial={
        "release_quantity": release.surplus_qty,
        "clearance_price": inv.proposed_clearance_price(batch.product.original_price)})
    can_decide = request.user.has_role(Role.COO)
    if request.method == "POST":
        if not can_decide:
            messages.error(request, "Only the COO approves releases.")
        elif "reject" in request.POST:
            try:
                inv.reject_release(request.user, release, request.POST.get("comment", ""))
                messages.success(request, "Release rejected; the batch is back to Near-Expiry.")
                return redirect("ims:releases")
            except ValidationError as e:
                _err(request, e)
        elif form.is_valid():
            d = form.cleaned_data
            try:
                listing = inv.approve_release(request.user, release, d["release_quantity"],
                                              d["clearance_price"], d["comment"])
                messages.success(request, f"Released and published as listing #{listing.pk}.")
                return redirect("ims:releases")
            except ValidationError as e:
                _err(request, e)
    return render(request, "ims/expiry/release_decide.html", {
        "release": release, "batch": batch, "form": form, "band": (lo, hi),
        "can_decide": can_decide, "offers": batch.offers.select_related("customer"),
        "registration": inv.registration_check(batch.product, batch.market, batch.expiry_date),
        "min_pct": rule("price_band_min_pct"), "max_pct": rule("price_band_max_pct")})


@role_required(Role.COO, Role.MANAGEMENT, Role.SALES)
def listings(request):
    items = Listing.objects.select_related("batch__product").order_by("status",
                                                                      "batch__expiry_date")
    return render(request, "ims/expiry/listings.html", {"items": items})


@role_required(Role.COO, Role.MANAGEMENT)
def listing_manage(request, pk):
    """UC-40 / FR-80."""
    listing = get_object_or_404(Listing.objects.select_related("batch__product"), pk=pk)
    form = ListingManageForm(request.POST or None, initial={
        "clearance_price": listing.clearance_price, "listed_quantity": listing.listed_qty})
    if request.method == "POST" and form.is_valid():
        if not request.user.has_role(Role.COO):
            messages.error(request, "Only the COO manages listings.")
            return redirect("ims:listing_manage", pk=pk)
        d = form.cleaned_data
        try:
            if d["action"] == "reprice":
                if d["clearance_price"] is None:
                    raise ValidationError("Enter the new price.")
                inv.reprice_listing(request.user, listing, d["clearance_price"])
            elif d["action"] == "quantity":
                if not d["listed_quantity"]:
                    raise ValidationError("Enter the new quantity.")
                inv.change_listing_quantity(request.user, listing, d["listed_quantity"])
            else:
                inv.withdraw_listing_by_user(request.user, listing,
                                             d["reason"] or "Withdrawn by Management")
            messages.success(request, "Listing updated. Confirmed orders are unchanged.")
            return redirect("ims:listings")
        except ValidationError as e:
            _err(request, e)
    lo, hi = inv.price_band(listing.original_price)
    return render(request, "ims/expiry/listing_manage.html",
                  {"listing": listing, "form": form, "band": (lo, hi),
                   "orders": listing.order_lines.select_related("order__buyer")})


@role_required(Role.REGULATORY, Role.COO)
def registrations(request):
    regs = ProductRegistration.objects.select_related("product", "market")
    return render(request, "ims/expiry/registrations.html", {"regs": regs})


@role_required(Role.REGULATORY)
def registration_edit(request, pk=None):
    """UC-20 / FR-35."""
    reg = get_object_or_404(ProductRegistration, pk=pk) if pk else None
    form = RegistrationForm(request.POST or None, request.FILES or None, instance=reg)
    if request.method == "POST" and form.is_valid():
        old = reg.get_status_display() if reg else ""
        reg = form.save(commit=False)
        reg.updated_by = request.user
        reg.save()
        from core.models import AuditLog
        from core.services import audit, notify_roles

        audit(request.user, AuditLog.CONFIGURATION, f"Registration saved for {reg.product}",
              f"REG-{reg.pk}", old=old, new=f"{reg.get_status_display()} until "
                                           f"{reg.expiry_date}")
        withdrawn = 0
        for listing in Listing.objects.filter(status=Listing.ACTIVE, batch__product=reg.product,
                                              market=reg.market):
            ok, reason = inv.registration_check(reg.product, reg.market,
                                                listing.batch.expiry_date)
            if not ok:
                inv.withdraw_listing(listing, "Registration: " + reason, request.user)
                withdrawn += 1
        if withdrawn:
            notify_roles(Role.COO, f"{withdrawn} listing(s) for {reg.product.name} withdrawn: "
                                   f"registration {reg.get_status_display()}.", "/listings/",
                         level="warning")
            messages.warning(request, f"{withdrawn} open listing(s) withdrawn and the COO "
                                      f"notified.")
        messages.success(request, "Registration saved.")
        return redirect("ims:registrations")
    return render(request, "ims/form.html", {
        "form": form, "title": "Product registration" if reg else "New product registration",
        "back": "/registrations/", "multipart": True})


@role_required(Role.ADMIN, Role.INVENTORY)
def scans(request):
    runs = ExpiryScanRun.objects.all()[:30]
    return render(request, "ims/expiry/scans.html", {"runs": runs})


@require_POST
@role_required(Role.ADMIN, Role.INVENTORY)
def run_scan(request):
    run = inv.run_expiry_scan(triggered_by=f"manual: {request.user.email}")
    if run:
        messages.success(request, f"Expiry scan complete: {run.batches_scanned} batches, "
                                  f"{run.new_near_expiry} new near-expiry, {run.expired} expired, "
                                  f"{run.listings_withdrawn} listings withdrawn.")
    else:
        messages.error(request, "The expiry scan failed; the System Administrator was alerted.")
    return redirect("ims:scans")
