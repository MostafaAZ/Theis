"""Sales and inventory analytics (Module 6, FR-55 to FR-64).

Each dashboard returns a plain dict of KPI tiles, tables and chart series,
so the same data drives the screen and the PDF/Excel export (FR-65).
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, F, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from commerce.models import Buyer, PlatformOrder
from inventory.models import Batch, BatchStatus, Listing, StockCount, WriteOff
from planning.models import ForecastProduct, PurchasePlanLine, SalesRecord

DASHBOARDS = [
    ("inventory", "Inventory Health"),
    ("sales", "Sales Performance"),
    ("forecast", "Forecast Accuracy"),
    ("recovery", "Recovery and Write-Off"),
    ("audit", "Audit Accuracy"),
    ("platform", "Platform Activity"),
]

ACTIVE = [BatchStatus.IN_STOCK, BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS,
          BatchStatus.RELEASED, BatchStatus.EXPIRED]
AT_RISK = [BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS, BatchStatus.RELEASED]


def egp(v):
    return f"{Decimal(v or 0):,.2f}"


def pct(v):
    return "–" if v is None else f"{v:,.1f}%"


class Filters:
    def __init__(self, data=None, user=None):
        data = data or {}
        today = timezone.localdate()
        self.date_from = _parse(data.get("date_from")) or date(today.year - 1, today.month, 1)
        self.date_to = _parse(data.get("date_to")) or today
        self.category = data.get("category") or ""
        self.product = data.get("product") or ""
        self.region = data.get("region") or ""
        self.warehouse = data.get("warehouse") or ""
        self.user = user

    def as_text(self):
        parts = [f"{self.date_from:%d %b %Y} – {self.date_to:%d %b %Y}"]
        from core.models import ProductCategory, Region, Warehouse
        from inventory.models import Product

        for label, model, value in (("Category", ProductCategory, self.category),
                                    ("Product", Product, self.product),
                                    ("Region", Region, self.region),
                                    ("Warehouse", Warehouse, self.warehouse)):
            if value:
                obj = model.objects.filter(pk=value).first()
                parts.append(f"{label}: {obj}")
        return "; ".join(parts)

    def _sales_rep_scope(self):
        from core.models import Role

        u = self.user
        if u and u.has_role(Role.SALES) and not u.has_role(
                Role.GM, Role.MANAGEMENT, Role.COO, Role.ADMIN, Role.FINANCE, Role.PROCUREMENT):
            return (list(u.categories.values_list("pk", flat=True)),
                    list(u.regions.values_list("pk", flat=True)))
        return None, None

    def batches(self):
        qs = Batch.objects.filter(qty_on_hand__gt=0, status__in=ACTIVE).select_related(
            "product__category")
        if self.category:
            qs = qs.filter(product__category_id=self.category)
        if self.product:
            qs = qs.filter(product_id=self.product)
        if self.warehouse:
            qs = qs.filter(stock__location__warehouse_id=self.warehouse,
                           stock__quantity__gt=0).distinct()
        cats, _regions = self._sales_rep_scope()
        if cats:
            qs = qs.filter(product__category_id__in=cats)
        return qs

    def sales(self):
        qs = SalesRecord.objects.filter(period__gte=self.date_from.replace(day=1),
                                        period__lte=self.date_to)
        if self.category:
            qs = qs.filter(product__category_id=self.category)
        if self.product:
            qs = qs.filter(product_id=self.product)
        if self.region:
            qs = qs.filter(region_id=self.region)
        cats, regions = self._sales_rep_scope()
        if cats:
            qs = qs.filter(product__category_id__in=cats)
        if regions:
            qs = qs.filter(region_id__in=regions)
        return qs


def _parse(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def table(title, headers, rows, link=None):
    return {"title": title, "headers": headers, "rows": rows, "link": link}


# --------------------------------------------------------------------------
def inventory_health(f):
    batches = list(f.batches())
    today = timezone.localdate()
    var_by = {"category": defaultdict(Decimal), "product": defaultdict(Decimal),
              "warehouse": defaultdict(Decimal)}
    bands = [("More than 12 months", 365, None), ("6 to 12 months", 181, 365),
             ("30 days to 6 months", 30, 180), ("Under 30 days", None, 29)]
    band_q = defaultdict(int)
    band_v = defaultdict(Decimal)
    total_value = Decimal(0)
    for b in batches:
        value = b.product.unit_cost * b.qty_on_hand
        total_value += value
        days = (b.expiry_date - today).days
        for name, lo, hi in bands:
            if (lo is None or days >= lo) and (hi is None or days <= hi):
                band_q[name] += b.qty_on_hand
                band_v[name] += value
                break
        if b.status in AT_RISK:
            var_by["category"][b.product.category.name] += value
            var_by["product"][b.product.name] += value
            for s in b.locations():
                var_by["warehouse"][s.location.warehouse.name] += b.product.unit_cost * s.quantity
    var_total = sum(var_by["category"].values(), Decimal(0))

    # Days of cover and turnover per product (FR-58).
    end = SalesRecord.objects.order_by("-period").values_list("period", flat=True).first()
    cover_rows = []
    if end:
        start3 = date(end.year, end.month, 1) - timedelta(days=89)
        start12 = date(end.year - 1, end.month, 1)
        sold3 = dict(SalesRecord.objects.filter(period__gte=start3).values_list(
            "product_id").annotate(q=Sum("quantity")).values_list("product_id", "q"))
        sold12 = dict(SalesRecord.objects.filter(period__gt=start12).values_list(
            "product_id").annotate(q=Sum("quantity")).values_list("product_id", "q"))
        on_hand = defaultdict(int)
        names, costs = {}, {}
        for b in batches:
            on_hand[b.product_id] += b.qty_on_hand
            names[b.product_id] = b.product.name
            costs[b.product_id] = b.product.unit_cost
        for pid, qty in sorted(on_hand.items(), key=lambda kv: names[kv[0]]):
            daily = (sold3.get(pid) or 0) / 90
            cover = round(qty / daily) if daily else None
            turnover = (sold12.get(pid) or 0) / qty if qty else 0
            cover_rows.append([names[pid], qty, "No recent sales" if cover is None else cover,
                               f"{turnover:.1f}"])
    near = [b for b in batches if b.status in AT_RISK]
    return {
        "kpis": [("Value at risk (EGP)", egp(var_total)),
                 ("Stock value at cost (EGP)", egp(total_value)),
                 ("Batches near expiry", len(near)),
                 ("Batches expired, not written off",
                  sum(1 for b in batches if b.status == BatchStatus.EXPIRED))],
        "tables": [
            table("Value at risk by category", ["Category", "EGP"],
                  [[k, egp(v)] for k, v in sorted(var_by["category"].items())]),
            table("Value at risk by product", ["Product", "EGP"],
                  [[k, egp(v)] for k, v in sorted(var_by["product"].items(),
                                                  key=lambda kv: -kv[1])[:15]]),
            table("Value at risk by warehouse", ["Warehouse", "EGP"],
                  [[k, egp(v)] for k, v in sorted(var_by["warehouse"].items())]),
            table("Stock ageing by remaining shelf life", ["Band", "Quantity", "EGP at cost"],
                  [[n, band_q[n], egp(band_v[n])] for n, _lo, _hi in bands],
                  link="/stock/"),
            table("Days of cover and inventory turnover",
                  ["Product", "On hand", "Days of cover", "Turnover (12m)"], cover_rows),
        ],
        "charts": [{"id": "ageing", "type": "bar", "title": "Stock ageing (EGP at cost)",
                    "labels": [n for n, _a, _b in bands],
                    "data": [float(band_v[n]) for n, _a, _b in bands]}],
    }


def sales_performance(f):
    qs = f.sales()
    total_q = qs.aggregate(t=Sum("quantity"))["t"] or 0
    total_v = qs.aggregate(t=Sum("total"))["t"] or 0
    by_month = list(qs.values("period").annotate(q=Sum("quantity"), v=Sum("total"))
                    .order_by("period"))
    by_product = list(qs.values("product_id", "product__name").annotate(
        q=Sum("quantity"), v=Sum("total")).order_by("-v"))
    by_region = list(qs.values("region__name").annotate(q=Sum("quantity"), v=Sum("total"))
                     .order_by("-v"))
    by_customer = list(qs.values("customer_name").annotate(q=Sum("quantity"), v=Sum("total"))
                       .order_by("-v")[:15])
    on_hand = dict(Batch.objects.filter(qty_on_hand__gt=0, status__in=ACTIVE).values(
        "product_id").annotate(q=Sum("qty_on_hand")).values_list("product_id", "q"))
    product_rows = []
    for r in by_product[:20]:
        stock = on_hand.get(r["product_id"], 0)
        sell_through = 100 * r["q"] / (r["q"] + stock) if (r["q"] + stock) else None
        product_rows.append([r["product__name"], r["q"], egp(r["v"]), pct(sell_through)])
    share = defaultdict(dict)
    totals = {r["product_id"]: r["q"] for r in by_product}
    for r in qs.values("product__name", "product_id", "region__name").annotate(
            q=Sum("quantity")).order_by("product__name"):
        if totals.get(r["product_id"]):
            share[r["product__name"]][r["region__name"]] = 100 * r["q"] / totals[r["product_id"]]
    regions = sorted({r["region__name"] for r in by_region})
    share_rows = [[p] + [pct(v.get(reg)) if reg in v else "–" for reg in regions]
                  for p, v in list(share.items())[:20]]
    return {
        "kpis": [("Units sold", f"{total_q:,}"), ("Sales value (EGP)", egp(total_v)),
                 ("Products sold", len(by_product)), ("Customers", len(by_customer))],
        "tables": [
            table("Sales by product (with sell-through)",
                  ["Product", "Units", "EGP", "Sell-through"], product_rows),
            table("Sales by region", ["Region", "Units", "EGP"],
                  [[r["region__name"], r["q"], egp(r["v"])] for r in by_region]),
            table("Top customers", ["Customer", "Units", "EGP"],
                  [[r["customer_name"] or "–", r["q"], egp(r["v"])] for r in by_customer]),
            table("Regional share of each product's sales", ["Product"] + regions, share_rows),
            table("Sales by month", ["Month", "Units", "EGP"],
                  [[f"{r['period']:%b %Y}", r["q"], egp(r["v"])] for r in by_month]),
        ],
        "charts": [{"id": "monthly", "type": "line", "title": "Sales by month (EGP)",
                    "labels": [f"{r['period']:%b %y}" for r in by_month],
                    "data": [float(r["v"]) for r in by_month]}],
    }


def forecast_accuracy(f):
    """FR-59: forecast vs actual and MAPE by product and region."""
    fps = ForecastProduct.objects.select_related("product", "region", "forecast").order_by(
        "-forecast__created_at")
    if f.product:
        fps = fps.filter(product_id=f.product)
    if f.category:
        fps = fps.filter(product__category_id=f.category)
    if f.region:
        fps = fps.filter(region_id=f.region)
    seen, rows, comp_rows = set(), [], []
    mapes = []
    for fp in fps[:400]:
        key = (fp.product_id, fp.region_id)
        if key in seen:
            continue
        seen.add(key)
        actual_total = forecast_total = 0
        for line in fp.lines.all():
            actual = SalesRecord.objects.filter(product=fp.product, region=fp.region,
                                                period=line.period).aggregate(
                t=Sum("quantity"))["t"]
            if actual is not None:
                actual_total += actual
                forecast_total += float(line.quantity)
        if fp.mape is not None:
            mapes.append(float(fp.mape))
        rows.append([fp.product.name, fp.region.name, fp.get_method_display(),
                     pct(fp.mape) if fp.mape is not None else "–",
                     "Low" if fp.low_confidence else "OK"])
        if actual_total:
            comp_rows.append([fp.product.name, fp.region.name, round(forecast_total),
                              actual_total,
                              pct(100 * abs(actual_total - forecast_total) / actual_total)])
    moq = PurchasePlanLine.objects.filter(status=PurchasePlanLine.APPROVED).aggregate(
        q=Sum("moq_surplus_qty"), v=Sum("moq_surplus_value"))
    avg_mape = sum(mapes) / len(mapes) if mapes else None
    return {
        "kpis": [("Average MAPE", pct(avg_mape)),
                 ("Forecasts flagged low confidence", sum(1 for r in rows if r[4] == "Low")),
                 ("MOQ surplus bought (units)", f"{moq['q'] or 0:,}"),
                 ("MOQ surplus bought (EGP)", egp(moq["v"]))],
        "tables": [
            table("Forecast accuracy by product and region (back-tested MAPE)",
                  ["Product", "Region", "Method", "MAPE", "Confidence"], rows),
            table("Forecast versus actual (months with actuals)",
                  ["Product", "Region", "Forecast", "Actual", "Error"], comp_rows),
        ],
        "charts": [],
    }


def recovery(f):
    """FR-60, FR-61, FR-40."""
    listings = Listing.objects.select_related("batch__product", "release")
    if f.category:
        listings = listings.filter(batch__product__category_id=f.category)
    if f.product:
        listings = listings.filter(batch__product_id=f.product)
    released_q = released_orig = 0
    for listing in listings:
        released_q += listing.listed_qty
        released_orig += listing.original_price * listing.listed_qty
    orders = PlatformOrder.objects.filter(status__in=[
        PlatformOrder.CONFIRMED, PlatformOrder.DISPATCHED, PlatformOrder.DELIVERED],
        created_at__date__gte=f.date_from, created_at__date__lte=f.date_to)
    sold_q = sold_v = 0
    days_to_sale = []
    for o in orders.prefetch_related("lines__listing"):
        for line in o.lines.all():
            if f.product and str(line.listing.batch.product_id) != str(f.product):
                continue
            sold_q += line.quantity
            sold_v += line.unit_price * line.quantity
            days_to_sale.append((o.created_at - line.listing.published_at).days)
    recovery_rate = 100 * float(sold_v) / float(released_orig) if released_orig else None
    wos = WriteOff.objects.filter(status__in=[WriteOff.APPROVED, WriteOff.DISPOSED],
                                  posted_at__date__gte=f.date_from,
                                  posted_at__date__lte=f.date_to).select_related(
        "batch__product", "purchase_plan")
    if f.product:
        wos = wos.filter(batch__product_id=f.product)
    wo_value = sum((w.write_off_value for w in wos), Decimal(0))
    disposal = sum((w.disposal_cost or 0 for w in wos), Decimal(0))
    expired_month = defaultdict(int)
    for b in Batch.objects.filter(expiry_date__gte=f.date_from, expiry_date__lte=f.date_to,
                                  status__in=[BatchStatus.EXPIRED, BatchStatus.DEPLETED]):
        if b.write_offs.exists() or b.status == BatchStatus.EXPIRED:
            expired_month[b.expiry_date.strftime("%Y-%m")] += 1
    return {
        "kpis": [("Released (units)", f"{released_q:,}"),
                 ("Sold through platform (units)", f"{sold_q:,}"),
                 ("Recovered value (EGP)", egp(sold_v)),
                 ("Recovery rate vs original value", pct(recovery_rate)),
                 ("Avg days listing to sale",
                  f"{sum(days_to_sale) / len(days_to_sale):.1f}" if days_to_sale else "–"),
                 ("Write-off value (EGP)", egp(wo_value)),
                 ("Disposal cost (EGP)", egp(disposal))],
        "tables": [
            table("Listings", ["Product", "Lot", "Listed", "Sold", "Clearance price", "Status"],
                  [[x.batch.product.name, x.batch.lot_number, x.listed_qty, x.sold_qty,
                    egp(x.clearance_price), x.get_status_display()] for x in listings[:50]]),
            table("Write-offs (linked to the purchase plan that bought the batch)",
                  ["Write-off", "Product", "Lot", "Qty", "Value", "Disposal cost", "Plan"],
                  [[str(w), w.batch.product.name, w.batch.lot_number, w.quantity,
                    egp(w.write_off_value), egp(w.disposal_cost),
                    w.purchase_plan.number if w.purchase_plan else "–"] for w in wos]),
            table("Batches expired per month", ["Month", "Batches"],
                  sorted([[k, v] for k, v in expired_month.items()])),
        ],
        "charts": [],
    }


def audit_accuracy(f):
    """FR-62."""
    counts = StockCount.objects.filter(status=StockCount.CLOSED,
                                       closed_at__date__gte=f.date_from,
                                       closed_at__date__lte=f.date_to).select_related(
        "warehouse")
    if f.warehouse:
        counts = counts.filter(warehouse_id=f.warehouse)
    rates = [float(c.accuracy_rate) for c in counts if c.accuracy_rate is not None]
    return {
        "kpis": [("Counts closed", counts.count()),
                 ("Average audit accuracy", pct(sum(rates) / len(rates) if rates else None)),
                 ("Total adjustment value (EGP)",
                  egp(counts.aggregate(t=Sum("adjustment_value"))["t"]))],
        "tables": [table("Closed counts",
                         ["Count", "Type", "Warehouse", "Closed", "Lines",
                          "Accuracy (within tolerance)", "Adjustment value (EGP)"],
                         [[c.number, c.get_count_type_display(), c.warehouse.name,
                           f"{c.closed_at:%d %b %Y}", c.lines.count(), pct(c.accuracy_rate),
                           egp(c.adjustment_value)] for c in counts])],
        "charts": [{"id": "accuracy", "type": "bar", "title": "Audit accuracy by count (%)",
                    "labels": [c.number for c in counts],
                    "data": [float(c.accuracy_rate or 0) for c in counts]}],
    }


def platform_activity(f):
    """FR-63."""
    verified = Buyer.objects.filter(status=Buyer.VERIFIED).count()
    new_by_month = list(Buyer.objects.filter(
        registered_at__date__gte=f.date_from, registered_at__date__lte=f.date_to)
        .annotate(m=TruncMonth("registered_at")).values("m").annotate(n=Count("id"))
        .order_by("m"))
    orders = PlatformOrder.objects.filter(created_at__date__gte=f.date_from,
                                          created_at__date__lte=f.date_to)
    valid = orders.exclude(status=PlatformOrder.CANCELLED)
    total = valid.aggregate(t=Sum("total"))["t"] or 0
    n = valid.count()
    by_type = valid.values(t=F("buyer__buyer_type__name")).annotate(
        n=Count("id"), v=Sum("total")).order_by("-v")
    by_status = orders.values("status").annotate(n=Count("id")).order_by("status")
    labels = dict(PlatformOrder.STATUSES)
    return {
        "kpis": [("Verified buyers", verified),
                 ("Pending verification", Buyer.objects.filter(status=Buyer.PENDING).count()),
                 ("Orders (excl. cancelled)", n), ("Sales value (EGP)", egp(total)),
                 ("Average order value (EGP)", egp(total / n if n else 0))],
        "tables": [
            table("New buyers per month", ["Month", "Registrations"],
                  [[f"{r['m']:%b %Y}", r["n"]] for r in new_by_month]),
            table("Sales by buyer type", ["Buyer type", "Orders", "EGP"],
                  [[r["t"], r["n"], egp(r["v"])] for r in by_type]),
            table("Orders by status", ["Status", "Orders"],
                  [[labels.get(r["status"], r["status"]), r["n"]] for r in by_status]),
        ],
        "charts": [],
    }


BUILDERS = {"inventory": inventory_health, "sales": sales_performance,
            "forecast": forecast_accuracy, "recovery": recovery, "audit": audit_accuracy,
            "platform": platform_activity}


def build(key, filters):
    data = BUILDERS[key](filters)
    data["key"] = key
    data["title"] = dict(DASHBOARDS)[key]
    return data
