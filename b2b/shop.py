"""Storefront pages: home, catalogue, product page, account area, help pages."""
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import ValidationError
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from commerce import services as shop
from commerce.models import Buyer, BuyerDocument, Invoice, PlatformOrder, SupportMessage
from core.models import AuditLog, ProductCategory, Role
from core.services import audit, notify_roles
from inventory.images import image_response
from inventory.models import Listing, Product

from .forms import PreferencesForm, ResubmitForm, Styled
from .views import _err, buyer_required, lang

CAT_ICONS = {"Vaccines": ("bi-droplet-half", "#dbeafe", "#2563eb"),
             "Veterinary Medicaments": ("bi-capsule", "#ccfbf1", "#0f766e"),
             "Feed Additives": ("bi-basket", "#fef3c7", "#b45309"),
             "Nutritional Products": ("bi-heart-pulse", "#ede9fe", "#7c3aed")}


def _viewer(request):
    u = request.user
    buyer = u.buyer if u.is_authenticated and u.is_buyer else None
    return buyer, bool(buyer and buyer.can_order)


def decorate(listings, buyer=None):
    """Add display helpers: shelf-life bar, savings, availability to this buyer."""
    out = []
    for item in listings:
        days = item.batch.days_to_expiry
        total = max(1, int(item.batch.product.shelf_life_months * 30.4))
        item.shelf_pct = max(4, min(100, int(days * 100 / total)))
        item.shelf_colour = "#16a34a" if days > 120 else "#f59e0b" if days > 60 else "#ef4444"
        item.saving = item.original_price - item.clearance_price
        item.blocked = bool(buyer and buyer.can_order and item.batch.is_cold_chain
                            and not buyer.cold_chain_eligible)
        out.append(item)
    return out


def public_listings():
    return Listing.objects.filter(status=Listing.ACTIVE, available_qty__gt=0).select_related(
        "batch__product__category")


def home(request):
    buyer, can_see = _viewer(request)
    live = public_listings()
    if can_see:
        live = shop.catalogue_for(buyer)
    discounts = [100 - float(x.clearance_price * 100 / x.original_price) for x in live
                 if x.original_price]
    cats = []
    for c in ProductCategory.objects.filter(is_active=True, is_expiring=True):
        icon, bg, fg = CAT_ICONS.get(c.name, ("bi-box", "#e2e8f0", "#475569"))
        cats.append({"cat": c, "count": live.filter(batch__product__category=c).count(),
                     "icon": icon, "bg": bg, "fg": fg})
    deals = decorate(list(live.order_by(F("clearance_price") / F("original_price"))[:8]), buyer)
    return render(request, "b2b/home.html", {
        "stats": {"live": live.count(), "saving": round(sum(discounts) / len(discounts))
                  if discounts else 30, "cats": len(cats)},
        "cats": cats, "deals": deals, "show_prices": can_see, "buyer": buyer})


def catalogue(request):
    buyer, can_see = _viewer(request)
    base = shop.catalogue_for(buyer) if can_see else public_listings()
    g = request.GET
    qs = base
    if g.get("q"):
        qs = qs.filter(Q(batch__product__name__icontains=g["q"])
                       | Q(batch__product__name_ar__icontains=g["q"])
                       | Q(batch__product__sku__icontains=g["q"])
                       | Q(batch__lot_number__icontains=g["q"]))
    if g.get("category"):
        qs = qs.filter(batch__product__category_id=g["category"])
    if g.get("min_days", "").isdigit():
        qs = qs.filter(batch__expiry_date__gte=timezone.localdate()
                       + timedelta(days=int(g["min_days"])))
    if g.get("storage") == "cold":
        qs = qs.exclude(batch__storage_condition="ambient")
    elif g.get("storage") == "ambient":
        qs = qs.filter(batch__storage_condition="ambient")
    if can_see and g.get("max_price"):
        try:
            qs = qs.filter(clearance_price__lte=float(g["max_price"]))
        except ValueError:
            pass
    sort = g.get("sort", "expiry")
    order = {"price": "clearance_price", "discount": F("clearance_price") / F("original_price"),
             "name": "batch__product__name"}.get(sort, "batch__expiry_date")
    qs = qs.order_by(order)
    counts = dict(base.values_list("batch__product__category_id").annotate(n=Count("id"))
                  .values_list("batch__product__category_id", "n"))
    cats = [(c, counts.get(c.pk, 0)) for c in
            (buyer.categories.all() if can_see else
             ProductCategory.objects.filter(is_expiring=True, is_active=True))]
    active_cat = ProductCategory.objects.filter(pk=g.get("category")).first() \
        if g.get("category", "").isdigit() else None
    return render(request, "b2b/catalogue.html", {
        "listings": decorate(list(qs), buyer), "cats": cats, "buyer": buyer,
        "show_prices": can_see, "g": g, "total": base.count(), "active_cat": active_cat,
        "filtered": any(g.get(k) for k in ("q", "category", "min_days", "storage",
                                           "max_price"))})


