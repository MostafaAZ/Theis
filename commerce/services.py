"""Business logic for the B2B platform (Modules 8 to 10) and the IMS screens
that serve it (buyer verification, payment confirmation, credit terms)."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.crypto import decrypt_bytes, encrypt_bytes
from core.models import AuditLog, Role, User
from core.rules import rule_int
from core.services import audit, email_buyer, notify, notify_roles
from inventory.models import Batch, Customer, Listing

from .models import Buyer, BuyerDocument, Invoice, OrderLine, PaymentProof, PlatformOrder

ALLOWED_TYPES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png"}


def b2b_url(path):
    return settings.B2B_BASE_URL.rstrip("/") + path


# --------------------------------------------------------------------------
# Files (FR-68)
# --------------------------------------------------------------------------
def validate_upload(f):
    """PDF, JPG or PNG up to 10 MB (UC-30 step 3)."""
    name = (f.name or "").lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if ext not in ALLOWED_EXT:
        raise ValidationError("Accepted formats are PDF, JPG and PNG.")
    if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f"Files must be {settings.MAX_UPLOAD_MB} MB or less.")
    head = f.read(8)
    f.seek(0)
    if not (head.startswith(b"%PDF") or head.startswith(b"\xff\xd8")
            or head.startswith(b"\x89PNG")):
        raise ValidationError("The file content is not a valid PDF, JPG or PNG.")


def _content_type(name):
    n = name.lower()
    if n.endswith(".pdf"):
        return "application/pdf"
    if n.endswith(".png"):
        return "image/png"
    return "image/jpeg"


def store_encrypted(field_file_owner, field_name, uploaded):
    data = encrypt_bytes(uploaded.read())
    getattr(field_file_owner, field_name).save(f"{uuid.uuid4().hex}.enc", ContentFile(data),
                                               save=False)


def read_encrypted(field_file):
    with field_file.open("rb") as fh:
        return decrypt_bytes(fh.read())


def save_document(buyer, doc_type, uploaded, expiry_date=None):
    validate_upload(uploaded)
    if doc_type == BuyerDocument.LICENCE:
        if not expiry_date:
            raise ValidationError("Enter the licence expiry date.")
        if expiry_date <= timezone.localdate():
            raise ValidationError("The licence has already expired.")
    buyer.documents.filter(doc_type=doc_type, is_current=True).update(is_current=False)
    doc = BuyerDocument(buyer=buyer, doc_type=doc_type, original_name=uploaded.name[:200],
                        content_type=_content_type(uploaded.name), size=uploaded.size,
                        expiry_date=expiry_date)
    store_encrypted(doc, "file", uploaded)
    doc.save()
    if doc_type == BuyerDocument.LICENCE:
        buyer.licence_expiry = expiry_date
        buyer.licence_alert_sent = False
        buyer.save(update_fields=["licence_expiry", "licence_alert_sent"])
    return doc


def current_documents(buyer):
    return {d.doc_type: d for d in buyer.documents.filter(is_current=True)}


def missing_mandatory(buyer):
    docs = current_documents(buyer)
    return [t for t in BuyerDocument.MANDATORY if t not in docs]


# --------------------------------------------------------------------------
# Module 8: Buyer registration and verification
# --------------------------------------------------------------------------
@transaction.atomic
def register_buyer(*, email, password, company_name, buyer_type, market, commercial_register_no,
                   tax_card_no, address, contact_person, phone, owner_full_name,
                   owner_national_id, documents, licence_expiry, consent):
    """UC-29 / UC-30 / FR-66 to FR-70."""
    if not consent:
        raise ValidationError("You must accept the Terms of Service and the data processing "
                              "consent.")
    email = email.strip().lower()
    if User.objects.filter(email__iexact=email).exists():
        raise ValidationError("This email address is already registered.")
    if Buyer.objects.filter(commercial_register_no=commercial_register_no.strip()).exists():
        raise ValidationError("This commercial register number is already registered.")
    for t in BuyerDocument.MANDATORY:
        if t not in documents:
            raise ValidationError("All mandatory documents must be uploaded.")
    user = User.objects.create_user(email=email, password=password,
                                    account_type=User.BUYER, first_name=contact_person[:150])
    now = timezone.now()
    buyer = Buyer(user=user, company_name=company_name, buyer_type=buyer_type, market=market,
                  commercial_register_no=commercial_register_no.strip(),
                  tax_card_no=tax_card_no, address=address, contact_person=contact_person,
                  phone=phone, owner_full_name=owner_full_name, terms_accepted_at=now,
                  consent_at=now)
    buyer.owner_national_id = owner_national_id
    match = Customer.objects.filter(
        commercial_register_no=commercial_register_no.strip()).first()
    buyer.matched_customer = match
    buyer.save()
    for doc_type, f in documents.items():
        save_document(buyer, doc_type, f,
                      licence_expiry if doc_type == BuyerDocument.LICENCE else None)
    if match:
        # FR-70: route to the existing customer's Sales Representative.
        rep = match.sales_rep
        msg = (f"Platform registration by {company_name} matches existing customer "
               f"{match.name}.")
        if rep:
            notify(rep, msg, f"/buyers/{buyer.pk}/")
        else:
            notify_roles(Role.SALES, msg, f"/buyers/{buyer.pk}/")
    email_buyer(user, "Confirm your email address",
                "Welcome to the Kubbar B2B clearance platform.\n\nConfirm your email address: "
                + b2b_url(f"/register/confirm/{buyer.email_token}/"))
    return buyer


@transaction.atomic
def confirm_email(token):
    buyer = Buyer.objects.select_related("user").filter(email_token=token).first()
    if buyer is None:
        raise ValidationError("This confirmation link is not valid.")
    if buyer.status == Buyer.PENDING_EMAIL:
        buyer.status = Buyer.PENDING
        buyer.save(update_fields=["status"])
        buyer.user.email_verified = True
        buyer.user.save(update_fields=["email_verified"])
        notify_roles(Role.REGULATORY, f"New buyer registration pending verification: "
                                      f"{buyer.company_name}.", f"/buyers/{buyer.pk}/")
    return buyer


@transaction.atomic
def resubmit_document(buyer, doc_type, uploaded, expiry_date=None):
    """UC-30 when verification requests a resubmission."""
    save_document(buyer, doc_type, uploaded, expiry_date)
    pending = buyer.documents.filter(is_current=True,
                                     review_status=BuyerDocument.RESUBMIT).exists()
    if buyer.status == Buyer.RESUBMISSION and not pending:
        buyer.status = Buyer.PENDING
        buyer.save(update_fields=["status"])
        notify_roles(Role.REGULATORY, f"{buyer.company_name} resubmitted documents.",
                     f"/buyers/{buyer.pk}/")


@transaction.atomic
def review_document(user, doc, decision, comment=""):
    if decision not in (BuyerDocument.APPROVED, BuyerDocument.RESUBMIT, BuyerDocument.REJECTED):
        raise ValidationError("Unknown decision.")
    if decision != BuyerDocument.APPROVED and not comment:
        raise ValidationError("Give a reason when a document is not approved.")
    doc.review_status = decision
    doc.review_comment = comment
    doc.reviewed_by = user
    doc.reviewed_at = timezone.now()
    doc.save()


@transaction.atomic
def verify_buyer(user, buyer, categories, cold_chain_eligible):
    """UC-31 / FR-71, FR-72: only when every mandatory document is approved."""
    docs = current_documents(buyer)
    for t in BuyerDocument.MANDATORY:
        d = docs.get(t)
        if d is None or d.review_status != BuyerDocument.APPROVED:
            raise ValidationError("Every mandatory document must be approved first.")
    if not categories:
        raise ValidationError("Select at least one licensed category.")
    cold_doc = docs.get(BuyerDocument.COLD_STORAGE)
    if cold_chain_eligible and not (cold_doc and cold_doc.review_status
                                    == BuyerDocument.APPROVED):
        raise ValidationError("Cold-chain eligibility needs an approved cold storage proof.")
    old = buyer.get_status_display()
    buyer.status = Buyer.VERIFIED
    buyer.cold_chain_eligible = cold_chain_eligible
    buyer.verified_by = user
    buyer.decided_at = timezone.now()
    buyer.decision_reason = ""
    buyer.ordering_suspended = False
    buyer.save()
    buyer.categories.set(categories)
    audit(user, AuditLog.VERIFICATION, f"Buyer {buyer.company_name} verified",
          f"BUYER-{buyer.pk}", old=old,
          new=f"Verified; categories={', '.join(c.name for c in categories)}; "
              f"cold_chain={cold_chain_eligible}")
    email_buyer(buyer.user, "Your account is verified",
                "Your company account has been verified. You can now see prices and order "
                "clearance stock.\n\n" + b2b_url("/catalogue/"))


@transaction.atomic
def request_resubmission(user, buyer, reason):
    if not reason:
        raise ValidationError("Give a reason for the resubmission request.")
    buyer.status = Buyer.RESUBMISSION
    buyer.decision_reason = reason
    buyer.decided_at = timezone.now()
    buyer.save()
    audit(user, AuditLog.VERIFICATION, f"Resubmission requested from {buyer.company_name}",
          f"BUYER-{buyer.pk}", new=reason)
    docs = [d.get_doc_type_display() for d in buyer.documents.filter(
        is_current=True, review_status=BuyerDocument.RESUBMIT)]
    email_buyer(buyer.user, "Please resubmit documents",
                f"Reason: {reason}\nDocuments: {', '.join(docs) or 'see your account'}\n\n"
                + b2b_url("/account/"))


@transaction.atomic
def reject_buyer(user, buyer, reason):
    """UC-31 A2: documents are deleted after the retention period (NFR-26)."""
    if not reason:
        raise ValidationError("Give a reason for the rejection.")
    buyer.status = Buyer.REJECTED
    buyer.decision_reason = reason
    buyer.decided_at = timezone.now()
    buyer.verified_by = user
    buyer.save()
    audit(user, AuditLog.VERIFICATION, f"Buyer {buyer.company_name} rejected",
          f"BUYER-{buyer.pk}", new=reason)
    email_buyer(buyer.user, "Your application was not approved", f"Reason: {reason}")


def purge_rejected_documents(today=None):
    today = today or timezone.localdate()
    cutoff = timezone.now() - timedelta(days=rule_int("rejected_doc_retention_days"))
    for buyer in Buyer.objects.filter(status=Buyer.REJECTED, decided_at__lt=cutoff):
        for doc in buyer.documents.all():
            doc.file.delete(save=False)
            doc.delete()


def licence_checks(today=None):
    """FR-74: alert before a buyer licence expires; suspend ordering on expiry."""
    today = today or timezone.localdate()
    alert_days = rule_int("licence_alert_days")
    for buyer in Buyer.objects.filter(status=Buyer.VERIFIED, licence_expiry__isnull=False):
        days = (buyer.licence_expiry - today).days
        if days <= alert_days and not buyer.licence_alert_sent and days >= 0:
            notify_roles(Role.REGULATORY, f"Licence of {buyer.company_name} expires in {days} "
                                          f"days.", f"/buyers/{buyer.pk}/")
            buyer.licence_alert_sent = True
            buyer.save(update_fields=["licence_alert_sent"])
        if days < 0 and not buyer.ordering_suspended:
            buyer.ordering_suspended = True
            buyer.save(update_fields=["ordering_suspended"])
            email_buyer(buyer.user, "Ordering suspended",
                        "Your veterinary trading licence has expired. Upload the renewed "
                        "licence to resume ordering.\n\n" + b2b_url("/account/"))


@transaction.atomic
def set_buyer_suspension(user, buyer, suspend, reason):
    """UC-41 A2 / FR-51."""
    if not reason:
        raise ValidationError("A reason is required.")
    old = buyer.get_status_display()
    if suspend:
        buyer.status = Buyer.SUSPENDED
    else:
        buyer.status = Buyer.VERIFIED if buyer.verified_by else Buyer.PENDING
    buyer.decision_reason = reason
    buyer.save()
    audit(user, AuditLog.CONFIGURATION,
          f"Buyer {buyer.company_name} {'suspended' if suspend else 'reactivated'}",
          f"BUYER-{buyer.pk}", old=old, new=buyer.get_status_display() + f" ({reason})")
    notify_roles(Role.REGULATORY, f"Buyer {buyer.company_name} "
                                  f"{'suspended' if suspend else 'reactivated'}: {reason}",
                 f"/buyers/{buyer.pk}/")


# --------------------------------------------------------------------------
# Module 9: Catalogue and ordering
# --------------------------------------------------------------------------
def catalogue_for(buyer):
    """FR-75: active listings in the buyer's market and licensed categories."""
    return (Listing.objects.filter(status=Listing.ACTIVE, available_qty__gt=0,
                                   market=buyer.market,
                                   batch__product__category__in=buyer.categories.all())
            .select_related("batch__product__category"))


