"""Storytelling charts and automated analysis for management (FR-55 to FR-64).

Every chart is a plain dict so the same data drives the screen, the PDF
export and the AI analysis:

    {"id", "type": "bar" | "hbar" | "line" | "grouped",
     "headline": the finding in one sentence (the chart's title),
     "subtitle": what is plotted, "labels": [...],
     "series": [{"name", "data", "colors"?}], "unit": "EGP" | "units" | "%",
     "analysis": {"insight": str, "implication": {perspective: str},
                  "action": {perspective: str}}}

The built-in analysis is computed from the numbers with fixed rules, so it is
always available, offline and repeatable. ``analytics.ai`` can replace it
with Claude's analysis of the same data.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

# Management perspectives the analysis is written for.
PERSPECTIVES = [
    ("ceo", "CEO"), ("cfo", "Group CFO"), ("coo", "COO"), ("gm", "General Manager"),
]
PERSPECTIVE_FOCUS = {
    "ceo": "the overall picture: how much value surplus stock costs the company, whether the "
           "new channel is recovering it, and where the next risk sits",
    "cfo": "money: write-off losses, cash tied up in surplus, recovered cash, receivables "
           "and credit notes",
    "coo": "operations: how fast near-expiry stock moves through offer, release and sale, "
           "listing performance and price",
    "gm": "control: purchase plan approvals, MOQ surplus created at purchase, write-off "
          "approvals and audit adjustments",
}

# Reference palette (dataviz skill, validated): one hue per job.
BLUE, BLUE_SOFT = "#2a78d6", "#86b6ef"
ORANGE, AQUA = "#eb6834", "#1baf7a"
GOOD, WARNING, SERIOUS, CRITICAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"
MUTED = "#c3c2b7"
FUNNEL = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]

ACTIVE_STATUSES = ["in_stock", "near_expiry", "surplus", "released"]
AT_RISK = ["near_expiry", "surplus", "released"]


def money(v):
    v = float(v or 0)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:,.2f}M EGP"
    if abs(v) >= 10_000:
        return f"{v / 1000:,.0f}K EGP"
    return f"{v:,.0f} EGP"


def pct(part, whole):
    return 100.0 * float(part) / float(whole) if whole else 0.0


def chart(cid, ctype, headline, subtitle, labels, series, unit, insight, implication=None,
          action=None):
    return {"id": cid, "type": ctype, "headline": headline, "subtitle": subtitle,
            "labels": list(labels), "series": series, "unit": unit,
            "analysis": {"insight": insight, "implication": implication or {},
                         "action": action or {}}}


def emphasis(values, index):
    """One highlighted bar, the rest recessive (emphasis rather than rainbow)."""
    return [BLUE if i == index else BLUE_SOFT for i in range(len(values))]


def _per(text_all=None, **kw):
    out = {p: text_all for p, _ in PERSPECTIVES if text_all}
    out.update(kw)
    return out


# ==========================================================================
# Data helpers
# ==========================================================================
def _months_back(n, end=None):
    end = end or timezone.localdate().replace(day=1)
    out = []
    y, m = end.year, end.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def _recovered_by_month(months):
    from commerce.models import OrderLine, PlatformOrder

    out = defaultdict(Decimal)
    lines = OrderLine.objects.filter(order__status__in=[
        PlatformOrder.CONFIRMED, PlatformOrder.DISPATCHED, PlatformOrder.DELIVERED],
        order__created_at__date__gte=months[0]).select_related("order")
    for line in lines:
        d = timezone.localtime(line.order.created_at).date().replace(day=1)
        out[d] += line.unit_price * line.active_qty
    return [out.get(m, Decimal(0)) for m in months]


def _written_off_by_month(months):
    from inventory.models import WriteOff

    out = defaultdict(Decimal)
    for w in WriteOff.objects.filter(status__in=[WriteOff.APPROVED, WriteOff.DISPOSED],
                                     posted_at__date__gte=months[0]):
        out[timezone.localtime(w.posted_at).date().replace(day=1)] += w.write_off_value
    return [out.get(m, Decimal(0)) for m in months]


def value_at_risk(f=None):
    from inventory.models import Batch

    qs = Batch.objects.filter(status__in=AT_RISK, qty_on_hand__gt=0).select_related(
        "product__category")
    if f is not None:
        qs = f.batches().filter(status__in=AT_RISK)
    return list(qs)


# ==========================================================================
# Charts
# ==========================================================================
def at_risk_outcomes(f):
    """Where the value of near-expiry stock ends up: recovered, at risk, destroyed."""
    from commerce.models import OrderLine, PlatformOrder
    from inventory.models import CustomerOffer, WriteOff

    recovered_b2b = sum((x.unit_price * x.active_qty for x in OrderLine.objects.filter(
        order__status__in=[PlatformOrder.CONFIRMED, PlatformOrder.DISPATCHED,
                           PlatformOrder.DELIVERED],
        order__created_at__date__gte=f.date_from, order__created_at__date__lte=f.date_to)),
        Decimal(0))
    customers = sum((o.qty_offered * o.batch.product.original_price
                     for o in CustomerOffer.objects.filter(
                         outcome=CustomerOffer.ACCEPTED, offered_at__date__gte=f.date_from,
                         offered_at__date__lte=f.date_to).select_related("batch__product")),
                    Decimal(0))
    at_risk = sum((b.product.unit_cost * b.qty_on_hand for b in value_at_risk(f)), Decimal(0))
    written = sum((w.write_off_value for w in WriteOff.objects.filter(
        status__in=[WriteOff.APPROVED, WriteOff.DISPOSED], posted_at__date__gte=f.date_from,
        posted_at__date__lte=f.date_to)), Decimal(0))
    labels = ["Sold to existing customers", "Recovered on the B2B platform",
              "Still at risk today", "Written off and destroyed"]
    values = [customers, recovered_b2b, at_risk, written]
    total = sum(values)
    saved = customers + recovered_b2b
    share_saved = pct(saved, total)
    if total == 0:
        headline = "No near-expiry stock has been recorded for this period"
    elif written > saved:
        headline = (f"More value was destroyed ({money(written)}) than recovered "
                    f"({money(saved)})")
    else:
        headline = (f"{share_saved:.0f}% of near-expiry value was recovered; "
                    f"{money(at_risk)} is still at risk")
    insight = (f"Of {money(total)} of near-expiry stock value in the period, "
               f"{money(customers)} went to existing customers, {money(recovered_b2b)} was "
               f"recovered through the B2B platform, {money(at_risk)} is still waiting for a "
               f"buyer and {money(written)} was written off.")
    return chart(
        "outcomes", "hbar", headline,
        "Near-expiry stock value by outcome (recovered at sale value, the rest at cost)",
        labels, [{"name": "Value", "data": [float(v) for v in values],
                  "colors": [GOOD, GOOD, WARNING, CRITICAL]}], "EGP", insight,
        implication=_per(
            ceo=f"Every pound in the last two bars is the cost of the forecast-to-MOQ error. "
                f"The share recovered ({share_saved:.0f}%) is the single best measure of "
                f"whether the clearance channel is working.",
            cfo=f"{money(at_risk)} at cost is exposed to a write-off if it does not sell "
                f"before expiry, on top of {money(written)} already lost.",
            coo=f"The {money(at_risk)} still at risk is the queue the release route must "
                f"clear before the batches expire.",
            gm=f"Write-offs of {money(written)} trace back to purchase plans; the MOQ "
               f"surplus view shows where the next ones come from."),
        action=_per(
            ceo="Track the recovered share monthly; it should rise as buyers join the "
                "platform.",
            cfo="Provision for the at-risk value that is less than 60 days from expiry.",
            coo="Release the at-risk batches with the shortest shelf life first and review "
                "clearance prices on listings that are not selling.",
            gm="Ask Procurement to explain the products behind the largest write-offs at "
               "the next plan review."))


def expiry_calendar(f):
    """Stock value at cost by month of expiry, next 12 months."""
    from inventory.models import Batch

    start = timezone.localdate().replace(day=1)
    months = _months_back(12, end=date(start.year + (start.month + 10) // 12,
                                       (start.month + 10) % 12 + 1, 1))
    by = defaultdict(Decimal)
    qs = f.batches().filter(status__in=ACTIVE_STATUSES) if f else Batch.objects.filter(
        status__in=ACTIVE_STATUSES, qty_on_hand__gt=0)
    for b in qs.select_related("product"):
        m = b.expiry_date.replace(day=1)
        if months[0] <= m <= months[-1]:
            by[m] += b.product.unit_cost * b.qty_on_hand
    values = [float(by.get(m, 0)) for m in months]
    total = sum(values)
    peak = max(range(len(values)), key=lambda i: values[i]) if total else 0
    next6 = sum(values[:6])
    headline = (f"{money(values[peak])} of stock expires in {months[peak]:%B %Y}, the peak "
                f"month" if total else "No stock expires in the next 12 months")
    insight = (f"{money(total)} of stock at cost expires within 12 months; {money(next6)} of "
               f"it ({pct(next6, total):.0f}%) within six months, which is already inside "
               f"the near-expiry window.")
    return chart(
        "expiry", "bar", headline, "Stock value at cost by month of expiry, next 12 months",
        [f"{m:%b %y}" for m in months],
        [{"name": "Value expiring", "data": values, "colors": emphasis(values, peak)}],
        "EGP", insight,
        implication=_per(
            ceo="This is the pipeline of future write-offs if nothing is done; it shows "
                "how much time the company has to act.",
            cfo=f"Value expiring in {months[peak]:%B} is the largest single exposure in the "
                f"next year.",
            coo=f"Plan offers and releases for {months[peak]:%B} expiries at least six "
                f"months ahead, so the batches reach buyers with shelf life left.",
            gm="A peak this concentrated usually comes from one large purchase; check the "
               "plan that bought it."),
        action=_per(
            "Open Near-expiry stock filtered to the peak month and confirm each batch has "
            "an owner.",
            cfo="Use the peak month to time the provision for expected losses."))


def sales_trend(f):
    from planning.models import SalesRecord

    qs = f.sales() if f else SalesRecord.objects.all()
    rows = {r["period"]: float(r["v"] or 0) for r in qs.values("period").annotate(
        v=Sum("total"))}
    if not rows:
        return None
    end = max(rows)
    months = _months_back(12, end=end)
    values = [rows.get(m, 0.0) for m in months]
    trend = []
    for i in range(len(values)):
        window = values[max(0, i - 2):i + 1]
        trend.append(sum(window) / len(window))
    last3 = sum(values[-3:])
    prev = [date(m.year - 1, m.month, 1) for m in months[-3:]]
    earlier = {r["period"]: float(r["v"] or 0) for r in (
        f.sales_any_date() if f else SalesRecord.objects.all()).filter(
        period__in=prev).values("period").annotate(v=Sum("total"))}
    prev3 = sum(earlier.get(m, 0.0) for m in prev)
    change = pct(last3 - prev3, prev3) if prev3 else None
    best = max(range(12), key=lambda i: values[i])
    if change is None:
        headline = f"Sales were {money(sum(values))} over the last 12 months"
    else:
        direction = "up" if change >= 0 else "down"
        headline = (f"Sales are {direction} {abs(change):.0f}% on the same three months last "
                    f"year")
    insight = (f"The last 12 months brought {money(sum(values))} in sales; the strongest "
               f"month was {months[best]:%B %Y} at {money(values[best])}."
               + (f" The last three months total {money(last3)} against {money(prev3)} a "
                  f"year earlier." if change is not None else ""))
    return chart(
        "sales", "line", headline, "Sales value per month and three-month average (EGP)",
        [f"{m:%b %y}" for m in months],
        [{"name": "Monthly sales", "data": values, "color": BLUE},
         {"name": "3-month average", "data": trend, "color": ORANGE}], "EGP", insight,
        implication=_per(
            ceo="Demand is the denominator of every surplus: when sales slow, the same "
                "purchase plan produces more near-expiry stock.",
            cfo="Sales trend drives the receivables and cash forecast.",
            coo="A slowing trend is an early warning for more batches entering the "
                "near-expiry window.",
            gm="Check that the next purchase plan reflects this trend rather than last "
               "year's volumes."),
        action=_per("Compare this trend with the forecast on the Forecast Accuracy tab."))


def clearance_funnel(f):
    """How far near-expiry batches get along the release route (batches)."""
    from inventory.models import (Batch, CustomerOffer, ExpiryAlert, Listing,
                                  SurplusRelease)

    reached = set(ExpiryAlert.objects.filter(alert_type=ExpiryAlert.NEAR_EXPIRY)
                  .values_list("batch_id", flat=True))
    reached |= set(Batch.objects.filter(status__in=AT_RISK).values_list("id", flat=True))
    offered = set(CustomerOffer.objects.values_list("batch_id", flat=True))
    flagged = set(SurplusRelease.objects.values_list("batch_id", flat=True))
    released = set(SurplusRelease.objects.filter(status=SurplusRelease.APPROVED)
                   .values_list("batch_id", flat=True))
    sold = {x.batch_id for x in Listing.objects.all() if x.sold_qty > 0}
    stages = ["Reached near-expiry", "Offered to customers", "Flagged as surplus",
              "Released by the COO", "Sold on the platform"]
    values = [len(reached), len(offered | flagged | released | sold),
              len(flagged | released), len(released), len(sold)]
    worst = min(range(1, 5), key=lambda i: (values[i] / values[i - 1]) if values[i - 1]
                else 1)
    headline = (f"{values[4]} of {values[0]} near-expiry batches reached a buyer on the "
                f"platform" if values[0] else "No batch has reached the near-expiry window")
    insight = (f"The largest drop is from '{stages[worst - 1]}' to '{stages[worst]}' "
               f"({values[worst - 1]} to {values[worst]} batches).")
    return chart(
        "funnel", "hbar", headline, "Batches at each step of the release route",
        stages, [{"name": "Batches", "data": values, "colors": FUNNEL}], "batches", insight,
        implication=_per(
            ceo="The route only recovers value if batches move through it; a large drop at "
                "one step is where the process stalls.",
            cfo="Batches that stall before release keep their full cost on the balance "
                "sheet until they expire.",
            coo=f"The '{stages[worst]}' step is the bottleneck in the release route.",
            gm="Ask the owner of the bottleneck step for the reason batches stop there."),
        action=_per(
            "Open Release requests and Near-expiry stock to see the batches stuck at the "
            "bottleneck.",
            coo="Decide waiting release requests within the week and set prices in the "
                "65-70% band so listings go live."))


def var_by_category(f):
    by = defaultdict(Decimal)
    for b in value_at_risk(f):
        by[b.product.category.name] += b.product.unit_cost * b.qty_on_hand
    if not by:
        return None
    items = sorted(by.items(), key=lambda kv: -kv[1])
    total = sum(by.values())
    top, top_v = items[0]
    values = [float(v) for _k, v in items]
    headline = f"{top} holds {pct(top_v, total):.0f}% of the value at risk"
    insight = (f"{money(total)} of stock at cost is near expiry, released or flagged as "
               f"surplus; {top} accounts for {money(top_v)}.")
    return chart(
        "var_cat", "hbar", headline, "Value at risk at cost by product category",
        [k for k, _v in items], [{"name": "Value at risk", "data": values,
                                  "colors": emphasis(values, 0)}], "EGP", insight,
        implication=_per(
            f"Concentration in {top} means a small number of decisions recovers most of the "
            f"value.",
            cfo=f"{top} is where a write-off provision would be largest.",
            gm=f"Review the MOQ and lead time of the {top} products in the next plan."),
        action=_per(f"Prioritise {top} batches for offers and release."))


def moq_surplus(f):
    from planning.models import PurchasePlan

    # The latest submitted or approved plan in which MOQ rounding created surplus.
    plan = lines = None
    for candidate in PurchasePlan.objects.filter(status__in=[
            PurchasePlan.APPROVED, PurchasePlan.SUBMITTED]).order_by("-created_at")[:5]:
        lines = [x for x in candidate.lines.select_related("product") if x.moq_surplus_value]
        if lines:
            plan = candidate
            break
    if not plan:
        return None
    lines.sort(key=lambda x: -x.moq_surplus_value)
    total = sum((x.moq_surplus_value for x in lines), Decimal(0))
    plan_value = sum((x.product.unit_cost * x.final_qty for x in plan.lines.select_related(
        "product")), Decimal(0))
    top = lines[0]
    values = [float(x.moq_surplus_value) for x in lines[:8]]
    headline = (f"Supplier minimums add {money(total)} of surplus to {plan.number}, "
                f"{pct(total, plan_value):.0f}% of the plan")
    insight = (f"{top.product.name} alone needs {top.recommended_qty:,} units but the MOQ "
               f"forces {top.rounded_qty:,}, adding {money(top.moq_surplus_value)}.")
    return chart(
        "moq", "hbar", headline,
        f"MOQ surplus value by product in {plan.number} ({plan.get_status_display()})",
        [x.product.name for x in lines[:8]],
        [{"name": "MOQ surplus", "data": values, "colors": emphasis(values, 0)}], "EGP",
        insight,
        implication=_per(
            ceo="This is the forecast-to-MOQ error from the Root Cause Analysis, now visible "
                "before the order rather than after the stock expires.",
            cfo=f"{money(total)} of cash is committed to stock that the forecast does not "
                f"need.",
            coo="Surplus bought now becomes the near-expiry stock the release route must "
                "clear later.",
            gm=f"Before approving, ask whether {top.product.name} can be ordered with a "
               f"lower MOQ, shared with another market, or delayed."),
        action=_per(
            "Open the purchase plan and review the lines with the largest MOQ surplus.",
            gm="Approve, return or override the plan with a recorded reason (FR-48)."))


def recovered_vs_written_off(f):
    months = _months_back(12)
    rec = [float(v) for v in _recovered_by_month(months)]
    wo = [float(v) for v in _written_off_by_month(months)]
    if not any(rec) and not any(wo):
        return None
    tr, tw = sum(rec), sum(wo)
    headline = (f"The platform recovered {money(tr)} while {money(tw)} was written off in "
                f"12 months")
    insight = (f"Recovered value is {tr / tw:.1f} times the write-off value." if tw else
               "No write-off was posted in the last 12 months.")
    return chart(
        "rec_wo", "grouped", headline, "Recovered through B2B sales vs written off, per month",
        [f"{m:%b %y}" for m in months],
        [{"name": "Recovered (B2B sales)", "data": rec, "color": BLUE},
         {"name": "Written off (at cost)", "data": wo, "color": ORANGE}], "EGP", insight,
        implication=_per(
            ceo="The gap between the two bars is the value the proposed solution saves.",
            cfo="Write-offs hit profit directly; recovered sales convert stock back to cash.",
            coo="Months with write-offs and no recovery show where the release route was "
                "too late.",
            gm="Every write-off here needed your approval; the trend shows whether fewer "
               "reach you."),
        action=_per("Review the months with write-offs against the batches that expired."))


def receivables(f):
    from commerce.models import CreditNote, Invoice

    today = timezone.localdate()
    buckets = ["Not yet due", "1-30 days overdue", "31-60 days overdue",
               "More than 60 days"]
    values = [0.0, 0.0, 0.0, 0.0]
    for inv in Invoice.objects.filter(status=Invoice.UNPAID):
        days = (today - inv.due_date).days
        idx = 0 if days <= 0 else 1 if days <= 30 else 2 if days <= 60 else 3
        values[idx] += float(inv.balance)
    refunds = float(CreditNote.objects.filter(status=CreditNote.REFUND_DUE).aggregate(
        t=Sum("amount"))["t"] or 0)
    total = sum(values)
    overdue = total - values[0]
    if not total:
        headline = "No platform invoices are outstanding"
    elif overdue >= total:
        headline = f"All {money(total)} of outstanding receivables is overdue"
    else:
        headline = f"{money(overdue)} of {money(total)} receivables is overdue"
    insight = (f"Outstanding platform invoices total {money(total)}; {money(overdue)} is past "
               f"its due date. Refunds still owed to buyers: {money(refunds)}.")
    return chart(
        "receivables", "bar", headline, "Unpaid platform invoices by age (EGP)", buckets,
        [{"name": "Outstanding", "data": values,
          "colors": [GOOD, WARNING, SERIOUS, CRITICAL]}], "EGP", insight,
        implication=_per(
            "Buyers with overdue invoices are blocked from paying on invoice (FR-84), which "
            "slows clearance sales.",
            cfo="Overdue balances on clearance sales are recovered value that has not yet "
                "turned into cash."),
        action=_per("Open Buyer credit to follow up overdue buyers.",
                    cfo="Chase the overdue balances and pay the refunds due."))


def forecast_error(f):
    from planning.models import ForecastProduct

    by = defaultdict(list)
    for fp in ForecastProduct.objects.select_related("product").filter(mape__isnull=False):
        by[fp.product.name].append(float(fp.mape))
    if not by:
        return None
    items = sorted(((k, sum(v) / len(v)) for k, v in by.items()), key=lambda kv: -kv[1])[:8]
    values = [round(v, 1) for _k, v in items]
    worst, w = items[0]
    avg = sum(values) / len(values)
    headline = f"{worst} is the hardest product to forecast ({w:.0f}% error)"
    insight = (f"Average forecast error (MAPE) across these products is {avg:.0f}%; above "
               f"30% the forecast is flagged as low confidence.")
    return chart(
        "mape", "hbar", headline, "Average forecast error (MAPE) by product, back-tested",
        [k for k, _v in items], [{"name": "MAPE", "data": values,
                                  "colors": [CRITICAL if v > 30 else BLUE_SOFT
                                             for v in values]}], "%", insight,
        implication=_per(
            "A high forecast error combined with a large MOQ is the pattern that produces "
            "surplus.",
            gm=f"Plan lines for {worst} deserve a closer look before approval."),
        action=_per("Review low-confidence products with Procurement before the next plan."))


# ==========================================================================
# Dashboards and summary
# ==========================================================================
def build_charts(builders, f):
    return [c for c in (b(f) for b in builders) if c]


EXECUTIVE = [at_risk_outcomes, expiry_calendar, var_by_category, clearance_funnel,
             moq_surplus, recovered_vs_written_off, sales_trend, receivables]

# Which charts each perspective reads first.
ORDER = {
    "ceo": ["outcomes", "rec_wo", "sales", "expiry", "var_cat", "funnel", "moq",
            "receivables"],
    "cfo": ["outcomes", "receivables", "moq", "rec_wo", "expiry", "var_cat", "sales",
            "funnel"],
    "coo": ["funnel", "expiry", "var_cat", "outcomes", "rec_wo", "sales", "moq",
            "receivables"],
    "gm": ["moq", "outcomes", "expiry", "rec_wo", "var_cat", "funnel", "sales",
           "receivables"],
}


def order_for(charts, perspective):
    rank = {cid: i for i, cid in enumerate(ORDER.get(perspective, []))}
    return sorted(charts, key=lambda c: rank.get(c["id"], 99))


def executive_kpis(charts):
    by = {c["id"]: c for c in charts}
    out = []
    if "outcomes" in by:
        v = by["outcomes"]["series"][0]["data"]
        total = sum(v)
        out += [("Value at risk", money(v[2])), ("Recovered", money(v[0] + v[1])),
                ("Written off", money(v[3])),
                ("Recovered share", f"{pct(v[0] + v[1], total):.0f}%" if total else "–")]
    if "moq" in by:
        out.append(("MOQ surplus in latest plan",
                    money(sum(by["moq"]["series"][0]["data"]))))
    if "receivables" in by:
        v = by["receivables"]["series"][0]["data"]
        out.append(("Overdue receivables", money(sum(v[1:]))))
    return out


def summarise(charts, perspective, title="this dashboard"):
    """Dashboard-level built-in analysis for one perspective."""
    charts = order_for(charts, perspective)
    findings = [c["headline"] + "." for c in charts[:4]]
    actions = []
    for c in charts:
        a = c["analysis"]["action"].get(perspective)
        if a and a not in actions:
            actions.append(a)
    lead = charts[0] if charts else None
    if lead:
        summary = (f"{lead['headline']}. "
                   + (lead["analysis"]["implication"].get(perspective) or ""))
    else:
        summary = "There is not enough data for these filters."
    return {"source": "builtin", "perspective": perspective, "summary": summary.strip(),
            "findings": findings, "actions": actions[:4], "risks": []}


def default_perspective(user):
    from core.models import Role

    if user.has_role(Role.COO):
        return "coo"
    if user.has_role(Role.GM):
        return "gm"
    if user.has_role(Role.FINANCE) or "cfo" in (user.department or "").lower():
        return "cfo"
    return "ceo"


def executive(f):
    charts = build_charts(EXECUTIVE, f)
    return {"kpis": executive_kpis(charts), "tables": [], "charts": charts}


def fmt(v, unit):
    if unit == "EGP":
        return f"{float(v or 0):,.0f} EGP"
    if unit == "%":
        return f"{float(v or 0):,.1f}%"
    return f"{float(v or 0):,.0f} {unit}".strip()


def prepare(charts, perspective):
    """Add what the page needs: the analysis for one perspective and a data table."""
    for c in charts:
        a = c["analysis"]
        c["view"] = {"insight": a["insight"],
                     "implication": a["implication"].get(perspective, ""),
                     "action": a["action"].get(perspective, "")}
        c["rows"] = [[label] + [fmt(s["data"][i], c["unit"]) for s in c["series"]]
                     for i, label in enumerate(c["labels"])]
    return charts
