import asyncio
import logging
import statistics

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .config import Config
from .db import Database
from .vinted_client import VintedClient, VintedItem

logger = logging.getLogger(__name__)

# Присылаем вещь только если она минимум на столько дешевле медианной цены
# похожих активных объявлений (тот же бренд + похожее название) — грубая
# прикидка "можно перепродать дороже", других источников рыночной цены у нас нет.
DEAL_MAX_PRICE_RATIO = 0.8
DEAL_MIN_SIMILAR_SAMPLES = 3


async def _find_deal_median(
    vinted: VintedClient, item: VintedItem, brand_id: int
) -> float | None:
    try:
        prices = await vinted.fetch_similar_total_prices(item.title, brand_id, item.item_id)
    except Exception:
        logger.exception("Не удалось оценить похожие цены для item %s", item.item_id)
        return None

    if len(prices) < DEAL_MIN_SIMILAR_SAMPLES:
        return None

    median_price = statistics.median(prices)
    try:
        item_price = float(item.total_price)
    except ValueError:
        return None

    if item_price <= median_price * DEAL_MAX_PRICE_RATIO:
        return median_price
    return None


def _format_caption(item: VintedItem, median_price: float) -> str:
    lines = [f"<b>{item.title}</b>"]
    price_line = f"{item.total_price} {item.currency}"
    if item.brand_title:
        price_line = f"{item.brand_title} — {price_line}"
    lines.append(price_line)
    lines.append(f"💰 похожие объявления обычно от ~{median_price:.0f} {item.currency}")
    details = []
    if item.size_title:
        details.append(f"размер {item.size_title}")
    if item.status:
        details.append(item.status)
    if details:
        lines.append(", ".join(details))
    if item.seller_login:
        lines.append(f"продавец: {item.seller_login}")
    lines.append(item.url)
    return "\n".join(lines)


async def _notify(bot: Bot, chat_id: int, item: VintedItem, median_price: float) -> None:
    caption = _format_caption(item, median_price)
    try:
        if item.photo_url:
            await bot.send_photo(chat_id, photo=item.photo_url, caption=caption, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, caption, parse_mode="HTML")
        logger.info(
            "Отправлена находка чату %s: item=%s %r цена=%s%s медиана=%.0f%s",
            chat_id, item.item_id, item.title, item.total_price, item.currency,
            median_price, item.currency,
        )
    except TelegramAPIError:
        logger.exception("Не удалось отправить уведомление в чат %s", chat_id)


async def poll_once(bot: Bot, db: Database, vinted: VintedClient, config: Config) -> None:
    chat_ids = await db.get_active_chats_with_brands()
    logger.debug("Опрашиваю %s активных чатов", len(chat_ids))
    for chat_id in chat_ids:
        try:
            chat = await db.get_chat(chat_id)
            brands = await db.list_brands(chat_id)
            brand_ids = [b.brand_id for b in brands]

            items = await vinted.fetch_newest_items(
                brand_ids=brand_ids,
                price_to=chat.max_price,
                per_page=config.items_per_page,
            )
            if not items:
                continue

            newest_id_in_batch = max(it.item_id for it in items)

            if chat.last_seen_id == 0:
                # Первый прогон для этого чата: не спамим всей историей,
                # просто запоминаем текущую границу "новых" объявлений.
                logger.info("Чат %s: первый опрос, запоминаю границу id=%s", chat_id, newest_id_in_batch)
                await db.update_last_seen_id(chat_id, newest_id_in_batch)
                continue

            new_items = [it for it in items if it.item_id > chat.last_seen_id]
            new_items.sort(key=lambda it: it.item_id)

            if new_items:
                logger.info("Чат %s: найдено %s новых объявлений", chat_id, len(new_items))

            brand_id_by_title = {b.brand_title.lower(): b.brand_id for b in brands}

            sent = 0
            for it in new_items:
                if sent >= config.max_notifications_per_poll:
                    logger.info("Чат %s: достигнут лимит уведомлений за опрос (%s)", chat_id, config.max_notifications_per_poll)
                    break
                brand_id = brand_id_by_title.get(it.brand_title.lower())
                if brand_id is None:
                    logger.warning("Чат %s: не найден brand_id для %r, пропускаю item %s", chat_id, it.brand_title, it.item_id)
                    continue
                median_price = await _find_deal_median(vinted, it, brand_id)
                if median_price is None:
                    logger.debug("Item %s (%r) не признан выгодным, пропускаю", it.item_id, it.title)
                    continue
                await _notify(bot, chat_id, it, median_price)
                sent += 1

            await db.update_last_seen_id(chat_id, max(newest_id_in_batch, chat.last_seen_id))

        except Exception:
            logger.exception("Ошибка при опросе Vinted для чата %s", chat_id)


async def poll_loop(bot: Bot, db: Database, vinted: VintedClient, config: Config) -> None:
    while True:
        await poll_once(bot, db, vinted, config)
        await asyncio.sleep(config.poll_interval_seconds)
