from django import forms
from django.contrib.auth.password_validation import validate_password

from commerce.models import Buyer, BuyerDocument
from core.models import BuyerType, Market

from .i18n import translate


class Styled:
    def __init__(self, *args, lang="en", **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.label = translate(str(field.label), lang)
            w = field.widget
            if isinstance(w, forms.CheckboxInput):
                w.attrs.setdefault("class", "form-check-input")
            elif isinstance(w, forms.Select):
                w.attrs.setdefault("class", "form-select")
            else:
                w.attrs.setdefault("class", "form-control")


class DateInput(forms.DateInput):
    input_type = "date"


DOC_FIELDS = [
    ("doc_owner_id", BuyerDocument.OWNER_ID, "Owner National ID (front and back)", True),
    ("doc_commercial_register", BuyerDocument.COMMERCIAL_REGISTER, "Commercial Register", True),
    ("doc_tax_card", BuyerDocument.TAX_CARD, "Tax Card", True),
    ("doc_licence", BuyerDocument.LICENCE, "Veterinary Trading Licence", True),
    ("doc_cold_storage", BuyerDocument.COLD_STORAGE, "Cold Storage Proof (optional)", False),
]


class RegisterForm(Styled, forms.Form):
    """UC-29 / UC-30 / FR-66, FR-67, FR-69."""

    company_name = forms.CharField(label="Company Name", max_length=160)
    commercial_register_no = forms.CharField(label="Commercial Register Number", max_length=40)
    tax_card_no = forms.CharField(label="Tax Card Number", max_length=40)
    buyer_type = forms.ModelChoiceField(label="Buyer Type",
                                        queryset=BuyerType.objects.filter(is_active=True))
    market = forms.ModelChoiceField(label="Market", queryset=Market.objects.filter(is_active=True))
    address = forms.CharField(label="Address", max_length=255)
    contact_person = forms.CharField(label="Contact Person", max_length=120)
    phone = forms.CharField(label="Phone", max_length=30)
    owner_full_name = forms.CharField(label="Owner Full Name", max_length=160)
    owner_national_id = forms.RegexField(label="National ID Number", regex=r"^\d{14}$",
                                         error_messages={"invalid": "Enter the 14-digit "
                                                                    "national ID number."})
    email = forms.EmailField(label="Email Address")
    password = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)
    doc_owner_id = forms.FileField(label="Owner National ID (front and back)")
    doc_commercial_register = forms.FileField(label="Commercial Register")
    doc_tax_card = forms.FileField(label="Tax Card")
    doc_licence = forms.FileField(label="Veterinary Trading Licence")
    licence_expiry = forms.DateField(label="Licence expiry date", widget=DateInput())
    doc_cold_storage = forms.FileField(label="Cold Storage Proof (optional)", required=False)
    accept_terms = forms.BooleanField(label="I accept the Terms of Service.", required=False)
    consent = forms.BooleanField(
        label="I consent to Kubbar processing my company's and owner's data to verify this "
              "account, under Egyptian Personal Data Protection Law No. 151 of 2020.",
        required=False)

    def clean(self):
        d = super().clean()
        if d.get("password") and d.get("password") != d.get("password2"):
            self.add_error("password2", "The passwords do not match.")
        elif d.get("password"):
            try:
                validate_password(d["password"])
            except forms.ValidationError as e:
                self.add_error("password", e)
        if not (d.get("accept_terms") and d.get("consent")):
            raise forms.ValidationError("You must accept the Terms of Service and the data "
                                        "processing consent.")
        return d


class LoginForm(Styled, forms.Form):
    email = forms.EmailField(label="Email Address")
    password = forms.CharField(label="Password", widget=forms.PasswordInput)


class CheckoutForm(Styled, forms.Form):
    delivery_address = forms.CharField(label="Delivery address", max_length=255)
    cold_chain_confirmed = forms.BooleanField(
        label="I confirm we can receive and store cold-chain products (2-8°C / frozen).",
        required=False)


class TransferForm(Styled, forms.Form):
    receipt = forms.FileField(label="Upload transfer receipt")
    amount = forms.DecimalField(label="Amount transferred (EGP)", max_digits=14,
                                decimal_places=2, min_value=0)


class ResubmitForm(Styled, forms.Form):
    doc_type = forms.ChoiceField(choices=BuyerDocument.TYPES)
    file = forms.FileField()
    expiry_date = forms.DateField(required=False, widget=DateInput(),
                                  label="Licence expiry date")


class PreferencesForm(Styled, forms.ModelForm):
    class Meta:
        model = Buyer
        fields = ["listing_alerts"]
        labels = {"listing_alerts": "Email me when new listings appear in my categories"}
