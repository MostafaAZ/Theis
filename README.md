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

On macOS the first double-click shows *"start.command" Not Opened* because the
script isn't signed by Apple. Click **Done**, then either:

- open **System Settings → Privacy & Security**, click **Open Anyway** next to the
  message about `start.command`, and double-click it again; or
- open **Terminal**, type `bash ` (with a space), drag `start.command` into the
  window and press Enter.

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

### Presentation mode

For a live demo, start **`start-presentation.command`** (macOS) or
**`start-presentation.bat`** (Windows) instead of the normal launcher. It opens the same
system with:

- a login page that shows **every user as a card**: click one to log in, with no
  password or code. Staff open the IMS; buyers open the platform in a new tab;
- a **Switch user** button in the top bar to jump to any other role mid-demo;
- **presenter notes** on each card and in the **?** help panel ("what to show as
  this user");
- its **own demo database** (`db_presentation.sqlite3`), so rehearsing never
  changes the normal one, and a **Reset demo data** button on the login page to
  start fresh before the presentation.

Close the normal launcher first – both use ports 8000 and 8001. Presentation mode
only switches on through these launchers (and never with `DJANGO_DEBUG=0`), so the
normal system always keeps its password and two-factor login.

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

### Finding your way around

- **Every user lands on a dashboard with "Your tasks"**: each line says what is
  waiting (e.g. *2 release requests to decide*) and opens the screen where it is
  done. Numbers in the left menu show the same counts.
- The **left menu is grouped by module** (Inventory, Expiry & clearance, B2B sales,
  Warehouse audit, Disposal, Purchasing, Customers, Reports, Administration) and
  only shows what your role can do.
- The **search bar** at the top finds products, lot numbers, orders, buyers,
  purchase orders and customers.
- Every page shows a one-line purpose under the breadcrumb; the **?** button opens
  "How it works" steps for that page.
- Your name (top right) → **My profile** and **Change password**.

### A five-minute walkthrough

1. **COO** → *Expiry & clearance → Post to B2B*: pick a batch, set quantity and
   price (65–70% of the original; anything else is refused) → it is live on the
   platform. *Release requests* holds batches Sales asked to release.
2. **buyer@vetclinic** on the platform → browse a category → open a product →
   add to cart → checkout → upload a transfer receipt (any PDF/JPG/PNG). Track it
   under *My Orders*.
3. **Finance** → *Payment queue* → confirm; an invoice is issued.
4. **Warehouse** → *Dispatch queue* → type the allocated lot number → dispatch.
   The buyer confirms delivery and downloads the invoice.
5. **Sales** → *Near-expiry stock* → open a batch, record a customer offer, then
   *Post to B2B* (goes to the COO for approval).
6. **Inventory Controller** → *Warehouse audit → Audits* → reconcile CNT-00001.
7. **Procurement** → *Purchasing*: import `sample_data/sales_history_sample.csv`,
   generate a forecast, build a purchase plan; after the GM approves it, create
   the purchase orders in one click. *Products* and *Suppliers* are edited here too.
8. **GM** → *Approvals* → approve the waiting plan and write-off.
9. **Regulatory Affairs** → *B2B sales → Buyers* → verify a pending buyer.
10. **Any staff role** → *Reports → Reports & exports* for PDF / Excel downloads;
    *B2B sales → Buyer messages* to answer questions sent from the platform.

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

87 tests cover the business rules (FEFO, price band, atomic reservation,
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
