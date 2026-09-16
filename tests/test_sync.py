"""Синк на фейковом API МС: без сети и без токена."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ms_bin import api, db
from ms_bin.sync import ENTITIES, sync_entity, sync_stock_by_store

PRODUCTS = next(e for e in ENTITIES if e.key == "products")


def meta(kind: str, id_: str) -> dict:
    return {"meta": {"href": f"{api.BASE_URL}/entity/{kind}/{id_}"}}


def product(id_: str, name: str, updated: str, barcodes=()) -> dict:
    return {"id": id_, "name": name, "updated": updated, "archived": False,
            "productFolder": meta("productfolder", "f1"),
            "barcodes": [{"ean13": b} for b in barcodes]}


class FakeMs:
    """Отвечает заданными строками и запоминает запросы."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.routes[request.url.path.removeprefix("/api/remap/1.2")]
        if callable(body):
            return body(request)
        if isinstance(body, list) and "limit" in request.url.params:
            return httpx.Response(200, json={"rows": body, "meta": {"size": len(body)}})
        return httpx.Response(200, json=body)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def ms():
    fake = FakeMs()
    with httpx.Client(base_url=api.BASE_URL, transport=httpx.MockTransport(fake)) as client:
        fake.client = client
        yield fake


def test_products_full_then_incremental(conn, ms):
    ms.routes["/entity/product"] = [
        product("p1", "Болт M8", "2026-09-16 10:00:00.123", ["4600000000017"]),
        product("p2", "Гайка", "2026-09-16 11:00:00.000"),
    ]
    assert sync_entity(ms.client, conn, PRODUCTS, full=True) == 2
    assert "updated" not in ms.requests[-1].url.params["filter"]
    assert conn.execute("SELECT assortment_id FROM barcodes").fetchall() == [("p1",)]
    assert conn.execute("SELECT folder_id FROM assortment WHERE id='p1'").fetchone() == ("f1",)

    # p2 пропал из МС, p1 сменил штрихкод
    ms.routes["/entity/product"] = [
        product("p1", "Болт M8", "2026-09-16 12:00:00.000", ["4600000000024"]),
    ]
    sync_entity(ms.client, conn, PRODUCTS)
    assert "updated>=2026-09-16 11:00:00" in ms.requests[-1].url.params["filter"]
    assert conn.execute("SELECT barcode FROM barcodes").fetchall() == [("4600000000024",)]
    # Инкрементальный синк удалений не видит
    assert conn.execute("SELECT COUNT(*) FROM assortment").fetchone() == (2,)

    sync_entity(ms.client, conn, PRODUCTS, full=True)
    assert conn.execute("SELECT id FROM assortment").fetchall() == [("p1",)]


def test_full_sync_keeps_variants(conn, ms):
    conn.execute("INSERT INTO assortment (id, type, product_id, name, name_search) "
                 "VALUES ('v1', 'variant', 'p1', 'Болт (синий)', '')")
    ms.routes["/entity/product"] = [product("p1", "Болт", "2026-09-16 10:00:00")]
    sync_entity(ms.client, conn, PRODUCTS, full=True)
    assert conn.execute("SELECT COUNT(*) FROM assortment").fetchone() == (2,)


def test_paging(conn, ms, monkeypatch):
    monkeypatch.setattr(api, "PAGE_SIZE", 2)
    rows = [product(f"p{i}", f"Товар {i}", "2026-09-16 10:00:00") for i in range(5)]

    def paged(request):
        offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx.Response(200, json={"rows": rows[offset:offset + limit],
                                         "meta": {"size": len(rows)}})

    ms.routes["/entity/product"] = paged
    assert sync_entity(ms.client, conn, PRODUCTS, full=True) == 5
    assert len(ms.requests) == 3


def test_stock_by_store_incremental(conn, ms):
    ms.routes["/report/stock/bystore/current"] = [
        {"assortmentId": "a1", "storeId": "s1", "stock": 5},
        {"assortmentId": "a2", "storeId": "s1", "stock": 3},
        {"assortmentId": "a3", "storeId": "s1", "stock": 0},
    ]
    sync_stock_by_store(ms.client, conn)
    assert "changedSince" not in ms.requests[-1].url.params
    assert conn.execute("SELECT COUNT(*) FROM stock_by_store").fetchone() == (2,)

    ms.routes["/report/stock/bystore/current"] = [
        {"assortmentId": "a1", "storeId": "s1", "stock": 0},
        {"assortmentId": "a2", "storeId": "s1", "stock": 7},
    ]
    sync_stock_by_store(ms.client, conn)
    assert "changedSince" in ms.requests[-1].url.params
    assert conn.execute("SELECT assortment_id, stock FROM stock_by_store").fetchall() == [("a2", 7)]


def test_stale_mark_means_full_stock(conn, ms):
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    conn.execute("INSERT INTO sync_state VALUES ('stock_by_store', ?)", (old.isoformat(),))
    conn.execute("INSERT INTO stock_by_store VALUES ('gone', 's1', 1)")
    ms.routes["/report/stock/bystore/current"] = []
    sync_stock_by_store(ms.client, conn)
    assert "changedSince" not in ms.requests[-1].url.params
    assert conn.execute("SELECT COUNT(*) FROM stock_by_store").fetchone() == (0,)


def test_retry_on_429(ms, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    answers = iter([
        httpx.Response(429, headers={"X-Lognex-Retry-After": "100"}),
        httpx.Response(200, json={"ok": True}),
    ])
    ms.routes["/entity/store"] = lambda request: next(answers)
    assert api.get_json(ms.client, "/entity/store") == {"ok": True}


def test_error_raises(ms, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    ms.routes["/entity/store"] = lambda request: httpx.Response(412, text="плохой фильтр")
    with pytest.raises(api.MsError) as error:
        api.get_json(ms.client, "/entity/store")
    assert error.value.status == 412
    assert len(ms.requests) == 1  # 4xx, кроме 429, не повторяем
