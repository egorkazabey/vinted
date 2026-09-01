import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from vinted_bot.config import load_config
from vinted_bot.db import Database
from vinted_bot.handlers import router
from vinted_bot.poller import poll_loop
from vinted_bot.vinted_client import VintedClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()

    db = Database(config.db_path)
    await db.connect()

    vinted = VintedClient(config.vinted_domain)

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    dp["db"] = db
    dp["vinted"] = vinted

    poller_task = asyncio.create_task(poll_loop(bot, db, vinted, config))

    try:
        await dp.start_polling(bot)
    finally:
        poller_task.cancel()
        await vinted.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
