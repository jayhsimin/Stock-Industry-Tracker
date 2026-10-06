import os
from datetime import date


def load_env(path: str = ".env") -> None:
    """Minimal .env loader (no python-dotenv dependency): sets os.environ
    for each KEY=VALUE line, without overwriting a var already set in the
    real environment. Silently does nothing if the file doesn't exist."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


def roc_to_iso(roc_date: str) -> str:
    """Convert TWSE ROC-calendar date string (e.g. '1150924') to ISO 'YYYY-MM-DD'."""
    roc_date = roc_date.strip()
    year = int(roc_date[:-4]) + 1911
    month = roc_date[-4:-2]
    day = roc_date[-2:]
    return f"{year}-{month}-{day}"


def today_iso() -> str:
    return date.today().isoformat()


def compact(iso_date: str) -> str:
    """'2026-09-24' -> '20260924'"""
    return iso_date.replace("-", "")
