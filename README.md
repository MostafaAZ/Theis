# Theis — Kubbar International Graduation Project

MOIS 4999 Graduation Project (Fall 2026): an Inventory Management System and a
B2B E-Commerce Platform for near-expiry veterinary stock at Kubbar International.

| Document | |
|---|---|
| [`docs/srs/SRS.md`](docs/srs/SRS.md) | Software Requirements Specification (Markdown rendering; figures in `docs/srs/figures/`) |
| [`docs/srs/MostafaSRSDraft1.docx`](docs/srs/MostafaSRSDraft1.docx) | Original Word document |
| [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) | How the prototype maps to the SRS: modules, use cases, screens, design decisions |

## The prototype

Two web applications built with Django that share **one database** (NFR-01) but
run as **separate applications with separate logins** (NFR-02):

| Application | Users | Default URL | Settings |
|---|---|---|---|
| **IMS** – Inventory Management System | Kubbar staff (11 roles) | http://localhost:8000 | `kubbar.settings.ims` |
| **B2B E-Commerce Platform** | Verified trade buyers | http://localhost:8001 | `kubbar.settings.b2b` |

IMS modules 1–7 (batch control, warehouse audit, expiry monitoring and surplus
release, disposal, forecasting and purchase planning, analytics, administration)
and platform modules 8–10 (buyer verification, catalogue and ordering, payment
and fulfilment) cover all 44 use cases at prototype depth. In line with the
release phasing, the prototype reads Odoo **exports** (Excel/CSV) rather than
the Odoo API.

### Run it

Requires Python 3.11+ ([python.org](https://www.python.org/downloads/); on Windows tick
"Add python.exe to PATH").

**Quickest way:** download the repository (*Code → Download ZIP*, then unzip) and
double-click **`start.bat`** on Windows or **`start.command`** on macOS
(Linux: `./start.command`). The first run sets everything up and loads the demo
data; then both apps open in your browser. Close the app windows (Windows) or
press Ctrl+C (macOS/Linux) to stop.

On macOS, if the file won't open the first time, right-click it → *Open* → *Open*.

**Manually:**

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo           # demo data; add --reset to start over

# two terminals:
python manage.py runserver 8000                                  # IMS
python manage.py runserver 8001 --settings=kubbar.settings.b2b   # B2B platform
```

Everything is served locally (Bootstrap, icons and Chart.js are vendored in
`core/static/vendor`), so the demo works offline. Emails print to the console.

### Demo accounts

Every account uses the password **`Kubbar2026demo`**.

**IMS** (`http://localhost:8000`). Internal users have two-factor login: after
the password, enter the 6-digit code. In development the code is also shown
on screen and printed in the server console.

| Email | Role |
|---|---|
| `sales@kubbar.example` | Sales Representative (vaccines and medicaments) |
| `sales2@kubbar.example` | Sales Representative (feed additives and nutrition) |
| `warehouse@kubbar.example` | Warehouse Officer |
| `inventory@kubbar.example` | Inventory Controller |
| `qa@kubbar.example` | QA Officer |
| `procurement@kubbar.example` | Procurement Officer |
| `regulatory@kubbar.example` | Regulatory Affairs Officer |
| `finance@kubbar.example` | Finance Officer |
| `gm@kubbar.example` | General Manager |
| `coo@kubbar.example` | Management (COO) |
| `ceo@kubbar.example` | Management (CEO) |
| `admin@kubbar.example` | System Administrator |

**B2B platform** (`http://localhost:8001`)

| Email | State |
|---|---|
| `buyer@vetclinic.example` | Verified, cold-chain eligible, bank transfer |
| `buyer@sinaifarms.example` | Verified, medicaments and feed additives, 30-day invoice terms |
| `buyer@newdistrib.example` | Pending verification (prices hidden) |
| `buyer@sharkia.example` | Pending, matches an existing Kubbar customer (FR-70) |

### A five-minute walkthrough

1. **COO** → *Release requests*: approve a surplus batch. A price outside 65–70%
   of the original is refused. The batch is published to the platform.
2. **buyer@vetclinic** on the platform → *Catalogue* → add to cart → checkout →
   upload a transfer receipt (any PDF/JPG/PNG).
3. **Finance** → *Payment queue* → confirm; an invoice is issued.
4. **Warehouse** → *Dispatch queue* → type the allocated lot number → dispatch.
   The buyer can now confirm delivery and download the invoice.
5. **Sales** → *Near-expiry stock* → review a batch, record a customer offer,
   flag the remainder as surplus.
6. **Inventory Controller** → *Audits* → reconcile CNT-00001 (one flagged variance).
7. **Procurement** → *Data import* → upload `sample_data/sales_history_sample.csv`
   (regions are standardised, bad rows rejected, "Ismailia" must be mapped) →
   *Demand forecast* → generate → build a purchase plan and see the MOQ surplus.
8. **GM** → *Pending approvals* → approve the plan; **Admin** → *Business rules*.

### Daily jobs

The expiry scan, reservation expiry, licence checks and document retention run
once a day (UC-14). Schedule it at 06:00 Cairo time:

```cron
0 6 * * *  cd /path/to/Theis && .venv/bin/python manage.py run_daily_jobs
```

It can also be run from the IMS (*Expiry scans* → *Run scan now*).

### Tests

```bash
python manage.py test --settings=kubbar.settings.test
```

70 tests cover the business rules (FEFO, price band, atomic reservation,
tolerance and approval thresholds, dual-approval write-offs, forecasting and MOQ
rounding, imports, encryption, lockout, two-factor login, role-based access,
separation of the two applications) and render every screen for every role.

### Project layout

```
kubbar/      settings (base, ims, b2b, test) and URL roots for the two apps
core/        users and roles, reference data, business rules, audit trail,
             notifications, encryption, demo seed and daily-jobs commands
inventory/   Modules 1–4: batches, stock, audits, expiry, release, listings, write-offs
planning/    Module 5: Odoo import, forecasting, purchase plans
analytics/   Module 6: dashboards and PDF/Excel export
commerce/    Modules 8–10: buyers, documents, orders, payments, invoices
ims/         IMS screens (views, forms, templates, role-based navigation)
b2b/         Platform screens (views, forms, templates, Arabic/English)
sample_data/ Odoo-style export files for the import screen
```

### Production notes

- Set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` and a Fernet
  `FIELD_ENCRYPTION_KEY` (documents and national IDs are encrypted with it).
- Use PostgreSQL (`DATABASE_ENGINE=postgresql` plus `DATABASE_*`), a real
  `DJANGO_EMAIL_BACKEND`, and serve both apps over HTTPS (NFR-22).
- Run each app with its WSGI module: `kubbar.wsgi_ims` and `kubbar.wsgi_b2b`.