def listing_detail(request, pk):
    listing = get_object_or_404(Listing.objects.select_related("batch__product__category"),
                                pk=pk)
    buyer, can_see = _viewer(request)
    reason = shop.listing_unavailable_reason(buyer, listing) if can_see else ""
    if listing.status != Listing.ACTIVE and not reason:
        reason = "This listing is no longer available."
    if request.method == "POST" and can_see and not reason:
        try:
            qty = int(request.POST.get("quantity", 0))
        except ValueError:
            qty = 0
        cart = request.session.get("cart", {})
        current = cart.get(str(pk), 0)
        if current + qty > listing.available_qty:
            qty = max(0, listing.available_qty - current)
            messages.warning(request, f"Quantity limited to the {listing.available_qty} "
                                      f"available.")
        if qty > 0:
            cart[str(pk)] = current + qty
            request.session["cart"] = cart
            messages.success(request, f"Added {qty} × {listing.batch.product.name} to your "
                                      f"cart.")
            return redirect("b2b:cart") if "go_cart" in request.POST else redirect(
                "b2b:listing", pk=pk)
    similar = public_listings().filter(
        batch__product__category=listing.batch.product.category).exclude(pk=pk)
    if can_see:
        similar = similar.filter(pk__in=shop.catalogue_for(buyer))
    return render(request, "b2b/listing.html", {
        "listing": decorate([listing], buyer)[0], "show_prices": can_see, "reason": reason,
        "buyer": buyer, "similar": decorate(list(similar[:4]), buyer),
        "in_cart": request.session.get("cart", {}).get(str(pk), 0)})


def product_image(request, pk):
    return image_response(get_object_or_404(Product.objects.select_related("category"), pk=pk))


# --------------------------------------------------------------------------
# Account area
# --------------------------------------------------------------------------
class CompanyForm(Styled, forms.ModelForm):
    class Meta:
        model = Buyer
        fields = ["contact_person", "phone", "address"]
        labels = {"contact_person": "Contact Person", "phone": "Phone", "address": "Address"}


def _steps(buyer):
    """What the buyer should do next, in plain words."""
    s = []
    if buyer.status == Buyer.PENDING_EMAIL:
        s.append(("bi-envelope", "Confirm your email address using the link we sent you.", ""))
    if buyer.status == Buyer.PENDING:
        s.append(("bi-hourglass-split", "Our Regulatory Affairs team is checking your "
                                        "documents. We'll email you the decision.", ""))
    if buyer.status == Buyer.RESUBMISSION:
        s.append(("bi-upload", "Upload the documents marked 'Resubmission Requested'.",
                  "/account/documents/"))
    if buyer.ordering_suspended:
        s.append(("bi-exclamation-triangle", "Your licence has expired. Upload the renewed "
                                             "licence to order again.", "/account/documents/"))
    if buyer.can_order:
        placed = buyer.orders.filter(status=PlatformOrder.PLACED).count()
        if placed:
            s.append(("bi-credit-card", f"{placed} order(s) waiting for your payment.",
                      "/orders/"))
        dispatched = buyer.orders.filter(status=PlatformOrder.DISPATCHED).count()
        if dispatched:
            s.append(("bi-truck", f"{dispatched} order(s) on the way – confirm when they "
                                  f"arrive.", "/orders/"))
        if not s:
            s.append(("bi-search", "Browse the catalogue for new clearance offers.",
                      "/catalogue/"))
    return s


@buyer_required()
def account(request):
    buyer = request.buyer
    docs = shop.current_documents(buyer)
    return render(request, "b2b/account/overview.html", {
        "buyer": buyer, "tab": "overview", "steps": _steps(buyer),
        "recent": buyer.orders.all()[:5],
        "missing": [label for t, label in BuyerDocument.TYPES
                    if t in BuyerDocument.MANDATORY and t not in docs],
        "unpaid": buyer.invoices.filter(status=Invoice.UNPAID).count()})


