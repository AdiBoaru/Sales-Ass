"""NX-341 — `get_products_by_ids` nu mai taie în tăcere la 6.

Plafonul ascuns `min(limit, 6)` pierdea produsele 7-10 ale unui checkout (linkul, totalul și
atribuirea acopereau 6, `ok=True`) și 7-8 ale unei pagini (cursorul avansa cu 8, hidratarea
dădea 6).
Acum `limit` e obligatoriu, validat, iar fiecare apelant din `src/` îl dă explicit (poarta AST).

Falsul de catalog de aici RESPECTĂ contractul funcției reale (primele `limit` id-uri, plafon
`PRODUCTS_BY_IDS_MAX`): falsul vechi din `test_checkout_link.py` ignora `limit`, de aceea
defectul n-a fost prins."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.db.queries import catalog as catalog_q
from src.db.queries.catalog import PRODUCTS_BY_IDS_MAX, get_products_by_ids
from src.tools import commerce_tools as cm
from tests.test_checkout_link import BASE, _ctx, _deps, _patch_create

ROOT = Path(__file__).resolve().parents[1]


class _RecordingConn:
    """Conexiune falsă: înregistrează argumentele interogării, nu întoarce rânduri."""

    def __init__(self) -> None:
        self.args: tuple = ()

    async def fetch(self, sql, *args):
        self.args = args
        return []


# --- funcția -------------------------------------------------------------------------------------


async def test_ten_ids_with_limit_ten_reach_the_query_whole():
    conn = _RecordingConn()
    ids = [f"00000000-0000-0000-0000-0000000000{i:02d}" for i in range(10)]
    await get_products_by_ids(conn, "b", ids, limit=10)
    _business, sent_ids, sent_limit = conn.args
    assert sent_ids == ids and sent_limit == 10  # pe `main`: 6 id-uri, limit 6


@pytest.mark.parametrize("limit", [0, PRODUCTS_BY_IDS_MAX + 1])
async def test_a_limit_outside_the_bounds_is_a_programming_error(limit):
    with pytest.raises(ValueError):
        await get_products_by_ids(_RecordingConn(), "b", ["x"], limit=limit)


async def test_the_limit_is_required():
    with pytest.raises(TypeError):
        await get_products_by_ids(_RecordingConn(), "b", ["x"])  # type: ignore[call-arg]


async def test_a_caller_can_still_take_the_first_n_of_its_candidates():
    conn = _RecordingConn()
    await get_products_by_ids(conn, "b", ["a", "b", "c", "d"], limit=2)
    assert conn.args[1] == ["a", "b"] and conn.args[2] == 2


# --- checkout: 7 și 10 produse --------------------------------------------------------------------


def _catalog(n: int) -> list[dict]:
    return [{"id": f"p{i}", "name": f"Produs {i}", "price": 10.0 + i} for i in range(1, n + 1)]


def _patch_real_by_ids(monkeypatch, rows: list[dict]) -> None:
    """Falsul care respectă contractul funcției reale (primele `limit`, plafonul, `limit` cerut)."""

    async def by_ids(conn, business_id, ids, *, limit, respect_content_status=False):
        if not 1 <= limit <= PRODUCTS_BY_IDS_MAX:
            raise ValueError(limit)
        wanted = list(ids)[:limit]
        by_id = {r["id"]: r for r in rows}
        return [by_id[i] for i in wanted if i in by_id]

    monkeypatch.setattr(cm, "get_products_by_ids", by_ids)


@pytest.mark.parametrize("n", [7, 10])
async def test_checkout_keeps_every_product_of_a_seven_or_ten_item_cart(monkeypatch, n):
    _patch_real_by_ids(monkeypatch, _catalog(n))
    writes: list[dict] = []
    _patch_create(monkeypatch, writes)
    ctx = _ctx(settings={"checkout_url": BASE})
    items = [{"product_id": f"p{i}", "variant_id": None, "quantity": 1} for i in range(1, n + 1)]
    res = await cm.checkout_link_tool(ctx, _deps(), {"cart_items": items})
    assert res.ok is True
    assert len(writes[0]["cart"]) == n  # pe `main`: 6
    ev = next(e for e in ctx.events if e.type == "checkout_link_created")
    assert ev.properties["items"] == n and ev.properties["dropped"] == 0
    assert f"Produs {n}" in res.llm_view


async def test_a_product_gone_from_the_catalog_is_counted_and_told(monkeypatch):
    _patch_real_by_ids(monkeypatch, _catalog(7)[:-1])  # p7 a dispărut
    writes: list[dict] = []
    _patch_create(monkeypatch, writes)
    ctx = _ctx(settings={"checkout_url": BASE})
    items = [{"product_id": f"p{i}", "variant_id": None, "quantity": 1} for i in range(1, 8)]
    res = await cm.checkout_link_tool(ctx, _deps(), {"cart_items": items})
    assert res.ok is True and len(writes[0]["cart"]) == 6
    ev = next(e for e in ctx.events if e.type == "checkout_link_created")
    assert ev.properties["dropped"] == 1
    assert "nu mai sunt în catalog" in res.llm_view


# --- poarta: niciun apelant fără `limit` explicit ------------------------------------------------


def calls_without_limit(root: Path) -> list[str]:
    """Apelurile `get_products_by_ids(...)` din `src/` fără `limit=` explicit (AST, nu grep)."""
    out: list[str] = []
    for path in sorted((root / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "get_products_by_ids" and not any(k.arg == "limit" for k in node.keywords):
                out.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return out


def test_every_caller_in_src_passes_an_explicit_limit():
    assert calls_without_limit(ROOT) == []


def test_the_gate_catches_a_call_without_limit(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text(
        "async def f(c):\n    return await get_products_by_ids(c, 'b', ['x'])\n", encoding="utf-8"
    )
    assert calls_without_limit(tmp_path) == ["src/x.py:2"]


def test_the_cap_matches_the_cart_facts_hydration():
    """Un singur plafon numit, aliniat cu hidratarea faptelor de coș (NX-237)."""
    import inspect

    from src.db.queries import carts

    default = inspect.signature(carts.load_cart_facts_rows).parameters["limit"].default
    assert PRODUCTS_BY_IDS_MAX == default == catalog_q.PRODUCTS_BY_IDS_MAX
