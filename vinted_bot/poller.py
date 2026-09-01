import asyncio
import logging
import statistics

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .config import Config
from .db import Database
from .gemini_client import DealVerdict, GeminiClient
from .vinted_client import VintedClient, VintedItem

logger = logging.getLogger(__name__)

# Нужно хотя бы столько похожих активных объявлений, чтобы дать Gemini осмысленный
# рыночный контекст (медианную цену). Меньше — Gemini судит по своим знаниям о рынке.
DEAL_MIN_SIMILAR_SAMPLES = 3


async def _evaluate_item(
    vinted: VintedClient, gemini: GeminiClient, item: VintedItem, brand_id: int
) -> tuple[DealVerdict, float | None] | None:
    try:
        prices = await vinted.fetch_similar_total_prices(item.title, brand_id, item.item_id)
    except Exception:
        logger.exception("Не удалось получить похожие цены для item %s", item.item_id)
        prices = []

    median_price = statistics.median(prices) if len(prices) >= DEAL_MIN_SIMILAR_SAMPLES else None

    verdict = await gemini.evaluate_deal(item, median_price, len(prices))
    if verdict is None or not verdict.is_good_deal:
        return None
    return verdict, median_price


def _format_caption(item: VintedItem, verdict: DealVerdict, median_price: float | None) -> str:
    lines = [f"<b>{item.title}</b>"]
    price_line = f"{item.total_price} {item.currency}"
    if item.brand_title:
        price_line = f"{item.brand_title} — {price_line}"
    lines.append(price_line)
    if median_price is not None:
        lines.append(f"💰 похожие объявления обычно от ~{median_price:.0f} {item.currency}")
    lines.append(f"🤖 {verdict.reason}")
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


async def _notify(
    bot: Bot, chat_id: int, item: VintedItem, verdict: DealVerdict, median_price: float | None
) -> None:
    caption = _format_caption(item, verdict, median_price)
    try:
        if item.photo_url:
            await bot.send_photo(chat_id, photo=item.photo_url, caption=caption, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, caption, parse_mode="HTML")
        logger.info(
            "Отправлена находка чату %s: item=%s %r цена=%s%s причина=%r",
            chat_id, item.item_id, item.title, item.total_price, item.currency, verdict.reason,
        )
    except TelegramAPIError:
        logger.exception("Не удалось отправить уведомление в чат %s", chat_id)


async def poll_once(
    bot: Bot, db: Database, vinted: VintedClient, gemini: GeminiClient, config: Config
) -> None:
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
                evaluation = await _evaluate_item(vinted, gemini, it, brand_id)
                if evaluation is None:
                    logger.debug("Item %s (%r) не признан выгодным, пропускаю", it.item_id, it.title)
                    continue
                verdict, median_price = evaluation
                await _notify(bot, chat_id, it, verdict, median_price)
                sent += 1

            await db.update_last_seen_id(chat_id, max(newest_id_in_batch, chat.last_seen_id))

        except Exception:
            logger.exception("Ошибка при опросе Vinted для чата %s", chat_id)


async def poll_loop(
    bot: Bot, db: Database, vinted: VintedClient, gemini: GeminiClient, config: Config
) -> None:
    while True:
        try:
            await poll_once(bot, db, vinted, gemini, config)
        except Exception:
            logger.exception("Опрос Vinted упал с ошибкой, продолжаю после паузы")
        await asyncio.sleep(config.poll_interval_seconds)
