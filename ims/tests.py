"""IMS web tests: authentication, RBAC, screens and exports."""
import re
from unittest import mock

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
        for key in ("inventory", "sales", "forecast", "recovery", "audit", "platform",
                    "exceptions"):
            r = self.client.get(f"/analytics/?d={key}")
            self.assertEqual(r.status_code, 200)
        r = self.client.get("/analytics/?d=exceptions&export=xlsx")
        self.assertTrue(r.content.startswith(b"PK"))
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


class PresentationModeTests(TestCase):
    def setUp(self):
        self.w = World()
        self.gm = self.w.user(Role.GM)

    def test_off_by_default(self):
        self.assertEqual(self.client.get(f"/demo/login/{self.gm.pk}/").status_code, 404)
        self.assertEqual(self.client.post("/demo/reset/").status_code, 404)
        self.assertNotContains(self.client.get("/login/"), "Presentation mode")

    @override_settings(DEMO_MODE=True)
    @mock.patch.dict("core.demo.STAFF", {"gm@test.example": ("GM", ["Approve things"]),
                                         "coo@test.example": ("COO", ["Release things"])})
    def test_one_click_login_and_switch(self):
        r = self.client.get("/login/")
        self.assertContains(r, "Who do you want to present as?")
        self.assertContains(r, f'/demo/login/{self.gm.pk}/')
        r = self.client.get(f"/demo/login/{self.gm.pk}/")
        self.assertRedirects(r, "/")
        r = self.client.get("/")
        self.assertContains(r, "Switch user")
        self.assertContains(r, "Presenter notes")
        coo = self.w.user(Role.COO)
        self.client.get(f"/demo/login/{coo.pk}/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), coo.pk)

    @override_settings(DEMO_MODE=True)
    def test_buyers_cannot_be_used_on_ims(self):
        b = self.w.buyer()
        self.assertEqual(self.client.get(f"/demo/login/{b.user.pk}/").status_code, 404)


class AfterSalesScreenTests(TestCase):
    """Order page actions, exception queue, returns and credit notes."""

    def setUp(self):
        from commerce import services as shop
        from core.testing import pdf

        self.w = World()
        listing = self.w.listing(qty=50)
        self.buyer = self.w.buyer()
        self.order, _ = shop.place_order(self.buyer, [(listing.pk, 10)], "addr")
        shop.submit_bank_transfer(self.order, pdf("r.pdf"), self.order.total)
        shop.confirm_payment(self.w.user(Role.FINANCE), self.order)

    def test_finance_holds_and_cancels_from_the_order_page(self):
        self.client.force_login(self.w.user(Role.FINANCE))
        url = f"/b2b/orders/{self.order.pk}/"
        self.assertContains(self.client.get(url), "Put on hold")
        self.client.post(url, {"action": "hold", "reason": "payment", "note": "Check"})
        self.order.refresh_from_db()
        self.assertTrue(self.order.on_hold)
        self.assertContains(self.client.get("/b2b/exceptions/"), self.order.reference)
        self.client.post(url, {"action": "release", "comment": "OK"})
        line = self.order.lines.get()
        self.client.post(url, {"action": "cancel_lines", f"cancel_{line.pk}": "2",
                               "reason": "Buyer asked"})
        line.refresh_from_db()
        self.assertEqual(line.cancelled_qty, 2)
        self.assertContains(self.client.get("/b2b/credit-notes/"), "Refund due")
        r = self.client.get(f"/b2b/orders/{self.order.pk}/packing-slip/")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_warehouse_cannot_cancel_lines(self):
        self.client.force_login(self.w.user(Role.WAREHOUSE))
        line = self.order.lines.get()
        self.client.post(f"/b2b/orders/{self.order.pk}/",
                         {"action": "cancel_lines", f"cancel_{line.pk}": "2", "reason": "x"})
        line.refresh_from_db()
        self.assertEqual(line.cancelled_qty, 0)
        self.assertEqual(self.client.get("/b2b/credit-notes/").status_code, 403)

    def test_return_steps_by_role(self):
        from commerce import orders, services as shop
        from inventory import services as inv

        o = inv.dispatch_platform_order(self.w.user(Role.WAREHOUSE), self.order,
                                        {self.order.lines.get().pk:
                                         self.order.lines.get().batch.lot_number},
                                        "Fleet", self.w.today)
        shop.confirm_delivery(o)
        rr = orders.request_return(self.buyer, o, [(o.lines.get(), 2, "damaged")])
        url = f"/b2b/returns/{rr.pk}/"
        self.client.force_login(self.w.user(Role.FINANCE))
        self.client.post(url, {"decide": "approve", "resolution": "credit"})
        self.client.force_login(self.w.user(Role.WAREHOUSE))
        rl = rr.lines.get()
        self.client.post(url, {"receive": "1", f"received_{rl.pk}": "2"})
        self.client.force_login(self.w.user(Role.QA))
        self.client.post(url, {"inspect": "1", f"outcome_{rl.pk}": "restock",
                               f"location_{rl.pk}": self.w.ambient.pk})
        rr.refresh_from_db()
        self.assertEqual(rr.status, "completed")
        self.assertEqual(rr.credit_notes.count(), 1)


class AnalyticsStoryTests(TestCase):
    """Storytelling charts, analysis per perspective and AI analysis (Module 6)."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            call_command("seed_demo", verbosity=0)

    def login(self, email):
        self.client.force_login(User.objects.get(email=email))

    def test_every_dashboard_and_perspective_renders(self):
        from analytics.services import DASHBOARDS
        from analytics.story import PERSPECTIVES

        self.login("cfo@kubbar.example")
        r = self.client.get("/analytics/")
        self.assertContains(r, "Executive Briefing")
        self.assertEqual(r.context["data"]["perspective"], "cfo")
        for key, _label in DASHBOARDS:
            for view, _ in PERSPECTIVES:
                r = self.client.get(f"/analytics/?d={key}&view={view}")
                self.assertEqual(r.status_code, 200, (key, view))
                self.assertEqual(r.context["data"]["analysis"]["perspective"], view)
                for c in r.context["data"]["charts"]:
                    self.assertTrue(c["headline"] and c["view"]["insight"], (key, c["id"]))

    def test_perspective_changes_the_story(self):
        from analytics.services import Filters, build

        user = User.objects.get(email="ceo@kubbar.example")
        coo = build("executive", Filters({}, user), "coo")
        gm = build("executive", Filters({}, user), "gm")
        self.assertEqual(coo["charts"][0]["id"], "funnel")
        self.assertEqual(gm["charts"][0]["id"], "moq")
        self.assertNotEqual(coo["analysis"]["actions"], gm["analysis"]["actions"])
        self.assertEqual(coo["analysis"]["perspective"], "coo")

    def test_executive_report_pdf_has_charts_and_analysis(self):
        self.login("gm@kubbar.example")
        r = self.client.get("/reports/executive.pdf")
        self.assertEqual(r["Content-Type"], "application/pdf")
        r = self.client.get("/analytics/?d=executive&export=xlsx")
        self.assertTrue(r.content.startswith(b"PK"))

    def test_home_briefing_for_executives_only(self):
        self.login("coo@kubbar.example")
        self.assertContains(self.client.get("/"), "Executive briefing")
        self.login("warehouse@kubbar.example")
        r = self.client.get("/")
        self.assertNotContains(r, "Executive briefing")
        self.assertTrue(r.context["charts"])

    def test_ai_analysis_without_key_explains_setup(self):
        self.login("ceo@kubbar.example")
        with mock.patch("analytics.ai.api_key", return_value=""):
            r = self.client.post("/analytics/ai/", {"d": "executive", "view": "ceo"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("API key", r.json()["error"])
        self.login("sales@kubbar.example")
        self.assertEqual(self.client.post("/analytics/ai/", {"d": "sales"}).status_code, 403)

    def test_ai_analysis_maps_claude_answer_to_charts(self):
        from django.core.cache import cache

        cache.clear()
        answer = {"summary": "Recovery lags write-offs.", "findings": ["F1"], "risks": ["R1"],
                  "actions": ["A1"], "charts": [
                      {"id": "outcomes", "insight": "I", "implication": "M", "action": "X"},
                      {"id": "made_up", "insight": "?", "implication": "?", "action": "?"}]}
        self.login("cfo@kubbar.example")
        with mock.patch("analytics.ai.api_key", return_value="sk-test"), \
                mock.patch("analytics.ai._call", return_value=answer) as call:
            r = self.client.post("/analytics/ai/", {"d": "executive", "view": "cfo"})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["source"], "ai")
            self.assertEqual(set(data["charts"]), {"outcomes"})
            prompt = call.call_args[0][0]
            self.assertIn("Group CFO", prompt)
            self.assertIn('"outcomes"', prompt)
            # A second request with the same data is served from the cache.
            self.client.post("/analytics/ai/", {"d": "executive", "view": "cfo"})
            self.assertEqual(call.call_count, 1)
