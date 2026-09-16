"""Синхронизация данных МС в локальную базу.

Справочники: инкрементально по полю updated, полная выгрузка удаляет пропавшее.
Остатки по складам: через changedSince (МС разрешает не старше 24 часов).
Остатки по ячейкам: полной выгрузкой по каждому складу с ячейками.
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from ms_bin.api import get_json, id_from_meta, iter_pages, load_slots, ms_time
from ms_bin.search import search_key

# Запас к 24 часам МС: отметка старше — делаем полную выгрузку
CHANGED_SINCE_MAX_AGE = timedelta(hours=23)


@dataclass(frozen=True)
class Entity:
    key: str                       # имя в sync_state и sync_runs
    path: str
    table: str
    columns: tuple[str, ...]       # первая колонка — id
    to_row: Callable[[dict], tuple]
    type_filter: str | None = None  # для assortment: product | variant


def _archived(r: dict) -> int:
    return int(r.get("archived", False))


def _folder_row(r: dict) -> tuple:
    return (r["id"], id_from_meta(r.get("productFolder")), r["name"],
            r.get("pathName", ""), _archived(r), r["updated"])


def _product_row(r: dict) -> tuple:
    return (r["id"], "product", r["id"], id_from_meta(r.get("productFolder")), r["name"],
            search_key(r["name"]), r.get("code"), r.get("article"), _archived(r), r["updated"])


def _variant_row(r: dict) -> tuple:
    return (r["id"], "variant", id_from_meta(r["product"]), None, r["name"],
            search_key(r["name"]), r.get("code"), None, _archived(r), r["updated"])


ASSORTMENT_COLUMNS = ("id", "type", "product_id", "folder_id", "name", "name_search",
                      "code", "article", "archived", "updated")

ENTITIES = (
    Entity("stores", "/entity/store", "stores", ("id", "name", "archived", "updated"),
           lambda r: (r["id"], r["name"], _archived(r), r["updated"])),
    Entity("organizations", "/entity/organization", "organizations",
           ("id", "name", "archived", "updated"),
           lambda r: (r["id"], r["name"], _archived(r), r["updated"])),
    Entity("employees", "/entity/employee", "employees",
           ("id", "short_name", "position", "archived", "updated"),
           lambda r: (r["id"], r.get("shortFio") or r["name"], r.get("position"),
                      _archived(r), r["updated"])),
    Entity("counterparties", "/entity/counterparty", "counterparties",
           ("id", "name", "name_search", "inn", "archived", "updated"),
           lambda r: (r["id"], r["name"], search_key(r["name"]), r.get("inn"),
                      _archived(r), r["updated"])),
    Entity("folders", "/entity/productfolder", "folders",
           ("id", "parent_id", "name", "path_name", "archived", "updated"), _folder_row),
    Entity("products", "/entity/product", "assortment", ASSORTMENT_COLUMNS, _product_row,
           type_filter="product"),
    Entity("variants", "/entity/variant", "assortment", ASSORTMENT_COLUMNS, _variant_row,
           type_filter="variant"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)", (key, value))


def _log_run(conn: sqlite3.Connection, kind: str, rows: int, note: str = "") -> None:
    conn.execute("INSERT INTO sync_runs (kind, finished_at, rows, note) VALUES (?, ?, ?, ?)",
                 (kind, _now(), rows, note))


def _upsert_sql(entity: Entity) -> str:
    cols = ", ".join(entity.columns)
    marks = ", ".join("?" for _ in entity.columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in entity.columns[1:])
    return (f"INSERT INTO {entity.table} ({cols}) VALUES ({marks}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}")


def _save_barcodes(conn: sqlite3.Connection, page: list[dict]) -> None:
    """Штрихкоды приходят списком вида [{"ean13": "..."}, {"code128": "..."}]."""
    ids = [(r["id"],) for r in page]
    conn.executemany("DELETE FROM barcodes WHERE assortment_id = ?", ids)
    conn.executemany(
        "INSERT OR IGNORE INTO barcodes (barcode, assortment_id) VALUES (?, ?)",
        [(code, r["id"]) for r in page for item in r.get("barcodes", []) for code in item.values()],
    )


def _delete_missing(conn: sqlite3.Connection, entity: Entity, seen: set[str]) -> int:
    """После полной выгрузки: удалить то, чего в МС больше нет."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen_ids (id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM seen_ids")
    conn.executemany("INSERT INTO seen_ids (id) VALUES (?)", [(i,) for i in seen])
    where = "id NOT IN (SELECT id FROM seen_ids)"
    params: tuple = ()
    if entity.type_filter:
        where += " AND type = ?"
        params = (entity.type_filter,)
    if entity.table == "assortment":
        conn.execute(f"DELETE FROM barcodes WHERE assortment_id IN "
                     f"(SELECT id FROM assortment WHERE {where})", params)
    return conn.execute(f"DELETE FROM {entity.table} WHERE {where}", params).rowcount


