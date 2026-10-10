from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from core.models import AuditLog, Notification, ReasonCode, Role
from core.testing import World
from inventory import services as inv
from inventory.models import (Batch, BatchStatus, ExpiryAlert, Listing, StockCount,
                              StockCountLine, StockMovement, WriteOff)


class InspectionAndReceiptTests(TestCase):
    def setUp(self):
        self.w = World()

    def inspect(self, **kw):
        data = dict(purchase_order=None, product=self.w.product, lot_number="LOT1",
                    manufacture_date=self.w.today - timedelta(days=30),
                    expiry_date=self.w.today + timedelta(days=700), storage_condition="ambient",
                    quantity_received=100, quantity_approved=90, quantity_rejected=10,
                    rejection_reason="Damaged")
        data.update(kw)
        return inv.record_inspection(self.w.user(Role.QA), **data)

    def test_expiry_before_manufacture_is_rejected(self):  # FR-08
        with self.assertRaises(ValidationError):
            self.inspect(expiry_date=self.w.today - timedelta(days=60))

    def test_quantities_must_add_up(self):  # UC-04 A3
        with self.assertRaises(ValidationError):
            self.inspect(quantity_rejected=5)

    def test_low_shelf_life_marks_early_monitoring(self):  # FR-09
        b = self.inspect(expiry_date=self.w.today + timedelta(days=200))
        self.assertTrue(b.early_monitoring)

    def test_stock_only_updates_after_booking(self):  # FR-10
        b = self.inspect()
        self.assertEqual(b.qty_on_hand, 0)
        inv.receive_batch(self.w.user(Role.INVENTORY), b, self.w.ambient)
        b.refresh_from_db()
        self.assertEqual(b.qty_on_hand, 90)
        self.assertEqual(b.status, BatchStatus.IN_STOCK)
        self.assertTrue(b.movements.filter(movement_type=StockMovement.RECEIPT).exists())

    def test_cold_chain_cannot_go_to_ambient(self):  # FR-11
        b = self.inspect(product=self.w.vaccine, storage_condition="refrigerated")
        with self.assertRaises(ValidationError):
            inv.receive_batch(self.w.user(Role.INVENTORY), b, self.w.ambient)

    def test_transfer_blocked_while_frozen(self):  # UC-07 A2
        b = self.w.batch()
        b.frozen_for_count = True
        b.save()
        with self.assertRaises(ValidationError):
            inv.transfer_stock(self.w.user(Role.WAREHOUSE), b, self.w.ambient, self.w.ambient2, 5)

    def test_transfer_moves_quantity(self):  # FR-13
        b = self.w.batch(qty=100)
        inv.transfer_stock(self.w.user(Role.WAREHOUSE), b, self.w.ambient, self.w.ambient2, 40)
        self.assertEqual(b.stock.get(location=self.w.ambient2).quantity, 40)
        b.refresh_from_db()
        self.assertEqual(b.qty_on_hand, 100)


class FefoTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_earliest_eligible_batch_first(self):  # FR-14, UC-09
        late = self.w.batch(days=600, qty=100, lot="LATE")
        early = self.w.batch(days=200, qty=30, lot="EARLY")
        self.w.batch(days=20, qty=500, lot="TOO-SHORT")  # below minimum sellable shelf life
        frozen = self.w.batch(days=100, qty=500, lot="FROZEN")
        frozen.frozen_for_count = True
        frozen.save()
        allocation, shortfall = inv.fefo_allocate(self.w.product, 50)
        self.assertEqual([(b.lot_number, q) for b, q in allocation], [("EARLY", 30), ("LATE", 20)])
        self.assertEqual(shortfall, 0)
        self.assertEqual(late.lot_number, "LATE")
        self.assertEqual(early.lot_number, "EARLY")

    def test_shortfall_is_flagged(self):  # UC-09 A1
        self.w.batch(days=300, qty=10)
        _alloc, shortfall = inv.fefo_allocate(self.w.product, 25)
        self.assertEqual(shortfall, 15)

    def test_field_dispatch_deducts_and_never_goes_negative(self):  # FR-15, NFR-09
        b = self.w.batch(days=300, qty=10)
        fd, short = inv.dispatch_field_order(self.w.user(Role.WAREHOUSE), self.w.product, 25)
        b.refresh_from_db()
        self.assertEqual((fd.quantity_dispatched, short, b.qty_on_hand), (10, 15, 0))
        self.assertEqual(b.status, BatchStatus.DEPLETED)


class ExpiryScanTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_near_expiry_alert_and_notification(self):  # FR-24, FR-25
        b = self.w.batch(days=150)
        run = inv.run_expiry_scan()
        b.refresh_from_db()
        self.assertEqual(b.status, BatchStatus.NEAR_EXPIRY)
        self.assertEqual(run.new_near_expiry, 1)
        self.assertTrue(Notification.objects.filter(user=self.w.user(Role.SALES)).exists())
        self.assertTrue(ExpiryAlert.objects.filter(batch=b,
                                                   alert_type=ExpiryAlert.NEAR_EXPIRY).exists())

    def test_expired_batch_is_blocked_and_listing_withdrawn(self):  # FR-37
        listing = self.w.listing()
        Batch.objects.filter(pk=listing.batch_id).update(
            expiry_date=self.w.today - timedelta(days=1))
        inv.run_expiry_scan()
        listing.refresh_from_db()
        self.assertEqual(listing.status, Listing.WITHDRAWN)
        self.assertEqual(listing.batch.status, BatchStatus.EXPIRED)
        self.assertEqual(inv.fefo_allocate(self.w.product, 1)[0], [])

    def test_listing_withdrawn_at_minimum_sellable_shelf_life(self):  # FR-34
        listing = self.w.listing()
        Batch.objects.filter(pk=listing.batch_id).update(
            expiry_date=self.w.today + timedelta(days=30))
        inv.run_expiry_scan()
        listing.refresh_from_db()
        self.assertEqual(listing.status, Listing.WITHDRAWN)

    def test_escalation_after_inaction(self):  # FR-26
        b = self.w.batch(days=150)
        inv.run_expiry_scan()
        Batch.objects.filter(pk=b.pk).update(last_action_at=self.w.today - timedelta(days=14))
        run = inv.run_expiry_scan()
        self.assertEqual(run.escalations, 1)
        self.assertTrue(Notification.objects.filter(user=self.w.user(Role.GM),
                                                    message__startswith="Escalation").exists())

    def test_registration_lapse_withdraws_listing(self):  # FR-34
        listing = self.w.listing()
        self.w.product.registrations.update(status="suspended")
        inv.run_expiry_scan()
        listing.refresh_from_db()
        self.assertEqual(listing.status, Listing.WITHDRAWN)


class ReleaseTests(TestCase):
    def setUp(self):
        self.w = World()
        self.batch = self.w.batch(days=150, qty=200)
        inv.evaluate_batch(self.batch)
        self.batch.refresh_from_db()

    def test_capital_equipment_cannot_be_flagged(self):  # UC-16 A2
        from inventory.models import Product

        eq = Product.objects.create(sku="EQ", name="Drinker", category=self.w.equipment,
                                    original_price=10, unit_cost=5)
        b = self.w.batch(product=eq, days=100, status=BatchStatus.NEAR_EXPIRY)
        with self.assertRaises(ValidationError):
            inv.flag_surplus(self.w.user(Role.SALES), b, 5, "no_demand")

    def test_surplus_cannot_exceed_free_stock(self):  # UC-16 A1
        with self.assertRaises(ValidationError):
            inv.flag_surplus(self.w.user(Role.SALES), self.batch, 201, "no_demand")

    def test_price_band_enforced(self):  # FR-31
        rel = inv.flag_surplus(self.w.user(Role.SALES), self.batch, 100, "no_demand")
        self.assertEqual(inv.proposed_clearance_price(Decimal("100")), Decimal("70.00"))
        for bad in (Decimal("64.99"), Decimal("70.01")):
            with self.assertRaises(ValidationError):
                inv.approve_release(self.w.user(Role.COO), rel, 100, bad)
        listing = inv.approve_release(self.w.user(Role.COO), rel, 100, Decimal("65"))
        self.assertEqual(listing.clearance_price, Decimal("65"))
        self.assertEqual(listing.batch.status, BatchStatus.RELEASED)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.PRICE_CHANGE).exists())

    def test_missing_registration_blocks_listing(self):  # FR-32, UC-18 A1
        self.w.product.registrations.all().delete()
        rel = inv.flag_surplus(self.w.user(Role.SALES), self.batch, 100, "no_demand")
        with self.assertRaises(ValidationError):
            inv.approve_release(self.w.user(Role.COO), rel, 100, Decimal("70"))
        self.assertFalse(Listing.objects.exists())

    def test_reject_returns_batch_to_near_expiry(self):  # UC-17 A2
        rel = inv.flag_surplus(self.w.user(Role.SALES), self.batch, 100, "no_demand")
        inv.reject_release(self.w.user(Role.COO), rel, "Keep for key account")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, BatchStatus.NEAR_EXPIRY)

    def test_reprice_within_band_only(self):  # FR-80
        listing = self.w.listing(batch=self.batch)
        with self.assertRaises(ValidationError):
            inv.reprice_listing(self.w.user(Role.COO), listing, Decimal("50"))
        inv.reprice_listing(self.w.user(Role.COO), listing, Decimal("66"))
        listing.refresh_from_db()
        self.assertEqual(listing.clearance_price, Decimal("66"))

    def test_projected_unsold_without_sales(self):  # FR-28
        self.assertEqual(inv.projected_unsold(self.batch), 200)