@buyer_required()
def account_profile(request):
    buyer = request.buyer
    form = CompanyForm(request.POST if "company" in request.POST else None, instance=buyer,
                       lang=lang(request))
    pw = PasswordChangeForm(buyer.user, request.POST if "password" in request.POST else None)
    for f in pw.fields.values():
        f.widget.attrs["class"] = "form-control"
    prefs = PreferencesForm(request.POST if "prefs" in request.POST else None, instance=buyer,
                            lang=lang(request))
    if request.method == "POST":
        if "company" in request.POST and form.is_valid():
            form.save()
            audit(buyer.user, AuditLog.CONFIGURATION, "Buyer updated company contact details",
                  f"BUYER-{buyer.pk}")
            messages.success(request, "Company details saved.")
            return redirect("b2b:account_profile")
        if "password" in request.POST and pw.is_valid():
            pw.save()
            update_session_auth_hash(request, pw.user)
            messages.success(request, "Password changed.")
            return redirect("b2b:account_profile")
        if "prefs" in request.POST and prefs.is_valid():
            prefs.save()
            messages.success(request, "Preferences saved.")
            return redirect("b2b:account_profile")
    return render(request, "b2b/account/profile.html", {
        "buyer": buyer, "tab": "profile", "form": form, "pw": pw, "prefs": prefs})


@buyer_required()
def account_documents(request):
    buyer = request.buyer
    form = ResubmitForm(request.POST or None, request.FILES or None, lang=lang(request))
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            shop.resubmit_document(buyer, d["doc_type"], d["file"], d["expiry_date"])
            if d["doc_type"] == BuyerDocument.LICENCE and buyer.ordering_suspended:
                notify_roles(Role.REGULATORY, f"{buyer.company_name} uploaded a renewed "
                                              f"licence.", f"/buyers/{buyer.pk}/")
            messages.success(request, "Document uploaded.")
            return redirect("b2b:account_documents")
        except ValidationError as e:
            _err(request, e)
    docs = shop.current_documents(buyer)
    return render(request, "b2b/account/documents.html", {
        "buyer": buyer, "tab": "documents", "form": form,
        "docs": [(t, label, docs.get(t), t in BuyerDocument.MANDATORY)
                 for t, label in BuyerDocument.TYPES]})


@buyer_required()
def account_invoices(request):
    buyer = request.buyer
    return render(request, "b2b/account/invoices.html", {
        "buyer": buyer, "tab": "invoices", "invoices": buyer.invoices.select_related("order"),
        "today": timezone.localdate()})


@buyer_required()
def account_messages(request):
    buyer = request.buyer
    return render(request, "b2b/account/messages.html", {
        "buyer": buyer, "tab": "messages", "items": buyer.messages.all()})


# --------------------------------------------------------------------------
# Help pages
# --------------------------------------------------------------------------
class ContactForm(Styled, forms.ModelForm):
    class Meta:
        model = SupportMessage
        fields = ["name", "email", "company", "phone", "topic", "message"]
        labels = {"name": "Name", "email": "Email Address", "company": "Company Name",
                  "phone": "Phone", "topic": "Topic", "message": "Message"}
        widgets = {"message": forms.Textarea(attrs={"rows": 5})}


def contact(request):
    buyer, _ = _viewer(request)
    initial = {}
    if buyer:
        initial = {"name": buyer.contact_person, "email": buyer.user.email,
                   "company": buyer.company_name, "phone": buyer.phone}
    if request.GET.get("order"):
        initial["topic"] = "order"
    form = ContactForm(request.POST or None, initial=initial, lang=lang(request))
    if request.method == "POST" and form.is_valid():
        msg = form.save(commit=False)
        msg.buyer = buyer
        if buyer and request.GET.get("order", "").isdigit():
            msg.order = buyer.orders.filter(pk=request.GET["order"]).first()
        msg.save()
        roles = {"order": [Role.WAREHOUSE, Role.FINANCE], "payment": [Role.FINANCE],
                 "account": [Role.REGULATORY]}.get(msg.topic, [Role.SALES])
        notify_roles(roles, f"New buyer message from {msg.company or msg.name}: "
                            f"{msg.get_topic_display()}", f"/b2b/messages/{msg.pk}/")
        messages.success(request, "Thank you – we received your message and will reply by "
                                  "email, usually within one working day.")
        return redirect("b2b:account_messages" if buyer else "b2b:contact")
    return render(request, "b2b/contact.html", {"form": form})


def page(request, name):
    return render(request, f"b2b/pages/{name}.html", {"page": name})

