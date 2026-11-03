"""Data-based reminders (FR-108 to FR-114, UC-56, UC-57).

Instead of a fixed reorder point, the system reminds each role of the
recurring work the data says is due: the purchase plan, the demand forecast,
customer follow-up, the warehouse audit and warehouse transfers. Every
reminder explains itself in four parts:

1. explain:        what the data (the table or figure behind it) shows,
2. impact:         how it affects the company and which KPIs move,
3. recommendation: what to do, and
4. basis:          the rules and data the recommendation is based on.

``generate()`` runs with the daily jobs (and on demand). It opens new
reminders, refreshes open ones, reopens snoozed or done ones that still apply
and resolves those whose condition has cleared.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

from .models import Reminder, Role
from .rules import rule_int
from .services import notify_roles

REFRESH_KEY = "reminders-generated-at"
RISK_LOC = "near-expiry, surplus or released batch location"
REFRESH_HOURS = 6


def _money(v):
    v = float(v or 0)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:,.2f}M EGP"
    if abs(v) >= 10_000:
        return f"{v / 1000:,.0f}K EGP"
    return f"{v:,.0f} EGP"


def _join(items, limit=3):
    items = list(items)
    shown = items[:limit]
    text = ", ".join(shown[:-1]) + (" and " if len(shown) > 1 else "") + shown[-1] \
        if shown else ""
    if len(items) > limit:
        text += f", and {len(items) - limit} more"
    return text


def _n(count, word, plural=None):
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _ago(days):
    return "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"


def _r(key, kind, roles, title, explain, impact, recommendation, basis, url, action,
       priority=Reminder.MEDIUM):
    return dict(key=key, kind=kind, roles=list(roles), title=title, explain=explain,
                impact=impact, recommendation=recommendation, basis=basis, url=url,
                action_label=action, priority=priority)


# --------------------------------------------------------------------------
# Purchase plan and forecast (Procurement Officer)
# --------------------------------------------------------------------------
def _avg_daily_sales(product_ids):
    from planning.models import SalesRecord

    latest = SalesRecord.objects.order_by("-period").values_list("period", flat=True).first()
    if not latest:
        return {}, None
    start = date(latest.year - (1 if latest.month <= 2 else 0),
                 (latest.month - 3) % 12 + 1, 1)
    rows = (SalesRecord.objects.filter(product_id__in=product_ids, period__gte=start)
            .values("product_id").annotate(q=Sum("quantity")))
    return {r["product_id"]: (r["q"] or 0) / 91.0 for r in rows}, latest


def short_cover_products():
    """Products whose usable stock plus open orders will not last the supplier lead time
    plus safety stock: they have to be in the next purchase plan."""
    from inventory.models import Product, PurchaseOrder, PurchaseOrderLine
    from planning.services import usable_stock

    products = list(Product.objects.filter(is_active=True))
    daily, _latest = _avg_daily_sales([p.pk for p in products])
    safety = rule_int("safety_stock_days")
    out = []
    for p in products:
        rate = daily.get(p.pk, 0)
        if rate <= 0:
            continue
        usable, _excluded = usable_stock(p, rate * 30.4)
        open_po = sum(x.open_quantity for x in PurchaseOrderLine.objects.filter(
            product=p, order__status=PurchaseOrder.OPEN))
        cover = (usable + open_po) / rate
        needed = p.lead_time_days + safety
        if cover < needed:
            out.append({"product": p, "cover": cover, "needed": needed, "usable": usable,
                        "open_po": open_po, "rate": rate})
    out.sort(key=lambda x: x["cover"] - x["needed"])
    return out


def purchase_plan_reminders():
    from planning.models import PurchasePlan, PurchasePlanLine

    today = timezone.localdate()
    cycle = rule_int("plan_cycle_days")
    out = []
    returned = PurchasePlan.objects.filter(status=PurchasePlan.RETURNED).first()
    if returned:
        out.append(_r(
            f"plan-returned-{returned.pk}", Reminder.PURCHASE_PLAN, [Role.PROCUREMENT],
            f"Revise purchase plan {returned.number}: the General Manager returned it",
            f"Purchase plan {returned.number} ({returned.period_label}) was returned by the "
            f"General Manager"
            + (f" with the comment \"{returned.decision_comment}\"." if
               returned.decision_comment else "."),
            "Until the plan is resubmitted no purchase order can be raised from it, so every "
            "day of delay shortens the stock cover of the products in it. KPIs affected: days "
            "of cover and, if orders are placed late in a hurry, MOQ surplus.",
            "Open the plan, change the returned lines or record an override reason, and "
            "resubmit it to the General Manager.",
            "The plan's status (Returned) and the General Manager's decision comment "
            "(FR-48, FR-49).",
            f"/plans/{returned.pk}/", "Open the plan", Reminder.HIGH))
    last = PurchasePlan.objects.order_by("-created_at").first()
    age = (today - timezone.localtime(last.created_at).date()).days if last else None
    short = short_cover_products()
    # Products already ordered in a plan that is still being prepared or approved
    # are covered; the reminder is about the rest.
    pending = PurchasePlan.objects.filter(status__in=[PurchasePlan.DRAFT,
                                                      PurchasePlan.SUBMITTED])
    covered = set(PurchasePlanLine.objects.filter(plan__in=pending, final_qty__gt=0)
                  .values_list("product_id", flat=True))
    short = [x for x in short if x["product"].pk not in covered]
    if short or ((age is None or age >= cycle) and not pending.exists()):
        names = [f"{x['product'].name} ({x['cover']:.0f} days of cover against "
                 f"{x['needed']} needed)" for x in short]
        explain = (f"The last purchase plan, {last.number}, was prepared {_ago(age)} "
                   f"({last.get_status_display().lower()})." if last else
                   "No purchase plan has been prepared yet.")
        if short:
            explain += (f" {_n(len(short), 'product')} not covered by any plan in progress "
                        f"will not last their supplier lead time plus safety stock: "
                        f"{_join(names)}.")
        else:
            explain += (f" Every product still has enough cover, but the {cycle}-day planning "
                        f"cycle is due.")
        out.append(_r(
            "plan-due", Reminder.PURCHASE_PLAN, [Role.PROCUREMENT],
            "Prepare this month's purchase plan"
            + (f": {_n(len(short), 'product')} will run short" if short else ""),
            explain,
            "A product ordered after its cover falls below its lead time arrives after the "
            "shelf is empty: the company loses sales or pays for an urgent shipment, and a "
            "late plan tends to over-order to catch up, which is how MOQ surplus becomes "
            "near-expiry stock. KPIs affected: days of cover, stock-outs, MOQ surplus (EGP) "
            "and value at risk.",
            ("Build the purchase plan now and include "
             + (f"{short[0]['product'].name} first" if short else "every active product")
             + ". Generate a new forecast first if the latest one is out of date, then submit "
               "the plan to the General Manager."),
            f"Purchase plan cycle: {cycle} days (business rule). Days of cover = (usable "
            f"stock + open purchase orders) ÷ average daily sales over the last three "
            f"months; usable stock excludes quantity projected to reach near-expiry before "
            f"it sells (FR-46). Each product is compared with its supplier lead time plus "
            f"{rule_int('safety_stock_days')} days of safety stock.",
            "/plans/", "Open purchase plans",
            Reminder.HIGH if short else Reminder.MEDIUM))
    return out


def forecast_reminders():
    from planning.models import DataImport, Forecast, ForecastProduct

    today = timezone.localdate()
    cycle = rule_int("forecast_cycle_days")
    last = Forecast.objects.order_by("-created_at").first()
    age = (today - timezone.localtime(last.created_at).date()).days if last else None
    imports = DataImport.objects.filter(data_type="sales", status=DataImport.IMPORTED)
    if last:
        imports = imports.filter(uploaded_at__gt=last.created_at)
    new_rows = sum(i.rows_accepted for i in imports)
    low = total = 0
    if last:
        fps = ForecastProduct.objects.filter(forecast=last)
        total = fps.count()
        low = fps.filter(low_confidence=True).count()
    if last and age < cycle and not new_rows:
        return []
    mape = rule_int("mape_low_confidence_pct")
    explain = (f"The latest demand forecast, {last}, was generated {_ago(age)}." if last else
               "No demand forecast has been generated yet.")
    if new_rows:
        explain += (f" {_n(imports.count(), 'sales import')} with {new_rows:,} rows "
                    f"{'has' if imports.count() == 1 else 'have'} been "
                    f"loaded since it was generated, so it does not reflect the latest sales.")
    if total:
        explain += (f" {low} of its {total} product-region forecasts are flagged low "
                    f"confidence (MAPE above {mape}%).")
    return [_r(
        "forecast-due", Reminder.FORECAST, [Role.PROCUREMENT],
        "Refresh the demand forecast before the next purchase plan",
        explain,
        "The purchase plan is built from the forecast. An out-of-date forecast repeats "
        "last month's assumptions, so the company buys for demand that has already moved: "
        "too much of slowing products (surplus, then write-offs) and too little of growing "
        "ones. KPIs affected: forecast accuracy (MAPE), MOQ surplus and value at risk.",
        "Open Demand forecast and generate a new forecast; review the low-confidence "
        "products and record a manual reason where you override them, then build the "
        "purchase plan from it.",
        f"Forecast refresh cycle: {cycle} days (business rule); sales imports dated after "
        f"the forecast; low-confidence threshold: MAPE above {mape}% (FR-43).",
        "/forecasts/", "Open demand forecast",
        Reminder.HIGH if new_rows or not last else Reminder.MEDIUM)]


# --------------------------------------------------------------------------
# Customer follow-up (Sales Representative)
# --------------------------------------------------------------------------
def follow_up_reminders():
    from inventory.models import CustomerOffer
    from planning.models import SalesRecord

    today = timezone.localdate()
    days = rule_int("offer_followup_days")
    min_shelf = rule_int("min_sellable_days")
    out = []
    for o in CustomerOffer.objects.filter(
            outcome=CustomerOffer.PENDING,
            offered_at__date__lte=today - timedelta(days=days)).select_related(
            "batch__product", "customer"):
        b = o.batch
        waited = (today - timezone.localtime(o.offered_at).date()).days
        value = o.qty_offered * b.product.original_price
        out.append(_r(
            f"offer-{o.pk}", Reminder.FOLLOW_UP, [Role.SALES],
            f"Follow up {o.customer.name} on the {b.product.name} offer",
            f"{o.customer.name} was offered {o.qty_offered:,} units of {b.product.name} (lot "
            f"{b.lot_number}, about {_money(value)} at list price) {waited} days ago and has "
            f"not answered. The batch has {b.days_to_expiry} days left before expiry.",
            "Every day without an answer uses shelf life the clearance channel needs. Once a "
            f"batch has less than {min_shelf} days left it can no longer be listed, so a late "
            "\"no\" turns recoverable stock into a write-off. KPIs affected: value at risk, "
            "recovered value and the share of near-expiry stock recovered.",
            f"Call {o.customer.name} today and record Accepted or Declined on the batch. If "
            "the answer is no, flag the batch as surplus so the COO can release it to the "
            "platform.",
            f"Customer offer follow-up: {days} days (business rule); offer date, batch "
            f"expiry date and minimum sellable shelf life of {min_shelf} days.",
            f"/expiry/batch/{b.pk}/", "Open the batch",
            Reminder.HIGH if b.days_to_expiry < 90 else Reminder.MEDIUM))

    latest = SalesRecord.objects.order_by("-period").values_list("period", flat=True).first()
    if latest:
        lapse = rule_int("lapsed_customer_days")
        ref = date(latest.year + latest.month // 12, latest.month % 12 + 1, 1)
        cutoff = ref - timedelta(days=lapse)
        window = date(cutoff.year - 1, cutoff.month, 1)
        months = defaultdict(set)
        value = defaultdict(Decimal)
        last_bought = {}
        for r in SalesRecord.objects.exclude(customer_name="").filter(
                period__gte=window).values("customer_name", "period", "total"):
            name = r["customer_name"]
            last_bought[name] = max(last_bought.get(name, r["period"]), r["period"])
            if r["period"] < cutoff:
                months[name].add(r["period"])
                value[name] += r["total"] or 0
        lapsed = [n for n, ms in months.items() if len(ms) >= 4 and last_bought[n] < cutoff]
        lapsed.sort(key=lambda n: -value[n])
        if lapsed:
            listed = [f"{n} (last bought {last_bought[n]:%b %Y}, {_money(value[n])} in the "
                      f"year before)" for n in lapsed]
            out.append(_r(
                "lapsed-customers", Reminder.FOLLOW_UP, [Role.SALES],
                (f"{_n(len(lapsed), 'regular customer')} "
                 f"{'has' if len(lapsed) == 1 else 'have'} stopped buying"),
                f"These customers bought in at least four months of the year before "
                f"{cutoff:%b %Y} but have not bought since: {_join(listed)}.",
                "The forecast still expects their demand, so the stock bought for them sits "
                "longer and moves towards near-expiry. A lost regular account also lowers "
                "sales that the field team counts on. KPIs affected: sales value, "
                "sell-through, days of cover and value at risk.",
                f"Contact {_join(lapsed[:3])} this week: find out whether they reduced "
                f"stock or moved to a competitor, and offer matching near-expiry batches "
                f"first. Tell Procurement if the demand is gone for good, so the next "
                f"forecast drops it.",
                f"Lapsed customer rule: {lapse} days (business rule); a regular customer "
                f"bought in at least four of the twelve months before the cut-off; sales "
                f"history imported up to {latest:%b %Y}.",
                "/customers/", "Open customers", Reminder.MEDIUM))
    return out


# --------------------------------------------------------------------------
# Inventory audit (Inventory Controller)
# --------------------------------------------------------------------------
def audit_reminders():
    from core.models import Warehouse
    from inventory.models import BatchStock, StockCount

    today = timezone.localdate()
    interval = rule_int("audit_interval_days")
    tol = rule_int("variance_tolerance_pct")
    out = []
    for w in Warehouse.objects.filter(is_active=True):
        stock = list(BatchStock.objects.filter(location__warehouse=w, quantity__gt=0)
                     .select_related("batch__product"))
        if not stock:
            continue
        value = sum((s.batch.product.unit_cost * s.quantity for s in stock), Decimal(0))
        at_risk = [s for s in stock if s.batch.status in ("near_expiry", "surplus",
                                                           "released")]
        risk_value = sum((s.batch.product.unit_cost * s.quantity for s in at_risk),
                         Decimal(0))
        last = StockCount.objects.filter(warehouse=w, status=StockCount.CLOSED).order_by(
            "-closed_at").first()
        open_count = StockCount.objects.filter(warehouse=w).exclude(
            status=StockCount.CLOSED).exists()
        age = (today - timezone.localtime(last.closed_at).date()).days if last else None
        weak = last is not None and last.accuracy_rate is not None and last.accuracy_rate < 95
        if open_count or (age is not None and age < interval and not weak):
            continue
        explain = (f"{w.name} was last counted {age} days ago ({last.number}"
                   + (f", accuracy {last.accuracy_rate:.0f}%" if last.accuracy_rate is not
                      None else "") + ")." if last else f"{w.name} has never been counted.")
        explain += (f" It holds {sum(s.quantity for s in stock):,} units worth "
                    f"{_money(value)} at cost"
                    + (f", including {_n(len(at_risk), RISK_LOC)} worth "
                       f"{_money(risk_value)}." if at_risk else "."))
        out.append(_r(
            f"audit-{w.pk}", Reminder.AUDIT, [Role.INVENTORY],
            f"Schedule a count at {w.name}",
            explain,
            "Every expiry alert, clearance listing and purchase plan reads this warehouse's "
            "stock record. If the record is wrong, the company offers stock it does not have "
            "or buys stock it already holds, and near-expiry value is misstated. KPIs "
            "affected: audit accuracy, adjustment value, value at risk and recovered value.",
            ("Schedule a cycle count of the near-expiry batches first, then a full count"
             if at_risk else "Schedule a full count")
            + f" at {w.name} from Audits → New count, and assign it to a Warehouse Officer.",
            f"Warehouse audit interval: {interval} days (business rule); last closed count "
            f"date and accuracy (below 95% triggers an earlier count); count variance "
            f"tolerance {tol}%; stock on hand at cost by batch status.",
            "/audits/new/", "Schedule a count",
            Reminder.HIGH if (age is None or weak or risk_value) else Reminder.MEDIUM))
    return out


# --------------------------------------------------------------------------
# Warehouse transfers (Warehouse Officer, Inventory Controller)
# --------------------------------------------------------------------------
def transfer_reminders():
    from core.models import BinLocation
    from inventory.models import BatchStock
    from inventory.services import location_load, storage_compatible

    out = []
    roles = [Role.WAREHOUSE, Role.INVENTORY]
    # 1. Stock in a location whose storage type does not suit it.
    for s in BatchStock.objects.filter(quantity__gt=0).select_related(
            "batch__product", "location__warehouse"):
        b, loc = s.batch, s.location
        if storage_compatible(b.storage_condition, loc.storage_type):
            continue
        target = BinLocation.objects.filter(
            is_active=True, storage_type=b.storage_condition).first()
        out.append(_r(
            f"transfer-storage-{s.pk}", Reminder.TRANSFER, roles,
            f"Move {b.product.name} lot {b.lot_number} to {b.get_storage_condition_display()} "
            f"storage",
            f"{s.quantity:,} units of {b.product.name} (lot {b.lot_number}), which must be "
            f"kept {b.get_storage_condition_display().lower()}, are in {loc} "
            f"({loc.get_storage_type_display().lower()} storage).",
            "Product kept outside its storage condition can lose potency and must be "
            "quarantined and possibly written off, which destroys its full value and puts "
            "regulated product at risk. KPIs affected: write-off value, value at risk and "
            "audit accuracy.",
            f"Transfer the stock now to {target or 'a matching location'} and ask QA to "
            "check the batch.",
            "The batch's storage condition compared with the location's storage type "
            "(FR-11).",
            f"/stock/transfer/?batch={b.pk}", "Transfer stock", Reminder.HIGH))
    # 2. Locations above capacity.
    for loc in BinLocation.objects.filter(is_active=True).select_related("warehouse"):
        load = location_load(loc)
        if load <= loc.capacity:
            continue
        spare = [x for x in BinLocation.objects.filter(
            is_active=True, storage_type=loc.storage_type).exclude(pk=loc.pk)
            if x.capacity - location_load(x) >= load - loc.capacity]
        out.append(_r(
            f"transfer-capacity-{loc.pk}", Reminder.TRANSFER, roles,
            f"{loc} is over capacity",
            f"{loc} holds {load:,} units against a capacity of {loc.capacity:,}, "
            f"{load - loc.capacity:,} units over.",
            "Overfilled locations slow picking and counting and make earliest-expiry-first "
            "allocation harder to follow, so older stock is left behind. KPIs affected: "
            "audit accuracy, dispatch time and value at risk.",
            f"Move {load - loc.capacity:,} units to "
            + (f"{spare[0]}, which has room" if spare else
               "another location of the same storage type") + ".",
            "Stock on hand per location compared with the location's capacity (reference "
            "data, FR-53).",
            "/stock/transfer/", "Transfer stock", Reminder.MEDIUM))
    # 3. A near-expiry or released batch split across warehouses.
    split = defaultdict(list)
    for s in BatchStock.objects.filter(quantity__gt=0, batch__status__in=[
            "near_expiry", "surplus", "released"]).select_related(
            "batch__product", "location__warehouse"):
        split[s.batch].append(s)
    for b, rows in split.items():
        by_wh = defaultdict(int)
        for s in rows:
            by_wh[s.location.warehouse] += s.quantity
        if len(by_wh) < 2:
            continue
        main = max(by_wh, key=by_wh.get)
        others = [(w, q) for w, q in by_wh.items() if w != main]
        moved = sum(q for _w, q in others)
        out.append(_r(
            f"transfer-split-{b.pk}", Reminder.TRANSFER, roles,
            f"Bring {b.product.name} lot {b.lot_number} together in {main.name}",
            f"Lot {b.lot_number} ({b.get_status_display().lower()}, {b.days_to_expiry} days "
            f"to expiry) is split: {by_wh[main]:,} units in {main.name} and "
            + _join([f"{q:,} in {w.name}" for w, q in others]) + ".",
            "A clearance order is picked from one place. Stock split across warehouses "
            "delays dispatch, risks part shipments and leaves a remainder that expires "
            "unnoticed. KPIs affected: recovered value, order-to-dispatch time and value at "
            "risk.",
            f"Transfer the {moved:,} units to {main.name} before the batch is ordered.",
            "Batch status (near-expiry, surplus or released) and stock on hand per "
            "warehouse.",
            f"/stock/transfer/?batch={b.pk}", "Transfer stock", Reminder.MEDIUM))
    return out


CHECKS = [purchase_plan_reminders, forecast_reminders, follow_up_reminders, audit_reminders,
          transfer_reminders]
FIELDS = ["kind", "roles", "title", "explain", "impact", "recommendation", "basis", "url",
          "action_label", "priority"]


def generate(notify=True):
    """Open, refresh, reopen and resolve reminders. Returns the number opened."""
    today = timezone.localdate()
    now = timezone.now()
    found = {}
    for check in CHECKS:
        for r in check():
            found[r["key"]] = r
    opened = 0
    existing = {x.key: x for x in Reminder.objects.all()}
    for key, data in found.items():
        rem = existing.get(key)
        reopen = False
        if rem is None:
            rem = Reminder(key=key, created_at=now)
            reopen = True
        elif rem.status == Reminder.SNOOZED and rem.snoozed_until and \
                rem.snoozed_until <= today:
            reopen = True
        elif rem.status == Reminder.DONE and rem.done_at and \
                (now - rem.done_at).days >= rule_int("reminder_snooze_days"):
            reopen = True
        elif rem.status == Reminder.RESOLVED:
            reopen = True
        for f in FIELDS:
            setattr(rem, f, data[f])
        rem.updated_at = now
        if reopen:
            rem.status, rem.snoozed_until = Reminder.OPEN, None
            opened += 1
        rem.save()
        if reopen and notify:
            notify_roles(data["roles"], f"Reminder: {data['title']}", "/reminders/",
                         level="warning" if data["priority"] == Reminder.HIGH else "info",
                         email=False)
    Reminder.objects.filter(status__in=[Reminder.OPEN, Reminder.SNOOZED]).exclude(
        key__in=found.keys()).update(status=Reminder.RESOLVED, updated_at=now)
    cache.set(REFRESH_KEY, now, REFRESH_HOURS * 3600)
    return opened


def refresh_if_stale():
    if cache.get(REFRESH_KEY) is None:
        generate()


def visible_to(user):
    qs = Reminder.objects.filter(status=Reminder.OPEN)
    if user.has_role(Role.GM, Role.MANAGEMENT, Role.COO, Role.ADMIN):
        return list(qs)
    return [r for r in qs if any(user.has_role(role) for role in r.roles)]


def sort_key(r):
    return ({"high": 0, "medium": 1, "low": 2}[r.priority], r.kind, r.title)
