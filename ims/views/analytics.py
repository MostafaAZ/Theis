"""Module 6: sales and inventory analytics with export (UC-27, UC-28)."""
import json

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import render

from analytics import exports
from analytics.services import DASHBOARDS, Filters, build
from core.models import AuditLog, ProductCategory, Region, Warehouse
from core.services import audit
from inventory.models import Product

from ..access import ANALYTICS, role_required


@role_required(*ANALYTICS)
def dashboards(request):
    key = request.GET.get("d", "inventory")
    if key not in dict(DASHBOARDS):
        key = "inventory"
    filters = Filters(request.GET, request.user)
    data = build(key, filters)
    fmt = request.GET.get("export")
    if fmt in ("pdf", "xlsx"):
        try:
            title = f"Kubbar – {data['title']}"
            if fmt == "pdf":
                content = exports.to_pdf(title, filters.as_text(), request.user, data["kpis"],
                                         data["tables"])
                ctype = "application/pdf"
            else:
                content = exports.to_excel(title, filters.as_text(), request.user, data["kpis"],
                                           data["tables"])
                ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        except Exception:  # noqa: BLE001 - UC-28 A1: show an error and offer Retry
            messages.error(request, "The export failed. Please retry.")
        else:
            audit(request.user, AuditLog.DATA_SYNC, f"Exported {data['title']} ({fmt})",
                  f"REPORT-{key}", new=filters.as_text())
            resp = HttpResponse(content, content_type=ctype)
            resp["Content-Disposition"] = f'attachment; filename="kubbar-{key}.{fmt}"'
            return resp
    query = request.GET.copy()
    query.pop("export", None)
    return render(request, "ims/analytics.html", {
        "dashboards": DASHBOARDS, "data": data, "filters": filters, "query": query.urlencode(),
        "charts_json": json.dumps(data.get("charts", [])),
        "categories": ProductCategory.objects.all(), "products": Product.objects.all(),
        "regions": Region.objects.all(), "warehouses": Warehouse.objects.all()})
