"""B2B E-Commerce Platform views (UC-02, UC-29 to UC-37)."""
import io
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import views as auth_views
from django.core import signing
from django.core.exceptions import ValidationError
from django.db.models import F, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from commerce import services as shop
from commerce.models import BuyerDocument, PlatformOrder
from core.models import AuditLog, ProductCategory, User
from core.rules import rule_int
from core.services import audit, email_buyer
from inventory.models import Listing

from .forms import (DOC_FIELDS, CheckoutForm, LoginForm, PreferencesForm, RegisterForm,
                    ResubmitForm, TransferForm)
from .i18n import translate

UNLOCK_SALT = "kubbar-b2b-unlock"
BANK_DETAILS = {
    "Bank": "National Bank of Egypt – Dokki branch",
    "Account name": "Kubbar International for Veterinary Products",
    "Account number": "1993 0000 4521 7788",
    "IBAN": "EG38 0003 0199 3000 0452 1778 8000",
    "SWIFT": "NBEGEGCX",
}


def lang(request):
    return request.session.get("lang", "en")


def _err(request, exc):
    for m in getattr(exc, "messages", [str(exc)]):
        messages.error(request, m)


def buyer_required(verified=False):
    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            u = request.user
            if not u.is_authenticated or not u.is_buyer:
                return redirect(f"/login/?next={request.path}")
            request.buyer = u.buyer
            if verified and not request.buyer.can_order:
                messages.info(request, translate(
                    "Prices and ordering are available to verified buyers only.", lang(request)))
                return redirect("b2b:account")
            return view(request, *args, **kwargs)
        return wrapper
    return deco


def set_language(request, code):
    request.session["lang"] = "ar" if code == "ar" else "en"
    nxt = request.GET.get("next", "/")
    return redirect(nxt if nxt.startswith("/") else "/")


def home(request):
    if request.user.is_authenticated and request.user.is_buyer and request.user.buyer.can_order:
        return redirect("b2b:catalogue")
    teaser = Listing.objects.filter(status=Listing.ACTIVE, available_qty__gt=0).select_related(
        "batch__product__category")[:6]
    return render(request, "b2b/home.html", {"teaser": teaser})


# --------------------------------------------------------------------------
# UC-29 / UC-30 registration
# --------------------------------------------------------------------------
def register(request):
    form = RegisterForm(request.POST or None, request.FILES or None, lang=lang(request))
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        docs = {doc_type: d[field] for field, doc_type, _l, _r in DOC_FIELDS if d.get(field)}
        try:
            for f in docs.values():
                shop.validate_upload(f)
            shop.register_buyer(
                email=d["email"], password=d["password"], company_name=d["company_name"],
                buyer_type=d["buyer_type"], market=d["market"],
                commercial_register_no=d["commercial_register_no"], tax_card_no=d["tax_card_no"],
                address=d["address"], contact_person=d["contact_person"], phone=d["phone"],
                owner_full_name=d["owner_full_name"], owner_national_id=d["owner_national_id"],
                documents=docs, licence_expiry=d["licence_expiry"], consent=True)
            return render(request, "b2b/register_done.html", {"email": d["email"]})
        except ValidationError as e:
            _err(request, e)
    return render(request, "b2b/register.html", {"form": form})


def confirm_email(request, token):
    try:
        shop.confirm_email(token)
    except ValidationError as e:
        _err(request, e)
        return redirect("b2b:home")
    messages.success(request, "Email confirmed. Your account is now pending verification by "
                              "our Regulatory Affairs team.")
    return redirect("b2b:account" if request.user.is_authenticated else "b2b:login")


# --------------------------------------------------------------------------
# UC-32 login, lock and unlock; UC-02 logout
# --------------------------------------------------------------------------
def _lock(user):
    user.is_locked = True
    user.save(update_fields=["is_locked", "failed_login_attempts"])
    token = signing.dumps(user.pk, salt=UNLOCK_SALT)
    email_buyer(user, "Your account has been locked",
                "Your account was locked after repeated failed log-in attempts. Use this link "
                "within 24 hours to unlock it:\n\n" + shop.b2b_url(f"/unlock/{token}/"))


