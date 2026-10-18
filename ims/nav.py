"""Navigation, page titles and in-app help for the IMS.

The sidebar is built from MENU and filtered by role, so users only see what
they can do. PAGES gives every screen a module, a one-line purpose and short
"how to" steps, shown in the page header and the Help panel.
"""
from core.models import Role

from .access import ANALYTICS, AUDIT_TRAIL, EXPIRY_VIEW

R = Role

# (module title, icon, [(label, url, roles or None for everyone, badge key)])
MENU = [
    ("Home", "bi-house-door", [
        ("Dashboard", "/", None, None),
        ("Approvals", "/approvals/", (R.GM, R.QA), "approvals"),
        ("Notifications", "/notifications/", None, "notifications"),
    ]),
    ("Inventory", "bi-box-seam", [
        ("Stock overview", "/stock/", None, None),
        ("Products", "/products/", None, None),
        ("Batch inspection", "/inspections/", (R.QA,), None),
        ("Stock receipt", "/stock/receipt/", (R.INVENTORY,), "receipts"),
        ("Stock transfer", "/stock/transfer/", (R.WAREHOUSE, R.INVENTORY), None),
        ("Field dispatch (FEFO)", "/dispatch/field/", (R.WAREHOUSE,), None),
    ]),
    ("Expiry & clearance", "bi-hourglass-split", [
        ("Near-expiry stock", "/expiry/", EXPIRY_VIEW, "near_expiry"),
        ("Post to B2B", "/b2b/post/", (R.SALES, R.COO), None),
        ("Release requests", "/releases/", (R.COO, R.MANAGEMENT), "releases"),
        ("B2B listings", "/listings/", None, None),
        ("Product registrations", "/registrations/", (R.REGULATORY, R.COO), None),
        ("Expiry scans", "/scans/", (R.ADMIN, R.INVENTORY), None),
    ]),
    ("B2B sales", "bi-shop", [
        ("Platform orders", "/b2b/orders/", None, None),
        ("Dispatch queue", "/dispatch/", (R.WAREHOUSE,), "dispatch"),
        ("Payment queue", "/payments/", (R.FINANCE,), "payments"),
        ("Buyers", "/buyers/", (R.REGULATORY, R.FINANCE, R.SALES, R.ADMIN), "buyers"),
        ("Buyer credit", "/credit/", (R.FINANCE,), None),
        ("Buyer messages", "/b2b/messages/", (R.SALES, R.REGULATORY, R.FINANCE, R.WAREHOUSE,
                                              R.COO), "messages"),
    ]),
    ("Warehouse audit", "bi-clipboard-check", [
        ("Audits", "/audits/", (R.INVENTORY, R.GM), None),
        ("My counts", "/audits/mine/", (R.WAREHOUSE,), "my_counts"),
    ]),
    ("Disposal", "bi-trash3", [
        ("Write-offs", "/write-offs/", (R.INVENTORY, R.GM, R.QA), "write_offs"),
    ]),
    ("Purchasing", "bi-cart-check", [
        ("Purchase orders", "/purchase-orders/", (R.PROCUREMENT, R.GM, R.QA, R.FINANCE), None),
        ("Data import", "/imports/", (R.PROCUREMENT,), None),
        ("Demand forecast", "/forecasts/", (R.PROCUREMENT, R.GM), None),
        ("Purchase plans", "/plans/", (R.PROCUREMENT, R.GM), "plans"),
        ("Suppliers", "/suppliers/", (R.PROCUREMENT, R.GM, R.ADMIN), None),
    ]),
    ("Customers", "bi-people", [
        ("Customers", "/customers/", (R.SALES, R.FINANCE, R.GM, R.ADMIN, R.MANAGEMENT, R.COO),
         None),
    ]),
    ("Reports", "bi-bar-chart-line", [
        ("Analytics dashboards", "/analytics/", ANALYTICS, None),
        ("Reports & exports", "/reports/", None, None),
    ]),
    ("Administration", "bi-gear", [
        ("Users and roles", "/admin/users/", (R.ADMIN,), None),
        ("Business rules", "/admin/rules/", (R.ADMIN,), None),
        ("Reference data", "/admin/reference/", (R.ADMIN,), None),
        ("Audit trail", "/admin/audit/", AUDIT_TRAIL, None),
    ]),
]

