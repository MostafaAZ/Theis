"""Module 4: disposal and write-off (UC-21, UC-22)."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Role
from inventory import services as inv
from inventory.models import Batch, BatchStatus, WriteOff

from ..access import role_required
from ..forms import DisposalForm, WriteOffForm
from .stock import _err


@role_required(Role.INVENTORY, Role.GM, Role.QA)
def write_offs(request):
    items = WriteOff.objects.select_related("batch__product", "requested_by")[:100]
    expired = Batch.objects.filter(status=BatchStatus.EXPIRED, qty_on_hand__gt=0).select_related(
        "product")
    return render(request, "ims/writeoffs/list.html", {"items": items, "expired": expired})


@role_required(Role.INVENTORY)
def request_write_off(request):
    form = WriteOffForm(request.POST or None, initial={"batch": request.GET.get("batch")})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        batch = d["batch"]
        untried = (batch.status == BatchStatus.NEAR_EXPIRY and batch.days_to_expiry >= 0
                   and not batch.releases.exists())
        if untried and not d["confirm_release_not_tried"]:
            messages.warning(request, "This batch is Near-Expiry and the release route has not "
                                      "been tried. Tick the confirmation to continue.")
        else:
            try:
                wo = inv.request_write_off(request.user, batch, d["quantity"], d["reason"],
                                           d["disposal_method"])
                messages.success(request, f"Write-off {wo} submitted for dual approval. The "
                                          f"quantity is blocked from sale.")
                return redirect("ims:write_off", pk=wo.pk)
            except ValidationError as e:
                _err(request, e)
    batch = None
    if request.GET.get("batch"):
        batch = Batch.objects.filter(pk=request.GET["batch"]).select_related("product").first()
    return render(request, "ims/writeoffs/request.html", {"form": form, "batch": batch})


@role_required(Role.INVENTORY, Role.GM, Role.QA)
def write_off(request, pk):
    wo = get_object_or_404(WriteOff.objects.select_related("batch__product"), pk=pk)
    disposal_form = DisposalForm(request.POST if "disposal" in request.POST else None)
    if request.method == "POST":
        try:
            if "approve" in request.POST:
                inv.approve_write_off(request.user, wo)
                wo.refresh_from_db()
                messages.success(request, "Approval recorded." + (
                    " Both approvals given: write-off posted." if wo.status == WriteOff.APPROVED
                    else " Waiting for the second approver."))
            elif "reject" in request.POST:
                if not request.user.has_role(Role.GM, Role.QA):
                    raise ValidationError("Only approvers can reject.")
                inv.reject_write_off(request.user, wo, request.POST.get("comment", ""))
                messages.success(request, "Write-off rejected and returned to the Inventory "
                                          "Controller.")
            elif "resubmit" in request.POST and request.user.has_role(Role.INVENTORY):
                inv.resubmit_write_off(request.user, wo)
                messages.success(request, "Resubmitted for approval.")
            elif "cancel" in request.POST and request.user.has_role(Role.INVENTORY):
                inv.cancel_write_off(request.user, wo)
                messages.success(request, "Request cancelled and the quantity released.")
            elif "disposal" in request.POST and request.user.has_role(Role.QA):
                if disposal_form.is_valid():
                    d = disposal_form.cleaned_data
                    inv.record_disposal(request.user, wo, d["disposal_date"], d["disposal_cost"],
                                        d["certificate_ref"])
                    messages.success(request, "Disposal recorded.")
                else:
                    raise ValidationError("Complete all disposal fields.")
            return redirect("ims:write_off", pk=pk)
        except ValidationError as e:
            _err(request, e)
    user = request.user
    can_approve = wo.status == WriteOff.PENDING and (
        (user.has_role(Role.GM) and not wo.gm_approved_by)
        or (user.has_role(Role.QA) and not wo.qa_approved_by))
    return render(request, "ims/writeoffs/detail.html", {
        "wo": wo, "can_approve": can_approve, "disposal_form": disposal_form,
        "history": wo.batch.releases.all(), "value": wo.batch.product.unit_cost * wo.quantity})
