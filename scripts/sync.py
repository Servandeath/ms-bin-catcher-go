"""Синхронизация данных МойСклад в локальную базу.

Запуск из корня репо:
  python -m scripts.sync                  справочники (изменения) и остатки
  python -m scripts.sync catalog          только справочники и ячейки
  python -m scripts.sync catalog --full   справочники целиком, с удалением пропавшего
  python -m scripts.sync stock            только остатки
  python -m scripts.sync --full           всё целиком (раз в сутки, ночью)

Склады с ячейками берутся из MOYSKLAD_DEFAULT_STORES (названия через запятую),
если пусто — все неархивные.
"""
import argparse
import os
import time

from dotenv import load_dotenv

from ms_bin.api import make_client
from ms_bin.db import DB_PATH, connect
from ms_bin.sync import sync_catalog, sync_slots, sync_stock_by_slot, sync_stock_by_store


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Синхронизация МойСклад → SQLite")
    parser.add_argument("what", nargs="?", choices=("all", "catalog", "stock"), default="all")
    parser.add_argument("--full", action="store_true", help="Всё целиком, с удалением пропавшего")
    return parser.parse_args()


def slot_store_ids(conn) -> list[str]:
    names = [n.strip() for n in os.getenv("MOYSKLAD_DEFAULT_STORES", "").split(",") if n.strip()]
    if not names:
        return [r[0] for r in conn.execute("SELECT id FROM stores WHERE archived = 0")]
    ids = []
    for name in names:
        row = conn.execute("SELECT id FROM stores WHERE name = ?", (name,)).fetchone()
        if not row:
            raise SystemExit(f"Склад «{name}» не найден в базе")
        ids.append(row[0])
    return ids


def main() -> None:
    load_dotenv()
    token = os.getenv("MOYSKLAD_TOKEN")
    if not token:
        raise SystemExit("MOYSKLAD_TOKEN пуст. Заполните .env")
    args = parse_args()

    conn = connect()
    try:
        with make_client(token) as client:
            if args.what in ("all", "catalog"):
                started = time.monotonic()
                for key, count in sync_catalog(client, conn, args.full).items():
                    print(f"{key}: {count}")
                slots = sync_slots(client, conn, slot_store_ids(conn))
                print(f"ячейки: {sum(slots.values())} на {sum(1 for n in slots.values() if n)} складах")
                print(f"справочники за {time.monotonic() - started:.1f} с")
            if args.what in ("all", "stock"):
                started = time.monotonic()
                print(f"остатки по складам: {sync_stock_by_store(client, conn, args.full)} строк")
                by_slot = sync_stock_by_slot(client, conn)
                print(f"остатки по ячейкам: {sum(by_slot.values())} строк")
                print(f"остатки за {time.monotonic() - started:.1f} с")
    finally:
        conn.close()
    print(f"База: {DB_PATH}")


if __name__ == "__main__":
    main()
