"""Shared models: users and roles, reference data, business rules, audit trail
and notifications. Both applications read these from the shared database."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.exceptions import PermissionDenied
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    """Internal roles from Exhibit #2 (User Profiles)."""

    SALES = "sales", "Sales Representative"
    WAREHOUSE = "warehouse", "Warehouse Officer"
    INVENTORY = "inventory", "Inventory Controller"
    QA = "qa", "QA Officer"
    PROCUREMENT = "procurement", "Procurement Officer"
    REGULATORY = "regulatory", "Regulatory Affairs Officer"
    FINANCE = "finance", "Finance Officer"
    GM = "gm", "General Manager"
    MANAGEMENT = "management", "Management (CEO, Group CFO)"
    COO = "coo", "Management (COO)"
    ADMIN = "admin", "System Administrator"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, username=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("roles", [Role.ADMIN])
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """A login for either application.

    ``account_type`` keeps internal staff and buyers apart: the IMS only accepts
    internal accounts and the platform only accepts buyer accounts (FR-02, NFR-02).
    """

    INTERNAL = "internal"
    BUYER = "buyer"
    ACCOUNT_TYPES = [(INTERNAL, "Internal"), (BUYER, "Buyer")]

    email = models.EmailField(unique=True)
    account_type = models.CharField(max_length=10, choices=ACCOUNT_TYPES, default=INTERNAL)
    roles = models.JSONField(default=list, blank=True)
    department = models.CharField(max_length=80, blank=True)
    # Responsibility scope for Sales Representatives (FR-25, UC-27 A2).
    categories = models.ManyToManyField("core.ProductCategory", blank=True, related_name="+")
    regions = models.ManyToManyField("core.Region", blank=True, related_name="+")
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    is_locked = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        ordering = ["first_name", "last_name"]

    def __str__(self):
        return self.get_full_name() or self.email

    @property
    def is_internal(self):
        return self.account_type == self.INTERNAL

    @property
    def is_buyer(self):
        return self.account_type == self.BUYER

    def has_role(self, *roles):
        return any(r in (self.roles or []) for r in roles)

    @property
    def role_labels(self):
        labels = dict(Role.choices)
        return [labels.get(r, r) for r in (self.roles or [])]

    @property
    def is_management(self):
        return self.has_role(Role.MANAGEMENT, Role.COO)


class Market(models.Model):
    """The nine markets; only Egypt is active in the pilot (NFR-21)."""

    code = models.CharField(max_length=3, unique=True)
    name = models.CharField(max_length=80)
    currency = models.CharField(max_length=3, default="EGP")
    is_active = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Region(models.Model):
    market = models.ForeignKey(Market, on_delete=models.PROTECT, related_name="regions")
    name = models.CharField(max_length=80)
    # Alternative spellings found in Odoo exports, separated by "|" (FR-42).
    aliases = models.CharField(max_length=255, blank=True,
                               help_text='Alternative spellings separated by "|"')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("market", "name")]

    def __str__(self):
        return self.name


class ProductCategory(models.Model):
    EDA = "EDA"
    GOVS = "GOVS"
    NONE = "-"
    AUTHORITIES = [(EDA, "EDA"), (GOVS, "GOVS"), (NONE, "Not registered")]

    name = models.CharField(max_length=80, unique=True)
    authority = models.CharField(max_length=5, choices=AUTHORITIES, default=EDA)
    is_expiring = models.BooleanField(
        default=True, help_text="Non-expiring products (capital equipment) are never listed.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "product categories"

    def __str__(self):
        return self.name


class StorageType(models.TextChoices):
    AMBIENT = "ambient", "Ambient"
    REFRIGERATED = "refrigerated", "Refrigerated 2-8°C"
    FROZEN = "frozen", "Frozen"


class Warehouse(models.Model):
    name = models.CharField(max_length=80, unique=True)
    market = models.ForeignKey(Market, on_delete=models.PROTECT)
    address = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class BinLocation(models.Model):
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="bins")
    code = models.CharField(max_length=30)
    storage_type = models.CharField(max_length=15, choices=StorageType.choices,
                                    default=StorageType.AMBIENT)
    capacity = models.PositiveIntegerField(default=10000, help_text="Units")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["warehouse__name", "code"]
        unique_together = [("warehouse", "code")]

    def __str__(self):
        return f"{self.warehouse.name} / {self.code}"


