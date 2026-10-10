"""IMS web tests: authentication, RBAC, screens and exports."""
import re

from django.test import TestCase, override_settings

from core.models import LoginCode, Role, User
from core.testing import PASSWORD, World


class LoginTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_two_factor_login(self):  # UC-01, NFR-23
        r = self.client.post("/login/", {"email": "gm@test.example", "password": PASSWORD})
        self.assertRedirects(r, "/login/verify/")
        self.assertNotIn("_auth_user_id", self.client.session)
        code = LoginCode.objects.get(user=self.w.user(Role.GM), used=False).code
        r = self.client.post("/login/verify/", {"code": "000000" if code != "000000" else "1"})
        self.assertNotIn("_auth_user_id", self.client.session)
        r = self.client.post("/login/verify/", {"code": code})
        self.assertRedirects(r, "/")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_lock_after_five_failures(self):  # FR-04
        for _ in range(5):
            self.client.post("/login/", {"email": "qa@test.example", "password": "wrong"})
        qa = User.objects.get(email="qa@test.example")
        self.assertTrue(qa.is_locked)
        self.assertTrue(self.w.user(Role.ADMIN).notifications.filter(
            message__contains="locked").exists())
        r = self.client.post("/login/", {"email": "qa@test.example", "password": PASSWORD})
        self.assertEqual(r.status_code, 200)  # still locked

    def test_buyer_cannot_log_in_to_ims(self):  # NFR-02
        self.w.buyer()
        r = self.client.post("/login/", {"email": "buyer@test.example", "password": PASSWORD})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(LoginCode.objects.exists())

    def test_buyer_session_is_rejected_by_ims(self):  # NFR-02
        b = self.w.buyer()
        self.client.force_login(b.user)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])


class AccessControlTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_role_restrictions(self):  # FR-03
        self.client.force_login(self.w.user(Role.QA))
        self.assertEqual(self.client.get("/inspections/").status_code, 200)
        for url in ("/releases/", "/admin/users/", "/payments/", "/plans/", "/analytics/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_documents_hidden_from_finance(self):  # NFR-25
        b = self.w.buyer()
        doc = b.documents.first()
        self.client.force_login(self.w.user(Role.FINANCE))
        self.assertEqual(self.client.get(f"/buyers/document/{doc.pk}/").status_code, 403)
        self.client.force_login(self.w.user(Role.REGULATORY))
        r = self.client.get(f"/buyers/document/{doc.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))
        from core.models import AuditLog

        self.assertTrue(AuditLog.objects.filter(action=AuditLog.DOCUMENT_VIEW).exists())


class ScreenTests(TestCase):
    """Every menu screen renders for every role, with the demo data loaded."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            call_command("seed_demo", verbosity=0)

    def test_all_menu_pages_and_links(self):
        from ims.nav import nav_for

        for user in User.objects.filter(account_type=User.INTERNAL):
            self.client.force_login(user)
            for section in nav_for(user):
                for url in [item["url"] for item in section["items"]]:
                    r = self.client.get(url)
                    self.assertEqual(r.status_code, 200, f"{user.email} {url}")
                    for link in sorted(set(re.findall(r'href="(/(?:stock/batch|expiry/batch|releases|'
                                               r'listings|audits|plans|forecasts|write-offs|'
                                               r'buyers|payments|credit)/\d+/)"',
                                               r.content.decode())))[:5]:
                        r2 = self.client.get(link)
                        self.assertIn(r2.status_code, (200, 302, 403), f"{user.email} {link}")

    def test_analytics_exports(self):  # FR-65
        self.client.force_login(User.objects.get(email="gm@kubbar.example"))
        for key in ("inventory", "sales", "forecast", "recovery", "audit", "platform"):
            r = self.client.get(f"/analytics/?d={key}")
            self.assertEqual(r.status_code, 200)
        r = self.client.get("/analytics/?d=sales&export=pdf")
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        r = self.client.get("/analytics/?d=inventory&export=xlsx")
        self.assertTrue(r.content.startswith(b"PK"))

    def test_release_workflow_through_screens(self):  # UC-15 to UC-18
        from inventory.models import Listing, SurplusRelease

        coo = User.objects.get(email="coo@kubbar.example")
        self.client.force_login(coo)
        rel = SurplusRelease.objects.filter(status=SurplusRelease.PENDING).first()
        lo, hi = __import__("inventory.services", fromlist=["x"]).price_band(
            rel.batch.product.original_price)
        r = self.client.post(f"/releases/{rel.pk}/", {"release_quantity": rel.surplus_qty,
                                                      "clearance_price": str(lo - 1)})
        self.assertContains(r, "Clearance price must be between")
        r = self.client.post(f"/releases/{rel.pk}/", {"release_quantity": rel.surplus_qty,
                                                      "clearance_price": str(hi)})
        self.assertRedirects(r, "/releases/")
        self.assertTrue(Listing.objects.filter(release=rel).exists())


class NewScreensTests(TestCase):
    """Dashboards, Post to B2B, purchase orders, search, profile, reports, messages."""

    def setUp(self):
        self.w = World()
        from inventory import services as inv

        self.inv = inv
        self.batch = self.w.batch(days=150, qty=300)
        inv.evaluate_batch(self.batch)

    def test_dashboard_lists_tasks_for_the_role(self):
        self.client.force_login(self.w.user(Role.SALES))
        r = self.client.get("/")
        self.assertContains(r, "Review near-expiry batches")
        self.assertContains(r, 'href="/b2b/post/"')

    def test_coo_posts_to_b2b_directly(self):
        from inventory.models import Listing

        self.client.force_login(self.w.user(Role.COO))
        r = self.client.get("/b2b/post/")
        self.assertContains(r, "Post this batch")
        r = self.client.post(f"/b2b/post/{self.batch.pk}/", {
            "quantity": 100, "reason": "no_demand", "clearance_price": "68.00"})
        self.assertRedirects(r, "/listings/")
        listing = Listing.objects.get()
        self.assertEqual((listing.listed_qty, str(listing.clearance_price)), (100, "68.00"))

    def test_sales_post_goes_to_coo(self):
        from inventory.models import Listing, SurplusRelease

        self.client.force_login(self.w.user(Role.SALES))
        r = self.client.post(f"/b2b/post/{self.batch.pk}/", {"quantity": 50,
                                                              "reason": "no_demand"})
        self.assertRedirects(r, "/b2b/post/")
        self.assertFalse(Listing.objects.exists())
        self.assertEqual(SurplusRelease.objects.get().status, SurplusRelease.PENDING)

    def test_unregistered_batch_is_not_postable(self):
        self.w.product.registrations.all().delete()
        eligible, blocked = self.inv.b2b_candidates()
        self.assertEqual(eligible, [])
        self.assertIn("registration", blocked[0][2])

    def test_purchase_order_by_hand(self):
        from inventory.models import PurchaseOrder

        self.client.force_login(self.w.user(Role.PROCUREMENT))
        r = self.client.post("/purchase-orders/new/", {
            "supplier": self.w.supplier.pk, "product_0": self.w.product.pk, "qty_0": "500"})
        po = PurchaseOrder.objects.get()
        self.assertRedirects(r, f"/purchase-orders/{po.pk}/")
        self.assertEqual(po.lines.get().quantity, 500)

    def test_product_create_and_detail(self):
        self.client.force_login(self.w.user(Role.PROCUREMENT))
        r = self.client.post("/products/new/", {
            "sku": "NEW-1", "name": "New Product", "category": self.w.meds.pk, "unit": "bottle",
            "storage_condition": "ambient", "original_price": "50", "unit_cost": "30",
            "moq": "100", "lead_time_days": "60", "shelf_life_months": "24", "is_active": "on"})
        from inventory.models import Product

        p = Product.objects.get(sku="NEW-1")
        self.assertRedirects(r, f"/products/{p.pk}/")
        self.assertContains(self.client.get(f"/products/{p.pk}/"), "New Product")
        img = self.client.get(f"/products/{p.pk}/image/")
        self.assertEqual(img["Content-Type"], "image/svg+xml")

    def test_global_search_and_profile(self):
        self.client.force_login(self.w.user(Role.GM))
        r = self.client.get("/search/?q=Enro")
        self.assertRedirects(r, f"/products/{self.w.product.pk}/")
        r = self.client.post("/profile/", {"password": "1", "old_password": PASSWORD,
                                           "new_password1": "Newpass2026abc",
                                           "new_password2": "Newpass2026abc"})
        self.assertRedirects(r, "/profile/")
        self.assertTrue(User.objects.get(pk=self.w.user(Role.GM).pk).check_password(
            "Newpass2026abc"))

    def test_reports_download(self):
        self.client.force_login(self.w.user(Role.INVENTORY))
        r = self.client.get("/reports/stock.xlsx")
        self.assertTrue(r.content.startswith(b"PK"))
        r = self.client.get("/reports/near-expiry.pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertEqual(self.client.get("/reports/orders.pdf").status_code, 200)
        self.client.force_login(self.w.user(Role.QA))
        self.assertEqual(self.client.get("/reports/sales.pdf").status_code, 404)

    def test_reply_to_buyer_message(self):
        from commerce.models import SupportMessage

        m = SupportMessage.objects.create(name="A", email="a@b.example", topic="other",
                                          message="Hi")
        self.client.force_login(self.w.user(Role.SALES))
        r = self.client.post(f"/b2b/messages/{m.pk}/", {"reply": "Hello back"})
        self.assertRedirects(r, "/b2b/messages/")
        m.refresh_from_db()
        self.assertEqual((m.status, m.reply), (SupportMessage.ANSWERED, "Hello back"))