def listing_unavailable_reason(buyer, listing):
    if listing.status != Listing.ACTIVE or listing.available_qty <= 0:
        return "This listing is no longer available."
    if listing.market_id != buyer.market_id:
        return "Not available in your market."
    if not buyer.categories.filter(pk=listing.batch.product.category_id).exists():
        return "Not in your licensed categories."
    if listing.batch.is_cold_chain and not buyer.cold_chain_eligible:
        return "Not Available to Your Account (cold-chain)."
    return ""


def next_reference(prefix, model, field):
    today = timezone.localdate()
    stem = f"{prefix}{today:%y%m}-"
    count = model.objects.filter(**{f"{field}__startswith": stem}).count() + 1
    while model.objects.filter(**{field: f"{stem}{count:04d}"}).exists():
        count += 1
    return f"{stem}{count:04d}"


@transaction.atomic
def place_order(buyer, items, delivery_address, cold_chain_confirmed=False):
    """UC-34 / UC-35 / FR-77 to FR-79, NFR-09, NFR-10.

    ``items`` is a list of ``(listing_id, quantity)``. Each reservation is a
    single conditional UPDATE, so when two buyers race for the last units
    exactly one succeeds and the other gets what remains. Returns the order
    (or None) and a list of messages about lines that were reduced or removed.
    """
    if not buyer.can_order:
        raise ValidationError("Your account cannot place orders at the moment.")
    if not delivery_address:
        raise ValidationError("Enter a delivery address.")
    notes, reserved = [], []
    for listing_id, qty in items:
        listing = Listing.objects.select_related("batch__product").filter(pk=listing_id).first()
        if listing is None or qty <= 0:
            continue
        reason = listing_unavailable_reason(buyer, listing)
        if reason:
            notes.append(f"{listing.batch.product.name}: {reason} Removed from your order.")
            continue
        if listing.batch.is_cold_chain and not cold_chain_confirmed:
            notes.append(f"{listing.batch.product.name}: cold-chain receiving capability not "
                         f"confirmed. Removed from your order.")
            continue
        take = qty
        while take > 0:
            updated = Listing.objects.filter(pk=listing.pk, status=Listing.ACTIVE,
                                             available_qty__gte=take).update(
                available_qty=F("available_qty") - take)
            if updated:
                break
            listing.refresh_from_db()
            take = min(take, max(0, listing.available_qty))
            if listing.status != Listing.ACTIVE:
                take = 0
        if take <= 0:
            notes.append(f"{listing.batch.product.name}: sold out before checkout. Removed.")
            continue
        if take < qty:
            notes.append(f"{listing.batch.product.name}: only {take} of {qty} were still "
                         f"available.")
        Batch.objects.filter(pk=listing.batch_id).update(qty_reserved=F("qty_reserved") + take)
        Listing.objects.filter(pk=listing.pk, available_qty__lte=0).update(
            status=Listing.SOLD_OUT)
        reserved.append((listing, take))
    if not reserved:
        return None, notes or ["Nothing could be reserved."]
    order = PlatformOrder.objects.create(
        reference=next_reference("KB-", PlatformOrder, "reference"), buyer=buyer,
        delivery_address=delivery_address, cold_chain_confirmed=cold_chain_confirmed,
        reservation_expires_at=timezone.now() + timedelta(
            hours=rule_int("reservation_hold_hours")))
    total = Decimal(0)
    for listing, qty in reserved:
        OrderLine.objects.create(order=order, listing=listing, batch=listing.batch,
                                 quantity=qty, unit_price=listing.clearance_price)
        total += listing.clearance_price * qty
    order.total = total
    order.save(update_fields=["total"])
    audit(buyer.user, AuditLog.ORDER, f"Order {order.reference} placed", order.reference,
          new=f"{total} EGP")
    return order, notes


