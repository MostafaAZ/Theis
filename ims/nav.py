"""Navigation menu filtered by role, so users only see what they can do."""
from core.models import Role

from .access import ANALYTICS, AUDIT_TRAIL, EXPIRY_VIEW

R = Role
MENU = [
    ("Overview", [
        ("Dashboard", "/", None),
        ("Pending approvals", "/approvals/", (R.GM, R.QA)),
        ("Stock search", "/stock/", None),
    ]),
    ("Inventory & batches", [
        ("Batch inspection", "/inspections/", (R.QA,)),
        ("Stock receipt", "/stock/receipt/", (R.INVENTORY,)),
        ("Stock transfer", "/stock/transfer/", (R.WAREHOUSE, R.INVENTORY)),
        ("Dispatch queue", "/dispatch/", (R.WAREHOUSE,)),
        ("Field dispatch (FEFO)", "/dispatch/field/", (R.WAREHOUSE,)),
    ]),
    ("Warehouse audit", [
        ("Audits", "/audits/", (R.INVENTORY, R.GM)),
        ("My counts", "/audits/mine/", (R.WAREHOUSE,)),
    ]),
    ("Expiry & clearance", [
        ("Near-expiry stock", "/expiry/", EXPIRY_VIEW),
        ("Release requests", "/releases/", (R.COO, R.MANAGEMENT)),
        ("Active listings", "/listings/", (R.COO, R.MANAGEMENT, R.SALES)),
        ("Product registrations", "/registrations/", (R.REGULATORY, R.COO)),
        ("Expiry scans", "/scans/", (R.ADMIN, R.INVENTORY)),
    ]),
    ("Disposal", [
        ("Write-offs", "/write-offs/", (R.INVENTORY, R.GM, R.QA)),
    ]),
    ("Planning", [
        ("Data import", "/imports/", (R.PROCUREMENT,)),
        ("Demand forecast", "/forecasts/", (R.PROCUREMENT, R.GM)),
        ("Purchase plans", "/plans/", (R.PROCUREMENT, R.GM)),
    ]),
    ("Analytics", [
        ("Dashboards", "/analytics/", ANALYTICS),
    ]),
    ("B2B platform", [
        ("Buyer verification", "/buyers/", (R.REGULATORY, R.FINANCE)),
        ("Payment queue", "/payments/", (R.FINANCE,)),
        ("Buyer credit", "/credit/", (R.FINANCE,)),
    ]),
    ("Administration", [
        ("Users and roles", "/admin/users/", (R.ADMIN,)),
        ("Business rules", "/admin/rules/", (R.ADMIN,)),
        ("Reference data", "/admin/reference/", (R.ADMIN,)),
        ("Audit trail", "/admin/audit/", AUDIT_TRAIL),
    ]),
]


def nav_for(user):
    sections = []
    for title, items in MENU:
        visible = [(label, url) for label, url, roles in items
                   if roles is None or user.has_role(*roles)]
        if visible:
            sections.append((title, visible))
    return sections
