from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from commerce import services as shop
from commerce.models import Buyer, BuyerDocument, Invoice, PlatformOrder
from core.models import Notification, Role
from core.testing import PDF, World, pdf
from inventory import services as inv
from inventory.models import Batch, Customer, Listing


class RegistrationTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_documents_and_national_id_are_encrypted(self):  # FR-68, NFR-25
        b = self.w.buyer(verified=False)
        doc = b.documents.first()
        with doc.file.open("rb") as fh:
            stored = fh.read()
        self.assertNotIn(b"%PDF", stored)
        self.assertEqual(shop.read_encrypted(doc.file), PDF)
        self.assertNotIn("29001011234567", b.owner_national_id_enc)
        self.assertEqual(b.owner_national_id, "29001011234567")

    def test_consent_is_mandatory(self):  # FR-69, UC-29 A3
        with self.assertRaises(ValidationError):
            shop.register_buyer(
                email="x@y.z", password="Testing2026pass", company_name="X",
                buyer_type=self.w.farm, market=self.w.egypt, commercial_register_no="CR-X",
                tax_card_no="T", address="A", contact_person="C", phone="1",
                owner_full_name="O", owner_national_id="1",
                documents={t: pdf() for t in BuyerDocument.MANDATORY},
                licence_expiry=self.w.today + timedelta(days=10), consent=False)

    def test_wrong_file_type_rejected(self):  # UC-30 A1
        from django.core.files.uploadedfile import SimpleUploadedFile

        with self.assertRaises(ValidationError):
            shop.validate_upload(SimpleUploadedFile("x.exe", b"MZ..."))
        with self.assertRaises(ValidationError):
            shop.validate_upload(SimpleUploadedFile("fake.pdf", b"not a pdf"))

    def test_existing_customer_routed_to_sales_rep(self):  # FR-70
        rep = self.w.user(Role.SALES)
        Customer.objects.create(name="Old Farm", commercial_register_no="CR-77", sales_rep=rep)
        b = self.w.buyer(cr="CR-77", verified=False)
        self.assertEqual(b.matched_customer.name, "Old Farm")
        self.assertTrue(Notification.objects.filter(user=rep, link=f"/buyers/{b.pk}/").exists())

    def test_verify_requires_all_documents_approved(self):  # FR-71
        b = self.w.buyer(verified=False)
        with self.assertRaises(ValidationError):
            shop.verify_buyer(self.w.user(Role.REGULATORY), b, [self.w.meds], False)

    def test_cold_chain_needs_cold_storage_proof(self):  # FR-72
        b = self.w.buyer(verified=False)
        ra = self.w.user(Role.REGULATORY)
        for d in b.documents.all():
            shop.review_document(ra, d, BuyerDocument.APPROVED)
        with self.assertRaises(ValidationError):
            shop.verify_buyer(ra, b, [self.w.meds], True)

    def test_licence_expiry_suspends_ordering(self):  # FR-74
        b = self.w.buyer()
        Buyer.objects.filter(pk=b.pk).update(licence_expiry=self.w.today - timedelta(days=1))
        shop.licence_checks()
        b.refresh_from_db()
        self.assertTrue(b.ordering_suspended)
        self.assertFalse(b.can_order)


