"""Inventory Management System (internal application)."""
from .base import *  # noqa: F401,F403

APP_NAME = "ims"
ROOT_URLCONF = "kubbar.urls_ims"
INSTALLED_APPS = INSTALLED_APPS + ["ims"]  # noqa: F405
SESSION_COOKIE_NAME = "ims_session"
CSRF_COOKIE_NAME = "ims_csrftoken"
SESSION_COOKIE_AGE = 15 * 60  # NFR-24: 15 minutes for internal users
LOGIN_URL = "ims:login"