# url_name: (module, page title, purpose, [steps])
PAGES = {
    "dashboard": ("Home", "Dashboard", "Your tasks, alerts and the key numbers for your role.",
                  ["Start with 'Your tasks': each line opens the screen where the work is done.",
                   "Cards show live totals; click one to see the records behind it."]),
    "approvals": ("Home", "Approvals", "Everything waiting for your decision in one place.",
                  ["Open an item, read the details, then approve or reject with a comment."]),
    "notifications": ("Home", "Notifications", "Every alert sent to you, newest first.", []),
    "profile": ("Home", "My profile", "Your details and password.", []),
    "search": ("Home", "Search", "Find products, lots, orders, buyers and purchase orders.", []),
    "stock_search": ("Inventory", "Stock overview",
                     "Every batch on hand, with quantity, location, expiry and status.",
                     ["Filter by product, category, warehouse or days to expiry.",
                      "Click a product to open its batch record and movement history.",
                      "Export the filtered list to Excel or PDF."]),
    "batch_detail": ("Inventory", "Batch record", "One lot: quantities, locations and history.",
                     []),
    "products": ("Inventory", "Products", "The product catalogue with stock and status.",
                 ["Search or filter by category.", "Open a product to see its batches, "
                  "registrations, sales and B2B listings.", "Add or edit products here."]),
    "product_detail": ("Inventory", "Product", "Everything about one product.", []),
    "product_new": ("Inventory", "New product", "Add a product to the catalogue.", []),
    "product_edit": ("Inventory", "Edit product", "Change product details and image.", []),
    "inspections": ("Inventory", "Batch inspection",
                    "Record lot, dates and the inspection result for each received batch.",
                    ["Choose the purchase order and product.",
                     "Enter lot, manufacture and expiry dates and the quantities.",
                     "Save: the Inventory Controller is notified to book it in."]),
    "receipts": ("Inventory", "Stock receipt",
                 "Book QA-approved batches into a warehouse location.",
                 ["Click 'Book in' on a batch.", "Pick a compatible bin and confirm."]),
    "receipt": ("Inventory", "Book in batch", "Choose the storage location for this batch.",
                []),
    "transfer": ("Inventory", "Stock transfer", "Move stock between bins or warehouses.", []),
    "field_dispatch": ("Inventory", "Field dispatch",
                       "Dispatch to a field customer with earliest-expiry-first allocation.",
                       ["Choose product and quantity.", "Preview the allocation, then confirm."]),
    "near_expiry": ("Expiry & clearance", "Near-expiry stock",
                    "Batches within 6 months of expiry and what to do with them.",
                    ["Open a batch and offer it to existing customers first.",
                     "Flag what they cannot absorb as surplus, or use 'Post to B2B'.",
                     "Unsold stock that expires goes to write-off."]),
    "batch_review": ("Expiry & clearance", "Batch review",
                     "Offer to customers, flag surplus, or keep under watch.", []),
    "post_batch": ("Expiry & clearance", "Post to B2B", "Set the quantity and price.", []),
    "platform_order": ("B2B sales", "Platform order", "Order details and status.", []),
    "messages_inbox": ("B2B sales", "Buyer messages",
                       "Questions sent from the platform's Contact page.",
                       ["Open a message, write the reply, send: the buyer gets it by email "
                        "and in their account."]),
    "message_detail": ("B2B sales", "Buyer message", "Read and reply.", []),
    "supplier_new": ("Purchasing", "New supplier", "", []),
    "customer_new": ("Customers", "New customer", "", []),
    "post_b2b": ("Expiry & clearance", "Post to B2B",
                 "Put a near-expiry batch on the B2B clearance platform.",
                 ["Pick an eligible batch (near-expiry, registered, more than 30 days left).",
                  "Enter the quantity and price (65–70% of the original price).",
                  "COO: the listing goes live immediately. Sales: it goes to the COO "
                  "for approval."]),
    "releases": ("Expiry & clearance", "Release requests",
                 "Surplus batches waiting for the COO to release them to B2B.", []),
    "release_decide": ("Expiry & clearance", "Release decision",
                       "Approve the release and set the clearance price.", []),
    "listings": ("Expiry & clearance", "B2B listings",
                 "Everything currently or previously offered on the platform.", []),
    "listing_manage": ("Expiry & clearance", "Manage listing",
                       "Reprice, change quantity or withdraw.", []),
    "registrations": ("Expiry & clearance", "Product registrations",
                      "EDA / GOVS registration status per product and market.", []),
    "scans": ("Expiry & clearance", "Expiry scans",
              "The daily scan that raises near-expiry and expiry alerts.", []),
    "platform_orders": ("B2B sales", "Platform orders",
                        "All orders placed by buyers on the B2B platform.", []),
    "dispatch_queue": ("B2B sales", "Dispatch queue",
                       "Paid platform orders waiting to be picked and shipped.",
                       ["Open an order, pick the listed lot, type the lot number, dispatch."]),
    "dispatch_order": ("B2B sales", "Dispatch order", "Pick, check the lot and dispatch.", []),
    "payments": ("B2B sales", "Payment queue",
                 "Transfer receipts and credit orders waiting for Finance.", []),
    "payment_detail": ("B2B sales", "Confirm payment", "Check the receipt, then confirm.", []),
    "buyers": ("B2B sales", "Buyers", "Trade buyers registered on the platform.",
               ["Pending buyers need document review and verification."]),
    "buyer_detail": ("B2B sales", "Buyer", "Company details, documents and decision.", []),
    "credit": ("B2B sales", "Buyer credit", "Payment terms and credit limits.", []),
    "credit_edit": ("B2B sales", "Payment terms", "Set terms and credit limit.", []),
    "audits": ("Warehouse audit", "Audits", "Full and cycle counts and their results.",
               ["'New count' schedules a count and assigns a Warehouse Officer.",
                "After counting, reconcile flagged variances."]),
    "audit_schedule": ("Warehouse audit", "Schedule count", "Plan a full or cycle count.", []),
    "audit_detail": ("Warehouse audit", "Count", "Lines, variances and status.", []),
    "my_counts": ("Warehouse audit", "My counts", "Counts assigned to you.", []),
    "count_sheet": ("Warehouse audit", "Count sheet",
                    "Blind count: enter what is physically there.", []),
    "reconcile": ("Warehouse audit", "Reconcile",
                  "Explain each flagged variance or ask for a recount.", []),
    "adjustment": ("Warehouse audit", "Adjustment approval", "Approve or reject.", []),
    "write_offs": ("Disposal", "Write-offs",
                   "Expired, damaged or rejected stock awaiting disposal.",
                   ["Inventory Controller requests; GM and QA both approve; QA records "
                    "disposal."]),
    "write_off_new": ("Disposal", "Request write-off", "Block stock and ask for approval.", []),
    "write_off": ("Disposal", "Write-off", "Approvals and disposal record.", []),
    "purchase_orders": ("Purchasing", "Purchase orders",
                        "Orders placed with suppliers and what has been received.",
                        ["Create a PO by hand, or from an approved purchase plan.",
                         "QA inspects deliveries against the PO."]),
    "po_new": ("Purchasing", "New purchase order", "Order from a supplier.", []),
    "po_detail": ("Purchasing", "Purchase order", "Lines, receipts and status.", []),
    "imports": ("Purchasing", "Data import", "Load Odoo exports (Excel or CSV).",
                ["Pick the data type, upload, check the preview, confirm."]),
    "import_preview": ("Purchasing", "Import preview", "Check before importing.", []),
    "forecasts": ("Purchasing", "Demand forecast",
                  "Monthly demand by product and region from sales history.", []),
    "forecast_detail": ("Purchasing", "Forecast", "Forecast vs actual, and build a plan.", []),
    "plans": ("Purchasing", "Purchase plans",
              "Recommended order quantities and the surplus MOQ rounding creates.", []),
    "plan_detail": ("Purchasing", "Purchase plan", "Review, override, submit, approve.", []),
    "suppliers": ("Purchasing", "Suppliers", "Overseas partners and lead times.", []),
    "supplier_edit": ("Purchasing", "Supplier", "Supplier details.", []),
    "customers": ("Customers", "Customers", "Existing field-sales customers from Odoo.", []),
    "customer_detail": ("Customers", "Customer", "Purchases and offers for this customer.",
                        []),
    "customer_edit": ("Customers", "Customer", "Customer details.", []),
    "analytics": ("Reports", "Analytics", "Six dashboards with filters and export.", []),
    "reports": ("Reports", "Reports & exports", "Ready-made reports to download.", []),
    "users": ("Administration", "Users and roles", "Internal accounts and buyer accounts.", []),
    "user_new": ("Administration", "New user", "Create an internal account.", []),
    "user_edit": ("Administration", "Edit user", "Roles, status and unlock.", []),
    "rules": ("Administration", "Business rules", "Thresholds the system applies.", []),
    "rule_edit": ("Administration", "Change rule", "Needs a Management approver.", []),
    "reference": ("Administration", "Reference data", "Lists used by every dropdown.", []),
    "reference_list": ("Administration", "Reference data", "Entries in this list.", []),
    "reference_new": ("Administration", "Reference data", "Add an entry.", []),
    "reference_edit": ("Administration", "Reference data", "Edit an entry.", []),
    "registration_new": ("Expiry & clearance", "New registration", "", []),
    "registration_edit": ("Expiry & clearance", "Edit registration", "", []),
    "audit_trail": ("Administration", "Audit trail", "Who did what, and when.", []),
}

