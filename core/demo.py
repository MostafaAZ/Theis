"""Presentation mode: who to show and what to show for each demo user.

Used only when settings.DEMO_MODE is on (the start-presentation launchers)."""
from django.conf import settings
from django.http import Http404

# email: (headline, [what to show])
STAFF = {
    "sales@kubbar.example": ("Offers near-expiry stock to customers first", [
        "Dashboard: near-expiry batches waiting for review",
        "Near-expiry stock → open a batch → record a customer offer",
        "Post to B2B → send a batch to the COO for approval"]),
    "sales2@kubbar.example": ("Sales for feed additives and nutrition", [
        "Sees only his own categories and regions",
        "Analytics → Sales Performance for his scope"]),
    "coo@kubbar.example": ("Releases surplus to B2B and sets the price", [
        "Release requests → approve; a price outside 65–70% is refused",
        "Post to B2B → publish a batch directly",
        "B2B listings → reprice or withdraw"]),
    "gm@kubbar.example": ("Approves plans, adjustments and write-offs", [
        "Approvals → purchase plan with its MOQ surplus",
        "Approvals → write-off (QA gives the second approval)",
        "Analytics → value at risk and recovery"]),
    "inventory@kubbar.example": ("Keeps the stock record true", [
        "Stock receipt → book a QA-approved batch into a bin",
        "Audits → reconcile CNT-00001 (flagged variance)",
        "Write-offs → request one for an expired batch"]),
    "warehouse@kubbar.example": ("Picks, dispatches and counts", [
        "Dispatch queue → type the allocated lot → dispatch",
        "My counts → blind count sheet (system quantity hidden)",
        "Field dispatch → earliest-expiry-first allocation"]),
    "qa@kubbar.example": ("Inspects deliveries and co-approves disposal", [
        "Batch inspection → record lot and expiry for a PO",
        "Approvals → second approval on a write-off",
        "Write-offs → record disposal and certificate"]),
    "procurement@kubbar.example": ("Forecasts demand and plans purchases", [
        "Data import → sample_data/sales_history_sample.csv",
        "Demand forecast → generate → build a purchase plan",
        "Purchase orders → create from the approved plan"]),
    "regulatory@kubbar.example": ("Registrations and buyer verification", [
        "Buyers → verify a pending buyer's documents",
        "Product registrations → a suspended registration blocks listing"]),
    "finance@kubbar.example": ("Payments, invoices and credit", [
        "Payment queue → confirm a transfer receipt; invoice issued",
        "Buyer credit → payment terms and limits",
        "Overdue invoice blocks pay-on-invoice"]),
    "ceo@kubbar.example": ("Management overview", [
        "Dashboard KPIs and charts",
        "Analytics dashboards and Reports → PDF / Excel"]),
    "admin@kubbar.example": ("Users, rules and the audit trail", [
        "Users and roles", "Business rules (65–70%, 180 days, 10,000 EGP…)",
        "Audit trail – every approval and price change"]),
}

BUYERS = {
    "buyer@vetclinic.example": ("Verified clinic, cold-chain eligible", [
        "Catalogue with prices → add to cart → checkout",
        "Pay by transfer receipt; track the order; download the invoice"]),
    "buyer@sinaifarms.example": ("Verified farm with 30-day credit", [
        "Has an overdue invoice → Pay on Invoice is blocked",
        "Cannot buy vaccines (not cold-chain eligible)"]),
    "buyer@newdistrib.example": ("Pending verification", [
        "Prices hidden until Regulatory Affairs verifies the account"]),
    "buyer@sharkia.example": ("Pending; matches an existing customer", [
        "Shows how a field-sales customer is flagged to Sales"]),
}


def require_demo():
    if not getattr(settings, "DEMO_MODE", False):
        raise Http404


def staff_cards():
    from core.models import User

    users = {u.email: u for u in User.objects.filter(account_type=User.INTERNAL,
                                                     is_active=True)}
    return [{"user": users[e], "headline": h, "tips": t}
            for e, (h, t) in STAFF.items() if e in users]


def buyer_cards():
    from core.models import User

    users = {u.email: u for u in User.objects.filter(account_type=User.BUYER)
             .select_related("buyer")}
    return [{"user": users[e], "headline": h, "tips": t}
            for e, (h, t) in BUYERS.items() if e in users]
