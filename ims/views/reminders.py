"""Data-based reminders (UC-56): review, act, snooze or mark done."""
from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core import reminders as svc
from core.models import AuditLog, Reminder
from core.rules import rule_int
from core.services import audit

from ..access import role_required


@role_required()
def reminder_list(request):
    user = request.user
    if request.method == "POST":
        if "refresh" in request.POST:
            n = svc.generate()
            messages.success(request, f"Reminders checked against the latest data: {n} new.")
            return redirect("ims:reminders")
        rem = get_object_or_404(Reminder, pk=request.POST.get("reminder"))
        if rem not in svc.visible_to(user):
            messages.error(request, "That reminder is not yours.")
            return redirect("ims:reminders")
        if "done" in request.POST:
            rem.status, rem.done_at, rem.done_by = Reminder.DONE, timezone.now(), user
            messages.success(request, "Marked as done. It comes back only if the data still "
                                      "calls for it later.")
        elif "snooze" in request.POST:
            days = rule_int("reminder_snooze_days")
            rem.status = Reminder.SNOOZED
            rem.snoozed_until = timezone.localdate() + timedelta(days=days)
            messages.info(request, f"Hidden for {days} days.")
        rem.save()
        audit(user, AuditLog.APPROVAL, f"Reminder {rem.get_status_display().lower()}: "
                                       f"{rem.title}", f"REMINDER-{rem.pk}")
        return redirect("ims:reminders")
    svc.refresh_if_stale()
    items = sorted(svc.visible_to(user), key=svc.sort_key)
    kinds = dict(Reminder.KINDS)
    kind = request.GET.get("kind", "")
    if kind in kinds:
        items = [r for r in items if r.kind == kind]
    return render(request, "ims/reminders.html", {"items": items, "kinds": Reminder.KINDS,
                                                  "kind": kind})