MODULE_ICON = {title: icon for title, icon, _items in MENU}


def badge_counts(user):
    """Live counts shown next to menu items."""
    from commerce.models import Buyer, PlatformOrder
    from inventory.models import (Batch, BatchStatus, StockCount, StockCountLine,
                                  SurplusRelease, WriteOff)
    from planning.models import PurchasePlan

    from commerce.models import SupportMessage

    c = {"notifications": user.notifications.filter(is_read=False).count(),
         "messages": SupportMessage.objects.filter(status=SupportMessage.OPEN).count()}
    if user.has_role(R.GM, R.QA):
        n = 0
        if user.has_role(R.GM):
            n += StockCountLine.objects.filter(status=StockCountLine.AWAITING).count()
            n += PurchasePlan.objects.filter(status=PurchasePlan.SUBMITTED).count()
            n += WriteOff.objects.filter(status=WriteOff.PENDING,
                                         gm_approved_by__isnull=True).count()
        if user.has_role(R.QA):
            n += WriteOff.objects.filter(status=WriteOff.PENDING,
                                         qa_approved_by__isnull=True).count()
        c["approvals"] = n
    if user.has_role(R.INVENTORY):
        c["receipts"] = Batch.objects.filter(status=BatchStatus.INSPECTED).count()
        c["write_offs"] = Batch.objects.filter(status=BatchStatus.EXPIRED,
                                               qty_on_hand__gt=0).count()
    if user.has_role(*EXPIRY_VIEW):
        c["near_expiry"] = Batch.objects.filter(status=BatchStatus.NEAR_EXPIRY,
                                                qty_on_hand__gt=0).count()
    if user.has_role(R.COO, R.MANAGEMENT):
        c["releases"] = SurplusRelease.objects.filter(status=SurplusRelease.PENDING).count()
    if user.has_role(R.WAREHOUSE):
        c["dispatch"] = PlatformOrder.objects.filter(status=PlatformOrder.CONFIRMED).count()
        c["my_counts"] = StockCount.objects.filter(
            assigned_to=user, status__in=[StockCount.SCHEDULED, StockCount.IN_PROGRESS]).count()
    if user.has_role(R.FINANCE):
        c["payments"] = PlatformOrder.objects.filter(
            status=PlatformOrder.AWAITING_PAYMENT).count()
    if user.has_role(R.REGULATORY):
        c["buyers"] = Buyer.objects.filter(status=Buyer.PENDING).count()
    if user.has_role(R.GM, R.PROCUREMENT):
        c["plans"] = PurchasePlan.objects.filter(
            status=PurchasePlan.SUBMITTED if user.has_role(R.GM) else PurchasePlan.RETURNED
        ).count()
    return c


def nav_for(user, path="/"):
    counts = badge_counts(user)
    sections = []
    for title, icon, items in MENU:
        visible = []
        for label, url, roles, badge in items:
            if roles is None or user.has_role(*roles):
                active = path == url or (url != "/" and path.startswith(url)
                                         and not any(path.startswith(u) and len(u) > len(url)
                                                     for _l, u, _r, _b in items))
                visible.append({"label": label, "url": url, "badge": counts.get(badge) or 0,
                                "active": active})
        if visible:
            sections.append({"title": title, "icon": icon, "items": visible,
                             "open": any(i["active"] for i in visible)})
    return sections


def page_info(url_name):
    module, title, purpose, steps = PAGES.get(url_name, ("", "", "", []))
    return {"module": module, "title": title, "purpose": purpose, "steps": steps,
            "icon": MODULE_ICON.get(module, "bi-circle")}
