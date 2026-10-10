"""UC-01 Log In (with two-factor code), UC-02 Log Out, UC-03 Dashboard."""
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core.models import AuditLog, LoginCode, Notification, Role, User
from core.rules import rule_int
from core.services import audit, notify_roles

from ..access import role_required
from ..forms import CodeForm, LoginForm


def _register_failure(user):
    user.failed_login_attempts += 1
    if user.failed_login_attempts >= rule_int("max_failed_logins") and not user.is_locked:
        user.is_locked = True
        notify_roles(Role.ADMIN, f"IMS account {user.email} locked after "
                                 f"{user.failed_login_attempts} failed login attempts.",
                     f"/admin/users/{user.pk}/", level="danger")
    user.save(update_fields=["failed_login_attempts", "is_locked"])


def _send_code(request, user):
    code = f"{secrets.randbelow(1_000_000):06d}"
    LoginCode.objects.filter(user=user, used=False).update(used=True)
    LoginCode.objects.create(user=user, code=code)
    send_mail("Kubbar IMS login code", f"Your one-time login code is {code}. It is valid for "
                                      f"10 minutes.", None, [user.email], fail_silently=True)
    if settings.SHOW_DEV_OTP:
        messages.info(request, f"Development mode: your one-time code is {code}.")


def login_view(request):
    if request.user.is_authenticated and request.user.is_internal:
        return redirect("ims:dashboard")
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].lower()
        password = form.cleaned_data["password"]
        user = User.objects.filter(email__iexact=email, account_type=User.INTERNAL).first()
        if user and user.is_locked:
            messages.error(request, "This account is locked. The System Administrator has been "
                                    "notified.")
        elif user and not user.is_active and user.check_password(password):
            messages.error(request, "This account has been deactivated.")
        else:
            auth_user = authenticate(request, email=email, password=password)
            if auth_user is None or not auth_user.is_internal:
                if user:
                    _register_failure(user)
                    if user.is_locked:
                        messages.error(request, "Too many failed attempts. The account is "
                                                "locked and the System Administrator has been "
                                                "notified.")
                        return render(request, "ims/login.html", {"form": form})
                messages.error(request, "Invalid email address or password.")
            else:
                request.session["pending_user"] = auth_user.pk
                request.session["pending_next"] = request.GET.get("next", "")
                _send_code(request, auth_user)
                return redirect("ims:login_verify")
    return render(request, "ims/login.html", {"form": form})


def login_verify(request):
    uid = request.session.get("pending_user")
    if not uid:
        return redirect("ims:login")
    user = get_object_or_404(User, pk=uid, account_type=User.INTERNAL)
    form = CodeForm(request.POST or None)
    if request.method == "POST" and "resend" in request.POST:
        _send_code(request, user)
        messages.success(request, "A new code has been sent.")
        return redirect("ims:login_verify")
    if request.method == "POST" and form.is_valid():
        code = LoginCode.objects.filter(user=user, used=False).order_by("-created_at").first()
        if code and code.is_valid() and secrets.compare_digest(code.code,
                                                               form.cleaned_data["code"]):
            code.used = True
            code.save()
            user.failed_login_attempts = 0
            user.save(update_fields=["failed_login_attempts"])
            login(request, user, backend="core.auth.EmailBackend")
            audit(user, AuditLog.LOGIN, "Logged in to the IMS", f"USER-{user.pk}",
                  request=request)
            nxt = request.session.pop("pending_next", "")
            request.session.pop("pending_user", None)
            if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}):
                return redirect(nxt)
            return redirect("ims:dashboard")
        messages.error(request, "The code is incorrect or has expired. You can request a new "
                                "one.")
    return render(request, "ims/login_verify.html", {"form": form, "pending": user})


@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect("ims:login")


@role_required()
def dashboard(request):
    import json

    from .. import dashboard as dash

    user = request.user
    hour = timezone.localtime().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    charts = dash.charts_for(user)
    return render(request, "ims/dashboard.html", {
        "greeting": greeting, "tasks": dash.tasks_for(user), "kpis": dash.kpis_for(user),
        "quick": dash.quick_for(user), "charts": charts, "charts_json": json.dumps(charts),
        "alerts": user.notifications.filter(is_read=False)[:6]})


@role_required()
def notifications(request):
    items = request.user.notifications.all()[:100]
    if request.method == "POST":
        request.user.notifications.filter(is_read=False).update(is_read=True)
        return redirect("ims:notifications")
    return render(request, "ims/notifications.html", {"items": items})


@role_required()
def open_notification(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    n.is_read = True
    n.save(update_fields=["is_read"])
    return redirect(n.link or "ims:dashboard")
