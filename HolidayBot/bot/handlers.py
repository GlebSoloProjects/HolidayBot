from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ChatMemberStatus
from aiogram.types import Message, User
from aiogram import Bot

from config import config
from .messages import format_holidays_digest
from .utils.holidays import get_autopost_time, get_today_holidays, update_autopost_time

router = Router()
if config.target_chat_id is not None:
    router.message.filter(F.chat.id == config.target_chat_id)


@router.message(CommandStart())
async def command_start(message: Message) -> None:
    await message.answer(
        "Привет! Этот бот отвечает за праздники чата. "
        "Используйте /holidays или дождитесь автоматической рассылки."
    )


@router.message(Command("holidays"))
async def command_holidays(message: Message) -> None:
    if not _is_allowed_chat(message):
        return
    result = await get_today_holidays()
    await message.answer(format_holidays_digest(result))


@router.message(Command("holidaystime"))
async def command_holidaystime(message: Message, bot: Bot) -> None:
    if config.target_chat_id is None:
        await message.answer(
            "ID чата ещё не настроен. Сначала выполните /chatid в нужном чате и "
            "укажите значение в HolidayBot/config/settings.json."
        )
        return
    if not await _is_chat_admin(bot, message.from_user):
        await message.answer("Команда доступна только администраторам.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await message.answer(f"Текущее время автопубликации: {get_autopost_time()} (МСК).")
        return

    new_time = parts[1].strip()
    if not new_time:
        await message.answer("Укажите время в формате ЧЧ:ММ, например 08:30.")
        return
    try:
        normalized = update_autopost_time(new_time)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    await message.answer(f"Время автопубликации обновлено: {normalized} (МСК).")


@router.message(Command("chatid"))
async def command_chat_id(message: Message) -> None:
    chat_id = message.chat.id
    await message.answer(
        f"ID этого чата: <code>{chat_id}</code>\n"
        "Укажите его в файле HolidayBot/config/settings.json, чтобы бот работал только здесь."
    )


def _is_allowed_chat(message: Message) -> bool:
    if config.target_chat_id is None:
        return True
    return message.chat.id == config.target_chat_id


async def _is_chat_admin(bot: Bot, from_user: User | None) -> bool:
    if from_user is None or from_user.id is None:
        return False
    if from_user.id in config.admin_user_ids:
        return True
    try:
        member = await bot.get_chat_member(config.target_chat_id, from_user.id)
    except TelegramBadRequest:
        return False
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


