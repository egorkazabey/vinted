import base64
import json
import logging
from dataclasses import dataclass

import httpx

from .vinted_client import VintedItem

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.0-flash"

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_good_deal": {"type": "BOOLEAN"},
        "reason": {"type": "STRING"},
    },
    "required": ["is_good_deal", "reason"],
}

_PROMPT_TEMPLATE = """Ты — эксперт по перепродаже одежды и аксессуаров на Vinted.
Оцени объявление и реши, можно ли эту вещь выгодно перепродать.

Объявление:
Название: {title}
Бренд: {brand}
Цена (с учётом защиты покупателя): {price} {currency}
Заявленное состояние: {status}
Размер: {size}

Рыночный контекст: {market_context}

Учти два фактора:
1. Действительно ли цена заметно ниже рыночной — так, что вещь можно перепродать дороже
   и заработать на разнице, а не просто "не переплатить".
2. Судя по приложенному фото (если оно есть), в каком состоянии вещь: есть ли видимые
   дефекты, сильный износ, следы подделки, либо фото настолько плохого качества, что
   состояние не оценить, — в таких случаях перепродажа рискованна.

Если сомневаешься — is_good_deal должен быть false. reason — краткое обоснование на
русском (1-2 предложения), которое можно показать пользователю."""


@dataclass
class DealVerdict:
    is_good_deal: bool
    reason: str


class GeminiClient:
    """Тонкий клиент над Gemini API (generateContent) для оценки вещей на перепродажу."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self._model = model
        # Отдельный клиент только для вызовов Gemini — ключ передаётся заголовком
        # и не должен попадать в запросы к сторонним хостам (например, к CDN с фото).
        self._api_client = httpx.AsyncClient(
            base_url="https://generativelanguage.googleapis.com",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            timeout=30.0,
        )
        self._image_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    async def close(self) -> None:
        await self._api_client.aclose()
        await self._image_client.aclose()

    async def _fetch_image(self, url: str) -> tuple[bytes, str] | None:
        try:
            resp = await self._image_client.get(url)
            resp.raise_for_status()
        except Exception:
            logger.warning("Не удалось скачать фото %s для оценки Gemini", url)
            return None
        mime_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not mime_type.startswith("image/"):
            mime_type = "image/jpeg"
        return resp.content, mime_type

    async def evaluate_deal(
        self, item: VintedItem, median_price: float | None, similar_count: int
    ) -> DealVerdict | None:
        if median_price is not None:
            market_context = (
                f"медианная цена ~{median_price:.0f} {item.currency} "
                f"по {similar_count} похожим активным объявлениям"
            )
        else:
            market_context = (
                "похожих активных объявлений найти не удалось, ориентируйся на "
                "собственные знания о рыночных ценах для такого бренда/вещи"
            )

        prompt = _PROMPT_TEMPLATE.format(
            title=item.title,
            brand=item.brand_title or "не указан",
            price=item.total_price,
            currency=item.currency,
            status=item.status or "не указано",
            size=item.size_title or "не указан",
            market_context=market_context,
        )
        parts: list[dict] = [{"text": prompt}]

        if item.photo_url:
            image = await self._fetch_image(item.photo_url)
            if image is not None:
                data, mime_type = image
                parts.append(
                    {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(data).decode("ascii")}}
                )

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
            },
        }

        try:
            resp = await self._api_client.post(f"/v1beta/models/{self._model}:generateContent", json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(text)
        except Exception:
            logger.exception("Gemini не смог оценить item %s", item.item_id)
            return None

        try:
            return DealVerdict(is_good_deal=bool(result["is_good_deal"]), reason=str(result["reason"]).strip())
        except (KeyError, TypeError):
            logger.warning("Gemini вернул неожиданный формат ответа для item %s: %r", item.item_id, result)
            return None
