import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .db import Database
from .vinted_client import BrandCandidate, VintedClient

logger = logging.getLogger(__name__)

router = Router()

# Временное хранилище результатов поиска брендов на время выбора пользователем:
# chat_id -> list[BrandCandidate]
_pending_brand_choices: dict[int, list[BrandCandidate]] = {}
# Остальные бренды из команды /addbrand с несколькими названиями через запятую,
# которые ещё предстоит обработать после текущего выбора: chat_id -> list[str]
_pending_brand_queue: dict[int, list[str]] = {}

HELP_TEXT = (
    "Я слежу за новыми объявлениями на Vinted и присылаю их сюда.\n\n"
    "<b>Команды:</b>\n"
    "/addbrand <название> — добавить бренд для отслеживания\n"
    "/removebrand <название> — убрать бренд из отслеживания\n"
    "/brands — список отслеживаемых брендов\n"
    "/setprice <сумма> — максимальная цена объявления\n"
    "/noprice — снять ограничение по цене\n"
    "/status — текущие настройки\n"
    "/pause — приостановить уведомления\n"
    "/resume — возобновить уведомления\n\n"
    "Добавьте хотя бы один бренд, чтобы бот начал присылать новые объявления."
)


@router.message(Command("start"))
async def cmd_start(message: Message, db: Database) -> None:
    await db.ensure_chat(message.chat.id)
    await message.answer(
        "Привет! " + HELP_TEXT,
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("addbrand"))
async def cmd_addbrand(message: Message, command: CommandObject, db: Database, vinted: VintedClient) -> None:
    queries = [q.strip() for q in (command.args or "").split(",")]
    queries = [q for q in queries if q]
    if not queries:
        await message.answer("Укажите название бренда: /addbrand Nike или /addbrand Nike, Adidas")
        return

    await _process_brand_queue(message, db, vinted, queries)


async def _process_brand_queue(
    message: Message, db: Database, vinted: VintedClient, queries: list[str]
) -> None:
    chat_id = message.chat.id
    while queries:
        query, *queries = queries
        try:
            candidates = await vinted.search_brand_candidates(query)
        except Exception:
            logger.exception("Ошибка поиска бренда %r", query)
            await message.answer(f"Не удалось выполнить поиск бренда «{query}» на Vinted. Попробуйте позже.")
            continue

        if not candidates:
            await message.answer(f"Бренд «{query}» не найден на Vinted.")
            continue

        if len(candidates) == 1:
            await _resolve_and_add(message, db, vinted, candidates[0])
            continue

        _pending_brand_choices[chat_id] = candidates
        _pending_brand_queue[chat_id] = queries
        buttons = [
            [InlineKeyboardButton(text=c.title, callback_data=f"addbrand:{i}")]
            for i, c in enumerate(candidates)
        ]
        await message.answer(
            f"Уточните бренд «{query}»:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return

    _pending_brand_queue.pop(chat_id, None)


async def _resolve_and_add(
    message: Message, db: Database, vinted: VintedClient, candidate: BrandCandidate
) -> None:
    try:
        brand_id = await vinted.resolve_brand_id(candidate)
    except Exception:
        logger.exception("Ошибка получения id бренда %r", candidate.title)
        brand_id = None

    if brand_id is None:
        await message.answer(
            f"Не удалось определить бренд «{candidate.title}» на Vinted. Попробуйте позже."
        )
        return

    added = await db.add_brand(message.chat.id, brand_id, candidate.title)
    logger.info(
        "Чат %s: бренд %r (id=%s) %s", message.chat.id, candidate.title, brand_id,
        "добавлен" if added else "уже был в списке",
    )
    await message.answer(
        f"Бренд «{candidate.title}» {'добавлен' if added else 'уже был в списке'}."
    )


@router.callback_query(F.data.startswith("addbrand:"))
async def cb_addbrand(callback: CallbackQuery, db: Database, vinted: VintedClient) -> None:
    chat_id = callback.message.chat.id
    choices = _pending_brand_choices.get(chat_id)
    if not choices:
        await callback.answer("Список устарел, повторите /addbrand", show_alert=True)
        return

    try:
        idx = int(callback.data.split(":", 1)[1])
        chosen = choices[idx]
    except (ValueError, IndexError):
        await callback.answer("Некорректный выбор", show_alert=True)
        return

    _pending_brand_choices.pop(chat_id, None)
    remaining = _pending_brand_queue.pop(chat_id, [])
    await callback.message.edit_text(f"Добавляю «{chosen.title}»…")
    await _resolve_and_add(callback.message, db, vinted, chosen)
    await callback.answer()

    if remaining:
        await _process_brand_queue(callback.message, db, vinted, remaining)


@router.message(Command("removebrand"))
async def cmd_removebrand(message: Message, command: CommandObject, db: Database) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Укажите название бренда: /removebrand Nike")
        return

    brands = await db.list_brands(message.chat.id)
    match = next((b for b in brands if b.brand_title.lower() == query.lower()), None)
    if not match:
        await message.answer(
            f"Бренд «{query}» не найден среди отслеживаемых. Посмотрите /brands."
        )
        return

    await db.remove_brand(message.chat.id, match.brand_id)
    logger.info("Чат %s: бренд %r (id=%s) удалён", message.chat.id, match.brand_title, match.brand_id)
    await message.answer(f"Бренд «{match.brand_title}» удалён из отслеживания.")


@router.message(Command("brands"))
async def cmd_brands(message: Message, db: Database) -> None:
    brands = await db.list_brands(message.chat.id)
    if not brands:
        await message.answer("Список брендов пуст. Добавьте бренд: /addbrand Nike")
        return
    lines = "\n".join(f"• {b.brand_title}" for b in brands)
    await message.answer(f"Отслеживаемые бренды:\n{lines}")


@router.message(Command("setprice"))
async def cmd_setprice(message: Message, command: CommandObject, db: Database) -> None:
    raw = (command.args or "").strip().replace(",", ".")
    if not raw:
        await message.answer("Укажите сумму: /setprice 500")
        return
    try:
        price = float(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Цена должна быть положительным числом, например: /setprice 500")
        return

    await db.set_max_price(message.chat.id, price)
    logger.info("Чат %s: установлена максимальная цена %s", message.chat.id, price)
    await message.answer(f"Максимальная цена установлена: {price:g}")


@router.message(Command("noprice"))
async def cmd_noprice(message: Message, db: Database) -> None:
    await db.set_max_price(message.chat.id, None)
    logger.info("Чат %s: ограничение по цене снято", message.chat.id)
    await message.answer("Ограничение по цене снято.")


@router.message(Command("status"))
async def cmd_status(message: Message, db: Database) -> None:
    chat = await db.get_chat(message.chat.id)
    brands = await db.list_brands(message.chat.id)
    brand_list = ", ".join(b.brand_title for b in brands) or "не заданы"
    price = f"до {chat.max_price:g}" if chat.max_price is not None else "без ограничений"
    state = "активны" if chat.active else "приостановлены"
    await message.answer(
        f"Бренды: {brand_list}\nЦена: {price}\nУведомления: {state}"
    )


@router.message(Command("pause"))
async def cmd_pause(message: Message, db: Database) -> None:
    await db.set_active(message.chat.id, False)
    logger.info("Чат %s: уведомления приостановлены", message.chat.id)
    await message.answer("Уведомления приостановлены. Возобновить: /resume")


@router.message(Command("resume"))
async def cmd_resume(message: Message, db: Database) -> None:
    await db.set_active(message.chat.id, True)
    logger.info("Чат %s: уведомления возобновлены", message.chat.id)
    await message.answer("Уведомления возобновлены.")