def payment_options(buyer, order):
    """FR-81, FR-84: invoice only for credit-approved buyers within limit and
    with no overdue invoice."""
    options = [PlatformOrder.BANK_TRANSFER]
    reason = ""
    if buyer.is_credit_approved:
        if buyer.has_overdue_invoice():
            reason = "You have an overdue invoice."
        elif buyer.outstanding_credit() + order.total > buyer.credit_limit:
            reason = "This order would exceed your credit limit."
        else:
            options.append(PlatformOrder.INVOICE)
    return options, reason


@transaction.atomic
def submit_bank_transfer(order, uploaded, amount):
    """UC-36 main flow."""
    if order.status not in (PlatformOrder.PLACED,):
        raise ValidationError("Payment cannot be submitted for this order.")
    validate_upload(uploaded)
    proof = PaymentProof(order=order, original_name=uploaded.name[:200],
                         content_type=_content_type(uploaded.name), amount=amount)
    store_encrypted(proof, "file", uploaded)
    proof.save()
    order.payment_method = PlatformOrder.BANK_TRANSFER
    order.status = PlatformOrder.AWAITING_PAYMENT
    order.save(update_fields=["payment_method", "status"])
    notify_roles(Role.FINANCE, f"Transfer receipt uploaded for order {order.reference} "
                               f"({order.total:,.2f} EGP).", f"/payments/{order.pk}/")
    notify_order_status(order)


