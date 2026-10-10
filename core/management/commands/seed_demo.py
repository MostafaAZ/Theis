"""Load demonstration data for both applications.

Creates one user per role, reference data, products, 30 months of sales
history, batches spread across the expiry spectrum, a live clearance
listing, verified and pending buyers, orders, and an audit history. The
data is synthetic: it mirrors the shape of Kubbar's Odoo exports, not its
actual figures.

    python manage.py seed_demo            # refuses if data already exists
    python manage.py seed_demo --reset    # wipes the database first
"""
import math
import random
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from commerce import services as shop
from commerce.models import BuyerDocument
from core.models import (BinLocation, BuyerType, Market, ProductCategory, ReasonCode, Region,
                         Role, StorageType, Supplier, User, Warehouse)
from core.rules import ensure_rules
from inventory import services as inv
from inventory.models import (Batch, BatchStatus, BatchStock, Customer, Product,
                              ProductRegistration, PurchaseOrder, PurchaseOrderLine,
                              StockMovement)
from planning.models import SalesRecord

PASSWORD = "Kubbar2026demo"

USERS = [
    ("sales@kubbar.example", "Karim", "Fathy", [Role.SALES], "Sales"),
    ("sales2@kubbar.example", "Nour", "Hassan", [Role.SALES], "Sales"),
    ("warehouse@kubbar.example", "Mahmoud", "Saleh", [Role.WAREHOUSE], "Warehouse"),
    ("inventory@kubbar.example", "Heba", "Mostafa", [Role.INVENTORY], "Warehouse"),
    ("qa@kubbar.example", "Dr. Yasmin", "Adel", [Role.QA], "Quality"),
    ("procurement@kubbar.example", "Omar", "Kamal", [Role.PROCUREMENT], "Procurement"),
    ("regulatory@kubbar.example", "Dr. Salma", "Ibrahim", [Role.REGULATORY], "Regulatory"),
    ("finance@kubbar.example", "Tarek", "Nabil", [Role.FINANCE], "Finance"),
    ("gm@kubbar.example", "Ahmed", "Rashad", [Role.GM], "Management"),
    ("coo@kubbar.example", "Laila", "Mansour", [Role.COO], "Management"),
    ("ceo@kubbar.example", "Hany", "Zaki", [Role.MANAGEMENT], "Management"),
    ("admin@kubbar.example", "System", "Administrator", [Role.ADMIN], "IT"),
]

MARKETS = [("EGY", "Egypt", "EGP", True), ("SAU", "Saudi Arabia", "SAR", False),
           ("ARE", "United Arab Emirates", "AED", False), ("KWT", "Kuwait", "KWD", False),
           ("JOR", "Jordan", "JOD", False), ("IRQ", "Iraq", "IQD", False),
           ("LBN", "Lebanon", "LBP", False), ("SDN", "Sudan", "SDG", False),
           ("LBY", "Libya", "LYD", False)]

REGIONS = [("Cairo & Giza", "Cairo&Giza|Cairo and Giza|Cairo|Giza|القاهرة والجيزة"),
           ("Alexandria", "Alex|الإسكندرية"), ("Delta", "Nile Delta|الدلتا"),
           ("Canal & Sinai", "Canal&Sinai|Canal|القناة"), ("Upper Egypt", "Upper|الصعيد"),
           ("Beheira", "El Beheira|البحيرة")]

CATEGORIES = [("Vaccines", "EDA", True), ("Veterinary Medicaments", "EDA", True),
              ("Feed Additives", "GOVS", True), ("Nutritional Products", "GOVS", True),
              ("Capital Equipment", "-", False)]

