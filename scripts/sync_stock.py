"""Синхронизация остатков складов с ячейками в SQLite.

Запуск из корня репо:
  python -m scripts.sync_stock                         склады из MOYSKLAD_DEFAULT_STORES
  python -m scripts.sync_stock "Нарвская" "Склад 2"    указанные склады
  python -m scripts.sync_stock --all                   все неархивные склады
"""
import argparse
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from ms_bin.api import find_store, get_json, list_stores, load_slots, make_client
from ms_bin.db import DB_PATH, connect
from ms_bin.search import search_key


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Синхронизация остатков по ячейкам в SQLite")
    parser.add_argument("stores", nargs="*", help="Названия складов")
    parser.add_argument("--all", action="store_true", help="Все неархивные склады")
    return parser.parse_args()


def resolve_stores(client, args) -> list[dict]:
    if args.all:
        return list_stores(client)
    names = args.stores or [
        n.strip() for n in os.getenv("MOYSKLAD_DEFAULT_STORES", "").split(",") if n.strip()
    ]
    if not names:
        raise SystemExit("Укажите склады, --all или MOYSKLAD_DEFAULT_STORES в .env")
    return [find_store(client, name) for name in names]


def save(conn, store, slots, slot_stock, store_stock) -> int:
    sid = store["id"]
    store_rows = [
        (r["assortmentId"], sid, r["stock"])
        for r in store_stock
        if r.get("storeId") == sid and r.get("stock") is not None
    ]
    with conn:  # один склад — одна транзакция
        conn.execute("INSERT OR REPLACE INTO stores (id, name) VALUES (?, ?)", (sid, store["name"]))
        for table in ("slots", "stock_by_slot", "stock_by_store"):
            conn.execute(f"DELETE FROM {table} WHERE store_id = ?", (sid,))
        conn.executemany(
            "INSERT INTO slots (id, store_id, name, name_search, barcode) VALUES (?, ?, ?, ?, ?)",
            [(s["id"], sid, s["name"], search_key(s["name"]), s.get("barcode")) for s in slots],
        )
        conn.executemany(
            "INSERT INTO stock_by_slot (assortment_id, store_id, slot_id, stock) VALUES (?, ?, ?, ?)",
            [(r["assortmentId"], sid, r["slotId"], r["stock"]) for r in slot_stock],
        )
        conn.executemany(
            "INSERT INTO stock_by_store (assortment_id, store_id, stock) VALUES (?, ?, ?)",
            store_rows,
        )
        conn.execute(
            "INSERT INTO sync_runs (store_id, finished_at, slots, slot_rows, store_rows) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             len(slots), len(slot_stock), len(store_rows)),
        )
    return conn.execute(
        "SELECT COUNT(*) FROM stock_outside_slots WHERE store_id = ?", (sid,)
    ).fetchone()[0]


def main() -> None:
    load_dotenv()
    token = os.getenv("MOYSKLAD_TOKEN")
    if not token:
        raise SystemExit("MOYSKLAD_TOKEN пуст. Заполните .env")
    args = parse_args()

    conn = connect()
    try:
        with make_client(token) as client:
            for store in resolve_stores(client, args):
                sid = store["id"]
                slots = load_slots(client, sid)
                if not slots:
                    print(f"{store['name']}: пропущен, ячеек нет")
                    continue
                slot_stock = get_json(client, "/report/stock/byslot/current", {"filter": f"storeId={sid}"})
                store_stock = get_json(client, "/report/stock/bystore/current", {"filter": f"storeId={sid}"})
                outside = save(conn, store, slots, slot_stock, store_stock)
                print(
                    f"{store['name']}: ячеек {len(slots)}, строк по ячейкам {len(slot_stock)}, "
                    f"вне ячеек {outside}"
                )
    finally:
        conn.close()
    print(f"База: {DB_PATH}")


if __name__ == "__main__":
    main()