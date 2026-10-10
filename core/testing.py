"""Shared fixtures for the test suite."""
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from core.models import (BinLocation, BuyerType, Market, ProductCategory, ReasonCode, Region,
                         Role, StorageType, Supplier, User, Warehouse)
from core.rules import ensure_rules

PASSWORD = "Testing2026pass"
PDF = b"%PDF-1.4\n%test document\n%%EOF\n"


def pdf(name="doc.pdf"):
    return SimpleUploadedFile(name, PDF, content_type="application/pdf")


class World:
    """A small but complete set of reference data, users and products."""

    def __init__(self):
        ensure_rules()
        self.today = timezone.localdate()
        self.egypt = Market.objects.create(code="EGY", name="Egypt", is_active=True)
        self.cairo = Region.objects.create(market=self.egypt, name="Cairo & Giza",
                                           aliases="Cairo&Giza|Cairo")
        self.alex = Region.objects.create(market=self.egypt, name="Alexandria")
        self.meds = ProductCategory.objects.create(name="Veterinary Medicaments", authority="EDA")
        self.vaccines = ProductCategory.objects.create(name="Vaccines", authority="EDA")
        self.equipment = ProductCategory.objects.create(name="Capital Equipment", authority="-",
                                                        is_expiring=False)
        self.wh = Warehouse.objects.create(name="Cairo", market=self.egypt)
        self.ambient = BinLocation.objects.create(warehouse=self.wh, code="A-01", capacity=100000)
        self.ambient2 = BinLocation.objects.create(warehouse=self.wh, code="A-02",
                                                   capacity=100000)
        self.cold = BinLocation.objects.create(warehouse=self.wh, code="C-01",
                                               storage_type=StorageType.REFRIGERATED,
                                               capacity=100000)
        self.supplier = Supplier.objects.create(name="Supplier")
        self.farm = BuyerType.objects.create(name="Farm")
        for label in ["Counting Error", "Damage", "Loss"]:
            ReasonCode.objects.create(kind=ReasonCode.VARIANCE,
                                      code=label.lower().replace(" ", "_"), label=label)
        self.users = {}
        for role in Role:
            self.users[role.value] = User.objects.create_user(
                email=f"{role.value}@test.example", password=PASSWORD, roles=[role.value],
                first_name=role.label)
        from inventory.models import Product, ProductRegistration

        self.product = Product.objects.create(
            sku="MED-1", name="Enrofloxacin", category=self.meds, original_price=Decimal("100"),
            unit_cost=Decimal("60"), moq=500, lead_time_days=60, shelf_life_months=24)
        self.vaccine = Product.objects.create(
            sku="VAC-1", name="ND Vaccine", category=self.vaccines,
            storage_condition=StorageType.REFRIGERATED, original_price=Decimal("200"),
            unit_cost=Decimal("120"), moq=100)
        for p in (self.product, self.vaccine):
            ProductRegistration.objects.create(product=p, market=self.egypt, authority="EDA",
                                               registration_no=f"R-{p.sku}",
                                               expiry_date=self.today + timedelta(days=1500))

    def user(self, role):
        return self.users[role]

    def batch(self, product=None, days=400, qty=100, lot=None, location=None, status=None):
        from inventory.models import Batch, BatchStatus, BatchStock

        product = product or self.product
        lot = lot or f"L{Batch.objects.count() + 1:04d}"
        b = Batch.objects.create(
            product=product, lot_number=lot,
            manufacture_date=self.today - timedelta(days=300),
            expiry_date=self.today + timedelta(days=days),
            storage_condition=product.storage_condition, market=self.egypt,
            quantity_received=qty, quantity_approved=qty, qty_on_hand=qty,
            status=status or BatchStatus.IN_STOCK, received_at=timezone.now())
        loc = location or (self.cold if product.is_cold_chain else self.ambient)
        BatchStock.objects.create(batch=b, location=loc, quantity=qty)
        return b

    def buyer(self, email="buyer@test.example", cr="CR-1", verified=True, cold=False):
        from commerce import services as shop
        from commerce.models import BuyerDocument

        docs = {t: pdf() for t in BuyerDocument.MANDATORY}
        if cold:
            docs[BuyerDocument.COLD_STORAGE] = pdf()
        b = shop.register_buyer(
            email=email, password=PASSWORD, company_name=f"Co {cr}", buyer_type=self.farm,
            market=self.egypt, commercial_register_no=cr, tax_card_no="T1", address="Cairo",
            contact_person="Contact", phone="1", owner_full_name="Owner",
            owner_national_id="29001011234567", documents=docs,
            licence_expiry=self.today + timedelta(days=300), consent=True)
        shop.confirm_email(b.email_token)
        if verified:
            ra = self.user(Role.REGULATORY)
            for d in b.documents.all():
                shop.review_document(ra, d, BuyerDocument.APPROVED)
            shop.verify_buyer(ra, b, [self.meds, self.vaccines], cold)
        b.refresh_from_db()
        return b

    def listing(self, batch=None, qty=50, price=None):
        from inventory import services as inv

        batch = batch or self.batch(days=150)
        inv.evaluate_batch(batch)
        batch.refresh_from_db()
        rel = inv.flag_surplus(self.user(Role.SALES), batch, qty, "no_demand")
        price = price or inv.proposed_clearance_price(batch.product.original_price)
        return inv.approve_release(self.user(Role.COO), rel, qty, price)
