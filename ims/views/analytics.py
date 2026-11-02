"""Module 6: sales and inventory analytics with export (UC-27, UC-28), with
storytelling charts and analysis written for each management perspective."""
import json

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from analytics import ai, exports
from analytics.services import DASHBOARDS, Filters, build
from analytics.story import PERSPECTIVES
from core.models import AuditLog, ProductCategory, Region, Role, Warehouse
from core.services import audit
from inventory.models import Product

from ..access import ANALYTICS, role_required

# Who may ask Claude for an analysis (it calls a paid API).
AI_ROLES = (Role.MANAGEMENT, Role.COO, Role.GM, Role.FINANCE)


def _perspective(request):
    view = request.GET.get("view") or request.POST.get("view")
    return view if view in dict(PERSPECTIVES) else None


def _key(request, default="executive"):
    key = request.GET.get("d") or request.POST.get("d")
    if key not in dict(DASHBOARDS):
        key = default if request.user.has_role(*AI_ROLES) else "inventory"
    return key


@role_required(*ANALYTICS)
def dashboards(request):
    key = _key(request)
    filters = Filters(request.GET, request.user)
    data = build(key, filters, _perspective(request))
    fmt = request.GET.get("export")
    if fmt in ("pdf", "xlsx"):
        try:
            title = f"VETREX – {data['title']}"
            maker = exports.to_pdf if fmt == "pdf" else exports.to_excel
            content = maker(title, filters.as_text(), request.user, data["kpis"],
                            data["tables"], analysis=data["analysis"], charts=data["charts"])
            ctype = ("application/pdf" if fmt == "pdf" else
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception:  # noqa: BLE001 - UC-28 A1: show an error and offer Retry
            messages.error(request, "The export failed. Please retry.")
        else:
            audit(request.user, AuditLog.DATA_SYNC, f"Exported {data['title']} ({fmt})",
                  f"REPORT-{key}", new=filters.as_text())
            resp = HttpResponse(content, content_type=ctype)
            resp["Content-Disposition"] = f'attachment; filename="vetrex-{key}.{fmt}"'
            return resp
    query = request.GET.copy()
    query.pop("export", None)
    base = query.copy()
    base.pop("view", None)
    base["d"] = key
    return render(request, "ims/analytics.html", {
        "dashboards": DASHBOARDS, "data": data, "filters": filters, "query": query.urlencode(),
        "base_query": base.urlencode(), "analysis": data["analysis"],
        "perspective_label": dict(PERSPECTIVES)[data["perspective"]],
        "charts_json": json.dumps(data["charts"]), "perspectives": PERSPECTIVES,
        "can_ai": request.user.has_role(*AI_ROLES), "ai_ready": ai.available(),
        "categories": ProductCategory.objects.all(), "products": Product.objects.all(),
        "regions": Region.objects.all(), "warehouses": Warehouse.objects.all()})


@require_POST
@role_required(*AI_ROLES)
def ai_analysis(request):
    """Claude's analysis of one dashboard for one perspective, as JSON."""
    key = _key(request)
    filters = Filters(request.POST, request.user)
    data = build(key, filters, _perspective(request))
    try:
        result = ai.analyse(data, filters.as_text(), data["perspective"])
    except ai.AIUnavailable as e:
        return JsonResponse({"error": str(e)}, status=503)
    audit(request.user, AuditLog.DATA_SYNC, f"AI analysis of {data['title']}",
          f"REPORT-{key}", new=f"{data['perspective']} · {filters.as_text()}")
    return JsonResponse(result)