@transaction.atomic
def choose_invoice(order):
    """UC-36 A1."""
    options, reason = payment_options(order.buyer, order)
    if PlatformOrder.INVOICE not in options:
        raise ValidationError(reason or "Pay on Invoice is not available to your account.")
    order.payment_method = PlatformOrder.INVOICE
    order.status = PlatformOrder.AWAITING_PAYMENT
    order.save(update_fields=["payment_method", "status"])
    notify_roles(Role.FINANCE, f"Credit order {order.reference} ({order.total:,.2f} EGP) "
                               f"awaits approval.", f"/payments/{order.pk}/")
    notify_order_status(order)


def notify_order_status(order):
    """FR-86: tell the buyer about each status change."""
    email_buyer(order.buyer.user, f"Order {order.reference}: {order.awaiting_label}",
                f"Your order {order.reference} is now {order.awaiting_label}.\n\n"
                + b2b_url(f"/orders/{order.pk}/"))


@transaction.atomic
def release_reservation(order):
    for line in order.lines.all():
        Listing.objects.filter(pk=line.listing_id).update(
            available_qty=F("available_qty") + line.quantity)
        Listing.objects.filter(pk=line.listing_id, status=Listing.SOLD_OUT,
                               available_qty__gt=0).update(status=Listing.ACTIVE)
        Batch.objects.filter(pk=line.batch_id).update(qty_reserved=F("qty_reserved")
                                                      - line.quantity)


