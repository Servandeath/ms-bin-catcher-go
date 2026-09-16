"""Локальная база SQLite: схема и подключение.

Таблицы здесь — копия данных МС, её всегда можно пересобрать синком.
Поэтому при смене схемы (SCHEMA_VERSION) они просто пересоздаются.
Таблицы приложения (коды входа, история операций) появятся отдельно
и пересоздаваться не будут.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ms_bin.db"
SCHEMA_VERSION = 2

CACHE_TABLES = (
    "stores", "slots", "stock_by_slot", "stock_by_store", "sync_runs",
    "folders", "assortment", "barcodes", "counterparties", "organizations",
    "employees", "sync_state",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    updated  TEXT
);
CREATE TABLE IF NOT EXISTS slots (
    id          TEXT PRIMARY KEY,
    store_id    TEXT NOT NULL,
    name        TEXT NOT NULL,   -- подпись для человека
    name_search TEXT NOT NULL,   -- ключ поиска, см. ms_bin.search
    barcode     TEXT
);
CREATE INDEX IF NOT EXISTS slots_store ON slots (store_id);
CREATE TABLE IF NOT EXISTS stock_by_slot (
    assortment_id TEXT NOT NULL,
    store_id      TEXT NOT NULL,
    slot_id       TEXT NOT NULL,
    stock         REAL NOT NULL,
    PRIMARY KEY (assortment_id, slot_id)
);
CREATE INDEX IF NOT EXISTS stock_by_slot_store ON stock_by_slot (store_id);
CREATE TABLE IF NOT EXISTS stock_by_store (
    assortment_id TEXT NOT NULL,
    store_id      TEXT NOT NULL,
    stock         REAL NOT NULL,
    PRIMARY KEY (assortment_id, store_id)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,   -- что синхронизировали
    finished_at TEXT NOT NULL,
    rows        INTEGER NOT NULL,
    note        TEXT
);
-- Последнее updated по каждому справочнику, отметка для остатков и т.п.
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id        TEXT PRIMARY KEY,
    parent_id TEXT,
    name      TEXT NOT NULL,
    path_name TEXT NOT NULL,     -- путь родителей, как в МС
    archived  INTEGER NOT NULL DEFAULT 0,
    updated   TEXT
);
-- Товары и модификации вместе: остатки в отчётах идут по assortmentId любого из них
CREATE TABLE IF NOT EXISTS assortment (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,   -- product | variant
    product_id  TEXT NOT NULL,   -- у товара — он сам, у модификации — её товар
    folder_id   TEXT,            -- у модификации пусто, берётся от товара
    name        TEXT NOT NULL,
    name_search TEXT NOT NULL,
    code        TEXT,
    article     TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    updated     TEXT
);
CREATE INDEX IF NOT EXISTS assortment_product ON assortment (product_id);
CREATE INDEX IF NOT EXISTS assortment_folder ON assortment (folder_id);
CREATE TABLE IF NOT EXISTS barcodes (
    barcode       TEXT NOT NULL,
    assortment_id TEXT NOT NULL,
    PRIMARY KEY (barcode, assortment_id)
);
CREATE INDEX IF NOT EXISTS barcodes_assortment ON barcodes (assortment_id);
CREATE TABLE IF NOT EXISTS counterparties (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_search TEXT NOT NULL,
    inn         TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    updated     TEXT
);
CREATE TABLE IF NOT EXISTS organizations (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    updated  TEXT
);
CREATE TABLE IF NOT EXISTS employees (
    id         TEXT PRIMARY KEY,
    short_name TEXT NOT NULL,    -- «Иванов И. И.», по нему вход
    position   TEXT,             -- различает однофамильцев
    archived   INTEGER NOT NULL DEFAULT 0,
    updated    TEXT
);

-- Склады, где ведётся учёт по ячейкам: только для них «вне ячеек» имеет смысл
CREATE VIEW IF NOT EXISTS slots_stores AS
SELECT DISTINCT store_id FROM slots;
CREATE VIEW IF NOT EXISTS stock_outside_slots AS
SELECT s.assortment_id,
       s.store_id,
       s.stock - COALESCE(SUM(b.stock), 0) AS outside
FROM stock_by_store s
JOIN slots_stores ss ON ss.store_id = s.store_id
LEFT JOIN stock_by_slot b
       ON b.assortment_id = s.assortment_id AND b.store_id = s.store_id
GROUP BY s.assortment_id, s.store_id, s.stock
HAVING outside > 0;
"""


def _reset_cache(conn: sqlite3.Connection) -> None:
    conn.execute("DROP VIEW IF EXISTS stock_outside_slots")
    conn.execute("DROP VIEW IF EXISTS slots_stores")
    for table in CACHE_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # WAL: сайт читает, пока синк пишет, и они не блокируют друг друга
    conn.execute("PRAGMA journal_mode = WAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        with conn:
            _reset_cache(conn)
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn
