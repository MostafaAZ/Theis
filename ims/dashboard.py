"""Role dashboards (UC-03): what each user should do next, and the numbers
that matter to them."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, F, Sum
from django.utils import timezone

from commerce.models import Buyer, Invoice, OrderLine, PlatformOrder
from core.models import Role, User
from core.rules import rule_int
from inventory.models import (Batch, BatchStatus, ExpiryAlert, Listing, ProductRegistration,
                              PurchaseOrder, StockCount, StockCountLine, SurplusRelease,
                              WriteOff)
from planning.models import PurchasePlan, SalesRecord

R = Role


def _task(count, title, sub, url, level="info", icon="bi-arrow-right-circle"):
    return {"count": count, "title": title, "sub": sub, "url": url, "level": level,
            "icon": icon}


def tasks_for(user):
    """Open work for the user's roles, most urgent first. Zero-count tasks are
    dropped."""
    today = timezone.localdate()
    t = []
    if user.has_role(R.SALES):
        cats = list(user.categories.values_list("pk", flat=True))
        ne = Batch.objects.filter(status=BatchStatus.NEAR_EXPIRY, qty_on_hand__gt=0)
        if cats:
            ne = ne.filter(product__category_id__in=cats)
        t.append(_task(ne.filter(escalated=True).count(), "Escalated near-expiry batches",
                       "No action for 14 days – the GM has been alerted", "/expiry/", "bad"))
        t.append(_task(ne.count(), "Review near-expiry batches",
                       "Offer to customers first, then flag surplus or post to B2B",
                       "/expiry/", "warn"))
        t.append(_task(SurplusRelease.objects.filter(flagged_by=user,
                                                     status=SurplusRelease.PENDING).count(),
                       "Your surplus flags waiting for the COO", "No action needed from you",
                       "/releases/" if user.has_role(R.COO) else "/expiry/"))
    if user.has_role(R.COO):
        t.append(_task(SurplusRelease.objects.filter(status=SurplusRelease.PENDING).count(),
                       "Release requests to decide", "Approve and set the clearance price",
                       "/releases/", "warn"))
        soon = Listing.objects.filter(status=Listing.ACTIVE,
                                      batch__expiry_date__lte=today + timedelta(days=60))
        t.append(_task(soon.count(), "Listings close to the 30-day cut-off",
                       "Consider repricing within the band", "/listings/"))
    if user.has_role(R.GM):
        t.append(_task(StockCountLine.objects.filter(status=StockCountLine.AWAITING).count(),
                       "Stock adjustments to approve", "Above the approval threshold",
                       "/approvals/", "warn"))
        t.append(_task(PurchasePlan.objects.filter(status=PurchasePlan.SUBMITTED).count(),
                       "Purchase plans to approve", "Check the MOQ surplus before approving",
                       "/plans/", "warn"))
        t.append(_task(WriteOff.objects.filter(status=WriteOff.PENDING,
                                               gm_approved_by__isnull=True).count(),
                       "Write-offs to approve", "Second approval comes from QA",
                       "/approvals/", "warn"))
        t.append(_task(ExpiryAlert.objects.filter(alert_type=ExpiryAlert.ESCALATION,
                                                  status=ExpiryAlert.OPEN).count(),
                       "Escalated near-expiry batches", "Sales has not acted for 14 days",
                       "/expiry/", "bad"))
    if user.has_role(R.INVENTORY):
        t.append(_task(Batch.objects.filter(status=BatchStatus.INSPECTED).count(),
                       "Batches to book into stock", "Approved by QA, waiting for a location",
                       "/stock/receipt/", "warn"))
        t.append(_task(StockCount.objects.filter(status=StockCount.SUBMITTED).count(),
                       "Counts to reconcile", "Explain flagged variances", "/audits/", "warn"))
        t.append(_task(Batch.objects.filter(status=BatchStatus.EXPIRED, qty_on_hand__gt=0)
                       .exclude(blocked_for_write_off__gte=F("qty_on_hand")).count(),
                       "Expired batches to write off", "Blocked from sale until disposed",
                       "/write-offs/", "bad"))
        t.append(_task(WriteOff.objects.filter(status=WriteOff.REJECTED).count(),
                       "Rejected write-offs", "Resubmit or cancel", "/write-offs/"))
    if user.has_role(R.WAREHOUSE):
        t.append(_task(PlatformOrder.objects.filter(status=PlatformOrder.CONFIRMED,
                                                    on_hold=False).count(),
                       "Orders to pick and dispatch", "Paid B2B orders", "/dispatch/", "warn"))
        t.append(_task(StockCount.objects.filter(assigned_to=user, status=StockCount.IN_PROGRESS)
                       .count(), "Counts to do", "Blind count sheets assigned to you",
                       "/audits/mine/", "warn"))
        t.append(_task(PlatformOrder.objects.exclude(delivery_issue="").filter(
            status__in=[PlatformOrder.DISPATCHED, PlatformOrder.DELIVERED]).count(),
            "Delivery problems reported", "Buyers reported an issue", "/dispatch/"))
    if user.has_role(R.QA):
        open_po = PurchaseOrder.objects.filter(status=PurchaseOrder.OPEN,
                                               expected_date__lte=today + timedelta(days=7))
        t.append(_task(open_po.count(), "Deliveries due for inspection",
                       "Open purchase orders expected this week", "/inspections/", "warn"))
        t.append(_task(WriteOff.objects.filter(status=WriteOff.PENDING,
                                               qa_approved_by__isnull=True).count(),
                       "Write-offs to approve", "Confirm the disposal method", "/approvals/",
                       "warn"))
        t.append(_task(WriteOff.objects.filter(status=WriteOff.APPROVED).count(),
                       "Disposals to record", "Enter date, cost and certificate",
                       "/write-offs/"))
    if user.has_role(R.PROCUREMENT):
        t.append(_task(PurchasePlan.objects.filter(status=PurchasePlan.RETURNED).count(),
                       "Plans returned by the GM", "Revise the returned lines", "/plans/",
                       "warn"))
        t.append(_task(PurchasePlan.objects.filter(status=PurchasePlan.DRAFT).count(),
                       "Draft plans", "Review and submit for approval", "/plans/"))
        t.append(_task(PurchasePlan.objects.filter(status=PurchasePlan.APPROVED,
                                                   purchase_orders__isnull=True).count(),
                       "Approved plans without purchase orders", "Create the POs",
                       "/purchase-orders/", "warn"))
        t.append(_task(PurchaseOrder.objects.filter(status=PurchaseOrder.OPEN,
                                                    expected_date__lt=today).count(),
                       "Overdue deliveries", "Expected date has passed", "/purchase-orders/",
                       "bad"))
    if user.has_role(R.REGULATORY):
        t.append(_task(Buyer.objects.filter(status=Buyer.PENDING).count(),
                       "Buyers waiting for verification", "Review documents and decide",
                       "/buyers/?status=pending", "warn"))
        days = rule_int("registration_alert_days")
        t.append(_task(ProductRegistration.objects.filter(
            status=ProductRegistration.ACTIVE, expiry_date__lte=today + timedelta(days=days))
            .count(), "Registrations expiring soon", f"Within {days} days", "/registrations/",
            "warn"))
        t.append(_task(ProductRegistration.objects.exclude(
            status=ProductRegistration.ACTIVE).count(), "Inactive registrations",
            "These products cannot be listed", "/registrations/"))
    if user.has_role(R.FINANCE):
        t.append(_task(PlatformOrder.objects.filter(
            status=PlatformOrder.AWAITING_PAYMENT).count(), "Payments to confirm",
            "Transfer receipts and credit orders", "/payments/", "warn"))
        t.append(_task(Invoice.objects.filter(status=Invoice.UNPAID, due_date__lt=today)
                       .count(), "Overdue invoices", "Follow up with the buyer", "/payments/",
                       "bad"))
    if user.has_role(R.ADMIN):
        t.append(_task(User.objects.filter(is_locked=True, account_type=User.INTERNAL).count(),
                       "Locked accounts", "Unlock after checking with the user",
                       "/admin/users/", "warn"))
        from inventory.models import ExpiryScanRun

        last = ExpiryScanRun.objects.first()
        t.append(_task(1 if last and not last.succeeded else 0, "Last expiry scan failed",
                       "Check the scan log and run it again", "/scans/", "bad"))
    if user.has_role(R.SALES, R.REGULATORY, R.FINANCE, R.WAREHOUSE, R.COO):
        from commerce.models import SupportMessage

        topics = set()
        if user.has_role(R.SALES, R.COO):
            topics |= {"product", "other"}
        if user.has_role(R.REGULATORY):
            topics.add("account")
        if user.has_role(R.FINANCE):
            topics |= {"payment", "order"}
        if user.has_role(R.WAREHOUSE):
            topics.add("order")
        t.append(_task(SupportMessage.objects.filter(status=SupportMessage.OPEN,
                                                     topic__in=topics).count(),
                       "Buyer messages to answer", "Sent from the platform's Contact page",
                       "/b2b/messages/", "warn"))
    order = {"bad": 0, "warn": 1, "info": 2}
    return sorted([x for x in t if x["count"]], key=lambda x: order[x["level"]])


def _var():
    total = Decimal(0)
    for b in Batch.objects.filter(status__in=[BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS,
                                              BatchStatus.RELEASED],
                                  qty_on_hand__gt=0).select_related("product"):
        total += b.product.unit_cost * b.qty_on_hand
    return total


def _recovered():
    lines = OrderLine.objects.filter(order__status__in=[
        PlatformOrder.CONFIRMED, PlatformOrder.DISPATCHED, PlatformOrder.DELIVERED])
    return sum((x.unit_price * x.quantity for x in lines), Decimal(0))


def money_short(v):
    v = float(v or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 10_000:
        return f"{v / 1000:.0f}K"
    return f"{v:,.0f}"


def kpis_for(user):
    """(label, value, icon, tone, url)."""
    near = Batch.objects.filter(status__in=[BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS,
                                            BatchStatus.RELEASED], qty_on_hand__gt=0).count()
    from .access import ANALYTICS

    can_analytics = user.has_role(*ANALYTICS)
    k = [("Value at risk (EGP)", money_short(_var()), "bi-exclamation-triangle", "warn",
          "/analytics/?d=inventory" if can_analytics else "/expiry/"),
         ("Near-expiry batches", near, "bi-hourglass-split", "bad", "/expiry/")]
    if user.has_role(R.FINANCE, R.COO, R.MANAGEMENT, R.GM, R.SALES):
        k.append(("Recovered via B2B (EGP)", money_short(_recovered()), "bi-graph-up-arrow",
                  "ok", "/analytics/?d=recovery" if can_analytics else "/b2b/orders/"))
    k.append(("Live B2B listings", Listing.objects.filter(status=Listing.ACTIVE).count(),
              "bi-shop", "info", "/listings/"))
    if user.has_role(R.WAREHOUSE, R.INVENTORY, R.QA):
        k.append(("Units on hand", f"{Batch.objects.aggregate(t=Sum('qty_on_hand'))['t'] or 0:,}",
                  "bi-box-seam", "info", "/stock/"))
    if user.has_role(R.REGULATORY, R.FINANCE):
        k.append(("Verified buyers", Buyer.objects.filter(status=Buyer.VERIFIED).count(),
                  "bi-patch-check", "ok", "/buyers/"))
    return k[:4]


QUICK = [
    ("Record inspection", "Log a received batch", "bi-clipboard-plus", "/inspections/", (R.QA,)),
    ("Book in stock", "Put approved batches away", "bi-box-arrow-in-down", "/stock/receipt/",
     (R.INVENTORY,)),
    ("Post to B2B", "Offer a near-expiry batch", "bi-shop", "/b2b/post/", (R.SALES, R.COO)),
    ("New count", "Schedule a warehouse audit", "bi-clipboard-check", "/audits/new/",
     (R.INVENTORY,)),
    ("Request write-off", "Expired or damaged stock", "bi-trash3", "/write-offs/new/",
     (R.INVENTORY,)),
    ("Dispatch orders", "Pick and ship B2B orders", "bi-truck", "/dispatch/", (R.WAREHOUSE,)),
    ("New purchase order", "Order from a supplier", "bi-cart-plus", "/purchase-orders/new/",
     (R.PROCUREMENT,)),
    ("Import Odoo data", "Sales, stock, POs", "bi-upload", "/imports/", (R.PROCUREMENT,)),
    ("Generate forecast", "Monthly demand", "bi-graph-up", "/forecasts/", (R.PROCUREMENT,)),
    ("Verify buyers", "Check documents", "bi-patch-check", "/buyers/?status=pending",
     (R.REGULATORY,)),
    ("Confirm payments", "Receipts and credit", "bi-cash-coin", "/payments/", (R.FINANCE,)),
    ("Add a product", "Extend the catalogue", "bi-plus-square", "/products/new/",
     (R.PROCUREMENT, R.ADMIN)),
    ("Add a user", "Create an account", "bi-person-plus", "/admin/users/new/", (R.ADMIN,)),
    ("Analytics", "Dashboards and exports", "bi-bar-chart-line", "/analytics/",
     (R.GM, R.MANAGEMENT, R.COO, R.SALES, R.FINANCE, R.PROCUREMENT)),
    ("Stock overview", "Find any batch", "bi-search", "/stock/", None),
]


def quick_for(user):
    return [q for q in QUICK if q[4] is None or user.has_role(*q[4])][:6]


def charts_for(user):
    today = timezone.localdate()
    bands = [("> 12 months", 365, None), ("6–12 months", 181, 364), ("1–6 months", 30, 180),
             ("< 30 days", 0, 29), ("Expired", None, -1)]
    values = []
    batches = list(Batch.objects.filter(qty_on_hand__gt=0).exclude(
        status=BatchStatus.INSPECTED).select_related("product"))
    for _n, lo, hi in bands:
        v = Decimal(0)
        for b in batches:
            d = (b.expiry_date - today).days
            if (lo is None or d >= lo) and (hi is None or d <= hi):
                v += b.product.unit_cost * b.qty_on_hand
        values.append(round(float(v)))
    charts = [{"id": "ageing", "type": "bar", "title": "Stock by remaining shelf life (EGP at cost)",
               "labels": [b[0] for b in bands], "data": values,
               "colors": ["#16a34a", "#65a30d", "#f59e0b", "#ef4444", "#374151"]}]
    if user.has_role(R.SALES, R.GM, R.MANAGEMENT, R.COO, R.PROCUREMENT, R.FINANCE):
        rows = list(SalesRecord.objects.values("period").annotate(v=Sum("total"))
                    .order_by("-period")[:12])[::-1]
        charts.append({"id": "sales", "type": "line", "title": "Sales, last 12 months (EGP)",
                       "labels": [f"{r['period']:%b %y}" for r in rows],
                       "data": [float(r["v"]) for r in rows]})
    else:
        rows = (Batch.objects.filter(status__in=[BatchStatus.NEAR_EXPIRY, BatchStatus.SURPLUS,
                                                 BatchStatus.RELEASED], qty_on_hand__gt=0)
                .values("product__category__name").annotate(n=Count("id")))
        charts.append({"id": "cat", "type": "doughnut", "title": "Near-expiry batches by category",
                       "labels": [r["product__category__name"] for r in rows],
                       "data": [r["n"] for r in rows]})
    return charts