@transaction.atomic
def cancel_order(order, reason, user=None):
    """UC-37 A1 and UC-35 A2."""
    order = PlatformOrder.objects.select_for_update().get(pk=order.pk)
    if order.status not in (PlatformOrder.PLACED, PlatformOrder.AWAITING_PAYMENT):
        raise ValidationError("Only orders not yet confirmed can be cancelled.")
    release_reservation(order)
    order.status = PlatformOrder.CANCELLED
    order.cancelled_at = timezone.now()
    order.cancel_reason = reason
    order.save()
    audit(user, AuditLog.ORDER, f"Order {order.reference} cancelled", order.reference,
          new=reason)
    notify_order_status(order)
    return order


def release_expired_reservations():
    """Cancel orders whose reservation hold lapsed without confirmation."""
    expired = PlatformOrder.objects.filter(
        status__in=[PlatformOrder.PLACED, PlatformOrder.AWAITING_PAYMENT],
        reservation_expires_at__lt=timezone.now())
    n = 0
    for order in expired:
        cancel_order(order, "Reservation hold expired without payment confirmation")
        n += 1
    return n


# --------------------------------------------------------------------------
# Module 10: Payment and fulfilment
# --------------------------------------------------------------------------
@transaction.atomic
def confirm_payment(user, order):
    """UC-38 / FR-83, FR-85, FR-88."""
    order = PlatformOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != PlatformOrder.AWAITING_PAYMENT:
        raise ValidationError("This order is not awaiting payment.")
    today = timezone.localdate()
    if order.payment_method == PlatformOrder.BANK_TRANSFER:
        proof = order.payments.filter(status=PaymentProof.SUBMITTED).first()
        if proof is None:
            raise ValidationError("No transfer receipt is awaiting confirmation.")
        proof.status = PaymentProof.CONFIRMED
        proof.reviewed_by = user
        proof.reviewed_at = timezone.now()
        proof.save()
        due, paid = today, True
    else:
        buyer = order.buyer
        if buyer.has_overdue_invoice():
            raise ValidationError("The buyer has an overdue invoice.")
        due, paid = today + timedelta(days=int(buyer.payment_terms)), False
    invoice = Invoice.objects.create(
        number=next_reference("INV-", Invoice, "number"), order=order, buyer=order.buyer,
        amount=order.total, issue_date=today, due_date=due,
        status=Invoice.PAID if paid else Invoice.UNPAID,
        paid_at=timezone.now() if paid else None)
    order.status = PlatformOrder.CONFIRMED
    order.confirmed_by = user
    order.confirmed_at = timezone.now()
    order.reservation_expires_at = None
    order.save()
    audit(user, AuditLog.APPROVAL, f"Payment confirmed for {order.reference}", order.reference,
          new=f"Invoice {invoice.number} {invoice.amount} EGP")
    notify_roles(Role.WAREHOUSE, f"Order {order.reference} confirmed and ready for dispatch.",
                 f"/dispatch/{order.pk}/")
    notify_order_status(order)
    return invoice


