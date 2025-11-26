from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    target_chat_id: int | None
    holidays_cache_path: Path
    holidays_autopost_time: str
    admin_user_ids: tuple[int, ...]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Settings file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse HolidayBot settings ({path}): {exc}") from exc


def _ensure_token(value: Any) -> str:
    if not value or not isinstance(value, str):
        raise RuntimeError("Telegram token must be set in HolidayBot/config/settings.json")
    return value


def _ensure_chat_id(value: Any) -> int | None:
    try:
        chat_id = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("target_chat_id must be an integer") from exc
    if chat_id == 0:
        return None
    return chat_id


def _ensure_time(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeError("holidays_autopost_time must be string in HH:MM format")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise RuntimeError("holidays_autopost_time must be in HH:MM format")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise RuntimeError("holidays_autopost_time must be in HH:MM format") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RuntimeError("holidays_autopost_time has invalid hour or minute")
    return f"{hour:02d}:{minute:02d}"


def _resolve_path(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError("holidays_cache_path must be a non-empty string")
    candidate = (BASE_DIR.parent / value).resolve()
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _ensure_admin_ids(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, int):
        return (value,)
    if not isinstance(value, Iterable):
        raise RuntimeError("admin_user_ids must be a list of integers")
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("admin_user_ids must contain integers only") from exc
    return tuple(result)


def load_config(path: Path | None = None) -> Config:
    payload = _load_json(path or SETTINGS_PATH)

    return Config(
        token=_ensure_token(payload.get("token")),
        target_chat_id=_ensure_chat_id(payload.get("target_chat_id")),
        holidays_cache_path=_resolve_path(payload.get("holidays_cache_path", "data/holidays.json")),
        holidays_autopost_time=_ensure_time(payload.get("holidays_autopost_time", "00:00")),
        admin_user_ids=_ensure_admin_ids(payload.get("admin_user_ids", ())),
    )


try:
    config = load_config()
except RuntimeError as exc:  # pragma: no cover - startup guard
    import sys
    import traceback

    traceback.print_exception(exc.__class__, exc, exc.__traceback__, file=sys.stderr)

    border = "═" * 58
    setup_hint = (
        f"\n╔{border}╗\n"
        "║ ⚠️  HolidayBot не настроен.                           \n"
        "║ Отредактируйте файл HolidayBot/config/settings.json   \n"
        "║ и заполните поля:                                     \n"
        "║   • token — токен Telegram-бота                       \n"
        "║   • target_chat_id — ID чата/канала (целое число)     \n"
        "║   • holidays_cache_path — путь до JSON-кэша           \n"
        "║       (по умолчанию data/holidays.json, можно оставить)\n"
        "║   • holidays_autopост_time — время ЧЧ:ММ              \n"
        "║   • admin_user_ids — список ID админов (опционально)  \n"
        f"╚{border}╝\n"
    )
    print(setup_hint, file=sys.stderr)
    raise SystemExit(1) from None