def sync_entity(client: httpx.Client, conn: sqlite3.Connection, entity: Entity,
                full: bool = False) -> int:
    """Справочник в базу. Без full — только изменённое с прошлого раза."""
    since = None if full else _get_state(conn, entity.key)
    # По умолчанию МС отдаёт только неархивные; нам нужны все, чтобы видеть архивацию
    conditions = ["archived=true", "archived=false"]
    if since:
        # >= а не >: строки на границе придут повторно, upsert это переживёт
        conditions.append(f"updated>={since}")
    sql = _upsert_sql(entity)
    total, last, seen = 0, since, set()
    for page in iter_pages(client, entity.path, {"filter": ";".join(conditions)}):
        with conn:  # страница — транзакция: сайт не ждёт конца всей выгрузки
            conn.executemany(sql, [entity.to_row(r) for r in page])
            if entity.table == "assortment":
                _save_barcodes(conn, page)
        total += len(page)
        seen.update(r["id"] for r in page)
        # МС отдаёт updated с миллисекундами, фильтр принимает до секунд
        last = max([last or "", *(r["updated"][:19] for r in page)])
    with conn:
        note = ""
        if full:
            note = f"удалено {_delete_missing(conn, entity, seen)}"
        if last:
            _set_state(conn, entity.key, last)
        _log_run(conn, entity.key + (" full" if full else ""), total, note)
    return total


def sync_catalog(client: httpx.Client, conn: sqlite3.Connection, full: bool = False) -> dict:
    return {e.key: sync_entity(client, conn, e, full) for e in ENTITIES}


def sync_slots(client: httpx.Client, conn: sqlite3.Connection, store_ids: list[str]) -> dict:
    """Список ячеек складов. Склады без ячеек пропускаются."""
    result = {}
    for sid in store_ids:
        slots = load_slots(client, sid)
        with conn:
            conn.execute("DELETE FROM slots WHERE store_id = ?", (sid,))
            conn.executemany(
                "INSERT INTO slots (id, store_id, name, name_search, barcode) "
                "VALUES (?, ?, ?, ?, ?)",
                [(s["id"], sid, s["name"], search_key(s["name"]), s.get("barcode"))
                 for s in slots],
            )
            _log_run(conn, "slots", len(slots), sid)
        result[sid] = len(slots)
    return result


def sync_stock_by_store(client: httpx.Client, conn: sqlite3.Connection,
                        full: bool = False) -> int:
    """Остатки по всем складам: изменения с прошлого раза или всё целиком."""
    started = datetime.now(timezone.utc)
    mark = None if full else _get_state(conn, "stock_by_store")
    incremental = mark and started - datetime.fromisoformat(mark) < CHANGED_SINCE_MAX_AGE
    params = {}
    if incremental:
        # С changedSince МС сам отдаёт и обнулившиеся позиции (zeroLines с ним запрещён)
        params = {"changedSince": ms_time(datetime.fromisoformat(mark))}
    rows = get_json(client, "/report/stock/bystore/current", params)
    with conn:
        if not incremental:
            conn.execute("DELETE FROM stock_by_store")
        conn.executemany(
            "INSERT OR REPLACE INTO stock_by_store (assortment_id, store_id, stock) "
            "VALUES (?, ?, ?)",
            [(r["assortmentId"], r["storeId"], r["stock"]) for r in rows if r.get("stock")],
        )
        conn.executemany(
            "DELETE FROM stock_by_store WHERE assortment_id = ? AND store_id = ?",
            [(r["assortmentId"], r["storeId"]) for r in rows if not r.get("stock")],
        )
        # Отметка — время начала запроса: изменения во время выгрузки придут в следующий раз
        _set_state(conn, "stock_by_store", started.isoformat(timespec="seconds"))
        _log_run(conn, "stock_by_store" + ("" if incremental else " full"), len(rows))
    return len(rows)


def sync_stock_by_slot(client: httpx.Client, conn: sqlite3.Connection) -> dict:
    """Остатки по ячейкам для складов, у которых в базе есть ячейки."""
    result = {}
    store_ids = [r[0] for r in conn.execute("SELECT store_id FROM slots_stores")]
    for sid in store_ids:
        rows = get_json(client, "/report/stock/byslot/current", {"filter": f"storeId={sid}"})
        with conn:
            conn.execute("DELETE FROM stock_by_slot WHERE store_id = ?", (sid,))
            conn.executemany(
                "INSERT INTO stock_by_slot (assortment_id, store_id, slot_id, stock) "
                "VALUES (?, ?, ?, ?)",
                [(r["assortmentId"], sid, r["slotId"], r["stock"]) for r in rows],
            )
            _log_run(conn, "stock_by_slot", len(rows), sid)
        result[sid] = len(rows)
    return result


def stock_freshness(conn: sqlite3.Connection) -> str | None:
    """Время последнего синка остатков, для подписи «остатки на 14:32»."""
    return _get_state(conn, "stock_by_store")
