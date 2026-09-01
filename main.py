import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from vinted_bot.config import load_config
from vinted_bot.db import Database
from vinted_bot.gemini_client import GeminiClient
from vinted_bot.handlers import router
from vinted_bot.poller import poll_loop
from vinted_bot.vinted_client import VintedClient

logger = logging.getLogger(__name__)


def setup_logging(log_file: str, log_level: str) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)


async def main() -> None:
    config = load_config()
    setup_logging(config.log_file, config.log_level)

    db = Database(config.db_path)
    await db.connect()

    vinted = VintedClient(config.vinted_domain)
    gemini = GeminiClient(config.gemini_api_key, config.gemini_model)

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    dp["db"] = db
    dp["vinted"] = vinted

    poller_task = asyncio.create_task(poll_loop(bot, db, vinted, gemini, config))

    def _log_poller_crash(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("poller_task неожиданно завершился с ошибкой", exc_info=exc)

    poller_task.add_done_callback(_log_poller_crash)

    try:
        await dp.start_polling(bot)
    finally:
        poller_task.cancel()
        await vinted.close()
        await gemini.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
