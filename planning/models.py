"""Demand forecasting and purchase planning (IMS Module 5)."""
from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import Region

User = settings.AUTH_USER_MODEL


class DataImport(models.Model):
    SALES = "sales"
    STOCK = "stock"
    OPEN_PO = "open_po"
    PRODUCTS = "products"
    TYPES = [(SALES, "Sales History"), (STOCK, "Stock on Hand"),
             (OPEN_PO, "Open Purchase Orders"), (PRODUCTS, "Products")]
    PREVIEW = "preview"
    IMPORTED = "imported"
    FAILED = "failed"
    STATUSES = [(PREVIEW, "Awaiting Confirmation"), (IMPORTED, "Imported"), (FAILED, "Failed")]

    data_type = models.CharField(max_length=10, choices=TYPES)
    file_name = models.CharField(max_length=200)
    source = models.CharField(max_length=20, default="file")
    uploaded_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=10, choices=STATUSES, default=PREVIEW)
    rows_total = models.PositiveIntegerField(default=0)
    rows_accepted = models.PositiveIntegerField(default=0)
    rows_corrected = models.PositiveIntegerField(default=0)
    rows_rejected = models.PositiveIntegerField(default=0)
    messages = models.JSONField(default=list, blank=True)
    # Validated rows kept until the officer confirms (UC-23 step 6).
    staged_rows = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.get_data_type_display()} – {self.file_name}"


class SalesRecord(models.Model):
    """One sales line imported from Odoo (by product, region, customer, month)."""

    order_number = models.CharField(max_length=40, blank=True)
    period = models.DateField(help_text="First day of the month", db_index=True)
    region = models.ForeignKey(Region, on_delete=models.PROTECT)
    customer = models.ForeignKey("inventory.Customer", null=True, blank=True,
                                 on_delete=models.SET_NULL)
    customer_name = models.CharField(max_length=160, blank=True)
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT,
                                related_name="sales")
    quantity = models.IntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    data_import = models.ForeignKey(DataImport, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-period"]


class Forecast(models.Model):
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    horizon_months = models.PositiveSmallIntegerField(default=6)
    start_period = models.DateField()
    category = models.ForeignKey("core.ProductCategory", null=True, blank=True,
                                 on_delete=models.SET_NULL)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"FC-{self.pk:04d} ({self.created_at:%d %b %Y})"


class ForecastProduct(models.Model):
    """Forecast summary for one product and region."""

    SEASONAL = "seasonal_es"
    MOVING_AVG = "moving_avg"
    MANUAL = "manual"
    METHODS = [(SEASONAL, "Seasonal Exponential Smoothing"), (MOVING_AVG, "6-Month Moving Average"),
               (MANUAL, "Manual Estimate")]

    forecast = models.ForeignKey(Forecast, on_delete=models.CASCADE, related_name="products")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    region = models.ForeignKey(Region, on_delete=models.PROTECT)
    method = models.CharField(max_length=12, choices=METHODS)
    history_months = models.PositiveSmallIntegerField(default=0)
    mape = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    low_confidence = models.BooleanField(default=False)
    insufficient_history = models.BooleanField(default=False)
    manual_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["product__name", "region__name"]
        unique_together = [("forecast", "product", "region")]


class ForecastLine(models.Model):
    forecast_product = models.ForeignKey(ForecastProduct, on_delete=models.CASCADE,
                                         related_name="lines")
    period = models.DateField()
    quantity = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["period"]


class PurchasePlan(models.Model):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    RETURNED = "returned"
    REJECTED = "rejected"
    STATUSES = [(DRAFT, "Draft"), (SUBMITTED, "Submitted"), (APPROVED, "Approved"),
                (RETURNED, "Returned for Revision"), (REJECTED, "Rejected")]

    number = models.CharField(max_length=20, unique=True)
    forecast = models.ForeignKey(Forecast, on_delete=models.PROTECT, related_name="plans")
    period_label = models.CharField(max_length=40, help_text="Planning period, e.g. 2026-Q4")
    prepared_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=10, choices=STATUSES, default=DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    approval_date = models.DateTimeField(null=True, blank=True)
    decision_comment = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.number


class PurchasePlanLine(models.Model):
    PENDING = "pending"
    APPROVED = "approved"
    RETURNED = "returned"
    REJECTED = "rejected"
    STATUSES = [(PENDING, "Pending"), (APPROVED, "Approved"), (RETURNED, "Returned"),
                (REJECTED, "Rejected")]

    plan = models.ForeignKey(PurchasePlan, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    forecast_qty = models.IntegerField(help_text="Demand over lead time and coverage")
    safety_stock = models.IntegerField(default=0)
    usable_stock = models.IntegerField(default=0)
    excluded_stock = models.IntegerField(default=0, help_text="Projected to reach near-expiry")
    open_po_qty = models.IntegerField(default=0)
    recommended_qty = models.IntegerField()
    moq = models.PositiveIntegerField()
    rounded_qty = models.IntegerField()
    moq_surplus_qty = models.IntegerField(default=0)
    moq_surplus_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    projected_left_at_expiry = models.IntegerField(default=0)
    final_qty = models.IntegerField()
    override_reason = models.CharField(max_length=255, blank=True)
    at_risk = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING)
    gm_comment = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["product__name"]

    @property
    def is_overridden(self):
        return self.final_qty != self.rounded_qty

    @property
    def line_value(self):
        return self.final_qty * self.product.unit_cost