class AuditCountTests(TestCase):
    def setUp(self):
        self.w = World()
        self.ic = self.w.user(Role.INVENTORY)
        self.wo = self.w.user(Role.WAREHOUSE)
        self.small = self.w.batch(qty=100, lot="SMALL")
        self.big = self.w.batch(qty=1000, lot="BIG")

    def schedule(self):
        return inv.schedule_count(self.ic, count_type="cycle", warehouse=self.w.wh,
                                  scope="all", scheduled_date=self.w.today, assigned_to=self.wo)

    def test_count_freezes_and_blocks_duplicates(self):  # FR-19, UC-10 A2
        self.schedule()
        self.assertTrue(Batch.objects.get(pk=self.small.pk).frozen_for_count)
        with self.assertRaises(ValidationError):
            self.schedule()

    def test_tolerance_threshold_and_gm_approval(self):  # FR-21 to FR-23, UC-13
        count = self.schedule()
        lines = {line.batch.lot_number: line for line in count.lines.all()}
        inv.submit_count(self.wo, count, {
            lines["SMALL"].pk: {"counted": 99},      # 1% – within tolerance, auto-posted
            lines["BIG"].pk: {"counted": 700},       # 300 × 60 = 18,000 EGP – needs GM
        })
        small = StockCountLine.objects.get(pk=lines["SMALL"].pk)
        self.assertEqual(small.status, StockCountLine.WITHIN)
        self.assertEqual(Batch.objects.get(pk=self.small.pk).qty_on_hand, 99)
        big = StockCountLine.objects.get(pk=lines["BIG"].pk)
        self.assertEqual(big.status, StockCountLine.FLAGGED)
        reason = ReasonCode.objects.get(code="loss")
        inv.reconcile_count(self.ic, count, {big.pk: {"action": "post", "reason": reason}})
        big.refresh_from_db()
        self.assertEqual(big.status, StockCountLine.AWAITING)
        self.assertEqual(Batch.objects.get(pk=self.big.pk).qty_on_hand, 1000)  # not yet
        inv.decide_adjustment(self.w.user(Role.GM), big, True, "Theft report filed")
        count.refresh_from_db()
        self.assertEqual(count.status, StockCount.CLOSED)
        self.assertEqual(Batch.objects.get(pk=self.big.pk).qty_on_hand, 700)
        self.assertFalse(Batch.objects.get(pk=self.big.pk).frozen_for_count)
        self.assertEqual(count.accuracy_rate, Decimal("50.00"))

    def test_rejected_adjustment_triggers_recount(self):  # UC-13 A1
        count = self.schedule()
        lines = {line.batch.lot_number: line for line in count.lines.all()}
        inv.submit_count(self.wo, count, {lines["SMALL"].pk: {"counted": 100},
                                          lines["BIG"].pk: {"counted": 700}})
        big = StockCountLine.objects.get(pk=lines["BIG"].pk)
        inv.reconcile_count(self.ic, count, {big.pk: {
            "action": "post", "reason": ReasonCode.objects.first()}})
        big.refresh_from_db()
        with self.assertRaises(ValidationError):
            inv.decide_adjustment(self.w.user(Role.GM), big, False, "")  # comment required
        inv.decide_adjustment(self.w.user(Role.GM), big, False, "Recount please")
        big.refresh_from_db()
        count.refresh_from_db()
        self.assertEqual((big.status, count.status), (StockCountLine.RECOUNT,
                                                      StockCount.IN_PROGRESS))

    def test_blank_line_blocks_submission(self):  # UC-11 A1
        count = self.schedule()
        with self.assertRaises(ValidationError):
            inv.submit_count(self.wo, count, {})


