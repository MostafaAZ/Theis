"""Module 7: system administration (UC-41 to UC-44)."""
import secrets

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db.models import ProtectedError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from analytics import exports
from commerce import services as shop
from commerce.models import Buyer
from core.models import AuditLog, BusinessRule, Role, User
from core.rules import ensure_rules
from core.services import audit

from ..access import AUDIT_TRAIL, role_required
from ..forms import REFERENCE_MODELS, AuditFilterForm, RuleForm, UserForm, reference_form
from .stock import _err


@role_required(Role.ADMIN)
def users(request):
    internal = User.objects.filter(account_type=User.INTERNAL)
    buyers = Buyer.objects.select_related("user", "buyer_type")
    return render(request, "ims/admin/users.html", {"internal": internal, "buyers": buyers})


@role_required(Role.ADMIN)
def user_edit(request, pk=None):
    user = get_object_or_404(User, pk=pk, account_type=User.INTERNAL) if pk else None
    old_roles = ", ".join(user.role_labels) if user else ""
    form = UserForm(request.POST or None, instance=user,
                    initial={"roles": user.roles} if user else {"is_active": True})
    if request.method == "POST" and "unlock" in request.POST and user:
        user.is_locked = False
        user.failed_login_attempts = 0
        user.save(update_fields=["is_locked", "failed_login_attempts"])
        audit(request.user, AuditLog.CONFIGURATION, f"Unlocked {user.email}", f"USER-{user.pk}")
        messages.success(request, "Account unlocked.")
        return redirect("ims:user_edit", pk=user.pk)
    if request.method == "POST" and "unlock" not in request.POST and form.is_valid():
        obj = form.save(commit=False)
        obj.username = obj.email
        obj.account_type = User.INTERNAL
        obj.roles = form.cleaned_data["roles"]
        created = obj.pk is None
        pw = form.cleaned_data.get("password")
        if pw:
            obj.set_password(pw)
        elif created:
            temp = secrets.token_urlsafe(9) + "7a"
            obj.set_password(temp)
            send_mail("Your Kubbar IMS account",
                      f"An IMS account has been created for you.\nEmail: {obj.email}\n"
                      f"Temporary password: {temp}\nYou will be asked for a one-time code at "
                      f"login.", None, [obj.email], fail_silently=True)
        obj.save()
        form.save_m2m()
        audit(request.user, AuditLog.CONFIGURATION,
              f"{'Created' if created else 'Updated'} user {obj.email}", f"USER-{obj.pk}",
              old=old_roles, new=", ".join(obj.role_labels) + ("" if obj.is_active
                                                               else " (deactivated)"))
        messages.success(request, "User saved." + (" An activation email was sent."
                                                   if created and not pw else ""))
        return redirect("ims:users")
    return render(request, "ims/admin/user_edit.html", {"form": form, "obj": user})


@role_required(Role.ADMIN)
def buyer_status(request, pk):
    """UC-41 A2: suspend or reactivate a buyer account."""
    buyer = get_object_or_404(Buyer, pk=pk)
    if request.method == "POST":
        try:
            shop.set_buyer_suspension(request.user, buyer, request.POST.get("action") ==
                                      "suspend", request.POST.get("reason", "").strip())
            messages.success(request, "Buyer account updated.")
        except ValidationError as e:
            _err(request, e)
    return redirect("ims:users")


@role_required(Role.ADMIN)
def rules(request):
    ensure_rules()
    return render(request, "ims/admin/rules.html", {"rules": BusinessRule.objects.all()})


@role_required(Role.ADMIN)
def rule_edit(request, pk):
    """UC-42 / FR-52."""
    r = get_object_or_404(BusinessRule, pk=pk)
    form = RuleForm(request.POST or None, initial={"new_value": r.value})
    if request.method == "POST" and form.is_valid():
        v = form.cleaned_data["new_value"]
        problem = None
        if (r.min_value is not None and v < r.min_value) or (r.max_value is not None
                                                             and v > r.max_value):
            problem = f"Value must be between {r.min_value} and {r.max_value}."
        lo = BusinessRule.objects.filter(key="price_band_min_pct").first()
        hi = BusinessRule.objects.filter(key="price_band_max_pct").first()
        if r.key == "price_band_min_pct" and hi and v > hi.value:
            problem = "The price band floor cannot be above its ceiling."
        if r.key == "price_band_max_pct" and lo and v < lo.value:
            problem = "The price band ceiling cannot be below its floor."
        if problem:
            messages.error(request, problem)
        else:
            old = r.value
            r.value = v
            r.changed_by = request.user
            r.approved_by = form.cleaned_data["approving_manager"]
            r.changed_at = timezone.now()
            r.save()
            audit(request.user, AuditLog.CONFIGURATION, f"Business rule '{r.name}' changed "
                                                        f"(approved by {r.approved_by})",
                  f"RULE-{r.key}", old=old, new=v)
            messages.success(request, f"{r.name} set to {r.display_value}.")
            return redirect("ims:rules")
    return render(request, "ims/admin/rule_edit.html", {"rule": r, "form": form})


