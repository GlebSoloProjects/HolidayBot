from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence

from aiohttp import ClientError, ClientSession, ClientTimeout

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CALEND_RU_URL = "https://www.calend.ru/day/"
MOSCOW_TZ = ZoneInfo("Europe/Moscow") if ZoneInfo else None


@dataclass(slots=True)
class HolidayResult:
    date: date
    holidays: tuple[str, ...]
    source_url: str
    fetched_at: datetime
    error: str | None = None

    @property
    def has_data(self) -> bool:
        return bool(self.holidays)


class _HolidayAnchorParser(HTMLParser):
    __slots__ = ("_target_div_id", "_inside_target", "_target_depth", "_capture", "_buffer", "_holidays")

    def __init__(self, target_div_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self._target_div_id = target_div_id
        self._inside_target = False
        self._target_depth = 0
        self._capture = False
        self._buffer: list[str] = []
        self._holidays: list[str] = []

    def feed(self, data: str) -> Sequence[str]:  # type: ignore[override]
        super().feed(data)
        return tuple(self._holidays)

    def handle_starttag(self, tag: str, attrs: Iterable[tuple[str, str | None]]) -> None:
        if tag == "div":
            attr_map = dict(attrs)
            if self._inside_target:
                self._target_depth += 1
            elif attr_map.get("id") == self._target_div_id:
                self._inside_target = True
                self._target_depth = 1
            return

        if not self._inside_target:
            return

        if tag == "a":
            href = dict(attrs).get("href") or ""
            if "/holidays/0/0/" in href:
                self._capture = True
                self._buffer.clear()

    def handle_endtag(self, tag: str) -> None:
        if self._inside_target and tag == "div":
            self._target_depth -= 1
            if self._target_depth <= 0:
                self._inside_target = False
                self._target_depth = 0
        elif tag == "a" and self._capture:
            text = "".join(self._buffer).strip()
            if text:
                self._holidays.append(text)
            self._capture = False
            self._buffer.clear()

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)


_cache_file: Path | None = None
_cache_payload: dict[str, Any] | None = None
_cached_result: HolidayResult | None = None
_refresh_lock = asyncio.Lock()
_autopost_event: asyncio.Event | None = None


def initialize_holiday_cache(cache_path: Path, default_autopost_time: str) -> None:
    """Bind the persistent cache file and load existing data."""
    global _cache_file, _cache_payload
    _cache_file = cache_path
    _cache_payload = _load_or_init_payload(cache_path, default_autopost_time)
    entry = (_cache_payload or {}).get("today", {})
    result = _payload_entry_to_result(entry)
    if result:
        _cache_store(result)


def register_autopost_event(event: asyncio.Event) -> None:
    global _autopost_event
    _autopost_event = event


def get_autopost_time() -> str:
    payload = _ensure_payload()
    return str(payload.get("autopost_time", "00:00"))


def update_autopost_time(value: str) -> str:
    normalized = _normalize_time(value)
    payload = _ensure_payload()
    if payload.get("autopost_time") == normalized:
        return normalized
    payload["autopost_time"] = normalized
    _write_payload(payload)
    _notify_autopost_update()
    return normalized


async def ensure_holidays_for_date(target_date: date) -> HolidayResult | None:
    cached = get_cached_holiday_result(target_date)
    if cached:
        return cached
    await refresh_holiday_cache()
    return get_cached_holiday_result(target_date)


def get_cached_holiday_result(target_date: date) -> HolidayResult | None:
    payload = _ensure_payload()
    for key in ("today", "tomorrow"):
        entry = payload.get(key) or {}
        raw_date = entry.get("date")
        if not raw_date:
            continue
        try:
            entry_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if entry_date == target_date:
            return _payload_entry_to_result(entry)
    return None


async def get_today_holidays(
    *,
    now: datetime | None = None,
    force_refresh: bool = False,
    session: ClientSession | None = None,
) -> HolidayResult:
    moment = _normalize_now(now)
    target_date = moment.date()

    if not force_refresh:
        cached = get_cached_holiday_result(target_date)
        if cached:
            _cache_store(cached)
            return cached

    try:
        result = await refresh_holiday_cache(now=moment, session=session)
        if result:
            return result
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to refresh holiday cache: %s", exc)

    cached = get_cached_holiday_result(target_date)
    if cached:
        cached = cached.__class__(
            date=cached.date,
            holidays=cached.holidays,
            source_url=cached.source_url,
            fetched_at=cached.fetched_at,
            error="Не удалось обновить данные о праздниках, показаны сохранённые ранее.",
        )
        _cache_store(cached)
        return cached

    fallback = HolidayResult(
        date=target_date,
        holidays=(),
        source_url=CALEND_RU_URL,
        fetched_at=moment,
        error="Не удалось получить данные о праздниках.",
    )
    _cache_store(fallback)
    return fallback


async def refresh_holiday_cache(
    *,
    now: datetime | None = None,
    session: ClientSession | None = None,
) -> HolidayResult | None:
    """Refresh the JSON cache for today and tomorrow.

    Called nightly at 23:50 MSK to fetch tomorrow's holidays in advance.
    Any other manual refresh works as a regular today/tomorrow update.
    """

    moment = _normalize_now(now)
    async with _refresh_lock:
        html = await _download_html(session=session)
        current_date = moment.date()

        hour = moment.hour
        minute = moment.minute
        is_near_midnight = hour == 23 and minute >= 45

        if is_near_midnight:
            today_date = current_date + timedelta(days=1)
            tomorrow_date = current_date + timedelta(days=2)
            logger.info(
                "Refreshing cache for tomorrow (23:50 logic): today=%s, tomorrow=%s",
                today_date,
                tomorrow_date,
            )
        else:
            today_date = current_date
            tomorrow_date = current_date + timedelta(days=1)
            logger.info(
                "Refreshing cache for today (normal refresh): today=%s, tomorrow=%s",
                today_date,
                tomorrow_date,
            )

        today_holidays = tuple(_parse_holidays(html, today_date))
        tomorrow_holidays = tuple(_parse_holidays(html, tomorrow_date))

        payload = _ensure_payload()
        payload["today"] = _serialize_day(today_date, today_holidays, moment)
        payload["tomorrow"] = _serialize_day(tomorrow_date, tomorrow_holidays, moment)
        payload["updated_at"] = _format_datetime(moment)
        _write_payload(payload)

        result = _payload_entry_to_result(payload["today"])
        if result:
            _cache_store(result)
        return result


