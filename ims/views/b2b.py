"""IMS side of the platform: 'Post to B2B' and the platform order overview."""
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from commerce.models import PlatformOrder
from core.models import Role
from core.rules import rule
from inventory import services as inv
from inventory.models import Batch, SurplusRelease

from ..access import ALL_INTERNAL, role_required
from ..forms import BootstrapMixin
from .stock import _err

ORDER_VIEWERS = ALL_INTERNAL


class PostForm(BootstrapMixin, forms.Form):
    quantity = forms.IntegerField(min_value=1, label="Quantity to offer")
    reason = forms.ChoiceField(choices=SurplusRelease.REASONS,
                               label="Why existing customers can't take it")
    comment = forms.CharField(required=False, max_length=255,
                              widget=forms.TextInput(attrs={"placeholder": "Optional note"}))
    clearance_price = forms.DecimalField(required=False, max_digits=12, decimal_places=2,
                                         label="Clearance price per unit (EGP)")


@role_required(Role.SALES, Role.COO)
def post_list(request):
    eligible, blocked = inv.b2b_candidates()
    return render(request, "ims/b2b/post_list.html", {"eligible": eligible, "blocked": blocked,
                                                      "is_coo": request.user.has_role(Role.COO)})


@role_required(Role.SALES, Role.COO)
def post_batch(request, pk):
    batch = get_object_or_404(Batch.objects.select_related("product__category"), pk=pk)
    eligible = {b.pk: free for b, free, _r in inv.b2b_candidates()[0]}
    if batch.pk not in eligible:
        messages.error(request, "This batch cannot be posted to B2B right now.")
        return redirect("ims:post_b2b")
    is_coo = request.user.has_role(Role.COO)
    lo, hi = inv.price_band(batch.product.original_price)
    form = PostForm(request.POST or None, initial={
        "quantity": eligible[batch.pk], "clearance_price": hi, "reason": "no_demand"})
    if not is_coo:
        form.fields.pop("clearance_price")
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            _release, listing = inv.post_to_b2b(request.user, batch, d["quantity"], d["reason"],
                                                d["comment"], d.get("clearance_price"))
            if listing:
                messages.success(request, f"Posted: {batch.product.name} is live on the B2B "
                                          f"platform as listing #{listing.pk} at "
                                          f"{listing.clearance_price:,.2f} EGP.")
                return redirect("ims:listings")
            messages.success(request, "Sent to the COO for approval. You'll be notified when it "
                                      "goes live.")
            return redirect("ims:post_b2b")
        except ValidationError as e:
            _err(request, e)
    return render(request, "ims/b2b/post_batch.html", {
        "batch": batch, "form": form, "free": eligible[batch.pk], "band": (lo, hi),
        "is_coo": is_coo, "min_pct": rule("price_band_min_pct"),
        "max_pct": rule("price_band_max_pct"), "offers": batch.offers.select_related("customer"),
        "unsold": inv.projected_unsold(batch)})


@role_required(*ORDER_VIEWERS)
def platform_orders(request):
    status = request.GET.get("status", "")
    qs = PlatformOrder.objects.select_related("buyer").prefetch_related("lines__batch__product")
    if status:
        qs = qs.filter(status=status)
    counts = {s: PlatformOrder.objects.filter(status=s).count() for s, _l in
              PlatformOrder.STATUSES}
    return render(request, "ims/b2b/orders.html", {
        "orders": qs[:300], "status": status, "statuses": PlatformOrder.STATUSES,
        "counts": counts})


@role_required(*ORDER_VIEWERS)
def platform_order(request, pk):
    order = get_object_or_404(PlatformOrder.objects.select_related("buyer"), pk=pk)
    return render(request, "ims/b2b/order.html", {
        "order": order, "lines": order.lines.select_related("batch__product"),
        "payments": order.payments.all(), "invoice": getattr(order, "invoice", None)})


MESSAGE_HANDLERS = (Role.SALES, Role.REGULATORY, Role.FINANCE, Role.WAREHOUSE, Role.COO)


@role_required(*MESSAGE_HANDLERS)
def messages_inbox(request):
    from commerce.models import SupportMessage

    status = request.GET.get("status", "open")
    qs = SupportMessage.objects.select_related("buyer", "order")
    if status != "all":
        qs = qs.filter(status=status)
    return render(request, "ims/b2b/messages.html", {"items": qs[:200], "status": status})


@role_required(*MESSAGE_HANDLERS)
def message_detail(request, pk):
    from commerce.models import SupportMessage
    from core.services import email_buyer
    from django.core.mail import send_mail
    from django.utils import timezone

    msg = get_object_or_404(SupportMessage, pk=pk)
    if request.method == "POST":
        reply = request.POST.get("reply", "").strip()
        if not reply:
            messages.error(request, "Write a reply first.")
        else:
            msg.reply, msg.replied_by, msg.replied_at = reply, request.user, timezone.now()
            msg.status = SupportMessage.ANSWERED
            msg.save()
            body = f"Your message:\n{msg.message}\n\nOur reply:\n{reply}"
            if msg.buyer:
                email_buyer(msg.buyer.user, "Reply to your message", body)
            else:
                send_mail("Kubbar B2B: reply to your message", body, None, [msg.email],
                          fail_silently=True)
            messages.success(request, "Reply sent by email and shown in the buyer's account.")
            return redirect("ims:messages_inbox")
    return render(request, "ims/b2b/message.html", {"msg": msg})