def login_view(request):
    if request.user.is_authenticated and request.user.is_buyer:
        return redirect("b2b:catalogue")
    form = LoginForm(request.POST or None, lang=lang(request))
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].lower()
        user = User.objects.filter(email__iexact=email, account_type=User.BUYER).first()
        if user and user.is_locked:
            messages.error(request, "This account is locked. Check your email for the unlock "
                                    "link.")
        else:
            auth_user = authenticate(request, email=email,
                                     password=form.cleaned_data["password"])
            if auth_user is None or not auth_user.is_buyer:
                if user:
                    user.failed_login_attempts += 1
                    if user.failed_login_attempts >= rule_int("max_failed_logins"):
                        _lock(user)
                        messages.error(request, "Too many failed attempts. We emailed you a "
                                                "link to unlock your account.")
                        return render(request, "b2b/login.html", {"form": form})
                    user.save(update_fields=["failed_login_attempts"])
                messages.error(request, "Invalid email address or password.")
            else:
                auth_user.failed_login_attempts = 0
                auth_user.save(update_fields=["failed_login_attempts"])
                login(request, auth_user, backend="core.auth.EmailBackend")
                audit(auth_user, AuditLog.LOGIN, "Logged in to the platform",
                      f"BUYER-{auth_user.buyer.pk}", request=request)
                nxt = request.GET.get("next", "")
                if nxt.startswith("/") and not nxt.startswith("//"):
                    return redirect(nxt)
                return redirect("b2b:catalogue" if auth_user.buyer.can_order else "b2b:account")
    return render(request, "b2b/login.html", {"form": form})


def unlock(request, token):
    try:
        uid = signing.loads(token, salt=UNLOCK_SALT, max_age=24 * 3600)
    except signing.BadSignature:
        messages.error(request, "This unlock link is invalid or has expired.")
        return redirect("b2b:login")
    User.objects.filter(pk=uid, account_type=User.BUYER).update(is_locked=False,
                                                                failed_login_attempts=0)
    messages.success(request, "Your account is unlocked. You can log in now.")
    return redirect("b2b:login")


@require_POST
def logout_view(request):
    logout(request)
    request.session["lang"] = request.POST.get("lang", "en")
    return redirect("b2b:home")


class PasswordResetView(auth_views.PasswordResetView):
    """UC-32 A2: the reset link is valid for 30 minutes (PASSWORD_RESET_TIMEOUT)."""

    template_name = "b2b/password_reset.html"
    email_template_name = "b2b/password_reset_email.txt"
    subject_template_name = "b2b/password_reset_subject.txt"
    success_url = "/password-reset/sent/"
    extra_email_context = {"site_url": settings.B2B_BASE_URL}

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["email"].widget.attrs["class"] = "form-control"
        return form


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "b2b/password_reset_confirm.html"
    success_url = "/login/"

    def form_valid(self, form):
        messages.success(self.request, "Your password has been changed. Please log in.")
        return super().form_valid(form)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for f in form.fields.values():
            f.widget.attrs["class"] = "form-control"
        return form


# --------------------------------------------------------------------------
# Account (status, documents, preferences)
# --------------------------------------------------------------------------
@buyer_required()
def account(request):
    buyer = request.buyer
    docs = shop.current_documents(buyer)
    resubmit_form = ResubmitForm(request.POST or None, request.FILES or None, lang=lang(request))
    prefs = PreferencesForm(request.POST or None, instance=buyer, lang=lang(request))
    if request.method == "POST" and "resubmit" in request.POST and resubmit_form.is_valid():
        d = resubmit_form.cleaned_data
        try:
            shop.resubmit_document(buyer, d["doc_type"], d["file"], d["expiry_date"])
            if d["doc_type"] == BuyerDocument.LICENCE and buyer.ordering_suspended:
                buyer.refresh_from_db()
                from core.models import Role
                from core.services import notify_roles

                notify_roles(Role.REGULATORY, f"{buyer.company_name} uploaded a renewed "
                                              f"licence.", f"/buyers/{buyer.pk}/")
            messages.success(request, "Document uploaded.")
            return redirect("b2b:account")
        except ValidationError as e:
            _err(request, e)
    if request.method == "POST" and "prefs" in request.POST and prefs.is_valid():
        prefs.save()
        messages.success(request, "Preferences saved.")
        return redirect("b2b:account")
    return render(request, "b2b/account.html", {
        "buyer": buyer, "docs": [(t, label, docs.get(t)) for t, label in BuyerDocument.TYPES],
        "resubmit_form": resubmit_form, "prefs": prefs})


