"""Поиск ячеек по части названия: что лежит и сколько этого товара вне ячеек.

Запуск из корня репо: python -m scripts.check_slot "од162"
"""
import sys

from ms_bin.db import DB_PATH, connect
from ms_bin.search import search_key

QUERY = """
SELECT st.name, sl.id, sl.name, b.assortment_id, b.stock, COALESCE(o.outside, 0)
FROM slots sl
JOIN stores st ON st.id = sl.store_id
JOIN stock_by_slot b ON b.slot_id = sl.id
LEFT JOIN stock_outside_slots o
       ON o.assortment_id = b.assortment_id AND o.store_id = b.store_id
WHERE sl.name_search LIKE ?
ORDER BY st.name, sl.name
"""


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit("Базы нет. Сначала: python -m scripts.sync_stock")
    text = sys.argv[1] if len(sys.argv) > 1 else input("Часть названия ячейки: ")
    conn = connect()
    try:
        rows = conn.execute(QUERY, (f"%{search_key(text)}%",)).fetchall()
    finally:
        conn.close()
    if not rows:
        print("Ничего не найдено")
    for store, slot_id, slot_name, assortment_id, stock, outside in rows:
        print(f"{store} | {slot_id}  # {slot_name} | {assortment_id} | "
              f"в ячейке: {stock:g} | вне ячеек: {outside:g}")


if __name__ == "__main__":
    main()