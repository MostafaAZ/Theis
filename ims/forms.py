from datetime import date

from django import forms
from django.contrib.auth.password_validation import validate_password

from core.models import (AuditLog, BinLocation, BuyerType, Market, ProductCategory,
                         ReasonCode, Region, Role, StorageType, Supplier, User, Warehouse)
from inventory.models import (Batch, BatchStatus, Customer, CustomerOffer, Product,
                              ProductRegistration, PurchaseOrder, StockCount, SurplusRelease,
                              WriteOff)
from planning.models import DataImport


class BootstrapMixin:
    """Give every widget Bootstrap classes; fixed lists render as dropdowns (NFR-05)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            w = field.widget
            if isinstance(w, (forms.CheckboxInput,)):
                w.attrs.setdefault("class", "form-check-input")
            elif isinstance(w, (forms.Select, forms.SelectMultiple)):
                w.attrs.setdefault("class", "form-select")
            elif isinstance(w, forms.RadioSelect):
                pass
            else:
                w.attrs.setdefault("class", "form-control")


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        kwargs.setdefault("format", "%Y-%m-%d")
        super().__init__(**kwargs)


# --- Authentication -------------------------------------------------------
class LoginForm(BootstrapMixin, forms.Form):
    email = forms.EmailField(label="Email Address")
    password = forms.CharField(widget=forms.PasswordInput)


class CodeForm(BootstrapMixin, forms.Form):
    code = forms.CharField(label="One-time code", max_length=6,
                           widget=forms.TextInput(attrs={"autocomplete": "one-time-code",
                                                         "inputmode": "numeric"}))


# --- Module 1 -------------------------------------------------------------
REJECTION_REASONS = [("", "—"), ("Damaged", "Damaged"), ("Short-Dated", "Short-Dated"),
                     ("Temperature Breach", "Temperature Breach"),
                     ("Documentation", "Documentation"), ("Other", "Other")]


class InspectionForm(BootstrapMixin, forms.Form):
    purchase_order = forms.ModelChoiceField(
        queryset=PurchaseOrder.objects.filter(status=PurchaseOrder.OPEN))
    product = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True))
    lot_number = forms.CharField(max_length=60)
    manufacture_date = forms.DateField(widget=DateInput())
    expiry_date = forms.DateField(widget=DateInput())
    storage_condition = forms.ChoiceField(choices=StorageType.choices)
    quantity_received = forms.IntegerField(min_value=1)
    quantity_approved = forms.IntegerField(min_value=0)
    quantity_rejected = forms.IntegerField(min_value=0, initial=0)
    rejection_reason = forms.ChoiceField(choices=REJECTION_REASONS, required=False)
    confirm_low_shelf_life = forms.BooleanField(
        required=False, label="I confirm receipt despite shelf life below the minimum")


class ReceiptForm(BootstrapMixin, forms.Form):
    location = forms.ModelChoiceField(queryset=BinLocation.objects.filter(is_active=True),
                                      label="Warehouse / Bin Location")
    allow_over_capacity = forms.BooleanField(required=False,
                                             label="Book in even though capacity is exceeded")


class StockSearchForm(BootstrapMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.all(), required=False)
    category = forms.ModelChoiceField(queryset=ProductCategory.objects.all(), required=False)
    lot_number = forms.CharField(required=False)
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.all(), required=False)
    expiry_from = forms.DateField(required=False, widget=DateInput())
    expiry_to = forms.DateField(required=False, widget=DateInput())
    days_band = forms.ChoiceField(required=False, label="Days to expiry", choices=[
        ("", "Any"), ("0-30", "Under 30 days"), ("30-180", "30 days to 6 months"),
        ("180-365", "6 to 12 months"), ("365-", "More than 12 months")])
    status = forms.ChoiceField(required=False, label="Release status",
                               choices=[("", "Any")] + list(BatchStatus.choices) +
                               [("reserved", "Reserved")])


class TransferForm(BootstrapMixin, forms.Form):
    batch = forms.ModelChoiceField(queryset=Batch.objects.filter(qty_on_hand__gt=0))
    from_location = forms.ModelChoiceField(queryset=BinLocation.objects.all(),
                                           label="Source location")
    to_location = forms.ModelChoiceField(queryset=BinLocation.objects.filter(is_active=True),
                                         label="Destination Warehouse / Bin")
    quantity = forms.IntegerField(min_value=1)


class DispatchForm(BootstrapMixin, forms.Form):
    delivery_method = forms.ChoiceField(choices=[
        ("Kubbar fleet", "Kubbar fleet"), ("Buyer collection", "Buyer collection"),
        ("Third-party courier", "Third-party courier"),
        ("Refrigerated courier", "Refrigerated courier")])
    delivery_date = forms.DateField(widget=DateInput(), initial=date.today)
    cold_chain_checked = forms.BooleanField(required=False,
                                            label="Cold-chain packaging check completed")


class FieldDispatchForm(BootstrapMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True))
    quantity = forms.IntegerField(min_value=1)
    customer = forms.ModelChoiceField(queryset=Customer.objects.all(), required=False)
    reference = forms.CharField(required=False, help_text="Odoo sales order number")


# --- Module 2 -------------------------------------------------------------
class ScheduleCountForm(BootstrapMixin, forms.Form):
    count_type = forms.ChoiceField(choices=StockCount.TYPES)
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.filter(is_active=True))
    scope = forms.ChoiceField(choices=StockCount.SCOPES)
    category = forms.ModelChoiceField(queryset=ProductCategory.objects.all(), required=False)
    batches = forms.ModelMultipleChoiceField(
        queryset=Batch.objects.filter(qty_on_hand__gt=0).select_related("product"),
        required=False, widget=forms.SelectMultiple(attrs={"size": 6}))
    scheduled_date = forms.DateField(widget=DateInput(), initial=date.today, label="Count date")
    assigned_to = forms.ModelChoiceField(queryset=User.objects.none(), label="Warehouse Officer")
    exclude_open_dispatch = forms.BooleanField(required=False,
                                               label="Exclude batches with open dispatches")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ids = [u.pk for u in User.objects.filter(account_type=User.INTERNAL, is_active=True)
               if u.has_role(Role.WAREHOUSE)]
        self.fields["assigned_to"].queryset = User.objects.filter(pk__in=ids)


class ExtraStockForm(BootstrapMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.all(), required=False)
    lot = forms.CharField(required=False, max_length=60)
    expiry = forms.DateField(required=False, widget=DateInput())
    location = forms.ModelChoiceField(queryset=BinLocation.objects.all(), required=False)
    quantity = forms.IntegerField(required=False, min_value=1)


# --- Module 3 -------------------------------------------------------------
class OfferForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = CustomerOffer
        fields = ["customer", "qty_offered", "outcome"]
        labels = {"qty_offered": "Quantity offered"}


class SurplusForm(BootstrapMixin, forms.Form):
    surplus_quantity = forms.IntegerField(min_value=1)
    reason = forms.ChoiceField(choices=SurplusRelease.REASONS)
    comment = forms.CharField(required=False, max_length=255)


class ReleaseDecisionForm(BootstrapMixin, forms.Form):
    release_quantity = forms.IntegerField(min_value=1)
    clearance_price = forms.DecimalField(max_digits=12, decimal_places=2,
                                         label="Clearance price (EGP per unit)")
    comment = forms.CharField(required=False, max_length=255)


class ListingManageForm(BootstrapMixin, forms.Form):
    action = forms.ChoiceField(choices=[("reprice", "Reprice"), ("quantity", "Change Quantity"),
                                        ("withdraw", "Withdraw")])
    clearance_price = forms.DecimalField(max_digits=12, decimal_places=2, required=False)
    listed_quantity = forms.IntegerField(min_value=1, required=False)
    reason = forms.CharField(required=False, max_length=255)


class RegistrationForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = ProductRegistration
        fields = ["product", "market", "authority", "registration_no", "status", "expiry_date",
                  "certificate"]
        widgets = {"expiry_date": DateInput()}
        labels = {"expiry_date": "Registration expiry date"}


# --- Module 4 -------------------------------------------------------------
class WriteOffForm(BootstrapMixin, forms.Form):
    batch = forms.ModelChoiceField(queryset=Batch.objects.filter(qty_on_hand__gt=0))
    quantity = forms.IntegerField(min_value=1)
    reason = forms.ChoiceField(choices=WriteOff.REASONS)
    disposal_method = forms.ChoiceField(choices=WriteOff.METHODS,
                                        label="Proposed disposal method")
    confirm_release_not_tried = forms.BooleanField(
        required=False, label="I confirm the release route has not been tried for this batch")


class DisposalForm(BootstrapMixin, forms.Form):
    disposal_date = forms.DateField(widget=DateInput(), initial=date.today)
    disposal_cost = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0,
                                       label="Disposal cost (EGP)")
    certificate_ref = forms.CharField(max_length=80, label="Certificate reference")


# --- Module 5 -------------------------------------------------------------
class ImportForm(BootstrapMixin, forms.Form):
    data_type = forms.ChoiceField(choices=DataImport.TYPES)
    file = forms.FileField(help_text="Excel (.xlsx) or CSV exported from Odoo")


class ForecastForm(BootstrapMixin, forms.Form):
    horizon = forms.TypedChoiceField(coerce=int, choices=[(3, "3 months"), (6, "6 months"),
                                                          (12, "12 months")], initial=6,
                                     label="Planning horizon")
    category = forms.ModelChoiceField(queryset=ProductCategory.objects.all(), required=False)
    products = forms.ModelMultipleChoiceField(
        queryset=Product.objects.filter(is_active=True), required=False,
        help_text="Leave empty for all products in the category",
        widget=forms.SelectMultiple(attrs={"size": 8}))


class ManualEstimateForm(BootstrapMixin, forms.Form):
    monthly_quantity = forms.DecimalField(min_value=0, max_digits=12, decimal_places=2)
    reason = forms.CharField(max_length=255)


class NewPlanForm(BootstrapMixin, forms.Form):
    period_label = forms.CharField(max_length=40, label="Planning period",
                                   help_text="For example 2027-Q1")


# --- Platform-facing IMS screens -------------------------------------------
class PaymentTermsForm(BootstrapMixin, forms.Form):
    payment_terms = forms.ChoiceField(choices=[("bank_only", "Bank Transfer Only"),
                                               ("15", "15 days"), ("30", "30 days"),
                                               ("45", "45 days")])
    credit_limit = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0,
                                      label="Credit limit (EGP)", initial=0)


class VerifyForm(BootstrapMixin, forms.Form):
    categories = forms.ModelMultipleChoiceField(
        queryset=ProductCategory.objects.filter(is_active=True, is_expiring=True),
        widget=forms.CheckboxSelectMultiple, label="Licensed categories", required=False)
    cold_chain_eligible = forms.BooleanField(required=False, label="Cold-chain eligible")


# --- Module 7 -------------------------------------------------------------
class UserForm(BootstrapMixin, forms.ModelForm):
    roles = forms.MultipleChoiceField(choices=Role.choices, widget=forms.CheckboxSelectMultiple)
    password = forms.CharField(required=False, widget=forms.PasswordInput,
                               help_text="Leave blank to send an activation email.")

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "department", "roles", "categories",
                  "regions", "is_active"]
        labels = {"categories": "Responsible categories (Sales)",
                  "regions": "Responsible regions (Sales)", "is_active": "Active"}
        widgets = {"categories": forms.SelectMultiple(attrs={"size": 4}),
                   "regions": forms.SelectMultiple(attrs={"size": 4})}

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        qs = User.objects.filter(email__iexact=email)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This email address is already in use.")
        return email

    def clean_password(self):
        pw = self.cleaned_data.get("password")
        if pw:
            validate_password(pw)
        return pw


class RuleForm(BootstrapMixin, forms.Form):
    new_value = forms.DecimalField(max_digits=14, decimal_places=2)
    approving_manager = forms.ModelChoiceField(queryset=User.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ids = [u.pk for u in User.objects.filter(account_type=User.INTERNAL, is_active=True)
               if u.has_role(Role.GM, Role.MANAGEMENT, Role.COO)]
        self.fields["approving_manager"].queryset = User.objects.filter(pk__in=ids)


class AuditFilterForm(BootstrapMixin, forms.Form):
    user = forms.ModelChoiceField(queryset=User.objects.all(), required=False)
    action = forms.ChoiceField(choices=[("", "Any")] + AuditLog.ACTIONS, required=False,
                               label="Action type")
    record = forms.CharField(required=False, label="Record reference")
    date_from = forms.DateField(required=False, widget=DateInput())
    date_to = forms.DateField(required=False, widget=DateInput())


REFERENCE_MODELS = {
    "warehouses": (Warehouse, ["name", "market", "address", "is_active"]),
    "locations": (BinLocation, ["warehouse", "code", "storage_type", "capacity", "is_active"]),
    "categories": (ProductCategory, ["name", "authority", "is_expiring", "is_active"]),
    "regions": (Region, ["market", "name", "aliases", "is_active"]),
    "markets": (Market, ["code", "name", "currency", "is_active"]),
    "suppliers": (Supplier, ["name", "country", "default_lead_time_days", "is_active"]),
    "reason-codes": (ReasonCode, ["kind", "code", "label", "is_active"]),
    "buyer-types": (BuyerType, ["name", "name_ar", "is_active"]),
    "products": (Product, ["sku", "name", "name_ar", "category", "unit", "storage_condition",
                           "original_price", "unit_cost", "supplier", "moq", "lead_time_days",
                           "shelf_life_months", "is_active"]),
    "customers": (Customer, ["name", "commercial_register_no", "region", "sales_rep",
                             "odoo_ref"]),
}


def reference_form(model, fields):
    return forms.modelform_factory(model, form=type("RefForm", (BootstrapMixin, forms.ModelForm),
                                                    {}), fields=fields)

