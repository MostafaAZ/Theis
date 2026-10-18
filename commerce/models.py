"""B2B E-Commerce domain: buyers and verification, orders, payments and
invoices (Platform Modules 8 to 10)."""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.crypto import decrypt_text, encrypt_text
from core.models import BuyerType, Market, ProductCategory

User = settings.AUTH_USER_MODEL


class Buyer(models.Model):
    PENDING_EMAIL = "pending_email"
    PENDING = "pending"
    RESUBMISSION = "resubmission"
    VERIFIED = "verified"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    STATUSES = [(PENDING_EMAIL, "Email Not Confirmed"), (PENDING, "Pending Verification"),
                (RESUBMISSION, "Resubmission Requested"), (VERIFIED, "Verified"),
                (REJECTED, "Rejected"), (SUSPENDED, "Suspended")]

    BANK_ONLY = "bank_only"
    TERMS = [(BANK_ONLY, "Bank Transfer Only"), ("15", "15 days"), ("30", "30 days"),
             ("45", "45 days")]

    user = models.OneToOneField(User, on_delete=models.PROTECT, related_name="buyer")
    company_name = models.CharField(max_length=160)
    buyer_type = models.ForeignKey(BuyerType, on_delete=models.PROTECT)
    market = models.ForeignKey(Market, on_delete=models.PROTECT)
    commercial_register_no = models.CharField(max_length=40, unique=True)
    tax_card_no = models.CharField(max_length=40)
    address = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=120)
    phone = models.CharField(max_length=30)
    owner_full_name = models.CharField(max_length=160)
    owner_national_id_enc = models.TextField(blank=True)
    status = models.CharField(max_length=15, choices=STATUSES, default=PENDING_EMAIL,
                              db_index=True)
    categories = models.ManyToManyField(ProductCategory, blank=True, related_name="buyers")
    cold_chain_eligible = models.BooleanField(default=False)
    licence_expiry = models.DateField(null=True, blank=True)
    licence_alert_sent = models.BooleanField(default=False)
    ordering_suspended = models.BooleanField(default=False)
    listing_alerts = models.BooleanField(default=False,
                                         help_text="Email me when new listings appear")
    payment_terms = models.CharField(max_length=10, choices=TERMS, default=BANK_ONLY)
    credit_limit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    consent_at = models.DateTimeField(null=True, blank=True)
    registered_at = models.DateTimeField(default=timezone.now)
    email_token = models.UUIDField(default=uuid.uuid4, editable=False)
    matched_customer = models.ForeignKey("inventory.Customer", null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="+")
    verified_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-registered_at"]

    def __str__(self):
        return self.company_name

    @property
    def owner_national_id(self):
        return decrypt_text(self.owner_national_id_enc)

    @owner_national_id.setter
    def owner_national_id(self, value):
        self.owner_national_id_enc = encrypt_text(value)

    @property
    def masked_national_id(self):
        nid = self.owner_national_id
        return ("•" * max(0, len(nid) - 4)) + nid[-4:] if nid else ""

    @property
    def can_order(self):
        return self.status == self.VERIFIED and not self.ordering_suspended

    @property
    def is_credit_approved(self):
        return self.payment_terms != self.BANK_ONLY and self.credit_limit > 0

    def outstanding_credit(self):
        from django.db.models import Sum

        unpaid = self.invoices.filter(status=Invoice.UNPAID).aggregate(t=Sum("amount"))["t"] or 0
        pending = self.orders.filter(
            payment_method=PlatformOrder.INVOICE,
            status__in=[PlatformOrder.PLACED, PlatformOrder.AWAITING_PAYMENT],
        ).aggregate(t=Sum("total"))["t"] or 0
        return unpaid + pending

    def has_overdue_invoice(self):
        return self.invoices.filter(status=Invoice.UNPAID,
                                    due_date__lt=timezone.localdate()).exists()