@transaction.atomic
def reject_payment(user, order, reason):
    """UC-38 A1: the buyer resubmits within the reservation hold."""
    if not reason:
        raise ValidationError("Give a reason.")
    if order.status != PlatformOrder.AWAITING_PAYMENT:
        raise ValidationError("This order is not awaiting payment.")
    order.payments.filter(status=PaymentProof.SUBMITTED).update(
        status=PaymentProof.REJECTED, reviewed_by=user, reviewed_at=timezone.now(),
        comment=reason)
    order.status = PlatformOrder.PLACED
    order.payment_rejection_reason = reason
    order.save(update_fields=["status", "payment_rejection_reason"])
    audit(user, AuditLog.APPROVAL, f"Payment rejected for {order.reference}", order.reference,
          new=reason)
    email_buyer(order.buyer.user, f"Payment for {order.reference} not confirmed",
                f"Reason: {reason}\nPlease resubmit before your reservation expires on "
                f"{timezone.localtime(order.reservation_expires_at):%d %b %Y %H:%M}.\n\n"
                + b2b_url(f"/orders/{order.pk}/pay/"))


@transaction.atomic
def mark_invoice_paid(user, invoice):
    invoice.status = Invoice.PAID
    invoice.paid_at = timezone.now()
    invoice.save()
    audit(user, AuditLog.APPROVAL, f"Invoice {invoice.number} marked paid", invoice.number)


@transaction.atomic
def set_payment_terms(user, buyer, terms, credit_limit):
    """UC-39 / FR-83."""
    if buyer.status != Buyer.VERIFIED:
        raise ValidationError("Only verified buyers can be given payment terms.")
    if terms != Buyer.BANK_ONLY and credit_limit <= 0:
        raise ValidationError("Enter a credit limit for invoice terms.")
    old = f"{buyer.get_payment_terms_display()} / {buyer.credit_limit}"
    buyer.payment_terms = terms
    buyer.credit_limit = credit_limit if terms != Buyer.BANK_ONLY else Decimal(0)
    buyer.save(update_fields=["payment_terms", "credit_limit"])
    audit(user, AuditLog.CONFIGURATION, f"Payment terms for {buyer.company_name}",
          f"BUYER-{buyer.pk}", old=old,
          new=f"{buyer.get_payment_terms_display()} / {buyer.credit_limit}")


@transaction.atomic
def confirm_delivery(order):
    if order.status != PlatformOrder.DISPATCHED:
        raise ValidationError("Only dispatched orders can be confirmed as delivered.")
    order.status = PlatformOrder.DELIVERED
    order.delivered_at = timezone.now()
    order.save(update_fields=["status", "delivered_at"])
    notify_order_status(order)


@transaction.atomic
def report_delivery_problem(order, text):
    if not text:
        raise ValidationError("Describe the problem.")
    order.delivery_issue = (order.delivery_issue + "\n" if order.delivery_issue else "") + \
        f"{timezone.localtime():%d %b %Y %H:%M}: {text}"
    order.save(update_fields=["delivery_issue"])
    notify_roles(Role.WAREHOUSE, f"Delivery problem reported on {order.reference}: {text[:80]}",
                 f"/dispatch/{order.pk}/", level="warning")