# sku, name, arabic, category, unit, storage, price, cost, moq, lead days, shelf months,
# monthly base demand, months of history
PRODUCTS = [
    ("VAC-ND-1000", "Newcastle Disease Vaccine LaSota 1000 ds", "لقاح نيوكاسل لاسوتا", 0,
     "vial", StorageType.REFRIGERATED, 240, 150, 2000, 120, 24, 520, 30),
    ("VAC-IB-1000", "Infectious Bronchitis Vaccine H120 1000 ds", "لقاح التهاب الشعب الهوائية", 0,
     "vial", StorageType.REFRIGERATED, 260, 165, 2000, 120, 24, 380, 30),
    ("VAC-MD-2000", "Marek's Disease Vaccine 2000 ds", "لقاح ماريك", 0, "ampoule",
     StorageType.FROZEN, 610, 410, 500, 150, 18, 90, 30),
    ("MED-AMX-1KG", "Amoxicillin 50% WSP 1 kg", "أموكسيسيلين ٥٠٪", 1, "pack",
     StorageType.AMBIENT, 1150, 720, 1000, 90, 36, 310, 30),
    ("MED-ENR-1L", "Enrofloxacin 10% Oral Solution 1 L", "إنروفلوكساسين ١٠٪", 1, "bottle",
     StorageType.AMBIENT, 640, 390, 1500, 90, 36, 420, 30),
    ("MED-IVM-50", "Ivermectin 1% Injection 50 ml", "إيفرمكتين ١٪", 1, "vial",
     StorageType.AMBIENT, 85, 48, 3000, 75, 30, 640, 30),
    ("MED-OTC-100", "Oxytetracycline 20% LA 100 ml", "أوكسي تتراسيكلين", 1, "vial",
     StorageType.AMBIENT, 120, 70, 2500, 75, 30, 450, 18),
    ("MED-TOL-1L", "Toltrazuril 2.5% Oral 1 L", "تولترازوريل", 1, "bottle",
     StorageType.AMBIENT, 980, 610, 600, 90, 36, 140, 4),
    ("NUT-AD3E-1L", "Vitamin AD3E Oral 1 L", "فيتامين أ د٣ هـ", 3, "bottle",
     StorageType.AMBIENT, 310, 180, 2000, 60, 24, 560, 30),
    ("NUT-ELEC-1KG", "Electrolytes Plus 1 kg", "إلكتروليت بلس", 3, "pack",
     StorageType.AMBIENT, 140, 82, 2000, 60, 24, 470, 12),
    ("FAD-MYC-25", "Mycotoxin Binder 25 kg", "رابط السموم الفطرية", 2, "bag",
     StorageType.AMBIENT, 2100, 1350, 400, 100, 24, 160, 30),
    ("FAD-PHY-20", "Phytase Enzyme 20 kg", "إنزيم الفيتيز", 2, "bag",
     StorageType.AMBIENT, 3400, 2250, 200, 100, 18, 70, 30),
    ("EQP-DRK-01", "Poultry Nipple Drinker Line 3 m", "خط سقاية", 4, "unit",
     StorageType.AMBIENT, 1900, 1300, 50, 120, 120, 25, 30),
]

CUSTOMERS = [("Al Nour Poultry Farms", "CR-10231", 0), ("Delta Feed Mills", "CR-22018", 2),
             ("Misr Vet Distributors", "CR-30442", 0), ("Alexandria Vet Pharmacy", "CR-41190", 1),
             ("Upper Egypt Livestock Co.", "CR-52007", 4), ("Canal Poultry Integrators", "CR-60913",
                                                            3),
             ("Beheira Broiler Farms", "CR-71155", 5), ("Giza Veterinary Clinic", "CR-80321", 0),
             ("Sharkia Layers", "CR-90877", 2), ("Ismailia Agri Supplies", "CR-11902", 3)]

