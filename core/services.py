"""Cross-cutting services: audit trail and notifications."""
from django.conf import settings
from django.core.mail import send_mail

from .models import AuditLog, Notification, Role, User


def audit(user, action, description, record_ref="", old="", new="", request=None):
    """Write an append-only audit entry (FR-54, NFR-27)."""
    ip = None
    if request is not None:
        ip = request.META.get("REMOTE_ADDR")
    return AuditLog.objects.create(
        user=user if (user and user.is_authenticated) else None,
        action=action,
        description=description[:255],
        record_ref=str(record_ref)[:80],
        old_value="" if old is None else str(old),
        new_value="" if new is None else str(new),
        ip_address=ip,
    )


def users_with_role(*roles):
    users = User.objects.filter(account_type=User.INTERNAL, is_active=True)
    return [u for u in users if u.has_role(*roles)]


def notify(users, message, link="", level="info", email=True):
    """Create in-app notifications and send an email copy (Exhibit #3)."""
    if isinstance(users, User):
        users = [users]
    users = [u for u in users if u is not None]
    for u in users:
        Notification.objects.create(user=u, message=message[:255], link=link, level=level)
    if email and users:
        base = settings.IMS_BASE_URL
        body = message + (f"\n\nOpen: {base}{link}" if link else "")
        send_mail("Kubbar IMS: " + message[:80], body, None, [u.email for u in users],
                  fail_silently=True)


def notify_roles(roles, message, link="", level="info", email=True):
    if isinstance(roles, str):
        roles = [roles]
    notify(users_with_role(*roles), message, link, level, email)


def email_buyer(buyer_user, subject, body):
    send_mail("Kubbar B2B: " + subject, body, None, [buyer_user.email], fail_silently=True)


__all__ = ["audit", "notify", "notify_roles", "users_with_role", "email_buyer", "Role"]
