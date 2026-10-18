"""Master data screens: products, customers, suppliers and purchase orders."""
import json
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.models import AuditLog, Role, Supplier
from core.services import audit
from inventory import services as inv
from inventory.images import image_response, validate_image
from inventory.models import (Batch, BatchStatus, Customer, CustomerOffer, Listing, Product,
                              PurchaseOrder)
from planning.models import PurchasePlan, SalesRecord

from ..access import role_required
from ..forms import BootstrapMixin, DateInput
from .stock import _err

PRODUCT_EDITORS = (Role.PROCUREMENT, Role.ADMIN)
CUSTOMER_VIEWERS = (Role.SALES, Role.FINANCE, Role.GM, Role.ADMIN, Role.MANAGEMENT, Role.COO)
PO_VIEWERS = (Role.PROCUREMENT, Role.GM, Role.QA, Role.FINANCE)


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------
class ProductForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Product
        fields = ["sku", "name", "name_ar", "category", "unit", "storage_condition",
                  "original_price", "unit_cost", "supplier", "moq", "lead_time_days",
                  "shelf_life_months", "description", "image", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3}),
                   "unit": forms.Select(choices=[(u, u) for u in (
                       "vial", "ampoule", "bottle", "bag", "pack", "unit")])}

    def clean_image(self):
        f = self.cleaned_data.get("image")
        if f and hasattr(f, "content_type"):
            validate_image(f)
        return f


@role_required()
def products(request):
    qs = Product.objects.select_related("category", "supplier").annotate(
        on_hand=Sum("batches__qty_on_hand"),
        near=Count("batches", filter=Q(batches__status__in=[
            BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS, BatchStatus.RELEASED],
            batches__qty_on_hand__gt=0)),
        live=Count("batches__listings", filter=Q(batches__listings__status=Listing.ACTIVE)))
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(name_ar__icontains=q))
    if request.GET.get("category"):
        qs = qs.filter(category_id=request.GET["category"])
    if request.GET.get("show") != "all":
        qs = qs.filter(is_active=True)
    from core.models import ProductCategory

    return render(request, "ims/catalog/products.html", {
        "products": qs, "categories": ProductCategory.objects.all(), "q": q,
        "can_edit": request.user.has_role(*PRODUCT_EDITORS)})


@role_required()
def product_detail(request, pk):
    p = get_object_or_404(Product.objects.select_related("category", "supplier"), pk=pk)
    batches = p.batches.exclude(status=BatchStatus.DEPLETED).order_by("expiry_date")
    end = SalesRecord.objects.order_by("-period").values_list("period", flat=True).first()
    monthly, regions = [], []
    if end:
        since = end - timedelta(days=365)
        monthly = list(p.sales.filter(period__gt=since).values("period")
                       .annotate(q=Sum("quantity"), v=Sum("total")).order_by("period"))
        regions = list(p.sales.filter(period__gt=since).values("region__name")
                       .annotate(q=Sum("quantity")).order_by("-q"))
    chart = {"labels": [f"{m['period']:%b %y}" for m in monthly],
             "data": [m["q"] for m in monthly]}
    return render(request, "ims/catalog/product_detail.html", {
        "p": p, "batches": batches,
        "on_hand": batches.aggregate(t=Sum("qty_on_hand"))["t"] or 0,
        "registrations": p.registrations.select_related("market"),
        "listings": Listing.objects.filter(batch__product=p).select_related("batch")[:20],
        "po_lines": p.purchaseorderline_set.select_related("order__supplier")
                     .order_by("-order__order_date")[:10],
        "avg": inv.avg_monthly_sales(p), "regions": regions,
        "chart_json": json.dumps(chart), "has_sales": bool(monthly),
        "can_edit": request.user.has_role(*PRODUCT_EDITORS)})


@role_required(*PRODUCT_EDITORS)
def product_edit(request, pk=None):
    p = get_object_or_404(Product, pk=pk) if pk else None
    form = ProductForm(request.POST or None, request.FILES or None, instance=p)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        audit(request.user, AuditLog.CONFIGURATION,
              f"Product {obj.sku} {'updated' if p else 'created'}", f"PRODUCT-{obj.pk}")
        messages.success(request, f"{obj.name} saved.")
        return redirect("ims:product_detail", pk=obj.pk)
    return render(request, "ims/form.html", {
        "form": form, "title": f"Edit {p.name}" if p else "New product", "multipart": True,
        "back": f"/products/{p.pk}/" if p else "/products/"})


def product_image(request, pk):
    """Product picture (shared with the platform; contains nothing sensitive)."""
    return image_response(get_object_or_404(Product.objects.select_related("category"), pk=pk))


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
class CustomerForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "commercial_register_no", "region", "sales_rep", "odoo_ref"]


@role_required(*CUSTOMER_VIEWERS)
def customers(request):
    end = SalesRecord.objects.order_by("-period").values_list("period", flat=True).first()
    since = (end - timedelta(days=365)) if end else timezone.localdate()
    qs = Customer.objects.select_related("region", "sales_rep").annotate(
        sales12=Sum("salesrecord__total", filter=Q(salesrecord__period__gt=since)))
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(commercial_register_no__icontains=q))
    if request.GET.get("mine") and request.user.has_role(Role.SALES):
        qs = qs.filter(sales_rep=request.user)
    return render(request, "ims/catalog/customers.html", {
        "customers": qs.order_by("-sales12"), "q": q,
        "can_edit": request.user.has_role(Role.SALES, Role.ADMIN)})


