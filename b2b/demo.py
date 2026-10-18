"""Presentation-mode views for the platform (only with settings.DEMO_MODE)."""
from django.contrib import messages
from django.contrib.auth import login, logout
from django.shortcuts import get_object_or_404, redirect

from core.demo import require_demo
from core.models import User


def demo_login(request, pk):
    require_demo()
    user = get_object_or_404(User, pk=pk, account_type=User.BUYER)
    user.is_locked = False
    user.failed_login_attempts = 0
    user.save(update_fields=["is_locked", "failed_login_attempts"])
    lang = request.session.get("lang", "en")
    logout(request)
    login(request, user, backend="core.auth.EmailBackend")
    request.session["lang"] = lang
    messages.info(request, f"Presenting as {user.buyer.company_name}.")
    return redirect("b2b:catalogue" if user.buyer.can_order else "b2b:account")
