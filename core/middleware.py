from django.conf import settings
from django.contrib.auth import logout


class AppBoundaryMiddleware:
    """Keep the two applications apart (NFR-02).

    A buyer session can never reach an IMS screen and an internal session is
    never accepted by the platform; a mismatched session is logged out.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        app = getattr(settings, "APP_NAME", "ims")
        if user is not None and user.is_authenticated:
            urlconf = getattr(request, "urlconf", None) or settings.ROOT_URLCONF
            is_b2b = app == "b2b" or urlconf.endswith("urls_b2b")
            if (is_b2b and not user.is_buyer) or (not is_b2b and not user.is_internal):
                logout(request)
        return self.get_response(request)