TINY_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids"
            b"[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]>>"
            b"endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def add_months(d, n):
    idx = d.year * 12 + d.month - 1 + n
    return date(idx // 12, idx % 12 + 1, 1)


class Command(BaseCommand):
    help = "Load demonstration data for the IMS and the B2B platform."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Flush the database first.")

    def handle(self, *args, **opts):
        if opts["reset"]:
            call_command("flush", interactive=False, verbosity=0)
        elif User.objects.exists():
            raise CommandError("Data already exists. Use --reset to start again.")
        random.seed(2026)
        # Keep the console quiet: the many notification emails go to memory.
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        with transaction.atomic():
            self.reference()
            self.users()
            self.products()
            self.history()
            self.stock()
        with transaction.atomic():
            self.clearance()
            self.buyers_and_orders()
            self.audits()
        inv.run_expiry_scan(triggered_by="seed")
        self.stdout.write(self.style.SUCCESS(
            f"Demo data loaded. Internal users and buyers share the password '{PASSWORD}'."))

    # ------------------------------------------------------------------
    def reference(self):
        ensure_rules()
        for code, name, cur, active in MARKETS:
            Market.objects.create(code=code, name=name, currency=cur, is_active=active)
        self.egypt = Market.objects.get(code="EGY")
        self.regions = [Region.objects.create(market=self.egypt, name=n, aliases=a)
                        for n, a in REGIONS]
        self.categories = [ProductCategory.objects.create(name=n, authority=a, is_expiring=e)
                           for n, a, e in CATEGORIES]
        cairo = Warehouse.objects.create(name="Cairo Main Warehouse", market=self.egypt,
                                         address="10th of Ramadan City")
        alex = Warehouse.objects.create(name="Alexandria Warehouse", market=self.egypt,
                                        address="Borg El Arab")
        self.bins = {}
        for wh, code, st, cap in [
                (cairo, "A-01", StorageType.AMBIENT, 20000), (cairo, "A-02", StorageType.AMBIENT,
                                                              20000),
                (cairo, "A-03", StorageType.AMBIENT, 15000), (cairo, "C-01",
                                                              StorageType.REFRIGERATED, 12000),
                (cairo, "C-02", StorageType.REFRIGERATED, 12000), (cairo, "F-01",
                                                                   StorageType.FROZEN, 3000),
                (alex, "AX-A1", StorageType.AMBIENT, 15000), (alex, "AX-C1",
                                                              StorageType.REFRIGERATED, 8000)]:
            self.bins[code] = BinLocation.objects.create(warehouse=wh, code=code,
                                                         storage_type=st, capacity=cap)
        self.suppliers = [Supplier.objects.create(name=n, country=c, default_lead_time_days=d)
                          for n, c, d in [("Ceva Santé Animale", "France", 120),
                                          ("Hipra", "Spain", 120),
                                          ("Interchemie", "Netherlands", 90),
                                          ("Kemin Industries", "United States", 100)]]
        for n, ar in [("Distributor", "موزع"), ("Veterinary Clinic", "عيادة بيطرية"),
                      ("Pharmacy", "صيدلية"), ("Farm", "مزرعة"), ("Feed Mill", "مصنع أعلاف"),
                      ("Supplier", "مورد")]:
            BuyerType.objects.create(name=n, name_ar=ar)
        for kind, items in [
                (ReasonCode.VARIANCE, ["Counting Error", "Damage", "Loss", "Expiry Not Recorded",
                                       "Mis-location"]),
                (ReasonCode.WRITE_OFF, ["Expired", "Damaged", "Rejected at Inspection",
                                        "Temperature Breach"])]:
            for label in items:
                ReasonCode.objects.create(kind=kind, code=label.lower().replace(" ", "_"),
                                          label=label)

    def users(self):
        self.u = {}
        for email, first, last, roles, dept in USERS:
            user = User.objects.create_user(email=email, password=PASSWORD, first_name=first,
                                            last_name=last, roles=roles, department=dept,
                                            email_verified=True)
            self.u[email.split("@")[0]] = user
        self.u["sales"].categories.set(self.categories[:2])
        self.u["sales"].regions.set(self.regions[:3])
        self.u["sales2"].categories.set(self.categories[2:4])
        self.u["sales2"].regions.set(self.regions[3:])
        self.customers = []
        for i, (name, cr, ridx) in enumerate(CUSTOMERS):
            rep = self.u["sales"] if ridx < 3 else self.u["sales2"]
            self.customers.append(Customer.objects.create(
                name=name, commercial_register_no=cr, region=self.regions[ridx], sales_rep=rep,
                odoo_ref=f"RES-{1000 + i}"))

    def products(self):
        self.products_ = []
        today = timezone.localdate()
        for i, (sku, name, ar, cidx, unit, st, price, cost, moq, lt, shelf, base, hist) in \
                enumerate(PRODUCTS):
            p = Product.objects.create(
                sku=sku, name=name, name_ar=ar, category=self.categories[cidx], unit=unit,
                storage_condition=st, original_price=Decimal(price), unit_cost=Decimal(cost),
                supplier=self.suppliers[i % 4], moq=moq, lead_time_days=lt,
                shelf_life_months=shelf)
            p.base_demand, p.history = base, hist
            self.products_.append(p)
            if self.categories[cidx].is_expiring:
                status, exp = ProductRegistration.ACTIVE, today + timedelta(days=900)
                if sku == "MED-OTC-100":
                    status = ProductRegistration.SUSPENDED  # demonstrates listing block
                if sku == "NUT-ELEC-1KG":
                    exp = today + timedelta(days=60)  # demonstrates the expiry alert
                ProductRegistration.objects.create(
                    product=p, market=self.egypt, authority=self.categories[cidx].authority,
                    registration_no=f"{self.categories[cidx].authority}-{24000 + i * 37}",
                    status=status, expiry_date=exp, updated_by=self.u["regulatory"])

    def history(self):
        """30 months of monthly sales with seasonality, by product, region and customer."""
        last = add_months(timezone.localdate().replace(day=1), -1)
        weights = [0.34, 0.16, 0.2, 0.1, 0.12, 0.08]
        recs = []
        for p in self.products_:
            for m in range(p.history):
                period = add_months(last, -(p.history - 1 - m))
                season = 1 + 0.25 * math.sin(2 * math.pi * (period.month - 3) / 12)
                trend = 1 + 0.01 * m
                total = p.base_demand * season * trend * random.uniform(0.85, 1.15)
                for r_i, region in enumerate(self.regions):
                    q = int(round(total * weights[r_i] * random.uniform(0.8, 1.2)))
                    if q <= 0:
                        continue
                    custs = [c for c in self.customers if c.region_id == region.pk] or \
                        self.customers[:1]
                    cust = random.choice(custs)
                    price = p.original_price * Decimal(random.choice([1, 1, 0.97, 0.95]))
                    price = price.quantize(Decimal("0.01"))
                    recs.append(SalesRecord(
                        order_number=f"SO{period:%y%m}{random.randint(1000, 9999)}",
                        period=period, region=region, customer=cust, customer_name=cust.name,
                        product=p, quantity=q, unit_price=price, total=price * q))
        SalesRecord.objects.bulk_create(recs)

    def _batch(self, p, lot, days_to_expiry, qty, bin_code, status=BatchStatus.IN_STOCK,
               po=None):
        today = timezone.localdate()
        expiry = today + timedelta(days=days_to_expiry)
        b = Batch.objects.create(
            product=p, lot_number=lot, manufacture_date=expiry - timedelta(
                days=int(p.shelf_life_months * 30.4)), expiry_date=expiry,
            storage_condition=p.storage_condition, market=self.egypt, purchase_order=po,
            quantity_received=qty, quantity_approved=qty, inspected_by=self.u["qa"],
            inspection_date=today - timedelta(days=max(1, p.shelf_life_months * 30 -
                                                       days_to_expiry)),
            received_at=timezone.now(), received_by=self.u["inventory"], status=status)
        if status != BatchStatus.INSPECTED:
            BatchStock.objects.create(batch=b, location=self.bins[bin_code], quantity=qty)
            b.qty_on_hand = qty
            b.save()
            StockMovement.objects.create(batch=b, movement_type=StockMovement.RECEIPT,
                                         quantity=qty, to_location=self.bins[bin_code],
                                         user=self.u["inventory"],
                                         reference=po.number if po else "Opening balance")
        return b

    def stock(self):
        P = {p.sku: p for p in self.products_}
        today = timezone.localdate()
        old_po = PurchaseOrder.objects.create(number="PO-2025-0418", supplier=self.suppliers[0],
                                              order_date=today - timedelta(days=500),
                                              status=PurchaseOrder.RECEIVED)
        cold = lambda p: "F-01" if p.storage_condition == StorageType.FROZEN else (  # noqa: E731
            "C-01" if p.storage_condition == StorageType.REFRIGERATED else "A-01")
        plan = [  # sku, lot suffix, days to expiry, qty
            ("VAC-ND-1000", "A", 520, 2400), ("VAC-ND-1000", "B", 140, 1600),
            ("VAC-IB-1000", "A", 410, 1800), ("VAC-IB-1000", "B", 95, 900),
            ("VAC-MD-2000", "A", 300, 400), ("MED-AMX-1KG", "A", 700, 900),
            ("MED-AMX-1KG", "B", 165, 1300), ("MED-ENR-1L", "A", 610, 1500),
            ("MED-ENR-1L", "B", 25, 220), ("MED-IVM-50", "A", 480, 3500),
            ("MED-IVM-50", "B", 120, 2600), ("MED-OTC-100", "A", 150, 1400),
            ("MED-TOL-1L", "A", 640, 500), ("NUT-AD3E-1L", "A", 380, 2200),
            ("NUT-AD3E-1L", "B", 70, 1900), ("NUT-ELEC-1KG", "A", 300, 1600),
            ("FAD-MYC-25", "A", 450, 420), ("FAD-MYC-25", "B", 175, 380),
            ("FAD-PHY-20", "A", 260, 160), ("EQP-DRK-01", "A", 3000, 60),
        ]
        for sku, sfx, days, qty in plan:
            p = P[sku]
            self._batch(p, f"{sku.split('-')[1]}{today:%y}{sfx}{random.randint(100, 999)}", days,
                        qty, cold(p), po=old_po if sfx == "B" else None)
        # One expired batch still on hand.
        self._batch(P["NUT-ELEC-1KG"], "ELEC24X118", -12, 140, "A-02")
        # Open purchase orders and a shipment waiting for QA / receipt.
        po1 = PurchaseOrder.objects.create(number="PO-2026-0712", supplier=self.suppliers[2],
                                           order_date=today - timedelta(days=80),
                                           expected_date=today + timedelta(days=5))
        PurchaseOrderLine.objects.create(order=po1, product=P["MED-ENR-1L"], quantity=1500)
        PurchaseOrderLine.objects.create(order=po1, product=P["MED-IVM-50"], quantity=3000)
        po2 = PurchaseOrder.objects.create(number="PO-2026-0745", supplier=self.suppliers[1],
                                           order_date=today - timedelta(days=60),
                                           expected_date=today + timedelta(days=40))
        PurchaseOrderLine.objects.create(order=po2, product=P["VAC-IB-1000"], quantity=2000)
        PurchaseOrderLine.objects.create(order=po2, product=P["VAC-ND-1000"], quantity=2000)
        self._batch(P["MED-IVM-50"], f"IVM{today:%y}N501", 690, 3000, "A-01",
                    status=BatchStatus.INSPECTED, po=po1)
        Batch.objects.filter(lot_number=f"IVM{today:%y}N501").update(qty_on_hand=0)
        # Mark all near-expiry batches.
        for b in Batch.objects.filter(qty_on_hand__gt=0):
            inv.evaluate_batch(b)

    def clearance(self):
        """Walk two batches through offer → surplus → release (one listed, one pending)."""
        sales, coo = self.u["sales"], self.u["coo"]
        nd = Batch.objects.get(product__sku="VAC-ND-1000", status=BatchStatus.NEAR_EXPIRY)
        inv.record_offer(sales, nd, self.customers[0], 600, "accepted")
        inv.record_offer(sales, nd, self.customers[2], 500, "declined")
        rel = inv.flag_surplus(sales, nd, 900, "declined", "Two main accounts already stocked")
        inv.approve_release(coo, rel, 900, inv.proposed_clearance_price(nd.product.original_price))
        ivm = Batch.objects.get(product__sku="MED-IVM-50", status=BatchStatus.NEAR_EXPIRY)
        rel2 = inv.flag_surplus(sales, ivm, 1800, "no_demand", "Season ending")
        inv.approve_release(coo, rel2, 1800, inv.price_band(ivm.product.original_price)[0])
        amx = Batch.objects.get(product__sku="MED-AMX-1KG", status=BatchStatus.NEAR_EXPIRY)
        inv.record_offer(sales, amx, self.customers[7], 300, "pending")
        inv.flag_surplus(sales, amx, 800, "no_demand", "Projected unsold at expiry")
        ad3e = Batch.objects.get(product__sku="NUT-AD3E-1L", status=BatchStatus.NEAR_EXPIRY)
        inv.flag_surplus(self.u["sales2"], ad3e, 1200, "seasonal", "Summer demand passed")

    def _register(self, email, company, btype, cr):
        files = {t: SimpleUploadedFile(f"{t}.pdf", TINY_PDF, content_type="application/pdf")
                 for t in BuyerDocument.MANDATORY}
        files[BuyerDocument.COLD_STORAGE] = SimpleUploadedFile("cold.pdf", TINY_PDF,
                                                               content_type="application/pdf")
        buyer = shop.register_buyer(
            email=email, password=PASSWORD, company_name=company,
            buyer_type=BuyerType.objects.get(name=btype), market=self.egypt,
            commercial_register_no=cr, tax_card_no=f"TX-{cr[3:]}-77",
            address="12 Tahrir St, Dokki, Giza", contact_person="Mona Samir",
            phone="+20 100 555 0101", owner_full_name="Samir Abdelrahman",
            owner_national_id="28901011234567", documents=files,
            licence_expiry=timezone.localdate() + timedelta(days=400), consent=True)
        shop.confirm_email(buyer.email_token)
        return buyer

    def buyers_and_orders(self):
        ra = self.u["regulatory"]
        v1 = self._register("buyer@vetclinic.example", "Nile Veterinary Clinic",
                            "Veterinary Clinic", "CR-70001")
        for d in v1.documents.all():
            shop.review_document(ra, d, BuyerDocument.APPROVED)
        shop.verify_buyer(ra, v1, list(ProductCategory.objects.filter(is_expiring=True)), True)
        v2 = self._register("buyer@sinaifarms.example", "Sinai Poultry Farms", "Farm", "CR-70002")
        for d in v2.documents.all():
            shop.review_document(ra, d, BuyerDocument.APPROVED)
        shop.verify_buyer(ra, v2, list(ProductCategory.objects.filter(
            name__in=["Veterinary Medicaments", "Feed Additives"])), False)
        shop.set_payment_terms(self.u["finance"], v2, "30", Decimal("250000"))
        self._register("buyer@newdistrib.example", "Pharaoh Vet Distribution", "Distributor",
                       "CR-70003")
        # Matches an existing customer's commercial register (FR-70).
        self._register("buyer@sharkia.example", "Sharkia Layers", "Farm", "CR-90877")
        # Orders: one confirmed waiting for dispatch, one awaiting payment confirmation.
        from inventory.models import Listing

        nd_listing = Listing.objects.get(batch__product__sku="VAC-ND-1000")
        ivm_listing = Listing.objects.get(batch__product__sku="MED-IVM-50")
        o1, _ = shop.place_order(v1, [(nd_listing.pk, 200)], v1.address, cold_chain_confirmed=True)
        receipt = SimpleUploadedFile("transfer.pdf", TINY_PDF, content_type="application/pdf")
        shop.submit_bank_transfer(o1, receipt, o1.total)
        shop.confirm_payment(self.u["finance"], o1)
        o2, _ = shop.place_order(v2, [(ivm_listing.pk, 400)], "Farm road 4, El Arish")
        receipt = SimpleUploadedFile("transfer.pdf", TINY_PDF, content_type="application/pdf")
        shop.submit_bank_transfer(o2, receipt, o2.total)

    def audits(self):
        ic, wo = self.u["inventory"], self.u["warehouse"]
        cairo = Warehouse.objects.get(name="Cairo Main Warehouse")
        vaccines = ProductCategory.objects.get(name="Feed Additives")
        count = inv.schedule_count(ic, count_type="cycle", warehouse=cairo, scope="category",
                                   category=vaccines, scheduled_date=timezone.localdate(),
                                   assigned_to=wo)
        entries = {}
        for i, line in enumerate(count.lines.all()):
            entries[line.pk] = {"counted": line.system_qty - (
                max(3, line.system_qty // 10) if i == 0 else 0)}
        inv.submit_count(wo, count, entries)
        inv.schedule_count(ic, count_type="cycle", warehouse=cairo, scope="category",
                           category=ProductCategory.objects.get(name="Nutritional Products"),
                           scheduled_date=timezone.localdate() + timedelta(days=3),
                           assigned_to=wo)

