from django.conf import settings


def app_context(request):
    ctx = {"APP_NAME": getattr(settings, "APP_NAME", "ims")}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_internal \
            and "ims" in settings.INSTALLED_APPS:
        from ims.nav import nav_for, page_info

        ctx["nav_sections"] = nav_for(user, request.path)
        ctx["unread_notifications"] = user.notifications.filter(is_read=False).count()
        ctx["recent_notifications"] = user.notifications.all()[:6]
        match = getattr(request, "resolver_match", None)
        ctx["page"] = page_info(match.url_name if match else "")
        ctx["initials"] = "".join(p[0] for p in (str(user).split()[:2]) if p).upper() or "U"
    return ctx