class OrderingTests(TestCase):
    def setUp(self):
        self.w = World()
        self.listing = self.w.listing(qty=50)
        self.a = self.w.buyer("a@test.example", "CR-A")
        self.b = self.w.buyer("b@test.example", "CR-B")

    def test_catalogue_respects_categories(self):  # FR-75
        self.assertIn(self.listing, shop.catalogue_for(self.a))
        self.a.categories.set([self.w.vaccines])
        self.assertNotIn(self.listing, shop.catalogue_for(self.a))

    def test_last_units_are_never_sold_twice(self):  # NFR-09, NFR-10
        o1, notes1 = shop.place_order(self.a, [(self.listing.pk, 40)], "addr")
        o2, notes2 = shop.place_order(self.b, [(self.listing.pk, 40)], "addr")
        self.listing.refresh_from_db()
        self.assertEqual(o1.lines.get().quantity, 40)
        self.assertEqual(o2.lines.get().quantity, 10)  # UC-35 A1: what remains
        self.assertTrue(notes2)
        self.assertEqual(self.listing.available_qty, 0)
        self.assertEqual(self.listing.status, Listing.SOLD_OUT)
        self.assertEqual(Batch.objects.get(pk=self.listing.batch_id).qty_reserved, 50)
        o3, _ = shop.place_order(self.a, [(self.listing.pk, 1)], "addr")
        self.assertIsNone(o3)

    def test_cancel_releases_reservation(self):  # UC-37 A1
        order, _ = shop.place_order(self.a, [(self.listing.pk, 20)], "addr")
        shop.cancel_order(order, "changed mind")
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.available_qty, 50)
        self.assertEqual(Batch.objects.get(pk=self.listing.batch_id).qty_reserved, 0)

    def test_reservation_hold_expires(self):  # UC-35 A2
        order, _ = shop.place_order(self.a, [(self.listing.pk, 20)], "addr")
        PlatformOrder.objects.filter(pk=order.pk).update(
            reservation_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(shop.release_expired_reservations(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, PlatformOrder.CANCELLED)

    def test_cold_chain_requires_eligibility_and_confirmation(self):  # FR-79
        vb = self.w.batch(product=self.w.vaccine, days=150, qty=40)
        vlisting = self.w.listing(batch=vb, qty=40)
        order, notes = shop.place_order(self.a, [(vlisting.pk, 5)], "addr", True)
        self.assertIsNone(order)  # buyer A is not cold-chain eligible
        cold = self.w.buyer("c@test.example", "CR-C", cold=True)
        order, notes = shop.place_order(cold, [(vlisting.pk, 5)], "addr", False)
        self.assertIsNone(order)
        order, _ = shop.place_order(cold, [(vlisting.pk, 5)], "addr", True)
        self.assertIsNotNone(order)

    def test_bank_transfer_confirm_and_dispatch(self):  # UC-36, UC-38, UC-08
        order, _ = shop.place_order(self.a, [(self.listing.pk, 10)], "addr")
        shop.submit_bank_transfer(order, pdf("receipt.pdf"), order.total)
        order.refresh_from_db()
        self.assertEqual(order.status, PlatformOrder.AWAITING_PAYMENT)
        invoice = shop.confirm_payment(self.w.user(Role.FINANCE), order)
        self.assertEqual(invoice.status, Invoice.PAID)
        batch = Batch.objects.get(pk=self.listing.batch_id)
        with self.assertRaises(ValidationError):  # wrong lot picked (UC-08 A1)
            inv.dispatch_platform_order(self.w.user(Role.WAREHOUSE), order,
                                        {order.lines.get().pk: "WRONG"}, "Fleet", self.w.today)
        inv.dispatch_platform_order(self.w.user(Role.WAREHOUSE), order,
                                    {order.lines.get().pk: batch.lot_number}, "Fleet",
                                    self.w.today)
        batch.refresh_from_db()
        self.assertEqual((batch.qty_on_hand, batch.qty_reserved), (90, 0))
        order.refresh_from_db()
        self.assertEqual(order.status, PlatformOrder.DISPATCHED)
        shop.confirm_delivery(order)
        order.refresh_from_db()
        self.assertEqual(order.status, PlatformOrder.DELIVERED)

    def test_invoice_only_within_credit_and_without_overdue(self):  # FR-81, FR-84
        order, _ = shop.place_order(self.a, [(self.listing.pk, 10)], "addr")
        self.assertNotIn(PlatformOrder.INVOICE, shop.payment_options(self.a, order)[0])
        shop.set_payment_terms(self.w.user(Role.FINANCE), self.a, "30", Decimal("1000"))
        self.assertIn(PlatformOrder.INVOICE, shop.payment_options(self.a, order)[0])
        shop.choose_invoice(order)
        inv_ = shop.confirm_payment(self.w.user(Role.FINANCE), order)
        self.assertEqual(inv_.due_date, self.w.today + timedelta(days=30))
        Invoice.objects.filter(pk=inv_.pk).update(due_date=self.w.today - timedelta(days=1))
        order2, _ = shop.place_order(self.a, [(self.listing.pk, 1)], "addr")
        options, reason = shop.payment_options(self.a, order2)
        self.assertNotIn(PlatformOrder.INVOICE, options)
        self.assertIn("overdue", reason)

    def test_withdrawal_keeps_confirmed_orders(self):  # FR-80
        order, _ = shop.place_order(self.a, [(self.listing.pk, 10)], "addr")
        inv.withdraw_listing_by_user(self.w.user(Role.COO), self.listing)
        order.refresh_from_db()
        self.assertEqual(order.status, PlatformOrder.PLACED)
        self.assertEqual(order.lines.get().quantity, 10)
