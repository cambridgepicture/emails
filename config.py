from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency in this workspace
    def load_dotenv(path: str | Path | None = None) -> bool:
        if path is None:
            return False
        env_path = Path(path)
        if not env_path.exists():
            return False
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return True


BASE_PATH = Path(__file__).resolve().parent
load_dotenv(BASE_PATH / ".env")


APPLICATION_ROOT = "/emails"
STATIC_URL_PATH = "/static"
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
BASE_URL = os.getenv("BASE_URL", "https://app.cambridgepicture.com/emails")
SQLITE_PATH = os.getenv("SQLITE_PATH", str(BASE_PATH / "data" / "emails.db"))
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


class Config:
    SECRET_KEY = SECRET_KEY
    BASE_URL = BASE_URL
    SQLITE_PATH = SQLITE_PATH
    APPLICATION_ROOT = APPLICATION_ROOT
    STATIC_URL_PATH = STATIC_URL_PATH