@role_required(Role.ADMIN)
def reference(request):
    lists = [(key, model._meta.verbose_name_plural.title(), model.objects.count())
             for key, (model, _f) in REFERENCE_MODELS.items()]
    return render(request, "ims/admin/reference.html", {"lists": lists})


@role_required(Role.ADMIN)
def reference_list(request, key):
    if key not in REFERENCE_MODELS:
        raise Http404
    model, fields = REFERENCE_MODELS[key]
    rows = model.objects.all()[:500]
    return render(request, "ims/admin/reference_list.html", {
        "key": key, "title": model._meta.verbose_name_plural.title(), "fields": fields,
        "headers": [model._meta.get_field(f).verbose_name.title() for f in fields],
        "rows": [(o, [getattr(o, f"get_{f}_display")() if hasattr(o, f"get_{f}_display")
                      else getattr(o, f) for f in fields]) for o in rows]})


@role_required(Role.ADMIN)
def reference_edit(request, key, pk=None):
    """UC-43: duplicates are blocked by unique constraints; entries in use are
    deactivated rather than deleted."""
    if key not in REFERENCE_MODELS:
        raise Http404
    model, fields = REFERENCE_MODELS[key]
    obj = get_object_or_404(model, pk=pk) if pk else None
    form = reference_form(model, fields)(request.POST or None, instance=obj)
    if request.method == "POST" and "delete" in request.POST and obj:
        try:
            obj.delete()
            messages.success(request, "Entry deleted.")
        except ProtectedError:
            if hasattr(obj, "is_active"):
                obj.is_active = False
                obj.save()
                messages.warning(request, "This entry is used by existing records, so it was "
                                          "deactivated instead: hidden from new records, kept on "
                                          "old ones.")
            else:
                messages.error(request, "This entry is used by existing records.")
        return redirect("ims:reference_list", key=key)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        audit(request.user, AuditLog.CONFIGURATION,
              f"Reference data {key}: {'updated' if obj else 'added'} {saved}",
              f"{key.upper()}-{saved.pk}")
        messages.success(request, "Saved. Dropdowns now show the change.")
        return redirect("ims:reference_list", key=key)
    return render(request, "ims/form.html", {
        "form": form, "title": f"{model._meta.verbose_name.title()}",
        "back": f"/admin/reference/{key}/", "can_delete": obj is not None})


@role_required(*AUDIT_TRAIL)
def audit_trail(request):
    """UC-44 / FR-54."""
    form = AuditFilterForm(request.GET or None)
    qs = AuditLog.objects.select_related("user")
    if form.is_valid():
        d = form.cleaned_data
        if d["user"]:
            qs = qs.filter(user=d["user"])
        if d["action"]:
            qs = qs.filter(action=d["action"])
        if d["record"]:
            qs = qs.filter(record_ref__icontains=d["record"])
        if d["date_from"]:
            qs = qs.filter(timestamp__date__gte=d["date_from"])
        if d["date_to"]:
            qs = qs.filter(timestamp__date__lte=d["date_to"])
    fmt = request.GET.get("export")
    if fmt in ("pdf", "xlsx"):
        rows = [[f"{timezone.localtime(e.timestamp):%Y-%m-%d %H:%M}", str(e.user or "System"),
                 e.get_action_display(), e.description, e.record_ref, e.old_value[:80],
                 e.new_value[:80]] for e in qs[:2000]]
        t = [{"title": "Audit trail", "headers": ["Time", "User", "Action", "Description",
                                                  "Record", "Old value", "New value"],
              "rows": rows}]
        fn = exports.to_pdf if fmt == "pdf" else exports.to_excel
        content = fn("Kubbar – Audit Trail", request.GET.urlencode() or "none", request.user,
                     [], t)
        ctype = "application/pdf" if fmt == "pdf" else \
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        resp = HttpResponse(content, content_type=ctype)
        resp["Content-Disposition"] = f'attachment; filename="audit-trail.{fmt}"'
        return resp
    query = request.GET.copy()
    query.pop("export", None)
    return render(request, "ims/admin/audit.html", {"form": form, "entries": qs[:300],
                                                    "query": query.urlencode()})
