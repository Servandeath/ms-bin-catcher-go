"""Синхронизация остатков склада с ячейками в SQLite.

Полная перезаливка данных одного склада в одной транзакции.
Запуск: python scripts/sync_stock.py "Нарвская"
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ms_bin.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    id          TEXT PRIMARY KEY,
    store_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_search TEXT NOT NULL,  -- название в нижнем регистре для поиска
    barcode     TEXT
);
CREATE TABLE IF NOT EXISTS stock_by_slot (
    assortment_id TEXT NOT NULL,
    store_id      TEXT NOT NULL,
    slot_id       TEXT NOT NULL,
    stock         REAL NOT NULL,
    PRIMARY KEY (assortment_id, slot_id)
);
CREATE TABLE IF NOT EXISTS stock_by_store (
    assortment_id TEXT NOT NULL,
    store_id      TEXT NOT NULL,
    stock         REAL NOT NULL,
    PRIMARY KEY (assortment_id, store_id)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id    TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    slots       INTEGER NOT NULL,
    slot_rows   INTEGER NOT NULL,
    store_rows  INTEGER NOT NULL
);
CREATE VIEW IF NOT EXISTS stock_outside_slots AS
SELECT s.assortment_id,
       s.store_id,
       s.stock - COALESCE(SUM(b.stock), 0) AS outside
FROM stock_by_store s
LEFT JOIN stock_by_slot b
       ON b.assortment_id = s.assortment_id AND b.store_id = s.store_id
GROUP BY s.assortment_id, s.store_id, s.stock
HAVING outside > 0;
"""


def make_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
        trust_env=False,
        timeout=60,
    )


def get_json(client: httpx.Client, path: str, params: dict | None = None):
    response = client.get(path, params=params)
    if response.status_code != 200:
        raise SystemExit(f"Ошибка {response.status_code} на {path}: {response.text[:300]}")
    return response.json()


def find_store(client: httpx.Client, name: str) -> dict:
    rows = get_json(client, "/entity/store", {"filter": f"name={name}"})["rows"]
    if not rows:
        raise SystemExit(f"Склад «{name}» не найден")
    return rows[0]


def load_slots(client: httpx.Client, store_id: str) -> list[dict]:
    slots: list[dict] = []
    offset = 0
    while True:
        data = get_json(
            client, f"/entity/store/{store_id}/slots", {"limit": 1000, "offset": offset}
        )
        rows = data["rows"]
        slots.extend(rows)
        offset += len(rows)
        if not rows or offset >= data["meta"]["size"]:
            return slots


def save(conn, store, slots, slot_stock, store_stock) -> None:
    sid = store["id"]
    with conn:  # одна транзакция: либо всё записалось, либо ничего
        conn.execute("INSERT OR REPLACE INTO stores (id, name) VALUES (?, ?)", (sid, store["name"]))
        for table in ("slots", "stock_by_slot", "stock_by_store"):
            conn.execute(f"DELETE FROM {table} WHERE store_id = ?", (sid,))
        conn.executemany(
            "INSERT INTO slots (id, store_id, name, name_search, barcode) VALUES (?, ?, ?, ?, ?)",
            [(s["id"], sid, s["name"], s["name"].casefold(), s.get("barcode")) for s in slots],
        )
        conn.executemany(
            "INSERT INTO stock_by_slot (assortment_id, store_id, slot_id, stock) VALUES (?, ?, ?, ?)",
            [(r["assortmentId"], sid, r["slotId"], r["stock"]) for r in slot_stock],
        )
        conn.executemany(
            "INSERT INTO stock_by_store (assortment_id, store_id, stock) VALUES (?, ?, ?)",
            [
                (r["assortmentId"], sid, r["stock"])
                for r in store_stock
                if r.get("storeId") == sid and r.get("stock") is not None
            ],
        )
        conn.execute(
            "INSERT INTO sync_runs (store_id, finished_at, slots, slot_rows, store_rows) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sid,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                len(slots),
                len(slot_stock),
                len(store_stock),
            ),
        )


def main() -> None:
    load_dotenv()
    token = os.getenv("MOYSKLAD_TOKEN")
    if not token:
        raise SystemExit("MOYSKLAD_TOKEN пуст. Заполните .env")
    store_name = sys.argv[1] if len(sys.argv) > 1 else input("Склад: ").strip()

    with make_client(token) as client:
        store = find_store(client, store_name)
        sid = store["id"]
        slots = load_slots(client, sid)
        slot_stock = get_json(client, "/report/stock/byslot/current", {"filter": f"storeId={sid}"})
        store_stock = get_json(client, "/report/stock/bystore/current", {"filter": f"storeId={sid}"})

    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        save(conn, store, slots, slot_stock, store_stock)
        outside = conn.execute(
            "SELECT COUNT(*) FROM stock_outside_slots WHERE store_id = ?", (sid,)
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"Склад: {store['name']}")
    print(f"Ячеек: {len(slots)}")
    print(f"Строк остатков по ячейкам: {len(slot_stock)}")
    print(f"Строк остатков по складу: {len(store_stock)}")
    print(f"Позиций с остатком вне ячеек: {outside}")
    print(f"База: {DB_PATH}")


if __name__ == "__main__":
    main()