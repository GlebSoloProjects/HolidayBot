"""Entry point so `python HolidayBot/bot.py` runs the holidays bot."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys


def _ensure_sys_path() -> None:
    """Guarantee that HolidayBot directory is importable as a package."""
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def main() -> None:
    _ensure_sys_path()
    from main import main as run_main  # noqa: WPS433 (runtime import)

    asyncio.run(run_main())


if __name__ == "__main__":
    main()


