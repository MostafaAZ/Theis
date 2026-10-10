"""B2B platform web tests."""
from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings

from commerce.models import Buyer, PlatformOrder
from core.models import Role, User
from core.testing import PASSWORD, World, pdf


@override_settings(ROOT_URLCONF="kubbar.urls_b2b", APP_NAME="b2b", LOGIN_URL="b2b:login")
class PlatformTests(TestCase):
    def setUp(self):
        self.w = World()
        self.listing = self.w.listing(qty=50)

    def login(self, email="buyer@test.example"):
        return self.client.post("/login/", {"email": email, "password": PASSWORD})

    def test_registration_flow(self):  # UC-29, UC-30
        data = {
            "company_name": "New Farm", "commercial_register_no": "CR-9",
            "tax_card_no": "T9", "buyer_type": self.w.farm.pk, "market": self.w.egypt.pk,
            "address": "Giza", "contact_person": "Mona", "phone": "0100",
            "owner_full_name": "Owner", "owner_national_id": "29001011234567",
            "email": "new@farm.example", "password": "Strongpass2026",
            "password2": "Strongpass2026", "licence_expiry":
                (self.w.today + timedelta(days=200)).isoformat(),
            "accept_terms": "on", "consent": "on",
            "doc_owner_id": pdf(), "doc_commercial_register": pdf(), "doc_tax_card": pdf(),
            "doc_licence": pdf()}
        r = self.client.post("/register/", data)
        self.assertContains(r, "Check your email")
        buyer = Buyer.objects.get(commercial_register_no="CR-9")
        self.assertEqual(buyer.status, Buyer.PENDING_EMAIL)
        self.assertTrue(any("Confirm your email" in m.subject for m in mail.outbox))
        self.client.get(f"/register/confirm/{buyer.email_token}/")
        buyer.refresh_from_db()
        self.assertEqual(buyer.status, Buyer.PENDING)
        self.assertTrue(self.w.user(Role.REGULATORY).notifications.exists())

    def test_prices_hidden_until_verified(self):  # FR-75, UC-32 A1
        price = f"{self.listing.clearance_price:,.2f}"
        self.assertNotContains(self.client.get("/catalogue/"), price)
        self.w.buyer(verified=False)
        self.login()
        self.assertNotContains(self.client.get("/catalogue/"), price)
        self.assertRedirects(self.client.get("/cart/"), "/account/")

    def test_verified_buyer_orders_and_pays(self):  # UC-33 to UC-36
        self.w.buyer()
        self.assertEqual(self.login().status_code, 302)
        self.assertContains(self.client.get("/catalogue/"),
                            f"{self.listing.clearance_price:,.2f}")
        self.client.post(f"/catalogue/{self.listing.pk}/", {"quantity": 999})
        cart = self.client.session["cart"]
        self.assertEqual(cart[str(self.listing.pk)], 50)  # FR-77: limited to available
        r = self.client.post("/cart/", {"checkout": "1", "delivery_address": "Farm road"})
        order = PlatformOrder.objects.get()
        self.assertRedirects(r, f"/orders/{order.pk}/pay/")
        r = self.client.post(f"/orders/{order.pk}/pay/", {"method": "bank", "amount": "3500",
                                                          "receipt": pdf("r.pdf")})
        order.refresh_from_db()
        self.assertEqual(order.status, PlatformOrder.AWAITING_PAYMENT)
        self.assertEqual(self.client.get(f"/orders/{order.pk}/").status_code, 200)

    def test_buyers_cannot_see_each_others_orders(self):
        a = self.w.buyer()
        from commerce import services as shop

        order, _ = shop.place_order(a, [(self.listing.pk, 1)], "x")
        self.w.buyer("other@test.example", "CR-2")
        self.login("other@test.example")
        self.assertEqual(self.client.get(f"/orders/{order.pk}/").status_code, 404)

    def test_internal_user_cannot_use_platform(self):  # NFR-02
        r = self.login("gm@test.example")
        self.assertEqual(r.status_code, 200)
        self.client.force_login(self.w.user(Role.GM))
        self.assertRedirects(self.client.get("/account/"), "/login/?next=/account/")

    def test_lock_and_self_service_unlock(self):  # FR-04, UC-32 A3
        self.w.buyer()
        for _ in range(5):
            self.client.post("/login/", {"email": "buyer@test.example", "password": "bad"})
        user = User.objects.get(email="buyer@test.example")
        self.assertTrue(user.is_locked)
        link = [m for m in mail.outbox if "locked" in m.subject][-1].body.split("/unlock/")[1]
        self.client.get("/unlock/" + link.split("/")[0] + "/")
        user.refresh_from_db()
        self.assertFalse(user.is_locked)

    def test_arabic_layout(self):  # NFR-04
        self.client.get("/lang/ar/")
        r = self.client.get("/")
        self.assertContains(r, 'dir="rtl"')
        self.assertContains(r, "سجّل شركتك")


@override_settings(ROOT_URLCONF="kubbar.urls_b2b", APP_NAME="b2b", LOGIN_URL="b2b:login")
class StorefrontTests(TestCase):
    def setUp(self):
        self.w = World()
        self.listing = self.w.listing(qty=50)

    def test_public_pages(self):
        for url in ("/", "/catalogue/", f"/catalogue/{self.listing.pk}/", "/how-it-works/",
                    "/faq/", "/terms/", "/privacy/", "/contact/",
                    f"/products/{self.w.product.pk}/image/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_catalogue_filters(self):
        self.w.buyer()
        self.client.post("/login/", {"email": "buyer@test.example", "password": PASSWORD})
        r = self.client.get(f"/catalogue/?category={self.w.vaccines.pk}")
        self.assertNotContains(r, self.w.product.name)
        r = self.client.get(f"/catalogue/?category={self.w.meds.pk}&q=Enro")
        self.assertContains(r, self.w.product.name)

    def test_account_area_and_contact(self):
        b = self.w.buyer()
        self.client.post("/login/", {"email": "buyer@test.example", "password": PASSWORD})
        for url in ("/account/", "/account/profile/", "/account/documents/",
                    "/account/invoices/", "/account/messages/", "/orders/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.post("/account/profile/", {"company": "1", "contact_person": "New Name",
                                                   "phone": "0122", "address": "New address"})
        b.refresh_from_db()
        self.assertEqual(b.contact_person, "New Name")
        r = self.client.post("/contact/", {"name": "N", "email": "buyer@test.example",
                                           "topic": "order", "message": "When?"})
        self.assertRedirects(r, "/account/messages/")
        self.assertEqual(b.messages.count(), 1)
        self.assertTrue(self.w.user(Role.FINANCE).notifications.filter(
            message__contains="New buyer message").exists())
