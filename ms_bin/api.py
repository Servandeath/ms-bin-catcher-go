"""Клиент JSON API МойСклад 1.2."""
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
PAGE_SIZE = 1000
MAX_ATTEMPTS = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}
# МС принимает и отдаёт даты по Москве; переходов на летнее время там нет
MSK = timezone(timedelta(hours=3))


class MsError(Exception):
    def __init__(self, status: int, path: str, text: str):
        super().__init__(f"МойСклад {status} на {path}: {text[:300]}")
        self.status = status


def make_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
        trust_env=False,  # не использовать системный прокси (VPN)
        timeout=60,
    )


def ms_time(moment: datetime) -> str:
    """Дата в формате фильтров МС: '2026-09-16 14:32:00' по Москве."""
    return moment.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Сколько ждать перед повтором: сколько просит МС, иначе растущая пауза."""
    header = response.headers.get("X-Lognex-Retry-After") if response is not None else None
    base = int(header) / 1000 if header else min(2 ** attempt, 30)
    # Случайная добавка, чтобы параллельные запросы не повторялись одновременно
    return base + random.uniform(0, 0.5)


def request(client: httpx.Client, method: str, path: str, **kwargs):
    """Запрос с повторами при 429, 5xx и обрыве связи. Ошибка — MsError."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.request(method, path, **kwargs)
        except httpx.TransportError:
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(_retry_delay(None, attempt))
            continue
        if response.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
            break
        time.sleep(_retry_delay(response, attempt))
    if response.status_code >= 400:
        raise MsError(response.status_code, path, response.text)
    return response.json()


def get_json(client: httpx.Client, path: str, params: dict | None = None):
    return request(client, "GET", path, params=params)


def iter_pages(client: httpx.Client, path: str, params: dict | None = None):
    """Страницы списка по PAGE_SIZE строк, чтобы не держать всё в памяти."""
    offset = 0
    while True:
        data = get_json(client, path, {**(params or {}), "limit": PAGE_SIZE, "offset": offset})
        page = data["rows"]
        if page:
            yield page
        offset += len(page)
        if not page or offset >= data["meta"]["size"]:
            return


def get_all_rows(client: httpx.Client, path: str, params: dict | None = None) -> list[dict]:
    return [row for page in iter_pages(client, path, params) for row in page]


def list_stores(client: httpx.Client) -> list[dict]:
    return get_all_rows(client, "/entity/store", {"filter": "archived=false"})


def find_store(client: httpx.Client, name: str) -> dict:
    rows = get_json(client, "/entity/store", {"filter": f"name={name}"})["rows"]
    if not rows:
        raise MsError(404, "/entity/store", f"склад «{name}» не найден")
    return rows[0]


def load_slots(client: httpx.Client, store_id: str) -> list[dict]:
    return get_all_rows(client, f"/entity/store/{store_id}/slots")


def id_from_meta(ref: dict | None) -> str | None:
    """id связанной сущности из её meta.href."""
    if not ref:
        return None
    return ref["meta"]["href"].rsplit("/", 1)[-1].split("?")[0]