# --------------------------------------------------------------------------
# UC-33 catalogue
# --------------------------------------------------------------------------
def catalogue(request):
    user = request.user
    buyer = user.buyer if user.is_authenticated and user.is_buyer else None
    if buyer and buyer.can_order:
        qs = shop.catalogue_for(buyer)
        categories = buyer.categories.all()
    else:
        qs = Listing.objects.filter(status=Listing.ACTIVE, available_qty__gt=0).select_related(
            "batch__product__category")
        categories = ProductCategory.objects.filter(is_expiring=True)
    g = request.GET
    if g.get("category"):
        qs = qs.filter(batch__product__category_id=g["category"])
    if g.get("q"):
        qs = qs.filter(Q(batch__product__name__icontains=g["q"])
                       | Q(batch__product__name_ar__icontains=g["q"])
                       | Q(batch__lot_number__icontains=g["q"]))
    if g.get("min_days", "").isdigit():
        qs = qs.filter(batch__expiry_date__gte=timezone.localdate()
                       + timedelta(days=int(g["min_days"])))
    if g.get("expiry_before"):
        qs = qs.filter(batch__expiry_date__lte=g["expiry_before"])
    if buyer and buyer.can_order and g.get("max_price"):
        try:
            qs = qs.filter(clearance_price__lte=g["max_price"])
        except (ValueError, ValidationError):
            pass
    sort = g.get("sort", "expiry")
    qs = qs.order_by({"price": "clearance_price", "discount": F("clearance_price") /
                      F("original_price"), "expiry": "batch__expiry_date"}.get(
        sort, "batch__expiry_date"))
    listings = list(qs)
    for item in listings:
        item.blocked = (buyer and buyer.can_order and item.batch.is_cold_chain
                        and not buyer.cold_chain_eligible)
    return render(request, "b2b/catalogue.html", {
        "listings": listings, "categories": categories, "buyer": buyer,
        "show_prices": bool(buyer and buyer.can_order), "g": g})


def listing_detail(request, pk):
    listing = get_object_or_404(Listing.objects.select_related("batch__product__category"),
                                pk=pk)
    user = request.user
    buyer = user.buyer if user.is_authenticated and user.is_buyer else None
    show_prices = bool(buyer and buyer.can_order)
    reason = shop.listing_unavailable_reason(buyer, listing) if show_prices else ""
    if show_prices and listing.status != Listing.ACTIVE and not reason:
        reason = "This listing is no longer available."
    if request.method == "POST" and show_prices and not reason:
        try:
            qty = int(request.POST.get("quantity", 0))
        except ValueError:
            qty = 0
        cart = request.session.get("cart", {})
        current = cart.get(str(pk), 0)
        qty = max(0, qty)
        if current + qty > listing.available_qty:
            qty = max(0, listing.available_qty - current)
            messages.warning(request, f"Quantity limited to the {listing.available_qty} "
                                      f"available.")
        if qty:
            cart[str(pk)] = current + qty
            request.session["cart"] = cart
            messages.success(request, f"Added {qty} × {listing.batch.product.name} to your "
                                      f"cart.")
        return redirect("b2b:cart")
    return render(request, "b2b/listing.html", {"listing": listing, "show_prices": show_prices,
                                                "reason": reason, "buyer": buyer})


# --------------------------------------------------------------------------
# UC-34 / UC-35 cart and checkout
# --------------------------------------------------------------------------
def _cart_lines(request, buyer):
    cart = request.session.get("cart", {})
    lines, total, changed = [], 0, False
    for lid, qty in list(cart.items()):
        listing = Listing.objects.select_related("batch__product").filter(pk=lid).first()
        reason = shop.listing_unavailable_reason(buyer, listing) if listing else "Removed."
        if reason:
            cart.pop(lid)
            changed = True
            if listing:
                messages.warning(request, f"{listing.batch.product.name}: {reason} Removed "
                                          f"from your cart.")
            continue
        if qty > listing.available_qty:
            cart[lid] = qty = listing.available_qty
            changed = True
            messages.warning(request, f"{listing.batch.product.name}: only {qty} available; "
                                      f"quantity reduced.")
        line_total = listing.clearance_price * qty
        total += line_total
        lines.append({"listing": listing, "qty": qty, "total": line_total})
    if changed:
        request.session["cart"] = cart
    return lines, total


@buyer_required(verified=True)
def cart(request):
    buyer = request.buyer
    if request.method == "POST" and "update" in request.POST:
        cart = request.session.get("cart", {})
        for lid in list(cart):
            raw = request.POST.get(f"qty_{lid}", "")
            if raw.isdigit():
                if int(raw) == 0:
                    cart.pop(lid)
                else:
                    cart[lid] = int(raw)
        request.session["cart"] = cart
        return redirect("b2b:cart")
    if request.method == "POST" and "remove" in request.POST:
        cart = request.session.get("cart", {})
        cart.pop(request.POST["remove"], None)
        request.session["cart"] = cart
        return redirect("b2b:cart")
    lines, total = _cart_lines(request, buyer)
    has_cold = any(line["listing"].batch.is_cold_chain for line in lines)
    form = CheckoutForm(request.POST if "checkout" in request.POST else None,
                        initial={"delivery_address": buyer.address}, lang=lang(request))
    if request.method == "POST" and "checkout" in request.POST and form.is_valid():
        if has_cold and not form.cleaned_data["cold_chain_confirmed"]:
            messages.error(request, "Confirm cold-chain receiving capability to order "
                                    "cold-chain products, or remove them from the cart.")
        else:
            try:
                order, notes = shop.place_order(
                    buyer, [(line["listing"].pk, line["qty"]) for line in lines],
                    form.cleaned_data["delivery_address"],
                    form.cleaned_data["cold_chain_confirmed"])
                for n in notes:
                    messages.warning(request, n)
                if order:
                    request.session["cart"] = {}
                    return redirect("b2b:pay", pk=order.pk)
            except ValidationError as e:
                _err(request, e)
    return render(request, "b2b/cart.html", {"lines": lines, "total": total, "form": form,
                                             "has_cold": has_cold})


