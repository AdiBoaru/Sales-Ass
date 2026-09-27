"""NX-333 felia 4b-3 — `rank_terms` în SQL-ul lexical: doar în `ORDER BY`, niciodată în predicat.

Contractul (I25): semnalele `unmapped` („bun pentru gaming", fără o fațetă de utilizare) ordonează
și nu filtrează niciodată. Trei garanții, fiecare cu testul ei:

- **Gol ⇒ SQL byte-identic cu `main`.** Snapshot-urile din `tests/kernel/sql/` au fost generate pe
  `origin/main` ÎNAINTEA schimbării (toate treptele × toate sorturile, plus calea lexicală veche),
  deci orice diferență pe calea vie pică aici (I16 pe SQL).
- **Doar după `order by`.** Placeholderul lui `rank_terms` nu apare niciodată în `WHERE`.
- **Niciun placeholder legat și nefolosit** (lecția NX-313: Postgres nu poate tipa un `$n` fără
  folosire și refuză tot query-ul). Pe sort explicit și pe calea veche nu se leagă nimic.

Regenerarea conștientă a snapshot-urilor:
`KERNEL_SQL_WRITE=1 pytest tests/test_rank_terms_sql.py`."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import pytest

from src.config import get_settings
from src.db.queries import catalog

SQL_DIR = Path(__file__).resolve().parent / "kernel" / "sql"
STEPS = ("strict", "relaxed", "relaxed_any", "fuzzy", "filters_only")
SORTS = ("relevance", "price_asc", "price_desc", "rating_desc")
TERMS = ["crema", "hidratanta", "ten"]


class _CaptureConn:
    def __init__(self) -> None:
        self.sql = ""
        self.params: tuple[Any, ...] = ()

    async def fetch(self, sql: str, *params: Any) -> list[Any]:
        self.sql, self.params = sql, params
        return []


def _render(step: str, sort_mode: str, *, v2: bool = True, terms=TERMS, **extra) -> _CaptureConn:
    """SQL-ul unei trepte, cu TOATE filtrele pe care le poate lega (fiecare placeholder exersat)."""
    conn = _CaptureConn()
    asyncio.run(
        catalog._lexical_fetch(
            conn,  # type: ignore[arg-type]
            "biz",
            query_text="crema hidratanta pentru ten",
            terms=list(terms),
            step=step,
            v2=v2,
            category=["ten"],
            brand="Anua",
            concerns=["dryness"],
            facet_filters={"skin_type": ["dry"]},
            features=["niacinamida"],
            searchable_facets=("key_ingredients",),
            variant_label="50 ml",
            price_max=99.5,
            constraints=(),
            sort_mode=sort_mode,
            in_stock_only=True,
            pool=50,
            **extra,
        )
    )
    return conn


def _cases() -> list[tuple[str, str, bool, tuple[str, ...]]]:
    """(nume fișier, treaptă, v2, termeni) × fiecare sort."""
    out = [(f"lexical_{step}", step, True, tuple(TERMS)) for step in STEPS]
    out.append(("lexical_filters_only_no_terms", "filters_only", True, ()))
    out.append(("lexical_v1", "strict", False, tuple(TERMS)))
    return out


def _snapshot_text(step: str, v2: bool, terms: tuple[str, ...], **extra) -> str:
    parts = []
    for sort_mode in SORTS:
        c = _render(step, sort_mode, v2=v2, terms=terms, **extra)
        parts.append(f"-- sort: {sort_mode}\n{c.sql}\n-- params: {list(c.params)!r}\n")
    return "\n".join(parts)


def _placeholders(sql: str) -> set[int]:
    return {int(n) for n in re.findall(r"\$(\d+)", sql)}


# --- I16 pe SQL: gol ⇒ byte-identic cu `main` -----------------------------------------------------


@pytest.mark.parametrize("name,step,v2,terms", _cases())
def test_empty_rank_terms_render_the_sql_of_main(name, step, v2, terms):
    """Snapshot generat pe `origin/main@11728b0` (înainte de câmpul nou): calea vie nu se schimbă
    cu un octet când plannerul nu dă termeni, pe nicio treaptă și niciun sort."""
    path = SQL_DIR / f"{name}.sql"
    text = _snapshot_text(step, v2, terms)
    if os.getenv("KERNEL_SQL_WRITE"):
        SQL_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8").replace("\r\n", "\n") == text, (
        f"{path.name}: SQL-ul fără rank_terms a divergat de main (I16)"
    )


def test_an_explicit_empty_list_is_the_same_as_no_field():
    for step in STEPS:
        for sort_mode in SORTS:
            base = _render(step, sort_mode)
            empty = _render(step, sort_mode, rank_terms=[])
            assert (empty.sql, empty.params) == (base.sql, base.params), (step, sort_mode)


# --- I25: doar în `ORDER BY` ----------------------------------------------------------------------


def _rank_placeholder(conn: _CaptureConn, value: str) -> str:
    [index] = [i for i, p in enumerate(conn.params, 1) if p == value]
    return f"${index}"


@pytest.mark.parametrize("step", STEPS)
def test_rank_terms_appear_only_after_order_by(step):
    c = _render(step, "relevance", rank_terms=["gaming"])
    ph = _rank_placeholder(c, "gaming")
    head, order = c.sql.rsplit(" order by ", 1)
    assert ph not in head, "rank_terms în predicat (I25)"
    assert f"ro_unaccent({ph})" in order


def test_the_rank_term_is_a_secondary_key_on_the_text_rungs():
    """Cheie SECUNDARĂ: un produs cu termenul urcă doar în grupul de egalitate al rangului de
    text."""
    for step in ("strict", "relaxed", "relaxed_any", "fuzzy"):
        c = _render(step, "relevance", rank_terms=["gaming"])
        ph = _rank_placeholder(c, "gaming")
        order = c.sql.rsplit(" order by ", 1)[1]
        text_rank = order.split(" desc, ", 1)[0]
        assert ph not in text_rank, step
        assert order.index(ph) > order.index(text_rank), step
        assert order.endswith(f"p.id limit ${len(c.params)}"), step


def test_filters_only_orders_by_the_text_first_and_by_the_rank_term_second():
    c = _render("filters_only", "relevance", rank_terms=["gaming"])
    ph = _rank_placeholder(c, "gaming")
    order = c.sql.rsplit(" order by ", 1)[1]
    first, second = order.split(" desc, ")[:2]
    assert ph not in first and ph in second


def test_filters_only_without_text_terms_orders_by_the_rank_term_then_rating():
    c = _render("filters_only", "relevance", terms=(), rank_terms=["gaming"])
    ph = _rank_placeholder(c, "gaming")
    order = c.sql.rsplit(" order by ", 1)[1]
    assert order.startswith(
        f"(ts_rank_cd(p.search_tsv, websearch_to_tsquery('simple', ro_unaccent({ph})))) desc, "
    )
    base = _render("filters_only", "relevance", terms=())
    assert order.endswith(
        base.sql.rsplit(" order by ", 1)[1].split(" limit ")[0] + f" limit ${len(c.params)}"
    )


def test_rank_terms_go_through_the_locale_stop_words():
    """„bun pentru gaming" ordonează pe «gaming», nu pe cuvintele goale ale locale-i."""
    conn = _CaptureConn()
    asyncio.run(
        catalog.search_products_lexical(
            conn,  # type: ignore[arg-type]
            "biz",
            "telefon",
            category=["telefoane"],
            locale="ro",
            rank_terms=["bun pentru gaming"],
        )
    )
    assert "bun or gaming" in conn.params  # „pentru" e cuvânt gol pe `ro`, deci nu ordonează
    assert not any(isinstance(p, str) and "pentru" in p for p in conn.params)