@role_required(*CUSTOMER_VIEWERS)
def customer_detail(request, pk):
    c = get_object_or_404(Customer.objects.select_related("region", "sales_rep"), pk=pk)
    sales = c.salesrecord_set.select_related("product")
    by_product = list(sales.values("product__name", "product_id").annotate(
        q=Sum("quantity"), v=Sum("total")).order_by("-v")[:15])
    return render(request, "ims/catalog/customer_detail.html", {
        "c": c, "by_product": by_product, "total": sales.aggregate(t=Sum("total"))["t"] or 0,
        "offers": CustomerOffer.objects.filter(customer=c).select_related("batch__product")[:20],
        "recent": sales.order_by("-period")[:15],
        "can_edit": request.user.has_role(Role.SALES, Role.ADMIN)})


@role_required(Role.SALES, Role.ADMIN)
def customer_edit(request, pk=None):
    c = get_object_or_404(Customer, pk=pk) if pk else None
    form = CustomerForm(request.POST or None, instance=c)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        messages.success(request, f"{obj.name} saved.")
        return redirect("ims:customer_detail", pk=obj.pk)
    return render(request, "ims/form.html", {
        "form": form, "title": f"Edit {c.name}" if c else "New customer",
        "back": f"/customers/{c.pk}/" if c else "/customers/"})


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------
class SupplierForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "country", "default_lead_time_days", "is_active"]


@role_required(Role.PROCUREMENT, Role.GM, Role.ADMIN)
def suppliers(request):
    qs = Supplier.objects.annotate(products=Count("product", distinct=True),
                                   open_pos=Count("purchaseorder", filter=Q(
                                       purchaseorder__status=PurchaseOrder.OPEN), distinct=True))
    return render(request, "ims/catalog/suppliers.html", {
        "suppliers": qs, "can_edit": request.user.has_role(Role.PROCUREMENT, Role.ADMIN)})


@role_required(Role.PROCUREMENT, Role.ADMIN)
def supplier_edit(request, pk=None):
    s = get_object_or_404(Supplier, pk=pk) if pk else None
    form = SupplierForm(request.POST or None, instance=s)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        messages.success(request, f"{obj.name} saved.")
        return redirect("ims:suppliers")
    return render(request, "ims/form.html", {
        "form": form, "title": f"Edit {s.name}" if s else "New supplier", "back": "/suppliers/"})


# --------------------------------------------------------------------------
# Purchase orders
# --------------------------------------------------------------------------
class POForm(BootstrapMixin, forms.Form):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.filter(is_active=True))
    number = forms.CharField(required=False, label="PO number",
                             help_text="Leave empty to number it automatically, or enter the "
                                       "Odoo PO number")
    expected_date = forms.DateField(required=False, widget=DateInput(),
                                    label="Expected delivery date")


@role_required(*PO_VIEWERS)
def purchase_orders(request):
    status = request.GET.get("status", "open")
    qs = PurchaseOrder.objects.select_related("supplier", "plan").prefetch_related(
        "lines__product")
    if status != "all":
        qs = qs.filter(status=status)
    pending_plans = PurchasePlan.objects.filter(status=PurchasePlan.APPROVED,
                                                purchase_orders__isnull=True)
    return render(request, "ims/catalog/purchase_orders.html", {
        "orders": qs[:200], "status": status, "statuses": PurchaseOrder.STATUSES,
        "pending_plans": pending_plans, "today": timezone.localdate(),
        "can_edit": request.user.has_role(Role.PROCUREMENT)})


@role_required(Role.PROCUREMENT)
def po_new(request):
    form = POForm(request.POST or None)
    products = Product.objects.filter(is_active=True).select_related("supplier")
    rows = range(8)
    if request.method == "POST" and "from_plan" in request.POST:
        plan = get_object_or_404(PurchasePlan, pk=request.POST["from_plan"])
        try:
            pos = inv.purchase_orders_from_plan(request.user, plan)
            messages.success(request, f"Created {len(pos)} purchase order(s) from {plan}: "
                             + ", ".join(p.number for p in pos) + ". Mirror them in Odoo.")
        except ValidationError as e:
            _err(request, e)
        return redirect("ims:purchase_orders")
    if request.method == "POST" and form.is_valid():
        lines = []
        for i in rows:
            pid, qty = request.POST.get(f"product_{i}"), request.POST.get(f"qty_{i}", "")
            if pid and qty.isdigit():
                lines.append((products.get(pk=pid), int(qty)))
        try:
            po = inv.create_purchase_order(request.user, form.cleaned_data["supplier"], lines,
                                           form.cleaned_data["expected_date"],
                                           number=form.cleaned_data["number"].strip())
            messages.success(request, f"Purchase order {po.number} created; QA has been told "
                                      f"to expect the delivery.")
            return redirect("ims:po_detail", pk=po.pk)
        except ValidationError as e:
            _err(request, e)
    return render(request, "ims/catalog/po_new.html",
                  {"form": form, "products": products, "rows": rows})


@role_required(*PO_VIEWERS)
def po_detail(request, pk):
    po = get_object_or_404(PurchaseOrder.objects.select_related("supplier", "plan"), pk=pk)
    if request.method == "POST" and "close" in request.POST and request.user.has_role(
            Role.PROCUREMENT):
        inv.close_purchase_order(request.user, po)
        messages.success(request, f"{po.number} closed.")
        return redirect("ims:po_detail", pk=pk)
    lines = list(po.lines.select_related("product"))
    for line in lines:
        line.value = line.quantity * line.product.unit_cost
    total = sum((line.value for line in lines), 0)
    return render(request, "ims/catalog/po_detail.html", {
        "po": po, "lines": lines, "total": total,
        "batches": Batch.objects.filter(purchase_order=po).select_related("product"),
        "can_edit": request.user.has_role(Role.PROCUREMENT),
        "can_inspect": request.user.has_role(Role.QA)})
