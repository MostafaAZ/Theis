"""Role-based access control for IMS screens (FR-03)."""
from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from core.models import Role

ALL_INTERNAL = tuple(r for r, _ in Role.choices)
ANALYTICS = (Role.SALES, Role.PROCUREMENT, Role.FINANCE, Role.GM, Role.MANAGEMENT, Role.COO)
EXPIRY_VIEW = (Role.SALES, Role.INVENTORY, Role.GM, Role.COO, Role.MANAGEMENT)
AUDIT_TRAIL = (Role.ADMIN, Role.MANAGEMENT, Role.COO)


def role_required(*roles):
    """Allow internal users holding any of ``roles``; no roles means any
    internal user."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated or not user.is_internal:
                messages.info(request, "Please log in.")
                return redirect(f"/login/?next={request.path}")
            if roles and not user.has_role(*roles):
                raise PermissionDenied("Your role does not have access to this screen.")
            return view(request, *args, **kwargs)

        wrapper.allowed_roles = roles
        return wrapper

    return decorator


def can(user, *roles):
    return user.is_authenticated and user.has_role(*roles)
