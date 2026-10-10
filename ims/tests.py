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
            for _section, items in nav_for(user):
                for _label, url in items:
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
