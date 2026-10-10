from django.conf import settings


def app_context(request):
    ctx = {"APP_NAME": getattr(settings, "APP_NAME", "ims")}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_internal:
        ctx["unread_notifications"] = user.notifications.filter(is_read=False).count()
        if "ims" in settings.INSTALLED_APPS:
            from ims.nav import nav_for

            ctx["nav_sections"] = nav_for(user)
    return ctx
