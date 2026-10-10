"""Global search, My profile and the Reports hub."""
from django import forms
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from analytics import exports
from analytics.services import Filters, build
from commerce.models import Buyer, PlatformOrder
from core.models import AuditLog, Role, User
from core.services import audit
from inventory.models import (Batch, BatchStatus, Customer, Product, PurchaseOrder, WriteOff)

from ..access import ANALYTICS, role_required
from ..forms import BootstrapMixin
from .b2b import ORDER_VIEWERS
from .catalog import CUSTOMER_VIEWERS, PO_VIEWERS


@role_required()
def search(request):
    q = request.GET.get("q", "").strip()
    user = request.user
    groups = []
    if len(q) >= 2:
        groups.append(("Products", "bi-box", [
            (p.name, f"{p.sku} · {p.category}", f"/products/{p.pk}/")
            for p in Product.objects.filter(Q(name__icontains=q) | Q(sku__icontains=q)
                                            | Q(name_ar__icontains=q)).select_related(
                "category")[:10]]))
        groups.append(("Batches / lots", "bi-upc-scan", [
            (f"Lot {b.lot_number}", f"{b.product.name} · {b.get_status_display()} · exp "
                                    f"{b.expiry_date}", f"/stock/batch/{b.pk}/")
            for b in Batch.objects.filter(lot_number__icontains=q).select_related("product")[:10]]))
        if user.has_role(*ORDER_VIEWERS):
            groups.append(("Platform orders", "bi-receipt", [
                (o.reference, f"{o.buyer} · {o.get_status_display()} · {o.total:,.2f} EGP",
                 f"/b2b/orders/{o.pk}/")
                for o in PlatformOrder.objects.filter(reference__icontains=q)
                .select_related("buyer")[:10]]))
        if user.has_role(Role.REGULATORY, Role.FINANCE, Role.SALES, Role.ADMIN):
            groups.append(("Buyers", "bi-shop", [
                (b.company_name, f"{b.buyer_type} · {b.get_status_display()}",
                 f"/buyers/{b.pk}/")
                for b in Buyer.objects.filter(Q(company_name__icontains=q)
                                              | Q(commercial_register_no__icontains=q))
                .select_related("buyer_type")[:10]]))
        if user.has_role(*PO_VIEWERS):
            groups.append(("Purchase orders", "bi-cart-check", [
                (p.number, f"{p.supplier} · {p.get_status_display()}",
                 f"/purchase-orders/{p.pk}/")
                for p in PurchaseOrder.objects.filter(number__icontains=q)
                .select_related("supplier")[:10]]))
        if user.has_role(*CUSTOMER_VIEWERS):
            groups.append(("Customers", "bi-people", [
                (c.name, c.commercial_register_no or "", f"/customers/{c.pk}/")
                for c in Customer.objects.filter(Q(name__icontains=q)
                                                 | Q(commercial_register_no__icontains=q))[:10]]))
    groups = [g for g in groups if g[2]]
    total = sum(len(g[2]) for g in groups)
    if total == 1:
        return redirect(groups[0][2][0][2])
    return render(request, "ims/search.html", {"q": q, "groups": groups, "total": total})


class ProfileForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "department"]


@role_required()
def profile(request):
    user = request.user
    pform = ProfileForm(request.POST if "profile" in request.POST else None, instance=user)
    wform = PasswordChangeForm(user, request.POST if "password" in request.POST else None)
    for f in wform.fields.values():
        f.widget.attrs["class"] = "form-control"
    if request.method == "POST" and "profile" in request.POST and pform.is_valid():
        pform.save()
        messages.success(request, "Profile saved.")
        return redirect("ims:profile")
    if request.method == "POST" and "password" in request.POST and wform.is_valid():
        wform.save()
        update_session_auth_hash(request, wform.user)
        audit(user, AuditLog.CONFIGURATION, "Changed own password", f"USER-{user.pk}")
        messages.success(request, "Password changed.")
        return redirect("ims:profile")
    return render(request, "ims/profile.html", {"pform": pform, "wform": wform})


# --------------------------------------------------------------------------
# Reports hub
# --------------------------------------------------------------------------
def _stock(_f):
    rows = [[b.product.name, b.product.sku, b.lot_number, str(b.expiry_date), b.days_to_expiry,
             b.qty_on_hand, b.qty_reserved, f"{b.cost_value:,.2f}", b.get_status_display(),
             ", ".join(str(s.location) for s in b.locations())]
            for b in Batch.objects.filter(qty_on_hand__gt=0).select_related("product")
            .order_by("expiry_date")]
    return [], [{"title": "Stock on hand by batch",
                 "headers": ["Product", "SKU", "Lot", "Expiry", "Days", "On hand", "Reserved",
                             "Cost value (EGP)", "Status", "Locations"], "rows": rows}]


def _near_expiry(_f):
    from inventory.services import avg_monthly_sales, projected_unsold

    rows = []
    for b in Batch.objects.filter(status__in=[BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS,
                                              BatchStatus.RELEASED], qty_on_hand__gt=0) \
            .select_related("product").order_by("expiry_date"):
        rows.append([b.product.name, b.lot_number, str(b.expiry_date), b.days_to_expiry,
                     b.qty_on_hand, f"{b.cost_value:,.2f}", avg_monthly_sales(b.product),
                     projected_unsold(b), b.get_status_display()])
    return [], [{"title": "Near-expiry stock and projected unsold quantity",
                 "headers": ["Product", "Lot", "Expiry", "Days", "On hand", "Cost (EGP)",
                             "Avg monthly sales", "Projected unsold", "Status"], "rows": rows}]


