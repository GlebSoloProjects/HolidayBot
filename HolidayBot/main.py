from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import BotCommand

from config import config
from bot.handlers import router
from bot.messages import format_holidays_digest
from bot.utils.holidays import (
    ensure_holidays_for_date,
    get_autopost_time,
    initialize_holiday_cache,
    refresh_holiday_cache,
    register_autopost_event,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

MOSCOW_TZ = ZoneInfo("Europe/Moscow") if ZoneInfo else None


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    initialize_holiday_cache(
        config.holidays_cache_path,
        config.holidays_autopost_time,
    )

    try:
        await refresh_holiday_cache()
    except Exception as exc:  # pragma: no cover
        logging.warning("Initial holiday cache refresh failed: %s", exc)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    bot = Bot(
        token=config.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await _startup_warnings(bot)

    autopost_event = asyncio.Event()
    register_autopost_event(autopost_event)

    refresh_task = asyncio.create_task(_holiday_cache_refresh_loop())
    autopost_task = asyncio.create_task(_holiday_autopost_loop(bot, autopost_event))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramNetworkError as exc:
        logging.warning("Не удалось снять вебхук: %s. Продолжаем polling.", exc)

    await _setup_commands(bot)

    try:
        await dispatcher.start_polling(bot)
    finally:
        refresh_task.cancel()
        autopost_task.cancel()
        await asyncio.gather(refresh_task, autopost_task, return_exceptions=True)


async def _setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Описание HolidayBot"),
            BotCommand(command="holidays", description="Праздники на сегодня"),
            BotCommand(command="holidaystime", description="Время автопубликации"),
        ]
    )


async def _holiday_cache_refresh_loop() -> None:
    """Fetch tomorrow's holidays each day at 23:50 MSK."""

    while True:
        now = _moscow_now()
        next_run = _next_run_at(now, hour=23, minute=50)
        delay = max(0.0, (next_run - now).total_seconds())
        logging.info(
            "Holiday cache refresh: current=%s next=%s delay=%.1fs",
            now.strftime("%Y-%m-%d %H:%M:%S"),
            next_run.strftime("%Y-%m-%d %H:%M:%S"),
            delay,
        )
        await asyncio.sleep(delay)
        try:
            await refresh_holiday_cache()
            logging.info("Holiday cache refresh finished successfully.")
        except Exception as exc:  # pragma: no cover
            logging.error("Holiday cache refresh failed: %s", exc, exc_info=True)


async def _holiday_autopost_loop(bot: Bot, update_event: asyncio.Event) -> None:
    """Send cached holidays to the target chat at configured Moscow time."""

    while True:
        try:
            now = _moscow_now()
            hour, minute = _parse_time_string(get_autopost_time())
            next_run = _next_run_at(now, hour=hour, minute=minute)
            delay = max(0.0, (next_run - now).total_seconds())
            logging.info(
                "Holiday autopost: current=%s target=%s delay=%.1fs",
                now.strftime("%Y-%m-%d %H:%M:%S"),
                next_run.strftime("%Y-%m-%d %H:%M:%S"),
                delay,
            )
            try:
                await asyncio.wait_for(update_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                update_event.clear()
                await _send_holiday_digest(bot)
            else:
                update_event.clear()
                logging.info("Holiday autopost: time updated, recalculating schedule.")
                continue
        except Exception as exc:
            logging.error("Holiday autopost loop error: %s", exc, exc_info=True)
            await asyncio.sleep(60)


async def _send_holiday_digest(bot: Bot) -> None:
    if config.target_chat_id is None:
        logging.warning("Holiday autopost skipped: target_chat_id is not configured.")
        return
    current_date = _moscow_now().date()
    result = await ensure_holidays_for_date(current_date)
    if result is None or not result.holidays:
        logging.warning("Holiday autopost: no holidays found for %s", current_date)
        return

    message_text = format_holidays_digest(result)
    await bot.send_message(config.target_chat_id, message_text)
    logging.info("Holiday autopost: digest sent for %s", current_date)


def _parse_time_string(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":", maxsplit=1)
    return int(hour_str), int(minute_str)


def _moscow_now() -> datetime:
    if MOSCOW_TZ:
        return datetime.now(MOSCOW_TZ)
    return datetime.now()


def _next_run_at(now: datetime, *, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


async def _startup_warnings(bot: Bot) -> None:
    if config.target_chat_id is None:
        instructions = [
            "target_chat_id не задан. HolidayBot продолжит работать только для команды /chatid.",
            "Инструкция:",
            "  1) Добавьте бота в нужный чат.",
            "  2) В этом чате выполните /chatid — бот отправит ID.",
            "  3) Пропишите ID в HolidayBot/config/settings.json и перезапустите бота.",
            "  4) Выдайте боту права администратора.",
        ]
        logging.warning(_format_box(instructions))
        return

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(config.target_chat_id, me.id)
    except TelegramBadRequest as exc:
        logging.warning(
            "Не удалось проверить права бота в чате %s: %s. "
            "Убедитесь, что бот добавлен в чат и назначен администратором.",
            config.target_chat_id,
            exc,
        )
        return

    if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        logging.warning(
            _format_box(
                [
                    f"HolidayBot добавлен в чат {config.target_chat_id}, но не является администратором.",
                    "Дайте боту права админа, чтобы автопост и управление заголовком работали корректно.",
                ]
            )
        )
        return

    logging.info(
        _format_box(
            [
                "╔══════════════════════════════╗",
                " ✔ УСПЕШНЫЙ ЗАПУСК       ",
                "╚══════════════════════════════╝",
                "",
                "HolidayBot настроен и готов к работе.",
                f"Чат: {config.target_chat_id}",
                "Праздники будут публиковаться по расписанию автопоста.",
                "",
                "Поддержать разработчика:",
                "  • Pixel-ut.pro",
                "  • yachtproject.space",
                "  • Telegram: https://t.me/PLAmong",
            ]
        )
    )


def _format_box(lines: list[str]) -> str:
    width = max(len(line) for line in lines)
    border = "═" * (width + 2)
    boxed_lines = [f"\n╔{border}╗"]
    for line in lines:
        padded = line.ljust(width)
        boxed_lines.append(f"║ {padded} ║")
    boxed_lines.append(f"╚{border}╝")
    return "\n".join(boxed_lines)



if __name__ == "__main__":
    asyncio.run(main())


