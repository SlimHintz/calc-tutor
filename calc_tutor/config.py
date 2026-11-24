"""All configuration comes from the environment (see .env.example)."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo


def auth_token() -> str | None:
    return os.environ.get("CALC_TUTOR_TOKEN") or None


def db_path() -> Path:
    return Path(os.environ.get("CALC_TUTOR_DB_PATH", "~/calc-tutor-data/tutor.db")).expanduser()


def log_path() -> Path:
    return Path(os.environ.get("CALC_TUTOR_LOG_PATH", "~/calc-tutor-data/auth.log")).expanduser()


def public_url() -> str:
    return os.environ.get("CALC_TUTOR_PUBLIC_URL", "").rstrip("/")


def host() -> str:
    return os.environ.get("CALC_TUTOR_HOST", "127.0.0.1")


def port() -> int:
    return int(os.environ.get("CALC_TUTOR_PORT", "8830"))


def ntfy_url() -> str | None:
    """Full ntfy topic URL, e.g. https://ntfy.sh/calc-tutor-<random>. Unset = in-app inbox only."""
    return os.environ.get("CALC_TUTOR_NTFY_URL") or None


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("CALC_TUTOR_TZ", "UTC"))


def scheduler_enabled() -> bool:
    return os.environ.get("CALC_TUTOR_SCHEDULER", "1") == "1"


def remarkable_host() -> str | None:
    """SSH destination for the reMarkable (e.g. an ~/.ssh/config alias). Unset = feature off."""
    return os.environ.get("CALC_TUTOR_REMARKABLE_HOST") or None


def remarkable_dir() -> Path:
    """Where reMarkable files are mirrored and captures kept (PDFs can be large)."""
    return Path(os.environ.get("CALC_TUTOR_REMARKABLE_DIR",
                               "~/calc-tutor-data/remarkable")).expanduser()


def books_dir() -> Path:
    """Book PDFs (the `local` paths in _books.yaml are relative to this, minus 'books/')."""
    return Path(os.environ.get("CALC_TUTOR_BOOKS_DIR",
                               str(Path(__file__).resolve().parents[1] / "books"))).expanduser()


def vault_dir() -> Path:
    """Where the Obsidian export is written (sync it into your notes vault however you like)."""
    return Path(os.environ.get("CALC_TUTOR_VAULT_DIR",
                               "~/calc-tutor-data/vault")).expanduser()
