"""Inventory domain: products, batches, stock, warehouse audits, expiry
monitoring, surplus release, clearance listings and write-offs
(IMS Modules 1 to 4)."""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from core.models import (BinLocation, Market, ProductCategory, Region, StorageType,
                         Supplier, Warehouse)

User = settings.AUTH_USER_MODEL


class Product(models.Model):
    sku = models.CharField("SKU", max_length=40, unique=True)
    name = models.CharField(max_length=160)
    name_ar = models.CharField("Arabic name", max_length=160, blank=True)
    category = models.ForeignKey(ProductCategory, on_delete=models.PROTECT,
                                 related_name="products")
    unit = models.CharField(max_length=20, default="unit")
    storage_condition = models.CharField(max_length=15, choices=StorageType.choices,
                                         default=StorageType.AMBIENT)
    original_price = models.DecimalField("Original sale price (EGP)", max_digits=12,
                                         decimal_places=2)
    unit_cost = models.DecimalField("Unit cost (EGP)", max_digits=12, decimal_places=2)
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL)
    moq = models.PositiveIntegerField("Supplier MOQ", default=1)
    lead_time_days = models.PositiveIntegerField(default=90)
    shelf_life_months = models.PositiveIntegerField("Typical shelf life (months)", default=24)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    @property
    def is_cold_chain(self):
        return self.storage_condition != StorageType.AMBIENT

    @property
    def is_expiring(self):
        return self.category.is_expiring


class ProductRegistration(models.Model):
    """Registration of a product in a market (FR-35, UC-20)."""

    ACTIVE = "active"
    PENDING = "pending"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    STATUSES = [(ACTIVE, "Active"), (PENDING, "Pending"), (SUSPENDED, "Suspended"),
                (EXPIRED, "Expired")]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="registrations")
    market = models.ForeignKey(Market, on_delete=models.PROTECT)
    authority = models.CharField(max_length=5, choices=ProductCategory.AUTHORITIES[:2])
    registration_no = models.CharField(max_length=60)
    status = models.CharField(max_length=10, choices=STATUSES, default=ACTIVE)
    expiry_date = models.DateField()
    certificate = models.FileField(upload_to="registrations/", blank=True)
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("product", "market")]
        ordering = ["product__name"]

    def __str__(self):
        return f"{self.product} – {self.market} ({self.get_status_display()})"


class Customer(models.Model):
    """Existing Kubbar customer served by the field sales team (from Odoo)."""

    name = models.CharField(max_length=160)
    commercial_register_no = models.CharField(max_length=40, blank=True, db_index=True)
    region = models.ForeignKey(Region, null=True, blank=True, on_delete=models.SET_NULL)
    sales_rep = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="customers")
    odoo_ref = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PurchaseOrder(models.Model):
    OPEN = "open"
    RECEIVED = "received"
    CLOSED = "closed"
    STATUSES = [(OPEN, "Open"), (RECEIVED, "Received"), (CLOSED, "Closed")]

    number = models.CharField(max_length=40, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    order_date = models.DateField(default=timezone.localdate)
    expected_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=OPEN)
    # Links the order to the approved purchase plan that justified it (FR-40).
    plan = models.ForeignKey("planning.PurchasePlan", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="purchase_orders")

    class Meta:
        ordering = ["-order_date", "number"]

    def __str__(self):
        return self.number


class PurchaseOrderLine(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    quantity_received = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.order} / {self.product}"

    @property
    def open_quantity(self):
        return max(0, self.quantity - self.quantity_received)


class BatchStatus(models.TextChoices):
    INSPECTED = "inspected", "Awaiting Receipt"
    IN_STOCK = "in_stock", "In Stock"
    NEAR_EXPIRY = "near_expiry", "Near-Expiry"
    SURPLUS = "surplus", "Surplus"
    RELEASED = "released", "Released"
    EXPIRED = "expired", "Expired"
    REJECTED = "rejected", "Rejected"
    DEPLETED = "depleted", "Depleted"


