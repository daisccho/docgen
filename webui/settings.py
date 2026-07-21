"""Settings for the local or office-hosted docgen WebUI."""

from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.environ.get("DOCGEN_DEBUG", "1") == "1"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DOCGEN_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DOCGEN_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "webui.webui",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "webui.urls"
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
            ],
        },
    }
]
WSGI_APPLICATION = "webui.wsgi.application"
ASGI_APPLICATION = "webui.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": os.environ.get("DOCGEN_DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ.get("DOCGEN_DB_NAME")
                or os.environ.get("DOCGEN_DB_PATH")
                or str(BASE_DIR / "db.sqlite3"),
        "USER": os.environ.get("DOCGEN_DB_USER", ""),
        "PASSWORD": os.environ.get("DOCGEN_DB_PASSWORD", ""),
        "HOST": os.environ.get("DOCGEN_DB_HOST", ""),
        "PORT": os.environ.get("DOCGEN_DB_PORT", ""),
    }
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = os.environ.get("DOCGEN_TIME_ZONE", "Europe/Moscow")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("DOCGEN_STATIC_ROOT", BASE_DIR / "staticfiles"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

DOCGEN_WORKSPACE_ROOT = Path(
    os.environ.get("DOCGEN_WORKSPACE_ROOT", BASE_DIR / "workspaces")
).resolve()
DOCGEN_PYTHON = os.environ.get("DOCGEN_PYTHON", "")
