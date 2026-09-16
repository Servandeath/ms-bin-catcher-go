"""Spike: штрихкод товара -> в каких ячейках он лежит.

Только чтение: в учёте ничего не меняет.
Запуск: python scripts/spike_slots.py <штрихкод>
"""
import os
import sys

import httpx
from dotenv import load_dotenv

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"


def make_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            # МС принимает только gzip; без этого заголовка ответит 415
            "Accept-Encoding": "gzip",
        },
        trust_env=False,  # не использовать системный SOCKS-прокси от VPN
        timeout=30,
    )


def get_json(client: httpx.Client, path: str, params: dict | None = None):
    """Один GET с понятной ошибкой вместо трейсбэка."""
    response = client.get(path, params=params)
    if response.status_code != 200:
        raise SystemExit(f"Ошибка {response.status_code} на {path}: {response.text[:300]}")
    return response.json()


def find_product(client: httpx.Client, barcode: str) -> dict:
    data = get_json(client, "/entity/assortment", {"filter": f"barcode={barcode}"})
    rows = data["rows"]
    if not rows:
        raise SystemExit(f"Товар со штрихкодом {barcode} не найден")
    if len(rows) > 1:
        print(f"Внимание: штрихкод {barcode} найден у {len(rows)} позиций, берём первую")
    return rows[0]


def load_slot_names(client: httpx.Client, store_id: str) -> dict[str, str]:
    """Справочник ячеек склада: id -> название. Грузит все страницы по 1000."""
    names: dict[str, str] = {}
    offset = 0
    while True:
        data = get_json(
            client, f"/entity/store/{store_id}/slots", {"limit": 1000, "offset": offset}
        )
        rows = data["rows"]
        names.update({row["id"]: row["name"] for row in rows})
        offset += len(rows)
        # Выходим, когда забрали всё или страница пустая (защита от вечного цикла)
        if not rows or offset >= data["meta"]["size"]:
            print(f"Ячеек загружено: {len(names)}")
            return names


def main() -> None:
    load_dotenv()
    token = os.getenv("MOYSKLAD_TOKEN")
    if not token:
        raise SystemExit("MOYSKLAD_TOKEN пуст. Заполните .env")

    barcode = sys.argv[1] if len(sys.argv) > 1 else input("Штрихкод: ").strip()

    with make_client(token) as client:
        product = find_product(client, barcode)
        print(f"Товар: {product['name']}")

        stock = get_json(
            client, "/report/stock/byslot/current", {"filter": f"assortmentId={product['id']}"}
        )
        if not stock:
            print("В ячейках товара нет (остаток вне ячеек этот отчёт не показывает)")
            return

        store_ids = {row["storeId"] for row in stock}
        for store_id in store_ids:
            store = get_json(client, f"/entity/store/{store_id}")
            slot_names = load_slot_names(client, store_id)
            print(f"\nСклад: {store['name']}")
            for row in stock:
                if row["storeId"] != store_id:
                    continue
                name = slot_names.get(row["slotId"], row["slotId"])
                print(f"  {name}: {row['stock']:g}")


if __name__ == "__main__":
    main()