class WriteOffTests(TestCase):
    def setUp(self):
        self.w = World()
        self.batch = self.w.batch(days=-5, qty=50, status=BatchStatus.EXPIRED)

    def test_dual_approval_required(self):  # FR-38, FR-39
        wo = inv.request_write_off(self.w.user(Role.INVENTORY), self.batch, 50, "expired",
                                   "destruction")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.blocked_for_write_off, 50)
        inv.approve_write_off(self.w.user(Role.GM), wo)
        wo.refresh_from_db()
        self.assertEqual(wo.status, WriteOff.PENDING)
        self.assertEqual(Batch.objects.get(pk=self.batch.pk).qty_on_hand, 50)
        with self.assertRaises(ValidationError):
            inv.approve_write_off(self.w.user(Role.GM), wo)  # no second GM approval
        inv.approve_write_off(self.w.user(Role.QA), wo)
        wo.refresh_from_db()
        self.assertEqual(wo.status, WriteOff.APPROVED)
        self.assertEqual(wo.write_off_value, Decimal("3000.00"))
        self.assertEqual(Batch.objects.get(pk=self.batch.pk).qty_on_hand, 0)
        inv.record_disposal(self.w.user(Role.QA), wo, self.w.today, Decimal("450"), "CERT-1")
        wo.refresh_from_db()
        self.assertEqual(wo.status, WriteOff.DISPOSED)

    def test_write_off_linked_to_purchase_plan(self):  # FR-40
        from inventory.models import PurchaseOrder
        from planning.models import Forecast, PurchasePlan

        fc = Forecast.objects.create(created_by=self.w.user(Role.PROCUREMENT),
                                     start_period=self.w.today.replace(day=1))
        plan = PurchasePlan.objects.create(number="PP-1", forecast=fc, period_label="Q1",
                                           prepared_by=self.w.user(Role.PROCUREMENT))
        po = PurchaseOrder.objects.create(number="PO-1", supplier=self.w.supplier, plan=plan)
        Batch.objects.filter(pk=self.batch.pk).update(purchase_order=po)
        wo = inv.request_write_off(self.w.user(Role.INVENTORY), self.batch, 10, "expired",
                                   "destruction")
        inv.approve_write_off(self.w.user(Role.GM), wo)
        inv.approve_write_off(self.w.user(Role.QA), wo)
        wo.refresh_from_db()
        self.assertEqual(wo.purchase_plan, plan)


class AuditLogTests(TestCase):
    def test_audit_log_is_append_only(self):  # NFR-27
        entry = AuditLog.objects.create(action=AuditLog.LOGIN, description="x")
        entry.description = "changed"
        with self.assertRaises(PermissionDenied):
            entry.save()
        with self.assertRaises(PermissionDenied):
            entry.delete()


class BusinessRuleTests(TestCase):
    def test_rules_drive_behaviour(self):  # FR-52, NFR-20
        from core.models import BusinessRule

        w = World()
        BusinessRule.objects.filter(key="near_expiry_days").update(value=90)
        b = w.batch(days=150)
        inv.run_expiry_scan()
        b.refresh_from_db()
        self.assertEqual(b.status, BatchStatus.IN_STOCK)