class Batch(models.Model):
    """A quantity of one product sharing one lot number and expiry date.

    Stock is always held at batch level and never merged (FR-12)."""

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="batches")
    lot_number = models.CharField(max_length=60)
    manufacture_date = models.DateField()
    expiry_date = models.DateField(db_index=True)
    storage_condition = models.CharField(max_length=15, choices=StorageType.choices)
    market = models.ForeignKey(Market, on_delete=models.PROTECT)
    purchase_order = models.ForeignKey(PurchaseOrder, null=True, blank=True,
                                       on_delete=models.SET_NULL, related_name="batches")

    # Inspection (FR-07)
    quantity_received = models.PositiveIntegerField()
    quantity_approved = models.PositiveIntegerField()
    quantity_rejected = models.PositiveIntegerField(default=0)
    rejection_reason = models.CharField(max_length=40, blank=True)
    inspection_date = models.DateField(default=timezone.localdate)
    inspected_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    early_monitoring = models.BooleanField(default=False)

    # Receipt (FR-10)
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")

    qty_on_hand = models.IntegerField(default=0)
    qty_reserved = models.IntegerField(default=0)
    status = models.CharField(max_length=15, choices=BatchStatus.choices,
                              default=BatchStatus.INSPECTED, db_index=True)
    frozen_for_count = models.BooleanField(default=False)
    blocked_for_write_off = models.IntegerField(default=0)
    near_expiry_since = models.DateField(null=True, blank=True)
    last_action_at = models.DateField(null=True, blank=True)
    escalated = models.BooleanField(default=False)
    keep_under_watch = models.BooleanField(default=False)

    class Meta:
        ordering = ["expiry_date", "lot_number"]
        unique_together = [("product", "lot_number")]
        verbose_name_plural = "batches"

    def __str__(self):
        return f"{self.product.name} – Lot {self.lot_number}"

    @property
    def days_to_expiry(self):
        return (self.expiry_date - timezone.localdate()).days

    @property
    def qty_available(self):
        """Unreserved, unblocked stock."""
        return max(0, self.qty_on_hand - self.qty_reserved - self.blocked_for_write_off)

    @property
    def cost_value(self):
        return (self.product.unit_cost * self.qty_on_hand).quantize(Decimal("0.01"))

    @property
    def is_cold_chain(self):
        return self.storage_condition != StorageType.AMBIENT

    def locations(self):
        return self.stock.filter(quantity__gt=0).select_related("location__warehouse")

    def recompute_on_hand(self):
        self.qty_on_hand = self.stock.aggregate(t=Sum("quantity"))["t"] or 0
        return self.qty_on_hand


class BatchStock(models.Model):
    """Quantity of a batch in one bin location."""

    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="stock")
    location = models.ForeignKey(BinLocation, on_delete=models.PROTECT, related_name="stock")
    quantity = models.IntegerField(default=0)

    class Meta:
        unique_together = [("batch", "location")]

    def __str__(self):
        return f"{self.batch} @ {self.location}: {self.quantity}"


class StockMovement(models.Model):
    """Every stock movement with batch, quantity, type, user and time (FR-17)."""

    RECEIPT = "receipt"
    TRANSFER = "transfer"
    DISPATCH = "dispatch"
    ADJUSTMENT = "adjustment"
    WRITE_OFF = "write_off"
    REVERSAL = "reversal"
    TYPES = [(RECEIPT, "Receipt"), (TRANSFER, "Transfer"), (DISPATCH, "Dispatch"),
             (ADJUSTMENT, "Adjustment"), (WRITE_OFF, "Write-Off"), (REVERSAL, "Reversal")]

    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=12, choices=TYPES)
    quantity = models.IntegerField(
        help_text="Signed change to stock on hand; units moved for transfers")
    from_location = models.ForeignKey(BinLocation, null=True, blank=True,
                                      on_delete=models.PROTECT, related_name="+")
    to_location = models.ForeignKey(BinLocation, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="+")
    reference = models.CharField(max_length=80, blank=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                             related_name="+")
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp", "-id"]

    def __str__(self):
        return f"{self.get_movement_type_display()} {self.quantity} {self.batch}"


class FieldDispatch(models.Model):
    """A dispatch outside the platform (e.g. a field sales order), allocated by FEFO."""

    reference = models.CharField(max_length=40)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    quantity_dispatched = models.PositiveIntegerField(default=0)
    dispatched_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    dispatched_at = models.DateTimeField(default=timezone.now)
    allocation = models.JSONField(default=list)

    class Meta:
        ordering = ["-dispatched_at"]


