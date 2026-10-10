"""Test settings: both applications' URLs are loaded so tests can exercise each."""
import tempfile

from .base import *  # noqa: F401,F403

APP_NAME = "ims"
ROOT_URLCONF = "kubbar.urls_ims"
INSTALLED_APPS = INSTALLED_APPS + ["ims", "b2b"]  # noqa: F405
SESSION_COOKIE_AGE = 15 * 60
LOGIN_URL = "ims:login"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="kubbar-test-media-"))  # noqa: F405
SHOW_DEV_OTP = True
TEMPLATES[0]["OPTIONS"]["context_processors"].append("b2b.i18n.language")  # noqa: F405
PASSWORD_RESET_TIMEOUT = 30 * 60
