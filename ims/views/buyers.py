"""IMS screens for the platform: buyer verification (UC-31), payment
confirmation (UC-38) and buyer credit (UC-39)."""
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from commerce import services as shop
from commerce.models import Buyer, BuyerDocument, Invoice, PaymentProof, PlatformOrder
from core.models import AuditLog, Role
from core.services import audit

from ..access import role_required
from ..forms import PaymentTermsForm, VerifyForm
from .stock import _err

DOC_VIEWERS = (Role.REGULATORY, Role.ADMIN)


@role_required(Role.REGULATORY, Role.FINANCE, Role.SALES, Role.ADMIN)
def buyers(request):
    status = request.GET.get("status", "")
    qs = Buyer.objects.select_related("buyer_type", "market", "matched_customer")
    if status:
        qs = qs.filter(status=status)
    return render(request, "ims/buyers/list.html",
                  {"buyers": qs, "status": status, "statuses": Buyer.STATUSES})


@role_required(Role.REGULATORY, Role.FINANCE, Role.SALES, Role.ADMIN)
def buyer_detail(request, pk):
    buyer = get_object_or_404(Buyer.objects.select_related("buyer_type", "market"), pk=pk)
    user = request.user
    is_ra = user.has_role(Role.REGULATORY)
    docs = list(buyer.documents.filter(is_current=True))
    form = VerifyForm(request.POST or None, initial={
        "categories": buyer.categories.all(), "cold_chain_eligible": buyer.cold_chain_eligible})
    if request.method == "POST":
        if not is_ra:
            raise PermissionDenied("Only the Regulatory Affairs Officer verifies buyers.")
        try:
            if "review" in request.POST:
                doc = get_object_or_404(BuyerDocument, pk=request.POST["doc"], buyer=buyer)
                shop.review_document(user, doc, request.POST["review"],
                                     request.POST.get(f"comment_{doc.pk}", "").strip())
                messages.success(request, f"{doc.get_doc_type_display()}: "
                                          f"{doc.get_review_status_display()}.")
            elif "verify" in request.POST and form.is_valid():
                shop.verify_buyer(user, buyer, list(form.cleaned_data["categories"]),
                                  form.cleaned_data["cold_chain_eligible"])
                messages.success(request, "Account verified and the buyer emailed.")
            elif "resubmit" in request.POST:
                shop.request_resubmission(user, buyer, request.POST.get("reason", "").strip())
                messages.success(request, "Resubmission requested.")
            elif "reject" in request.POST:
                shop.reject_buyer(user, buyer, request.POST.get("reason", "").strip())
                messages.success(request, "Application rejected. Documents will be deleted "
                                          "after the retention period.")
            return redirect("ims:buyer_detail", pk=pk)
        except ValidationError as e:
            _err(request, e)
    return render(request, "ims/buyers/detail.html", {
        "buyer": buyer, "docs": docs, "form": form, "is_ra": is_ra,
        "can_see_docs": user.has_role(*DOC_VIEWERS),
        "orders": buyer.orders.all()[:10]})


@role_required(*DOC_VIEWERS)
def buyer_document(request, pk):
    """Decrypt and stream a document; every view is logged (NFR-25)."""
    doc = get_object_or_404(BuyerDocument, pk=pk)
    data = shop.read_encrypted(doc.file)
    audit(request.user, AuditLog.DOCUMENT_VIEW,
          f"Viewed {doc.get_doc_type_display()} of {doc.buyer.company_name}",
          f"DOC-{doc.pk}", request=request)
    resp = HttpResponse(data, content_type=doc.content_type)
    resp["Content-Disposition"] = f'inline; filename="{doc.original_name}"'
    resp["Cache-Control"] = "no-store"
    return resp


@role_required(Role.FINANCE)
def payment_proof(request, pk):
    proof = get_object_or_404(PaymentProof, pk=pk)
    audit(request.user, AuditLog.DOCUMENT_VIEW, f"Viewed transfer receipt for "
                                                f"{proof.order.reference}", proof.order.reference)
    resp = HttpResponse(shop.read_encrypted(proof.file), content_type=proof.content_type)
    resp["Content-Disposition"] = f'inline; filename="{proof.original_name}"'
    resp["Cache-Control"] = "no-store"
    return resp


@role_required(Role.FINANCE)
def payments(request):
    queue = PlatformOrder.objects.filter(status=PlatformOrder.AWAITING_PAYMENT).select_related(
        "buyer")
    invoices = Invoice.objects.filter(status=Invoice.UNPAID).select_related("buyer", "order")
    return render(request, "ims/buyers/payments.html", {"queue": queue, "invoices": invoices})


@role_required(Role.FINANCE)
def payment_detail(request, pk):
    order = get_object_or_404(PlatformOrder.objects.select_related("buyer"), pk=pk)
    if request.method == "POST":
        try:
            if "confirm" in request.POST:
                inv = shop.confirm_payment(request.user, order)
                messages.success(request, f"Order {order.reference} confirmed; invoice "
                                          f"{inv.number} issued and the order released to "
                                          f"dispatch.")
                return redirect("ims:payments")
            if "reject" in request.POST:
                shop.reject_payment(request.user, order, request.POST.get("reason", "").strip())
                messages.success(request, "Payment rejected; the buyer has been asked to "
                                          "resubmit.")
                return redirect("ims:payments")
            if "paid" in request.POST:
                invoice = get_object_or_404(Invoice, pk=request.POST["invoice"])
                shop.mark_invoice_paid(request.user, invoice)
                messages.success(request, f"Invoice {invoice.number} marked paid.")
                return redirect("ims:payments")
        except ValidationError as e:
            _err(request, e)
    buyer = order.buyer
    return render(request, "ims/buyers/payment_detail.html", {
        "order": order, "lines": order.lines.select_related("batch__product"),
        "proofs": order.payments.all(), "buyer": buyer,
        "outstanding": buyer.outstanding_credit(), "overdue": buyer.has_overdue_invoice()})


@role_required(Role.FINANCE)
def credit(request):
    items = Buyer.objects.filter(status=Buyer.VERIFIED).select_related("buyer_type")
    return render(request, "ims/buyers/credit.html", {"items": items})


@role_required(Role.FINANCE)
def credit_edit(request, pk):
    buyer = get_object_or_404(Buyer, pk=pk)
    form = PaymentTermsForm(request.POST or None, initial={
        "payment_terms": buyer.payment_terms, "credit_limit": buyer.credit_limit})
    overdue = buyer.has_overdue_invoice()
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if overdue and d["payment_terms"] != Buyer.BANK_ONLY and "confirm_overdue" not in \
                request.POST:
            messages.warning(request, "This buyer has an overdue invoice. Tick the confirmation "
                                      "to grant credit anyway.")
        else:
            try:
                shop.set_payment_terms(request.user, buyer, d["payment_terms"],
                                       d["credit_limit"])
                messages.success(request, "Payment terms saved; they apply to future orders.")
                return redirect("ims:credit")
            except ValidationError as e:
                _err(request, e)
    return render(request, "ims/buyers/credit_edit.html", {
        "buyer": buyer, "form": form, "overdue": overdue,
        "orders": buyer.orders.all()[:15], "invoices": buyer.invoices.all()[:15],
        "outstanding": buyer.outstanding_credit()})