# --------------------------------------------------------------------------
# Warehouse audit (Module 2)
# --------------------------------------------------------------------------
class StockCount(models.Model):
    FULL = "full"
    CYCLE = "cycle"
    TYPES = [(FULL, "Full Count"), (CYCLE, "Cycle Count")]
    SCOPE_ALL = "all"
    SCOPE_CATEGORY = "category"
    SCOPE_BATCHES = "batches"
    SCOPES = [(SCOPE_ALL, "All"), (SCOPE_CATEGORY, "Category"), (SCOPE_BATCHES, "Selected Batches")]
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    CLOSED = "closed"
    STATUSES = [(SCHEDULED, "Scheduled"), (IN_PROGRESS, "In Progress"),
                (SUBMITTED, "Submitted"), (CLOSED, "Closed")]

    number = models.CharField(max_length=20, unique=True)
    count_type = models.CharField(max_length=10, choices=TYPES)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT)
    scope = models.CharField(max_length=10, choices=SCOPES, default=SCOPE_ALL)
    category = models.ForeignKey(ProductCategory, null=True, blank=True, on_delete=models.PROTECT)
    scheduled_date = models.DateField()
    assigned_to = models.ForeignKey(User, on_delete=models.PROTECT, related_name="assigned_counts")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=12, choices=STATUSES, default=SCHEDULED)
    submitted_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    accuracy_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    adjustment_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ["-scheduled_date", "-id"]

    def __str__(self):
        return self.number


class StockCountLine(models.Model):
    PENDING = "pending"
    WITHIN = "within"
    FLAGGED = "flagged"
    POSTED = "posted"
    AWAITING = "awaiting_approval"
    RECOUNT = "recount"
    STATUSES = [(PENDING, "Pending"), (WITHIN, "Within Tolerance"), (FLAGGED, "Flagged"),
                (POSTED, "Posted"), (AWAITING, "Awaiting Approval"), (RECOUNT, "Recount")]

    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name="lines")
    batch = models.ForeignKey(Batch, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="count_lines")
    location = models.ForeignKey(BinLocation, on_delete=models.PROTECT)
    system_qty = models.IntegerField(default=0)
    counted_qty = models.IntegerField(null=True, blank=True)
    not_found = models.BooleanField(default=False)
    observed_expiry = models.DateField(null=True, blank=True)
    # Stock found that was not on the sheet (UC-11 step 4).
    is_extra = models.BooleanField(default=False)
    extra_product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+")
    extra_lot = models.CharField(max_length=60, blank=True)
    extra_expiry = models.DateField(null=True, blank=True)
    variance_qty = models.IntegerField(default=0)
    variance_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUSES, default=PENDING)
    reason_code = models.ForeignKey("core.ReasonCode", null=True, blank=True,
                                    on_delete=models.PROTECT)
    recount_round = models.PositiveSmallIntegerField(default=0)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    decision_comment = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["location__code", "batch__lot_number"]


# --------------------------------------------------------------------------
# Expiry monitoring and surplus release (Module 3)
# --------------------------------------------------------------------------
class ExpiryScanRun(models.Model):
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)
    batches_scanned = models.PositiveIntegerField(default=0)
    new_near_expiry = models.PositiveIntegerField(default=0)
    expired = models.PositiveIntegerField(default=0)
    listings_withdrawn = models.PositiveIntegerField(default=0)
    escalations = models.PositiveIntegerField(default=0)
    value_at_risk = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    error = models.TextField(blank=True)
    triggered_by = models.CharField(max_length=60, default="scheduler")

    class Meta:
        ordering = ["-started_at"]


class ExpiryAlert(models.Model):
    NEAR_EXPIRY = "near_expiry"
    MIN_SHELF = "min_shelf"
    EXPIRED = "expired"
    ESCALATION = "escalation"
    REGISTRATION = "registration"
    LOW_SHELF_RECEIPT = "low_shelf_receipt"
    TYPES = [(NEAR_EXPIRY, "Near-Expiry"), (MIN_SHELF, "Below Minimum Sellable Shelf Life"),
             (EXPIRED, "Expired"), (ESCALATION, "Escalated"),
             (REGISTRATION, "Registration Lapsed"), (LOW_SHELF_RECEIPT, "Low Shelf Life at Receipt")]
    OPEN = "open"
    ACTIONED = "actioned"
    CLOSED = "closed"
    STATUSES = [(OPEN, "Open"), (ACTIONED, "Actioned"), (CLOSED, "Closed")]

    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="alerts")
    alert_type = models.CharField(max_length=20, choices=TYPES)
    raised_date = models.DateField(default=timezone.localdate)
    days_to_expiry = models.IntegerField()
    status = models.CharField(max_length=10, choices=STATUSES, default=OPEN)

    class Meta:
        ordering = ["-raised_date", "-id"]


