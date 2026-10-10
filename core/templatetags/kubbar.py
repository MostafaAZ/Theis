from decimal import Decimal

from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def egp(value):
    """Format a money value in EGP with two decimals (NFR-03)."""
    if value in (None, ""):
        return "–"
    try:
        return f"{Decimal(value):,.2f}"
    except Exception:  # noqa: BLE001
        return value


@register.filter
def days_left(d):
    if not d:
        return ""
    return (d - timezone.localdate()).days


@register.filter
def expiry_class(days):
    try:
        days = int(days)
    except (TypeError, ValueError):
        return ""
    if days < 0:
        return "text-bg-dark"
    if days <= 30:
        return "text-bg-danger"
    if days <= 180:
        return "text-bg-warning"
    return "text-bg-success"


@register.filter
def status_class(status):
    return {
        "in_stock": "text-bg-success", "near_expiry": "text-bg-warning",
        "surplus": "text-bg-info", "released": "text-bg-primary", "expired": "text-bg-dark",
        "rejected": "text-bg-secondary", "depleted": "text-bg-light", "inspected": "text-bg-light",
        "pending": "text-bg-warning", "approved": "text-bg-success",
        "verified": "text-bg-success", "active": "text-bg-success",
        "awaiting_payment": "text-bg-warning", "placed": "text-bg-info",
        "confirmed": "text-bg-primary", "dispatched": "text-bg-info",
        "delivered": "text-bg-success", "cancelled": "text-bg-secondary",
        "withdrawn": "text-bg-secondary", "sold_out": "text-bg-dark",
        "submitted": "text-bg-info", "closed": "text-bg-success",
        "in_progress": "text-bg-warning", "scheduled": "text-bg-light",
        "flagged": "text-bg-danger", "within": "text-bg-success", "posted": "text-bg-success",
        "awaiting_approval": "text-bg-warning", "recount": "text-bg-info",
        "returned": "text-bg-info", "disposed": "text-bg-dark", "draft": "text-bg-light",
        "suspended": "text-bg-danger", "resubmission": "text-bg-info",
        "pending_email": "text-bg-light", "resubmit": "text-bg-info",
    }.get(str(status), "text-bg-light")


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def add_class(field, css):
    return field.as_widget(attrs={"class": css})
