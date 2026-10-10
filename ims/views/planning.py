"""Module 5: import, forecast and purchase plan (UC-23 to UC-26)."""
import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Region, Role
from inventory.models import Product
from planning import services as plan_svc
from planning.models import (DataImport, Forecast, ForecastProduct, PurchasePlan,
                             PurchasePlanLine)

from ..access import role_required
from ..forms import ForecastForm, ImportForm, ManualEstimateForm, NewPlanForm
from .stock import _err


@role_required(Role.PROCUREMENT)
def imports(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            imp, unknown = plan_svc.stage_import(request.user, form.cleaned_data["data_type"],
                                                 form.cleaned_data["file"])
            if unknown:
                request.session[f"unknown_regions_{imp.pk}"] = unknown
            return redirect("ims:import_preview", pk=imp.pk)
        except ValidationError as e:
            _err(request, e)
    history = DataImport.objects.select_related("uploaded_by")[:20]
    return render(request, "ims/planning/imports.html", {"form": form, "history": history})


@role_required(Role.PROCUREMENT)
def import_preview(request, pk):
    imp = get_object_or_404(DataImport, pk=pk)
    unknown = request.session.get(f"unknown_regions_{imp.pk}", [])
    regions = Region.objects.filter(is_active=True)
    if request.method == "POST" and imp.status == DataImport.PREVIEW:
        if "cancel" in request.POST:
            imp.status = DataImport.FAILED
            imp.staged_rows = []
            imp.messages = imp.messages + ["Cancelled by the officer before import."]
            imp.save()
            messages.info(request, "Import cancelled; no data was written.")
            return redirect("ims:imports")
        mapping = {}
        for i, raw in enumerate(unknown):
            rid = request.POST.get(f"map_{i}")
            mapping[raw] = regions.filter(pk=rid).first() if rid else None
        try:
            plan_svc.confirm_import(request.user, imp, mapping)
            request.session.pop(f"unknown_regions_{imp.pk}", None)
            messages.success(request, f"Imported {imp.rows_accepted} rows "
                                      f"({imp.rows_corrected} corrected, {imp.rows_rejected} "
                                      f"rejected).")
            return redirect("ims:imports")
        except ValidationError as e:
            _err(request, e)
    preview = imp.staged_rows[:15]
    return render(request, "ims/planning/import_preview.html",
                  {"imp": imp, "unknown": list(enumerate(unknown)), "regions": regions,
                   "preview": preview})


@role_required(Role.PROCUREMENT, Role.GM)
def forecasts(request):
    form = ForecastForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not request.user.has_role(Role.PROCUREMENT):
            messages.error(request, "Only the Procurement Officer generates forecasts.")
            return redirect("ims:forecasts")
        d = form.cleaned_data
        products = d["products"] or Product.objects.filter(is_active=True)
        if d["category"]:
            products = products.filter(category=d["category"])
        try:
            fc = plan_svc.generate_forecast(request.user, d["horizon"], list(products),
                                           d["category"])
            messages.success(request, f"Forecast {fc} generated.")
            return redirect("ims:forecast_detail", pk=fc.pk)
        except ValidationError as e:
            _err(request, e)
    items = Forecast.objects.select_related("created_by", "category")[:20]
    return render(request, "ims/planning/forecasts.html", {"form": form, "items": items,
                                                           "latest_period":
                                                               plan_svc.latest_sales_period()})


@role_required(Role.PROCUREMENT, Role.GM)
def forecast_detail(request, pk):
    fc = get_object_or_404(Forecast, pk=pk)
    end = plan_svc.add_months(fc.start_period, -1)
    selected = request.GET.get("fp")
    fps = list(fc.products.select_related("product", "region").prefetch_related("lines"))
    for fp in fps:
        fp.total = sum(line.quantity for line in fp.lines.all())
    focus = next((fp for fp in fps if str(fp.pk) == selected), fps[0] if fps else None)
    chart = None
    if focus:
        actual = plan_svc.actuals_for(focus.product, focus.region, end)
        chart = {"labels": [f"{p:%b %y}" for p, _ in actual]
                           + [f"{line.period:%b %y}" for line in focus.lines.all()],
                 "actual": [q for _, q in actual] + [None] * focus.lines.count(),
                 "forecast": [None] * (len(actual) - 1) + [actual[-1][1]]
                             + [float(line.quantity) for line in focus.lines.all()]}
    manual_form = ManualEstimateForm(request.POST or None)
    if request.method == "POST" and manual_form.is_valid() and request.user.has_role(
            Role.PROCUREMENT):
        fp = get_object_or_404(ForecastProduct, pk=request.POST.get("fp"), forecast=fc)
        try:
            plan_svc.set_manual_estimate(fp, manual_form.cleaned_data["monthly_quantity"],
                                         manual_form.cleaned_data["reason"])
            messages.success(request, "Manual estimate saved.")
        except ValidationError as e:
            _err(request, e)
        return redirect(f"{request.path}?fp={fp.pk}")
    plan_form = NewPlanForm(initial={"period_label": f"{fc.start_period:%Y}-"
                                                     f"Q{(fc.start_period.month - 1) // 3 + 1}"})
    return render(request, "ims/planning/forecast_detail.html",
                  {"fc": fc, "fps": fps, "focus": focus, "chart": chart,
                   "chart_json": json.dumps(chart) if chart else "",
                   "manual_form": manual_form, "plan_form": plan_form})


@role_required(Role.PROCUREMENT, Role.GM)
def plans(request):
    items = PurchasePlan.objects.select_related("prepared_by", "approved_by")[:30]
    return render(request, "ims/planning/plans.html", {"items": items})


@role_required(Role.PROCUREMENT)
def plan_new(request, forecast_pk):
    fc = get_object_or_404(Forecast, pk=forecast_pk)
    form = NewPlanForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            plan = plan_svc.build_plan(request.user, fc, form.cleaned_data["period_label"])
            messages.success(request, f"Purchase plan {plan.number} built from {fc}.")
            return redirect("ims:plan_detail", pk=plan.pk)
        except ValidationError as e:
            _err(request, e)
    return redirect("ims:forecast_detail", pk=fc.pk)


@role_required(Role.PROCUREMENT, Role.GM)
def plan_detail(request, pk):
    """UC-25 (officer) and UC-26 (General Manager) on one screen."""
    plan = get_object_or_404(PurchasePlan, pk=pk)
    lines = list(plan.lines.select_related("product"))
    user = request.user
    editable = user.has_role(Role.PROCUREMENT) and plan.status in (PurchasePlan.DRAFT,
                                                                   PurchasePlan.RETURNED)
    deciding = user.has_role(Role.GM) and plan.status == PurchasePlan.SUBMITTED
    if request.method == "POST":
        try:
            if editable and ("save" in request.POST or "submit" in request.POST):
                for line in lines:
                    raw = request.POST.get(f"final_{line.pk}")
                    if raw is None:
                        continue
                    if plan.status == PurchasePlan.RETURNED and \
                            line.status != PurchasePlanLine.RETURNED:
                        continue
                    plan_svc.override_line(line, int(raw),
                                           request.POST.get(f"reason_{line.pk}", "").strip())
                if "submit" in request.POST:
                    plan_svc.submit_plan(user, plan)
                    messages.success(request, "Plan submitted; the General Manager has been "
                                              "notified.")
                else:
                    messages.success(request, "Plan saved.")
                return redirect("ims:plan_detail", pk=pk)
            if deciding and "decide" in request.POST:
                decisions = {line.pk: (request.POST.get(f"decision_{line.pk}",
                                                        PurchasePlanLine.APPROVED),
                                       request.POST.get(f"comment_{line.pk}", "").strip())
                             for line in lines}
                if request.POST.get("decide") == "reject_all":
                    decisions = {k: (PurchasePlanLine.REJECTED,
                                     v[1] or request.POST.get("comment", ""))
                                 for k, v in decisions.items()}
                plan_svc.decide_plan(user, plan, decisions, request.POST.get("comment", ""))
                messages.success(request, f"Plan {plan.number}: {plan.get_status_display()}.")
                return redirect("ims:plan_detail", pk=pk)
        except (ValidationError, ValueError) as e:
            if isinstance(e, ValidationError):
                _err(request, e)
            else:
                messages.error(request, "Quantities must be whole numbers.")
    totals = {"value": sum(line.line_value for line in lines),
              "moq_surplus_value": sum(line.moq_surplus_value for line in lines),
              "at_risk": sum(1 for line in lines if line.at_risk)}
    return render(request, "ims/planning/plan_detail.html",
                  {"plan": plan, "lines": lines, "editable": editable, "deciding": deciding,
                   "totals": totals, "write_offs": plan.write_offs.select_related(
                       "batch__product")})