class BuyerDocument(models.Model):
    OWNER_ID = "owner_id"
    COMMERCIAL_REGISTER = "commercial_register"
    TAX_CARD = "tax_card"
    LICENCE = "licence"
    COLD_STORAGE = "cold_storage"
    TYPES = [(OWNER_ID, "Owner National ID (front and back)"),
             (COMMERCIAL_REGISTER, "Commercial Register"), (TAX_CARD, "Tax Card"),
             (LICENCE, "Veterinary Trading Licence"), (COLD_STORAGE, "Cold Storage Proof")]
    MANDATORY = [OWNER_ID, COMMERCIAL_REGISTER, TAX_CARD, LICENCE]

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESUBMIT = "resubmit"
    REVIEW_STATUSES = [(PENDING, "Uploaded"), (APPROVED, "Approved"), (REJECTED, "Rejected"),
                       (RESUBMIT, "Resubmission Requested")]

    buyer = models.ForeignKey(Buyer, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=20, choices=TYPES)
    # Stored encrypted (FR-68); never served directly from MEDIA_URL.
    file = models.FileField(upload_to="buyer_docs/")
    original_name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=60)
    size = models.PositiveIntegerField(default=0)
    expiry_date = models.DateField(null=True, blank=True)
    uploaded_at = models.DateTimeField(default=timezone.now)
    review_status = models.CharField(max_length=10, choices=REVIEW_STATUSES, default=PENDING)
    review_comment = models.CharField(max_length=255, blank=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    is_current = models.BooleanField(default=True)

    class Meta:
        ordering = ["doc_type", "-uploaded_at"]


class PlatformOrder(models.Model):
    # Statuses follow FR-86.
    PLACED = "placed"
    AWAITING_PAYMENT = "awaiting_payment"
    CONFIRMED = "confirmed"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    STATUSES = [(PLACED, "Placed"), (AWAITING_PAYMENT, "Awaiting Payment"),
                (CONFIRMED, "Confirmed"), (DISPATCHED, "Dispatched"), (DELIVERED, "Delivered"),
                (CANCELLED, "Cancelled")]
    BANK_TRANSFER = "bank_transfer"
    INVOICE = "invoice"
    METHODS = [(BANK_TRANSFER, "Bank Transfer"), (INVOICE, "Pay on Invoice")]

    reference = models.CharField(max_length=20, unique=True)
    buyer = models.ForeignKey(Buyer, on_delete=models.PROTECT, related_name="orders")
    created_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUSES, default=PLACED, db_index=True)
    payment_method = models.CharField(max_length=15, choices=METHODS, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    delivery_address = models.CharField(max_length=255)
    cold_chain_confirmed = models.BooleanField(default=False)
    reservation_expires_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    payment_rejection_reason = models.CharField(max_length=255, blank=True)
    dispatched_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="+")
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivery_method = models.CharField(max_length=60, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    on_hold = models.BooleanField(default=False)
    hold_reason = models.CharField(max_length=255, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    delivery_issue = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference

    @property
    def awaiting_label(self):
        if self.status != self.AWAITING_PAYMENT:
            return self.get_status_display()
        if self.payment_method == self.INVOICE:
            return "Awaiting Payment (credit approval)"
        return "Awaiting Payment (confirmation)"

    @property
    def has_cold_chain(self):
        return any(line.batch.is_cold_chain for line in self.lines.all())


class OrderLine(models.Model):
    order = models.ForeignKey(PlatformOrder, on_delete=models.CASCADE, related_name="lines")
    listing = models.ForeignKey("inventory.Listing", on_delete=models.PROTECT,
                                related_name="order_lines")
    batch = models.ForeignKey("inventory.Batch", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    picked_lot = models.CharField(max_length=60, blank=True)

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class PaymentProof(models.Model):
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    STATUSES = [(SUBMITTED, "Submitted"), (CONFIRMED, "Confirmed"), (REJECTED, "Rejected")]

    order = models.ForeignKey(PlatformOrder, on_delete=models.CASCADE, related_name="payments")
    file = models.FileField(upload_to="payment_proofs/")
    original_name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=60)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    uploaded_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=10, choices=STATUSES, default=SUBMITTED)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    comment = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]


class Invoice(models.Model):
    UNPAID = "unpaid"
    PAID = "paid"
    STATUSES = [(UNPAID, "Unpaid"), (PAID, "Paid")]

    number = models.CharField(max_length=20, unique=True)
    order = models.OneToOneField(PlatformOrder, on_delete=models.PROTECT, related_name="invoice")
    buyer = models.ForeignKey(Buyer, on_delete=models.PROTECT, related_name="invoices")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUSES, default=UNPAID)
    paid_at = models.DateTimeField(null=True, blank=True)
    synced_to_odoo = models.BooleanField(default=False)

    class Meta:
        ordering = ["-issue_date", "-id"]

    def __str__(self):
        return self.number

    @property
    def is_overdue(self):
        return self.status == self.UNPAID and self.due_date < timezone.localdate()


class SupportMessage(models.Model):
    """A question from a buyer or visitor, answered by Kubbar staff in the IMS."""

    TOPICS = [("order", "An order"), ("product", "A product or listing"),
              ("account", "My account or verification"), ("payment", "Payment or invoice"),
              ("other", "Something else")]
    OPEN = "open"
    ANSWERED = "answered"
    STATUSES = [(OPEN, "Open"), (ANSWERED, "Answered")]

    buyer = models.ForeignKey(Buyer, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="messages")
    name = models.CharField(max_length=120)
    email = models.EmailField()
    company = models.CharField(max_length=160, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    topic = models.CharField(max_length=10, choices=TOPICS, default="other")
    order = models.ForeignKey(PlatformOrder, null=True, blank=True, on_delete=models.SET_NULL)
    message = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=10, choices=STATUSES, default=OPEN)
    reply = models.TextField(blank=True)
    replied_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    replied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_topic_display()} – {self.name}"