# --------------------------------------------------------------------------
# UC-36 payment, UC-37 tracking
# --------------------------------------------------------------------------
def _order_for(request, pk):
    order = get_object_or_404(PlatformOrder, pk=pk)
    if order.buyer_id != request.buyer.pk:
        raise Http404
    return order


@buyer_required()
def pay(request, pk):
    order = _order_for(request, pk)
    if order.status != PlatformOrder.PLACED:
        return redirect("b2b:order", pk=pk)
    options, reason = shop.payment_options(request.buyer, order)
    form = TransferForm(request.POST or None, request.FILES or None, lang=lang(request),
                        initial={"amount": order.total})
    if request.method == "POST":
        try:
            if request.POST.get("method") == "invoice":
                shop.choose_invoice(order)
                messages.success(request, "Order submitted for credit approval.")
                return redirect("b2b:order", pk=pk)
            if form.is_valid():
                shop.submit_bank_transfer(order, form.cleaned_data["receipt"],
                                          form.cleaned_data["amount"])
                messages.success(request, "Receipt received. Finance will confirm your payment.")
                return redirect("b2b:order", pk=pk)
        except ValidationError as e:
            _err(request, e)
    return render(request, "b2b/pay.html", {
        "order": order, "lines": order.lines.select_related("batch__product"), "form": form,
        "can_invoice": PlatformOrder.INVOICE in options, "invoice_reason": reason,
        "buyer": request.buyer, "bank": BANK_DETAILS})


@buyer_required()
def orders(request):
    items = request.buyer.orders.all()
    return render(request, "b2b/orders.html", {"items": items})


@buyer_required()
def order_detail(request, pk):
    order = _order_for(request, pk)
    if request.method == "POST":
        try:
            if "cancel" in request.POST:
                shop.cancel_order(order, "Cancelled by buyer", request.user)
                messages.success(request, "Order cancelled and the reservation released.")
            elif "delivered" in request.POST:
                shop.confirm_delivery(order)
                messages.success(request, "Thank you – delivery confirmed.")
            elif "problem" in request.POST:
                shop.report_delivery_problem(order, request.POST.get("text", "").strip())
                messages.success(request, "Problem reported to our warehouse team.")
            return redirect("b2b:order", pk=pk)
        except ValidationError as e:
            _err(request, e)
    history = [("Placed", order.created_at)]
    if order.payments.exists() or order.payment_method == PlatformOrder.INVOICE:
        first = order.payments.order_by("uploaded_at").first()
        history.append(("Awaiting Payment", first.uploaded_at if first else None))
    for label, ts in (("Confirmed", order.confirmed_at), ("Dispatched", order.dispatched_at),
                      ("Delivered", order.delivered_at), ("Cancelled", order.cancelled_at)):
        if ts:
            history.append((label, ts))
    invoice = getattr(order, "invoice", None)
    return render(request, "b2b/order.html", {
        "order": order, "lines": order.lines.select_related("batch__product"),
        "history": history, "invoice": invoice})


@buyer_required()
def invoice_pdf(request, pk):
    order = _order_for(request, pk)
    invoice = getattr(order, "invoice", None)
    if invoice is None:
        raise Http404
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=invoice.number)
    st = getSampleStyleSheet()
    buyer = order.buyer
    rows = [["Product", "Lot", "Expiry", "Qty", "Unit price (EGP)", "Total (EGP)"]]
    for line in order.lines.select_related("batch__product"):
        rows.append([line.batch.product.name, line.batch.lot_number,
                     str(line.batch.expiry_date), line.quantity, f"{line.unit_price:,.2f}",
                     f"{line.line_total:,.2f}"])
    rows.append(["", "", "", "", "Total", f"{invoice.amount:,.2f}"])
    t = Table(rows, repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCEFE7")),
                           ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]))
    story = [Paragraph("Kubbar International – Tax Invoice", st["Title"]),
             Paragraph(f"Invoice {invoice.number} · issued {invoice.issue_date} · due "
                       f"{invoice.due_date} · {invoice.get_status_display()}", st["Normal"]),
             Paragraph(f"Order {order.reference}", st["Normal"]), Spacer(1, 10),
             Paragraph(f"<b>Bill to:</b> {buyer.company_name}, {buyer.address}. Commercial "
                       f"register {buyer.commercial_register_no}, tax card {buyer.tax_card_no}.",
                       st["Normal"]), Spacer(1, 12), t]
    doc.build(story)
    resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{invoice.number}.pdf"'
    return resp

