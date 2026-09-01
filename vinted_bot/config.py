import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    vinted_domain: str
    poll_interval_seconds: int
    db_path: str
    items_per_page: int
    max_notifications_per_poll: int
    log_file: str
    log_level: str
    gemini_api_key: str
    gemini_model: str


def load_config() -> Config:
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируйте .env.example в .env и укажите токен бота."
        )

    return Config(
        bot_token=bot_token,
        vinted_domain=os.environ.get("VINTED_DOMAIN", "vinted.cz").strip().lstrip(".").removeprefix("www."),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "90")),
        db_path=os.environ.get("DB_PATH", "vinted_bot.sqlite3"),
        items_per_page=int(os.environ.get("ITEMS_PER_PAGE", "20")),
        max_notifications_per_poll=int(os.environ.get("MAX_NOTIFICATIONS_PER_POLL", "8")),
        log_file=os.environ.get("LOG_FILE", "bot.log"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip(),
    )
