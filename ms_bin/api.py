"""Клиент JSON API МойСклад 1.2."""
import httpx

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"


def make_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
        trust_env=False,  # не использовать системный прокси (VPN)
        timeout=60,
    )


def get_json(client: httpx.Client, path: str, params: dict | None = None):
    response = client.get(path, params=params)
    if response.status_code != 200:
        raise SystemExit(f"Ошибка {response.status_code} на {path}: {response.text[:300]}")
    return response.json()


def get_all_rows(client: httpx.Client, path: str, params: dict | None = None) -> list[dict]:
    """Все строки списка, постранично по 1000."""
    rows: list[dict] = []
    offset = 0
    while True:
        data = get_json(client, path, {**(params or {}), "limit": 1000, "offset": offset})
        page = data["rows"]
        rows.extend(page)
        offset += len(page)
        if not page or offset >= data["meta"]["size"]:
            return rows


def list_stores(client: httpx.Client) -> list[dict]:
    return get_all_rows(client, "/entity/store", {"filter": "archived=false"})


def find_store(client: httpx.Client, name: str) -> dict:
    rows = get_json(client, "/entity/store", {"filter": f"name={name}"})["rows"]
    if not rows:
        raise SystemExit(f"Склад «{name}» не найден")
    return rows[0]


def load_slots(client: httpx.Client, store_id: str) -> list[dict]:
    return get_all_rows(client, f"/entity/store/{store_id}/slots")