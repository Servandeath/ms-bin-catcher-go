"""Что лежит в ячейках, найденных по части названия, и сколько этого товара вне ячеек.

Запуск: python scripts/check_slot.py "од162"
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ms_bin.db"

QUERY = """
SELECT sl.name, b.assortment_id, b.stock, COALESCE(o.outside, 0) AS outside
FROM slots sl
JOIN stock_by_slot b ON b.slot_id = sl.id
LEFT JOIN stock_outside_slots o
       ON o.assortment_id = b.assortment_id AND o.store_id = b.store_id
WHERE sl.name_search LIKE ?
ORDER BY sl.name
"""


def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else input("Часть названия ячейки: ")
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(QUERY, (f"%{text.casefold()}%",)).fetchall()
    finally:
        conn.close()
    if not rows:
        print("Ничего не найдено")
    for name, assortment_id, stock, outside in rows:
        print(f"{name} | {assortment_id} | в ячейке: {stock:g} | вне ячеек: {outside:g}")


if __name__ == "__main__":
    main()