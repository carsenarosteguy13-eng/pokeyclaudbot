import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "listings.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                card_name    TEXT    NOT NULL,
                set_name     TEXT,
                sku          TEXT    NOT NULL,
                offer_id     TEXT    NOT NULL,
                listing_id   TEXT    NOT NULL,
                price        REAL    NOT NULL,
                condition    TEXT,
                ebay_url     TEXT,
                created_at   TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def save(chat_id: int, card_name: str, set_name: str, sku: str,
         offer_id: str, listing_id: str, price: float,
         condition: str, ebay_url: str) -> int:
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO listings
                (chat_id, card_name, set_name, sku, offer_id, listing_id, price, condition, ebay_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, card_name, set_name, sku, offer_id, listing_id, price, condition, ebay_url))
        conn.commit()
        return cur.lastrowid


def get_all(chat_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE chat_id = ? ORDER BY created_at DESC",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_by_id(db_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (db_id,)).fetchone()
    return dict(row) if row else None


def delete(db_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM listings WHERE id = ?", (db_id,))
        conn.commit()
