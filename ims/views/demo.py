"""Presentation-mode views (only with settings.DEMO_MODE)."""
from django.contrib import messages
from django.contrib.auth import login, logout
from django.core.management import call_command
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from core.demo import require_demo
from core.models import User


def demo_login(request, pk):
    """One click: log in as this staff user, skipping password and code."""
    require_demo()
    user = get_object_or_404(User, pk=pk, account_type=User.INTERNAL, is_active=True)
    user.is_locked = False
    user.failed_login_attempts = 0
    user.save(update_fields=["is_locked", "failed_login_attempts"])
    logout(request)
    login(request, user, backend="core.auth.EmailBackend")
    messages.info(request, f"Presenting as {user} – {', '.join(user.role_labels)}.")
    return redirect("ims:dashboard")


@require_POST
def demo_reset(request):
    """Reload the demo data so the presentation starts fresh."""
    require_demo()
    logout(request)
    call_command("seed_demo", reset=True, verbosity=0)
    messages.success(request, "Demo data reset. Pick a user to start.")
    return redirect("ims:login")
