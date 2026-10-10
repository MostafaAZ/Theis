"""B2B E-Commerce Platform (public-facing application)."""
from .base import *  # noqa: F401,F403

APP_NAME = "b2b"
ROOT_URLCONF = "kubbar.urls_b2b"
INSTALLED_APPS = INSTALLED_APPS + ["b2b"]  # noqa: F405
SESSION_COOKIE_NAME = "b2b_session"
CSRF_COOKIE_NAME = "b2b_csrftoken"
SESSION_COOKIE_AGE = 30 * 60  # NFR-24: 30 minutes for buyers
LOGIN_URL = "b2b:login"
TEMPLATES[0]["OPTIONS"]["context_processors"].append("b2b.i18n.language")  # noqa: F405
PASSWORD_RESET_TIMEOUT = 30 * 60  # UC-32 A2: reset link valid for 30 minutes