def _write_offs(_f):
    rows = [[str(w), w.batch.product.name, w.batch.lot_number, w.quantity,
             w.get_reason_display(), w.get_status_display(), f"{w.write_off_value:,.2f}",
             f"{w.disposal_cost or 0:,.2f}", w.certificate_ref,
             w.purchase_plan.number if w.purchase_plan else ""]
            for w in WriteOff.objects.select_related("batch__product", "purchase_plan")]
    return [], [{"title": "Write-off and disposal register",
                 "headers": ["Ref", "Product", "Lot", "Qty", "Reason", "Status", "Value (EGP)",
                             "Disposal cost", "Certificate", "Plan"], "rows": rows}]


def _orders(_f):
    rows = [[o.reference, o.buyer.company_name, f"{timezone.localtime(o.created_at):%Y-%m-%d}",
             o.get_payment_method_display(), o.get_status_display(), f"{o.total:,.2f}"]
            for o in PlatformOrder.objects.select_related("buyer")]
    return [], [{"title": "B2B platform orders", "headers": ["Order", "Buyer", "Date",
                                                             "Payment", "Status", "Total (EGP)"],
                 "rows": rows}]


def _pos(_f):
    rows = []
    for po in PurchaseOrder.objects.select_related("supplier", "plan").prefetch_related(
            "lines__product"):
        for line in po.lines.all():
            rows.append([po.number, po.supplier.name, str(po.order_date),
                         str(po.expected_date or ""), line.product.name, line.quantity,
                         line.quantity_received, po.get_status_display(),
                         po.plan.number if po.plan else ""])
    return [], [{"title": "Purchase orders", "headers": ["PO", "Supplier", "Ordered",
                                                         "Expected", "Product", "Qty",
                                                         "Received", "Status", "Plan"],
                 "rows": rows}]


def _analytics(key):
    def fn(f):
        data = build(key, f)
        return data["kpis"], data["tables"]
    return fn


# key: (title, description, icon, roles or None, builder)
REPORTS = {
    "stock": ("Stock on hand", "Every batch with quantity, expiry, value and location.",
              "bi-box-seam", None, _stock),
    "near-expiry": ("Near-expiry stock", "Batches within 6 months of expiry and the quantity "
                    "projected to be left unsold.", "bi-hourglass-split", None, _near_expiry),
    "inventory": ("Value at risk & stock ageing", "Value at risk by category, product and "
                  "warehouse; ageing bands; days of cover.", "bi-exclamation-triangle",
                  ANALYTICS, _analytics("inventory")),
    "sales": ("Sales performance", "Sales by product, region, customer and month.",
              "bi-graph-up", ANALYTICS, _analytics("sales")),
    "forecast": ("Forecast accuracy", "MAPE by product and region, MOQ surplus bought.",
                 "bi-bullseye", ANALYTICS, _analytics("forecast")),
    "recovery": ("B2B recovery and write-off", "What the clearance channel recovered and what "
                 "was destroyed.", "bi-recycle", ANALYTICS, _analytics("recovery")),
    "audit": ("Audit accuracy", "Closed counts, accuracy and adjustments.",
              "bi-clipboard-check", ANALYTICS + (Role.INVENTORY,), _analytics("audit")),
    "write-offs": ("Write-off register", "All write-offs with values and disposal "
                   "certificates.", "bi-trash3", (Role.INVENTORY, Role.GM, Role.QA, Role.FINANCE,
                                                  Role.MANAGEMENT, Role.COO), _write_offs),
    "orders": ("B2B orders register", "Every order placed on the platform.", "bi-receipt",
               ORDER_VIEWERS, _orders),
    "purchase-orders": ("Purchase orders register", "Orders to suppliers and receipts.",
                        "bi-cart-check", PO_VIEWERS, _pos),
}


def _allowed(user, roles):
    return roles is None or user.has_role(*roles)


@role_required()
def reports(request):
    items = [(k, *v[:3]) for k, v in REPORTS.items() if _allowed(request.user, v[3])]
    return render(request, "ims/reports.html", {"items": items,
                                                "can_analytics": request.user.has_role(*ANALYTICS),
                                                "can_audit": request.user.has_role(
                                                    Role.ADMIN, Role.MANAGEMENT, Role.COO)})


@role_required()
def report_download(request, key, fmt):
    if key not in REPORTS or fmt not in ("pdf", "xlsx"):
        raise Http404
    title, _d, _i, roles, fn = REPORTS[key]
    if not _allowed(request.user, roles):
        raise Http404
    f = Filters(request.GET, request.user)
    kpis, tables = fn(f)
    maker = exports.to_pdf if fmt == "pdf" else exports.to_excel
    content = maker(f"Kubbar – {title}", f.as_text(), request.user, kpis, tables)
    audit(request.user, AuditLog.DATA_SYNC, f"Downloaded report {title} ({fmt})",
          f"REPORT-{key}")
    resp = HttpResponse(content, content_type="application/pdf" if fmt == "pdf" else
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="kubbar-{key}.{fmt}"'
    return resp

