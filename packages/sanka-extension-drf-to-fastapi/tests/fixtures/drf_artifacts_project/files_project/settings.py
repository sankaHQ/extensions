from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "sanka-bench-synthetic-fixture-009-only"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
ROOT_URLCONF = "files_project.urls"
MIDDLEWARE: list[str] = []
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "rest_framework",
    "artifacts",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("BENCH_DB_PATH", str(BASE_DIR / "db.sqlite3")),
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
APPEND_SLASH = False
USE_TZ = True
TIME_ZONE = "UTC"
MEDIA_ROOT = os.environ.get("BENCH_MEDIA_ROOT", str(BASE_DIR / "media"))
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "artifacts.renderers.ApiJSONRenderer",
    ],
    "UNAUTHENTICATED_USER": None,
}
