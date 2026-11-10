"""Settings shared by both web applications.

The IMS and the B2B E-Commerce Platform are two separate web applications
(NFR-02) that read and write one shared database (NFR-01). Each has its own
settings module that sets its URL configuration, session cookie and timeout.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core",
    "inventory",
    "planning",
    "commerce",
    "analytics",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.AppBoundaryMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.app_context",
            ],
        },
    },
]

# One shared database for both applications (NFR-01). SQLite is used for the
# prototype; point DATABASE_* at PostgreSQL for the full release.
if os.environ.get("DATABASE_ENGINE") == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DATABASE_NAME", "kubbar"),
            "USER": os.environ.get("DATABASE_USER", "kubbar"),
            "PASSWORD": os.environ.get("DATABASE_PASSWORD", ""),
            "HOST": os.environ.get("DATABASE_HOST", "localhost"),
            "PORT": os.environ.get("DATABASE_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / os.environ.get("DATABASE_NAME", "db.sqlite3"),
            "OPTIONS": {"timeout": 20},
        }
    }

AUTH_USER_MODEL = "core.User"
AUTHENTICATION_BACKENDS = ["core.auth.EmailBackend"]

# NFR-22: passwords hashed and salted (PBKDF2 by default).
# NFR-23: at least 10 characters, mixing letters and numbers.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "core.validators.LettersAndNumbersValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en"
TIME_ZONE = "Africa/Cairo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))
MEDIA_URL = "media/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Sessions expire after inactivity (NFR-24): the age is refreshed on every request.
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = "Kubbar International <no-reply@kubbar.example>"

# Encryption key for buyer documents and identity numbers at rest (FR-68, NFR-25).
# Must be a urlsafe base64 32-byte Fernet key in production.
FIELD_ENCRYPTION_KEY = os.environ.get("FIELD_ENCRYPTION_KEY", "")

# Applications address each other for cross-links (e.g. emails to buyers).
IMS_BASE_URL = os.environ.get("IMS_BASE_URL", "http://localhost:8000")
B2B_BASE_URL = os.environ.get("B2B_BASE_URL", "http://localhost:8001")

# In development the 2FA one-time code is also shown on screen so the
# prototype can be demonstrated without a mail server.
SHOW_DEV_OTP = DEBUG

# Presentation mode: one-click login as any demo user and a "Switch user" menu.
# Only the start-presentation launchers turn it on, and never with DEBUG off.
DEMO_MODE = DEBUG and os.environ.get("KUBBAR_DEMO_MODE") == "1"

MAX_UPLOAD_MB = 10
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "1") == "1"
    SECURE_HSTS_SECONDS = 31536000
