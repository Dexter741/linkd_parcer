"""Django settings for the LinkedIn jobs parser.

Everything environment-specific comes from environment variables (see .env.example).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the project has no extra dependency for it."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "jobs",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves the admin's CSS/JS without DEBUG or a separate web server.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "linkd"),
        "USER": os.environ.get("POSTGRES_USER", "linkd"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "linkd"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Scraper ---------------------------------------------------------------
# Only LinkedIn's public guest endpoints are used: no login, no cookies.
LINKEDIN = {
    # Random pause between any two requests, seconds.
    "MIN_DELAY": float(os.environ.get("LINKEDIN_MIN_DELAY", "2.0")),
    "MAX_DELAY": float(os.environ.get("LINKEDIN_MAX_DELAY", "5.0")),
    # Retries on 429 / 999 / 5xx / network errors, with exponential backoff.
    "MAX_RETRIES": int(os.environ.get("LINKEDIN_MAX_RETRIES", "4")),
    "BACKOFF_BASE": float(os.environ.get("LINKEDIN_BACKOFF_BASE", "30")),
    "TIMEOUT": float(os.environ.get("LINKEDIN_TIMEOUT", "30")),
    # Optional outbound proxy, e.g. http://user:pass@host:port
    "PROXY": os.environ.get("LINKEDIN_PROXY", ""),
    # A detail fetch that failed this many times is no longer retried automatically.
    # How many results LinkedIn serves per search at most (measure: linkedin_probe --find-cap).
    "RESULT_CAP": int(os.environ.get("LINKEDIN_RESULT_CAP", "1000")),
    "DETAIL_MAX_ATTEMPTS": int(os.environ.get("LINKEDIN_DETAIL_MAX_ATTEMPTS", "3")),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"jobs": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")}},
}
