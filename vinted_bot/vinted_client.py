import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Vinted's public REST API has no working "search brands by name" endpoint
# (the seemingly appropriate /api/v2/brands ignores its search/query params
# and always returns a fixed top-12 list). Brand names are instead resolved
# by full-text item search + scraping the numeric brand id off an item's
# detail page (its "/brand/<id>-<slug>" link) — this was verified manually
# against the live API before implementing it here.
BRAND_LINK_RE = re.compile(r"/brand/(\d+)-")


@dataclass
class BrandCandidate:
    """A brand title found via search, not yet resolved to a numeric id."""

    title: str
    sample_item_url: str


@dataclass
class VintedItem:
    item_id: int
    title: str
    price: str
    total_price: str
    currency: str
    brand_title: str
    size_title: str | None
    status: str | None
    url: str
    photo_url: str | None
    seller_login: str | None


class VintedClient:
    """Тонкий клиент над внутренним REST API Vinted (используется веб-версией сайта)."""

    def __init__(self, domain: str):
        self._base_url = f"https://www.{domain}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=20.0,
            follow_redirects=True,
        )
        self._session_ready = False

    async def close(self) -> None:
        await self._client.aclose()

    async def _ensure_session(self) -> None:
        if self._session_ready:
            return
        await self._refresh_session()

    async def _refresh_session(self) -> None:
        resp = await self._client.get("/")
        resp.raise_for_status()
        self._session_ready = True

    async def _get_json(self, path: str, params: list[tuple[str, str]]) -> dict:
        await self._ensure_session()
        resp = await self._client.get(path, params=params)
        if resp.status_code in (401, 403):
            logger.warning("Vinted вернул %s, обновляю сессию и повторяю запрос", resp.status_code)
            await self._refresh_session()
            resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def search_brand_candidates(self, query: str, limit: int = 5) -> list[BrandCandidate]:
        """Find distinct brand titles matching `query` via full-text item search."""
        data = await self._get_json(
            "/api/v2/catalog/items",
            [
                ("page", "1"),
                ("per_page", "20"),
                ("order", "relevance"),
                ("search_text", query),
            ],
        )
        needle = query.lower()
        candidates: dict[str, str] = {}
        for it in data.get("items", []):
            title = (it.get("brand_title") or "").strip()
            if not title or title in candidates:
                continue
            if needle in title.lower():
                candidates[title] = it.get("url", "")
        # Exact (case-insensitive) match first, then the rest in discovery order.
        ordered = sorted(candidates.items(), key=lambda kv: kv[0].lower() != needle)
        return [BrandCandidate(title=t, sample_item_url=u) for t, u in ordered[:limit]]

    async def resolve_brand_id(self, candidate: BrandCandidate) -> int | None:
        """Scrape the numeric brand id off a sample item's detail page."""
        await self._ensure_session()
        resp = await self._client.get(candidate.sample_item_url)
        resp.raise_for_status()
        match = BRAND_LINK_RE.search(resp.text)
        if not match:
            return None
        return int(match.group(1))

    async def fetch_newest_items(
        self,
        brand_ids: list[int],
        price_to: float | None,
        per_page: int = 20,
    ) -> list[VintedItem]:
        params: list[tuple[str, str]] = [
            ("page", "1"),
            ("per_page", str(per_page)),
            ("order", "newest_first"),
        ]
        for brand_id in brand_ids:
            params.append(("brand_ids[]", str(brand_id)))
        if price_to is not None:
            params.append(("price_to", str(price_to)))

        data = await self._get_json("/api/v2/catalog/items", params)
        items = []
        for it in data.get("items", []):
            photo = it.get("photo") or {}
            price = it.get("price") or {}
            total_price = it.get("total_item_price") or price
            user = it.get("user") or {}
            items.append(
                VintedItem(
                    item_id=it["id"],
                    title=it.get("title", ""),
                    price=str(price.get("amount", "?")),
                    total_price=str(total_price.get("amount", "?")),
                    currency=str(price.get("currency_code", "")),
                    brand_title=it.get("brand_title") or "",
                    size_title=it.get("size_title"),
                    status=it.get("status"),
                    url=it.get("url", f"{self._base_url}{it.get('path', '')}"),
                    photo_url=photo.get("url"),
                    seller_login=user.get("login"),
                )
            )
        return items

    async def fetch_similar_total_prices(
        self, title: str, brand_id: int, exclude_item_id: int, limit: int = 20
    ) -> list[float]:
        """Total prices (incl. buyer protection) of other current listings matching
        this item's title within the same brand — a rough proxy for its resale value."""
        params = [
            ("page", "1"),
            ("per_page", str(limit)),
            ("order", "relevance"),
            ("search_text", title),
            ("brand_ids[]", str(brand_id)),
        ]
        data = await self._get_json("/api/v2/catalog/items", params)
        prices: list[float] = []
        for it in data.get("items", []):
            if it.get("id") == exclude_item_id:
                continue
            total = it.get("total_item_price") or it.get("price") or {}
            try:
                prices.append(float(total["amount"]))
            except (KeyError, TypeError, ValueError):
                continue
        return prices

