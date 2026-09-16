"""Локальная база SQLite: схема и подключение."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ms_bin.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    id          TEXT PRIMARY KEY,
    store_id    TEXT NOT NULL,
    name        TEXT NOT NULL,   -- подпись для человека
    name_search TEXT NOT NULL,   -- ключ поиска, см. ms_bin.search
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


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn