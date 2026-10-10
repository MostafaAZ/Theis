"""Module 2: warehouse audit (UC-10 to UC-13) and the GM's approvals inbox."""
from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from core.models import ReasonCode, Role
from inventory import services as inv
from inventory.models import StockCount, StockCountLine, WriteOff
from planning.models import PurchasePlan

from ..access import role_required
from ..forms import ExtraStockForm, ScheduleCountForm
from .stock import _err


@role_required(Role.INVENTORY, Role.GM)
def audits(request):
    counts = StockCount.objects.select_related("warehouse", "assigned_to")[:100]
    return render(request, "ims/audits/list.html", {"counts": counts})


@role_required(Role.INVENTORY)
def schedule(request):
    form = ScheduleCountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            count = inv.schedule_count(
                request.user, count_type=d["count_type"], warehouse=d["warehouse"],
                scope=d["scope"], scheduled_date=d["scheduled_date"],
                assigned_to=d["assigned_to"], category=d["category"],
                batch_ids=[b.pk for b in d["batches"]],
                exclude_open_dispatch=d["exclude_open_dispatch"])
            messages.success(request, f"Count {count.number} scheduled with "
                                      f"{count.lines.count()} lines and assigned to "
                                      f"{count.assigned_to}.")
            return redirect("ims:audit_detail", pk=count.pk)
        except ValidationError as e:
            _err(request, e)
    return render(request, "ims/audits/schedule.html", {"form": form})


@role_required(Role.INVENTORY, Role.GM, Role.WAREHOUSE)
def detail(request, pk):
    count = get_object_or_404(StockCount, pk=pk)
    blind = (request.user.has_role(Role.WAREHOUSE)
             and not request.user.has_role(Role.INVENTORY, Role.GM)
             and count.status != StockCount.CLOSED)
    if blind:
        return redirect("ims:count_sheet", pk=pk)
    lines = count.lines.select_related("batch__product", "location", "reason_code",
                                       "extra_product")
    return render(request, "ims/audits/detail.html", {"count": count, "lines": lines})


@role_required(Role.WAREHOUSE)
def my_counts(request):
    counts = StockCount.objects.filter(assigned_to=request.user).exclude(
        status=StockCount.CLOSED).select_related("warehouse")
    done = StockCount.objects.filter(assigned_to=request.user,
                                     status=StockCount.CLOSED).select_related("warehouse")[:10]
    return render(request, "ims/audits/mine.html", {"counts": counts, "done": done})


@role_required(Role.WAREHOUSE)
def count_sheet(request, pk):
    """UC-11: blind count sheet; the system quantity is never shown (FR-20)."""
    count = get_object_or_404(StockCount, pk=pk)
    if count.assigned_to_id != request.user.pk:
        raise PermissionDenied("This count is assigned to another officer.")
    if count.status == StockCount.SCHEDULED and count.scheduled_date <= date.today():
        inv.start_count(count)
    lines = list(count.lines.filter(status__in=[StockCountLine.PENDING, StockCountLine.RECOUNT])
                 .select_related("batch__product", "location"))
    extra_forms = [ExtraStockForm(request.POST or None, prefix=f"x{i}") for i in range(3)]
    if request.method == "POST" and count.status == StockCount.IN_PROGRESS:
        entries = {}
        for line in lines:
            raw = request.POST.get(f"count_{line.pk}", "").strip()
            exp = request.POST.get(f"expiry_{line.pk}", "").strip()
            try:
                entries[line.pk] = {
                    "counted": int(raw) if raw != "" else None,
                    "not_found": request.POST.get(f"nf_{line.pk}") == "on",
                    "observed_expiry": date.fromisoformat(exp) if exp else None}
            except ValueError:
                messages.error(request, "Counted quantities must be whole numbers.")
                return redirect("ims:count_sheet", pk=pk)
        extras = []
        for f in extra_forms:
            if f.is_valid() and f.cleaned_data.get("product") and f.cleaned_data.get("quantity"):
                cd = f.cleaned_data
                if not (cd["lot"] and cd["location"]):
                    messages.error(request, "Found stock needs a lot and a location.")
                    return redirect("ims:count_sheet", pk=pk)
                extras.append(cd)
        try:
            inv.submit_count(request.user, count, entries, extras)
            messages.success(request, f"Count {count.number} submitted. The Inventory "
                                      f"Controller has been notified.")
            return redirect("ims:my_counts")
        except ValidationError as e:
            _err(request, e)
    return render(request, "ims/audits/count_sheet.html",
                  {"count": count, "lines": lines, "extra_forms": extra_forms})


@role_required(Role.INVENTORY)
def reconcile(request, pk):
    """UC-12."""
    count = get_object_or_404(StockCount, pk=pk)
    lines = count.lines.select_related("batch__product", "location", "reason_code",
                                       "extra_product")
    reasons = ReasonCode.objects.filter(kind=ReasonCode.VARIANCE, is_active=True)
    if request.method == "POST":
        decisions = {}
        for line in lines:
            if line.status != StockCountLine.FLAGGED:
                continue
            action = request.POST.get(f"action_{line.pk}")
            if action == "recount":
                decisions[line.pk] = {"action": "recount"}
            elif action == "post":
                rc = reasons.filter(pk=request.POST.get(f"reason_{line.pk}") or 0).first()
                decisions[line.pk] = {"action": "post", "reason": rc}
        try:
            inv.reconcile_count(request.user, count, decisions)
            count.refresh_from_db()
            messages.success(request, f"Count {count.number}: {count.get_status_display()}.")
            return redirect("ims:audit_detail", pk=pk)
        except ValidationError as e:
            _err(request, e)
    return render(request, "ims/audits/reconcile.html",
                  {"count": count, "lines": lines, "reasons": reasons,
                   "threshold": inv.rule("adjustment_threshold_egp")})


@role_required(Role.GM, Role.QA)
def approvals(request):
    """Pending approvals inbox for the General Manager (and QA for write-offs)."""
    user = request.user
    adjustments = StockCountLine.objects.filter(status=StockCountLine.AWAITING).select_related(
        "count", "batch__product", "reason_code") if user.has_role(Role.GM) else []
    write_offs = WriteOff.objects.filter(status=WriteOff.PENDING).select_related(
        "batch__product")
    if user.has_role(Role.GM) and not user.has_role(Role.QA):
        write_offs = write_offs.filter(gm_approved_by__isnull=True)
    elif user.has_role(Role.QA) and not user.has_role(Role.GM):
        write_offs = write_offs.filter(qa_approved_by__isnull=True)
    plans = PurchasePlan.objects.filter(status=PurchasePlan.SUBMITTED) if user.has_role(
        Role.GM) else []
    return render(request, "ims/approvals.html",
                  {"adjustments": adjustments, "write_offs": write_offs, "plans": plans})


@role_required(Role.GM)
def adjustment(request, pk):
    """UC-13."""
    line = get_object_or_404(StockCountLine.objects.select_related(
        "count", "batch__product", "reason_code", "location"), pk=pk)
    if request.method == "POST":
        approve = request.POST.get("decision") == "approve"
        try:
            inv.decide_adjustment(request.user, line, approve, request.POST.get("comment", ""))
            messages.success(request, "Adjustment approved and posted." if approve else
                             "Adjustment rejected; a recount has been requested.")
            return redirect("ims:approvals")
        except ValidationError as e:
            _err(request, e)
    history = StockCountLine.objects.filter(batch=line.batch).exclude(pk=line.pk).select_related(
        "count")[:10]
    return render(request, "ims/audits/adjustment.html", {"line": line, "history": history})
