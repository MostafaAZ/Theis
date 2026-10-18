# Implementation notes: SRS to prototype

This document maps the SRS to the code and records the decisions taken where
the SRS left something open.

## Architecture

- **Two applications, one database (NFR-01, NFR-02).** One Django project with
  two settings modules. `kubbar.settings.ims` loads only the IMS URLs and
  `kubbar.settings.b2b` only the platform URLs. Each has its own session cookie
  and timeout (15 and 30 minutes, NFR-24). `core.middleware.AppBoundaryMiddleware`
  logs out any session that belongs to the other application, so a buyer
  account can never reach an internal screen.
- **Domain logic lives in services**, not views: `inventory/services.py`,
  `planning/services.py`, `commerce/services.py`. Every stock change runs in a
  transaction and writes a `StockMovement` (FR-17). Approvals, price changes,
  adjustments, write-offs, verification decisions and configuration changes
  write an append-only `AuditLog` entry (FR-54, NFR-27).
- **Business rules (Exhibit #4)** are rows in `BusinessRule`, read through
  `core.rules.rule()`. Changing one in *Administration → Business rules*
  records the approving manager (FR-52, NFR-20).

## Use cases to screens

| UC | Screen (IMS unless noted) | Main code |
|---|---|---|
| 01–02 | `/login/` → `/login/verify/` (one-time code), log out | `ims/views/auth.py` |
| 03 | `/` role dashboard and alerts | `analytics.services.dashboard_cards` |
| 04 | `/inspections/` | `inventory.services.record_inspection` |
| 05 | `/stock/receipt/` | `receive_batch` |
| 06 | `/stock/`, `/stock/batch/<id>/` | `ims/views/stock.py` |
| 07 | `/stock/transfer/` | `transfer_stock` |
| 08–09 | `/dispatch/`, `/dispatch/<order>/`, `/dispatch/field/` | `dispatch_platform_order`, `fefo_allocate` |
| 10–13 | `/audits/`, `/audits/new/`, `/audits/<id>/count/`, `/audits/<id>/reconcile/`, `/approvals/` | `schedule_count`, `submit_count`, `reconcile_count`, `decide_adjustment` |
| 14 | `/scans/` and `manage.py run_daily_jobs` | `run_expiry_scan` |
| 15–16 | `/expiry/`, `/expiry/batch/<id>/` | `record_offer`, `flag_surplus`, `projected_unsold` |
| 17–19 | `/releases/<id>/` | `approve_release`, `listing_blockers`, `registration_check` |
| 20 | `/registrations/` | `ims/views/expiry.py` |
| 21–22 | `/write-offs/` | `request_write_off`, `approve_write_off`, `record_disposal` |
| 23 | `/imports/` | `planning.services.stage_import`, `confirm_import` |
| 24 | `/forecasts/` | `generate_forecast`, `planning/forecasting.py` |
| 25–26 | `/plans/<id>/` | `build_plan`, `plan_line_figures`, `decide_plan` |
| 27–28 | `/analytics/` (six dashboards, PDF/Excel export) | `analytics/services.py`, `analytics/exports.py` |
| 29–30 | Platform `/register/`, `/account/` | `commerce.services.register_buyer`, `save_document` |
| 31 | `/buyers/<id>/` | `review_document`, `verify_buyer` |
| 32 | Platform `/login/`, `/password-reset/`, `/unlock/<token>/` | `b2b/views.py` |
| 33 | Platform `/catalogue/` | `catalogue_for` |
| 34–35 | Platform `/cart/` | `place_order` (atomic reservation) |
| 36 | Platform `/orders/<id>/pay/` | `submit_bank_transfer`, `choose_invoice` |
| 37 | Platform `/orders/`, `/orders/<id>/` | `cancel_order`, `confirm_delivery` |
| 38 | `/payments/` | `confirm_payment`, `reject_payment` |
| 39 | `/credit/` | `set_payment_terms` |
| 40 | `/listings/<id>/` | `reprice_listing`, `change_listing_quantity` |
| 41–44 | `/admin/users/`, `/admin/rules/`, `/admin/reference/`, `/admin/audit/` | `ims/views/admin.py` |

## Screens added beyond the use cases

These make the system easier to use and close gaps found in user testing.

| Screen | Where | Code |
|---|---|---|
| Role dashboard with "Your tasks", KPIs, quick actions, charts | IMS `/` | `ims/dashboard.py` |
| Post to B2B (one guided flow; COO publishes, Sales requests) | IMS `/b2b/post/` | `inventory.services.post_to_b2b` |
| Products (list, detail with batches/sales/registrations/listings, create, edit, image) | IMS `/products/` | `ims/views/catalog.py` |
| Customers, suppliers | IMS `/customers/`, `/suppliers/` | `ims/views/catalog.py` |
| Purchase orders (create, from approved plan, close) | IMS `/purchase-orders/` | `inventory.services.create_purchase_order`, `purchase_orders_from_plan` |
| Platform orders overview | IMS `/b2b/orders/` | `ims/views/b2b.py` |
| Buyer messages inbox and replies | IMS `/b2b/messages/` | `commerce.models.SupportMessage` |
| Global search, My profile, Reports & exports | IMS `/search/`, `/profile/`, `/reports/` | `ims/views/misc.py` |
| Page purpose, breadcrumbs and "How it works" help on every screen | IMS | `ims/nav.py` (`PAGES`) |
| Storefront home, category browsing, product page, account area, contact, FAQ, terms, privacy | Platform | `b2b/shop.py` |

## Algorithms

- **FEFO (FR-14, UC-09).** Eligible batches are In Stock or Near-Expiry, not
  frozen for a count, and above the minimum sellable shelf life. They are taken
  in expiry order. Stock that is reserved, blocked for write-off, or offered on
  the platform is never allocated twice.
- **Forecast (FR-43, FR-44).** Additive Holt-Winters with a 12-month season and
  a small grid search over the smoothing parameters when 24 months of history
  exist. A 6-month moving average is used from 6 to 23 months; below 6 months
  the officer enters a manual estimate. MAPE is back-tested on the last 6 months.
- **Recommended order (FR-45 to FR-47).** Forecast demand over (lead time +
  coverage period), plus safety stock, minus usable stock, minus open purchase
  orders. Usable stock simulates FEFO sell-down at the forecast rate and
  excludes whatever would reach Near-Expiry first. The result is rounded up to
  the MOQ, and the plan shows the MOQ surplus in units and EGP and the quantity
  projected to be left at expiry ("At Risk of Surplus").
- **Reservation (NFR-09, NFR-10).** One conditional `UPDATE … WHERE
  available_qty >= n` per line, so two buyers racing for the last units cannot
  both succeed. The second buyer is offered whatever remains (UC-35 A1).

## Decisions where the SRS was open

These close the gaps noted in the SRS review.

1. **Order statuses** follow FR-86 exactly. "Awaiting Payment" covers both
   bank-transfer confirmation and credit approval; the screen says which.
2. **Approval threshold** is "at or above" 10,000 EGP, as in UC-12 A2.
3. **Variances within tolerance** are posted automatically, so the record
   matches the physical count. Only flagged variances need a reason code.
4. **Reservation hold** applies to bank-transfer *and* invoice orders, until
   Finance confirms. Both are cancelled automatically when the hold lapses.
5. **MOQ rounding** means "at least the MOQ" (`max(recommended, MOQ)`), not
   multiples of the MOQ.
6. **Thresholds that were hard-coded in the SRS are configurable rules too:**
   coverage period, safety stock, low-confidence MAPE, registration and licence
   alert lead times, rejected-document retention, and the failed-login limit.
7. **A rejected write-off** keeps its quantity blocked until the Inventory
   Controller resubmits or cancels the request (UC-22 A1).
8. **Cold-chain storage**: refrigerated and frozen stock must go to matching
   locations. Ambient stock may use any location except frozen.
9. **Withdrawn listings** keep existing orders (FR-80). Only new orders stop.

## Not in the prototype (full release)

- Odoo API synchronisation (FR-87 to FR-89). The invoice model has a
  `synced_to_odoo` flag ready for it, and imports already write `Data Sync`
  audit entries.
- Arabic translation of product category names and of IMS screens. NFR-04 asks
  for an English IMS; the platform is fully bilingual.
- Real 2FA delivery by SMS. Codes are sent by email.
