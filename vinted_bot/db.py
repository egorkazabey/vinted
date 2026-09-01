from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    max_price REAL,
    active INTEGER NOT NULL DEFAULT 1,
    last_seen_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS brands (
    chat_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    brand_title TEXT NOT NULL,
    PRIMARY KEY (chat_id, brand_id)
);
"""


@dataclass
class Brand:
    brand_id: int
    brand_title: str


@dataclass
class ChatSettings:
    chat_id: int
    max_price: float | None
    active: bool
    last_seen_id: int


class Database:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def ensure_chat(self, chat_id: int) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,)
        )
        await self._conn.commit()

    async def get_chat(self, chat_id: int) -> ChatSettings:
        await self.ensure_chat(chat_id)
        async with self._conn.execute(
            "SELECT chat_id, max_price, active, last_seen_id FROM chats WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return ChatSettings(
            chat_id=row["chat_id"],
            max_price=row["max_price"],
            active=bool(row["active"]),
            last_seen_id=row["last_seen_id"],
        )

    async def set_max_price(self, chat_id: int, max_price: float | None) -> None:
        await self.ensure_chat(chat_id)
        await self._conn.execute(
            "UPDATE chats SET max_price = ? WHERE chat_id = ?", (max_price, chat_id)
        )
        await self._conn.commit()

    async def set_active(self, chat_id: int, active: bool) -> None:
        await self.ensure_chat(chat_id)
        await self._conn.execute(
            "UPDATE chats SET active = ? WHERE chat_id = ?", (int(active), chat_id)
        )
        await self._conn.commit()

    async def update_last_seen_id(self, chat_id: int, last_seen_id: int) -> None:
        await self._conn.execute(
            "UPDATE chats SET last_seen_id = ? WHERE chat_id = ?",
            (last_seen_id, chat_id),
        )
        await self._conn.commit()

    async def reset_last_seen_id(self, chat_id: int) -> None:
        await self.update_last_seen_id(chat_id, 0)

    async def add_brand(self, chat_id: int, brand_id: int, brand_title: str) -> bool:
        await self.ensure_chat(chat_id)
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO brands (chat_id, brand_id, brand_title) VALUES (?, ?, ?)",
            (chat_id, brand_id, brand_title),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def remove_brand(self, chat_id: int, brand_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM brands WHERE chat_id = ? AND brand_id = ?", (chat_id, brand_id)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def list_brands(self, chat_id: int) -> list[Brand]:
        async with self._conn.execute(
            "SELECT brand_id, brand_title FROM brands WHERE chat_id = ? ORDER BY brand_title",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [Brand(brand_id=r["brand_id"], brand_title=r["brand_title"]) for r in rows]

    async def get_active_chats_with_brands(self) -> list[int]:
        async with self._conn.execute(
            """
            SELECT DISTINCT c.chat_id
            FROM chats c
            JOIN brands b ON b.chat_id = c.chat_id
            WHERE c.active = 1
            """
        ) as cur:
            rows = await cur.fetchall()
        return [r["chat_id"] for r in rows]