# --- NX-313: niciun placeholder legat și nefolosit ------------------------------------------------


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("sort_mode", SORTS)
def test_every_bound_parameter_is_used_with_rank_terms(step, sort_mode):
    for terms in (TERMS, ()):
        c = _render(step, sort_mode, terms=terms, rank_terms=["gaming"])
        assert _placeholders(c.sql) == set(range(1, len(c.params) + 1)), (step, sort_mode, terms)


@pytest.mark.parametrize("sort_mode", ("price_asc", "price_desc", "rating_desc"))
def test_an_explicit_sort_binds_no_rank_term(sort_mode):
    for step in STEPS:
        with_terms = _render(step, sort_mode, rank_terms=["gaming"])
        base = _render(step, sort_mode)
        assert (with_terms.sql, with_terms.params) == (base.sql, base.params), (step, sort_mode)


def test_the_old_lexical_path_binds_no_rank_term():
    """`LEXICAL_QUERY_V2_ENABLED=false`: o singură treaptă veche, iar `rank_terms` nu se leagă."""
    for sort_mode in SORTS:
        with_terms = _render("strict", sort_mode, v2=False, rank_terms=["gaming"])
        base = _render("strict", sort_mode, v2=False)
        assert (with_terms.sql, with_terms.params) == (base.sql, base.params), sort_mode
        assert "gaming" not in with_terms.params


def test_the_old_lexical_path_through_the_public_function(monkeypatch):
    monkeypatch.setenv("LEXICAL_QUERY_V2_ENABLED", "false")
    get_settings.cache_clear()
    try:
        conn = _CaptureConn()
        asyncio.run(
            catalog.search_products_lexical(
                conn,  # type: ignore[arg-type]
                "biz",
                "telefon",
                locale="ro",
                rank_terms=["gaming"],
            )
        )
        assert "gaming" not in conn.params
        assert _placeholders(conn.sql) == set(range(1, len(conn.params) + 1))
    finally:
        get_settings.cache_clear()