class Supplier(models.Model):
    name = models.CharField(max_length=120, unique=True)
    country = models.CharField(max_length=60, blank=True)
    default_lead_time_days = models.PositiveIntegerField(default=90)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class BuyerType(models.Model):
    name = models.CharField(max_length=60, unique=True)
    name_ar = models.CharField(max_length=60, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ReasonCode(models.Model):
    """Configurable reason codes used by dropdowns (NFR-05)."""

    VARIANCE = "variance"
    WRITE_OFF = "write_off"
    REJECTION = "rejection"
    SURPLUS = "surplus"
    KINDS = [
        (VARIANCE, "Count variance"),
        (WRITE_OFF, "Write-off"),
        (REJECTION, "Inspection rejection"),
        (SURPLUS, "Surplus flag"),
    ]

    kind = models.CharField(max_length=20, choices=KINDS)
    code = models.CharField(max_length=40)
    label = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["kind", "label"]
        unique_together = [("kind", "code")]

    def __str__(self):
        return self.label


class BusinessRule(models.Model):
    """Configurable business rules (Exhibit #4, FR-52, NFR-20)."""

    key = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=120)
    value = models.DecimalField(max_digits=14, decimal_places=2)
    unit = models.CharField(max_length=30, blank=True)
    min_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    max_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    changed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def display_value(self):
        v = self.value.normalize() if self.value == self.value.to_integral() else self.value
        return f"{v:,} {self.unit}".strip() if isinstance(v, Decimal) else str(v)


class AuditLog(models.Model):
    """Append-only audit trail (FR-54, NFR-27). Entries can be neither edited
    nor deleted through the application."""

    LOGIN = "login"
    APPROVAL = "approval"
    PRICE_CHANGE = "price_change"
    ADJUSTMENT = "adjustment"
    WRITE_OFF = "write_off"
    VERIFICATION = "verification"
    CONFIGURATION = "configuration"
    DATA_SYNC = "data_sync"
    STOCK = "stock"
    ORDER = "order"
    DOCUMENT_VIEW = "document_view"
    ACTIONS = [
        (LOGIN, "Login"),
        (APPROVAL, "Approval"),
        (PRICE_CHANGE, "Price Change"),
        (ADJUSTMENT, "Adjustment"),
        (WRITE_OFF, "Write-Off"),
        (VERIFICATION, "Verification"),
        (CONFIGURATION, "Configuration"),
        (DATA_SYNC, "Data Sync"),
        (STOCK, "Stock Movement"),
        (ORDER, "Order"),
        (DOCUMENT_VIEW, "Document View"),
    ]

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                             related_name="+")
    action = models.CharField(max_length=20, choices=ACTIONS)
    description = models.CharField(max_length=255)
    record_ref = models.CharField(max_length=80, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-timestamp", "-id"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.get_action_display()} {self.record_ref}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise PermissionDenied("Audit log entries cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Audit log entries cannot be deleted.")


class Notification(models.Model):
    """In-app notification; an email copy is sent at the same time."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    message = models.CharField(max_length=255)
    link = models.CharField(max_length=255, blank=True)
    level = models.CharField(max_length=10, default="info")
    created_at = models.DateTimeField(default=timezone.now)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.message


class LoginCode(models.Model):
    """One-time code for internal two-factor authentication (NFR-23, UC-01)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(default=timezone.now)
    used = models.BooleanField(default=False)

    def is_valid(self, minutes=10):
        return not self.used and timezone.now() - self.created_at < timedelta(minutes=minutes)