def select_autopost_holiday(holidays: Sequence[str]) -> str | None:
    if not holidays:
        return None
    without_russia = [item for item in holidays if "россия" not in item.lower() and "russia" not in item.lower()]
    if without_russia:
        return without_russia[0]
    return holidays[0]


def _serialize_day(target_date: date, holidays: Sequence[str], fetched_at: datetime) -> dict[str, Any]:
    return {
        "date": target_date.isoformat(),
        "holidays": list(holidays),
        "fetched_at": _format_datetime(fetched_at),
        "source_url": CALEND_RU_URL,
    }


def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        if MOSCOW_TZ:
            return datetime.now(MOSCOW_TZ)
        return datetime.now()
    if MOSCOW_TZ and value.tzinfo is None:
        return value.replace(tzinfo=MOSCOW_TZ)
    if MOSCOW_TZ:
        return value.astimezone(MOSCOW_TZ)
    return value


async def _download_html(*, session: ClientSession | None = None, timeout: float = 10.0) -> str:
    if session is None:
        client_timeout = ClientTimeout(total=timeout)
        async with ClientSession(timeout=client_timeout) as owned_session:
            return await _download_html(session=owned_session, timeout=timeout)

    try:
        async with session.get(
            CALEND_RU_URL,
            headers={"User-Agent": "HolidayBot/1.0 (+https://github.com/gleb/WelcomeBot)"},
        ) as response:
            response.raise_for_status()
            return await response.text()
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Превышено время ожидания ответа calend.ru") from exc
    except ClientError as exc:
        raise RuntimeError("Ошибка сети при обращении к calend.ru") from exc


def _parse_holidays(html: str, target_date: date) -> Sequence[str]:
    parser = _HolidayAnchorParser(f"div_{target_date:%Y-%m-%d}")
    return parser.feed(html)


def _payload_entry_to_result(entry: dict[str, Any]) -> HolidayResult | None:
    raw_date = entry.get("date")
    if not raw_date:
        return None
    try:
        parsed_date = date.fromisoformat(raw_date)
    except ValueError:
        return None

    holidays = tuple(entry.get("holidays", ()))
    fetched_at = _parse_datetime(entry.get("fetched_at")) or _normalize_now(None)
    source_url = entry.get("source_url") or CALEND_RU_URL
    error = None if holidays else "Не найдено праздников на сегодня."
    return HolidayResult(
        date=parsed_date,
        holidays=holidays,
        source_url=source_url,
        fetched_at=fetched_at,
        error=error,
    )


def _load_or_init_payload(cache_path: Path, default_autopost_time: str) -> dict[str, Any]:
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Holiday cache corrupted (%s), recreating.", exc)
            payload = _default_payload(default_autopost_time)
    else:
        payload = _default_payload(default_autopost_time)

    if not payload.get("autopost_time"):
        payload["autopost_time"] = default_autopost_time
    if not payload.get("today"):
        payload["today"] = _serialize_day(date.today(), (), _normalize_now(None))
    if not payload.get("tomorrow"):
        payload["tomorrow"] = _serialize_day(date.today() + timedelta(days=1), (), _normalize_now(None))

    _write_payload(payload, cache_path=cache_path)
    return payload


def _default_payload(autopost_time: str) -> dict[str, Any]:
    moment = _normalize_now(None)
    return {
        "autopost_time": autopost_time,
        "updated_at": _format_datetime(moment),
        "today": _serialize_day(moment.date(), (), moment),
        "tomorrow": _serialize_day(moment.date() + timedelta(days=1), (), moment),
    }


def _ensure_payload() -> dict[str, Any]:
    global _cache_payload
    if _cache_payload is None:
        if _cache_file is None:
            raise RuntimeError("Holiday cache is not initialized")
        _cache_payload = _load_or_init_payload(_cache_file, "00:00")
    return _cache_payload


def _write_payload(payload: dict[str, Any], *, cache_path: Path | None = None) -> None:
    target_path = cache_path or _cache_file
    if target_path is None:
        return
    try:
        target_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover
        logger.warning("Failed to persist holiday cache: %s", exc)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None and MOSCOW_TZ:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _normalize_now(value).isoformat()


def _normalize_time(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Время должно быть в формате ЧЧ:ММ")
    valuestrip = value.strip()
    if not valuestrip:
        raise ValueError("Время должно быть в формате ЧЧ:ММ")
    parts = valuestrip.split(":")
    if len(parts) != 2:
        raise ValueError("Время должно быть в формате ЧЧ:ММ")
    hour, minute = parts
    try:
        hour_int = int(hour)
        minute_int = int(minute)
    except ValueError as exc:
        raise ValueError("Время должно быть в формате ЧЧ:ММ") from exc
    if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
        raise ValueError("Недопустимое значение часов или минут")
    return f"{hour_int:02d}:{minute_int:02d}"


def _notify_autopost_update() -> None:
    if _autopost_event is not None:
        _autopost_event.set()


def _cache_store(result: HolidayResult) -> None:
    global _cached_result
    _cached_result = result