class CustomerOffer(models.Model):
    """Offer of near-expiry stock to an existing customer (FR-27, UC-15)."""

    ACCEPTED = "accepted"
    DECLINED = "declined"
    PENDING = "pending"
    OUTCOMES = [(ACCEPTED, "Accepted"), (DECLINED, "Declined"), (PENDING, "Pending")]

    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="offers")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    qty_offered = models.PositiveIntegerField()
    outcome = models.CharField(max_length=10, choices=OUTCOMES, default=PENDING)
    offered_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    offered_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-offered_at"]


class SurplusRelease(models.Model):
    """A surplus flag by Sales and the COO's release decision (FR-29 to FR-31)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STATUSES = [(PENDING, "Pending"), (APPROVED, "Approved"), (REJECTED, "Rejected")]
    REASONS = [("no_demand", "No Customer Demand"), ("declined", "Customer Declined"),
               ("seasonal", "Seasonal Demand Passed"), ("other", "Other")]

    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="releases")
    flagged_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    flagged_at = models.DateTimeField(default=timezone.now)
    flag_reason = models.CharField(max_length=20, choices=REASONS)
    comment = models.CharField(max_length=255, blank=True)
    surplus_qty = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING)
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    release_qty = models.PositiveIntegerField(null=True, blank=True)
    clearance_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    decision_comment = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-flagged_at"]

    def __str__(self):
        return f"Release #{self.pk} – {self.batch}"


class Listing(models.Model):
    """A released batch on the E-Commerce Platform (FR-33)."""

    ACTIVE = "active"
    SOLD_OUT = "sold_out"
    WITHDRAWN = "withdrawn"
    STATUSES = [(ACTIVE, "Active"), (SOLD_OUT, "Sold Out"), (WITHDRAWN, "Withdrawn")]

    release = models.OneToOneField(SurplusRelease, on_delete=models.PROTECT,
                                   related_name="listing")
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="listings")
    market = models.ForeignKey(Market, on_delete=models.PROTECT)
    listed_qty = models.PositiveIntegerField()
    available_qty = models.IntegerField()
    clearance_price = models.DecimalField(max_digits=12, decimal_places=2)
    original_price = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUSES, default=ACTIVE, db_index=True)
    published_at = models.DateTimeField(default=timezone.now)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["batch__expiry_date"]

    def __str__(self):
        return f"Listing #{self.pk} – {self.batch}"

    @property
    def product(self):
        return self.batch.product

    @property
    def discount_pct(self):
        if not self.original_price:
            return 0
        return int(round(100 - self.clearance_price * 100 / self.original_price))

    @property
    def sold_qty(self):
        from commerce.models import OrderLine, PlatformOrder

        return OrderLine.objects.filter(listing=self).exclude(
            order__status=PlatformOrder.CANCELLED).aggregate(t=Sum("quantity"))["t"] or 0


# --------------------------------------------------------------------------
# Disposal and write-off (Module 4)
# --------------------------------------------------------------------------
class WriteOff(models.Model):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISPOSED = "disposed"
    CANCELLED = "cancelled"
    STATUSES = [(PENDING, "Pending Approval"), (APPROVED, "Posted – Awaiting Disposal"),
                (REJECTED, "Rejected"), (DISPOSED, "Disposed"), (CANCELLED, "Cancelled")]
    REASONS = [("expired", "Expired"), ("damaged", "Damaged"),
               ("rejected", "Rejected at Inspection"), ("temperature", "Temperature Breach")]
    METHODS = [("destruction", "Licensed Destruction"), ("return", "Return to Supplier"),
               ("incineration", "Incineration")]

    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="write_offs")
    quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=15, choices=REASONS)
    disposal_method = models.CharField(max_length=15, choices=METHODS)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING)
    gm_approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+")
    gm_approved_at = models.DateTimeField(null=True, blank=True)
    qa_approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+")
    qa_approved_at = models.DateTimeField(null=True, blank=True)
    rejection_comment = models.CharField(max_length=255, blank=True)
    write_off_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    disposal_date = models.DateField(null=True, blank=True)
    disposal_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    certificate_ref = models.CharField(max_length=80, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    # The purchase plan that bought the batch, so the loss feeds back (FR-40).
    purchase_plan = models.ForeignKey("planning.PurchasePlan", null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="write_offs")

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"WO-{self.pk:05d}"
