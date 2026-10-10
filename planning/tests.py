import math
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.models import Role
from core.testing import World
from planning import forecasting
from planning import services as svc
from planning.models import DataImport, PurchasePlan, PurchasePlanLine, SalesRecord


class ForecastingUnitTests(TestCase):
    def test_method_selection_by_history(self):  # FR-43, UC-24 A1
        self.assertEqual(forecasting.choose_method(24), "seasonal_es")
        self.assertEqual(forecasting.choose_method(23), "moving_avg")
        self.assertEqual(forecasting.choose_method(6), "moving_avg")
        self.assertEqual(forecasting.choose_method(5), "manual")

    def test_seasonal_smoothing_tracks_seasonality(self):
        series = [100 + 40 * math.sin(2 * math.pi * m / 12) for m in range(36)]
        seasonal = forecasting.holt_winters(series, 12)
        flat = forecasting.moving_average(series, 12)
        actual = [100 + 40 * math.sin(2 * math.pi * m / 12) for m in range(36, 48)]
        self.assertLess(forecasting.mape(actual, seasonal), forecasting.mape(actual, flat))
        self.assertLess(forecasting.mape(actual, seasonal), 5)

    def test_mape_ignores_zero_actuals(self):
        self.assertAlmostEqual(forecasting.mape([0, 100], [5, 90]), 10.0)


class ImportTests(TestCase):
    def setUp(self):
        self.w = World()
        self.user = self.w.user(Role.PROCUREMENT)

    def csv(self, text):
        return SimpleUploadedFile("sales.csv", text.encode())

    def test_region_standardisation_and_rejections(self):  # FR-42
        f = self.csv("Order Number,Area,Customer,Product,Quantity,Price,Total,Month,Year\n"
                     "S1,Cairo&Giza,Farm A,MED-1,10,100,1000,1,2026\n"
                     "S2,Cairo & Giza,Farm A,MED-1,5,100,500,2,2026\n"
                     "S3,Alexandria,Farm B,MED-1,,100,0,2,2026\n"
                     "S4,Alexandria,Farm B,UNKNOWN,3,100,300,2,2026\n")
        imp, unknown = svc.stage_import(self.user, DataImport.SALES, f)
        self.assertEqual((imp.rows_accepted, imp.rows_corrected, imp.rows_rejected), (2, 1, 2))
        self.assertEqual(unknown, [])
        self.assertFalse(SalesRecord.objects.exists())  # NFR-13: nothing before confirm
        svc.confirm_import(self.user, imp)
        self.assertEqual(SalesRecord.objects.filter(region=self.w.cairo).count(), 2)

    def test_unknown_region_must_be_mapped(self):  # UC-23 A2
        f = self.csv("Area,Product,Quantity,Month,Year\nIsmailia,MED-1,4,3,2026\n")
        imp, unknown = svc.stage_import(self.user, DataImport.SALES, f)
        self.assertEqual(unknown, ["Ismailia"])
        with self.assertRaises(ValidationError):
            svc.confirm_import(self.user, imp)
        self.assertFalse(SalesRecord.objects.exists())
        svc.confirm_import(self.user, imp, {"Ismailia": self.w.alex})
        self.assertIn("Ismailia", self.w.alex.__class__.objects.get(pk=self.w.alex.pk).aliases)

    def test_missing_required_column(self):  # UC-23 A1
        with self.assertRaises(ValidationError):
            svc.stage_import(self.user, DataImport.SALES, self.csv("Area,Quantity\nX,1\n"))


class PurchasePlanTests(TestCase):
    def setUp(self):
        self.w = World()
        self.user = self.w.user(Role.PROCUREMENT)
        start = date(2024, 1, 1)
        recs = []
        for m in range(30):
            period = svc.add_months(start, m)
            recs.append(SalesRecord(period=period, region=self.w.cairo, product=self.w.product,
                                    quantity=100, unit_price=100, total=10000))
        SalesRecord.objects.bulk_create(recs)

    def test_forecast_and_moq_surplus(self):  # FR-43 to FR-47
        self.w.product.moq = 1000
        self.w.product.save()
        fc = svc.generate_forecast(self.user, 6, [self.w.product])
        fp = fc.products.get()
        self.assertEqual(fp.method, "seasonal_es")
        self.assertAlmostEqual(float(sum(line.quantity for line in fp.lines.all())), 600,
                               delta=30)
        plan = svc.build_plan(self.user, fc, "2026-Q3")
        line = plan.lines.get()
        # Lead time 60 days ≈ 1.97 months + 3 months coverage ≈ 497 units, plus 15 days
        # of safety stock ≈ 49 units, with no stock on hand or on order.
        self.assertAlmostEqual(line.recommended_qty, 546, delta=30)
        self.assertEqual(line.rounded_qty, 1000)  # rounded up to the MOQ
        self.assertEqual(line.moq_surplus_qty, 1000 - line.recommended_qty)
        self.assertEqual(line.moq_surplus_value, Decimal(60 * line.moq_surplus_qty))

    def test_near_expiry_stock_is_not_usable(self):  # FR-46
        self.w.batch(days=200, qty=1000)  # only ~20 days before it hits near-expiry
        usable, excluded = svc.usable_stock(self.w.product, 100.0)
        self.assertLess(usable, 100)
        self.assertGreater(excluded, 900)

    def test_override_needs_reason_and_gm_decides(self):  # FR-48, FR-49
        fc = svc.generate_forecast(self.user, 6, [self.w.product])
        plan = svc.build_plan(self.user, fc, "Q")
        line = plan.lines.get()
        with self.assertRaises(ValidationError):
            svc.override_line(line, line.rounded_qty + 100, "")
        svc.override_line(line, line.rounded_qty + 100, "Supplier promotion")
        svc.submit_plan(self.user, plan)
        with self.assertRaises(ValidationError):
            svc.override_line(PurchasePlanLine.objects.get(pk=line.pk), 1, "x")  # locked
        with self.assertRaises(ValidationError):
            svc.decide_plan(self.w.user(Role.GM), plan, {line.pk: ("returned", "")})
        svc.decide_plan(self.w.user(Role.GM), plan, {line.pk: ("returned", "Too much")})
        plan.refresh_from_db()
        self.assertEqual(plan.status, PurchasePlan.RETURNED)
        svc.submit_plan(self.user, plan)
        svc.decide_plan(self.w.user(Role.GM), plan, {line.pk: ("approved", "")})
        plan.refresh_from_db()
        self.assertEqual(plan.status, PurchasePlan.APPROVED)
