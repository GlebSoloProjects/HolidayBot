from __future__ import annotations

from datetime import date

from .utils.holidays import HolidayResult


def format_holidays_digest(result: HolidayResult, limit: int = 10) -> str:
    if not result.holidays:
        return "🗓 Сегодня нет праздников."

    visible = result.holidays[:limit]
    lines = [
        "🎉 Праздники на сегодня:",
        "",
        *[f"{_select_holiday_emoji(item)} {item}" for item in visible],
    ]

    remaining = len(result.holidays) - len(visible)
    if remaining > 0:
        lines.append(f"… и ещё {remaining}")

    if result.error:
        lines.append("")
        lines.append(result.error)

    return "\n".join(lines)


def format_single_holiday(holiday_name: str, target_date: date) -> str:
    emoji = _select_holiday_emoji(holiday_name)
    return f"{emoji} Сегодня, {target_date:%d.%m.%Y}, {holiday_name}"


def _select_holiday_emoji(holiday_name: str) -> str:
    name_lower = holiday_name.lower()
    if "рождеств" in name_lower or "пасх" in name_lower:
        return "✝️"
    if "нов" in name_lower or "ёлк" in name_lower:
        return "🎄"
    if "день рождения" in name_lower or "birthday" in name_lower:
        return "🥳"
    if "памяти" in name_lower or "вспомин" in name_lower:
        return "🕯"
    if "день" in name_lower and "россии" in name_lower:
        return "🇷🇺"
    if "мир" in name_lower:
        return "🕊️"
    if "люб" in name_lower:
        return "💞"
    if "косм" in name_lower:
        return "🚀"
    if "арм" in name_lower or "защитник" in name_lower:
        return "🛡️"
    if "семь" in name_lower:
        return "👨‍👩‍👧"
    return "✨"


