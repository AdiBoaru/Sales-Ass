"""NX-358 — diversificarea se face ÎN INTERIORUL cererii, nu peste ea.

Conversația `1848eeba` (`sole-ro`, 2026-09-30): «vreau o crema de hidratare» → «am tenul uscat» →
«ceva sub 100 lei». Relevanța punea creme de față pe locurile 1-7, iar pagina a ieșit cu trei măști
din cinci carduri. Două mecanisme din `diversify_pool` tăiau ACELEAȘI creme:

1. faza pe preț era o singură trecere: sărea tot ce cădea într-o terță deja acoperită și, după ce
   găsea ultima terță, lua produsele de DUPĂ ea (măștile), nu pe cele sărite;
2. cota „max 2 pe tip" plafona chiar tipul cerut, iar măștile fără tip derivat treceau libere.

Măsurat pe pool-ul real: fiecare reparație singură lasă tot trei măști; amândouă dau șase creme.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps

CREAM = "crema de fata"
MASK = "masca de fata"


def _p(pid: str, brand: str, price: float, ptype: str | None, **extra) -> dict:
    d = {"id": pid, "name": pid, "brand": brand, "price": price, **extra}
    if ptype is not None:
        d["attributes"] = {"product_type": ptype}
    return d


#: Pool-ul real al turului 3, în ordinea de relevanță de după retrogradarea epuizatelor (reprodus
#: cu `search_products_lexical` pe catalogul SOLE, 12/12 id-uri comune cu sesiunea salvată).
#: Două măști ANUA n-au `product_type` derivat, exact ca în catalog.
POOL_1848 = [
    _p("mizon-collagen", "MIZON", 60.0, None),
    _p("eubos-urea", "EUBOS", 100.0, CREAM),
    _p("althea-aqua", "Dr. Althea", 80.0, CREAM),
    _p("iunik-beta", "IUNIK", 100.0, CREAM),
    _p("boj-dynasty", "BEAUTY OF JOSEON", 93.41, CREAM),
    _p("konopka-age", "DR. KONOPKA'S", 30.0, CREAM),
    _p("anua-rice-mask", "ANUA", 26.0, None),
    _p("mediheal-mask", "MEDIHEAL", 13.0, MASK),
    _p("anua-peach-mask", "ANUA", 26.0, None),
    _p("vt-reedle-mask", "VT COSMETICS", 20.0, None),
    _p("mizon-firming", "MIZON", 93.41, CREAM),
    _p("cosrx-mask", "COSRX", 79.05, None),
]
MASKS = {"anua-rice-mask", "mediheal-mask", "anua-peach-mask", "vt-reedle-mask", "cosrx-mask"}


def _page(pool: list[dict], limit: int = 6, **kw) -> list[str]:
    return [p["id"] for p in ct.diversify_pool(pool, limit, max_per_type=2, **kw)[:limit]]


# --- funcția pură --------------------------------------------------------------------------------


def test_today_the_cream_request_gets_three_masks() -> None:
    """Defectul, pinuit pe calea veche (flagul stins = argumentele implicite)."""
    assert len(set(_page(POOL_1848)) & MASKS) == 3


@pytest.mark.parametrize(
    "kw",
    [{"tertile_representatives": True}, {"subject_types": frozenset({CREAM})}],
    ids=["doar-terte", "doar-tipul-cerut"],
)
def test_one_fix_alone_changes_nothing(kw) -> None:
    """Măsurătoarea care a decis forma reparației: fiecare mecanism singur taie aceleași creme."""
    assert len(set(_page(POOL_1848, **kw)) & MASKS) == 3


def test_both_fixes_serve_the_creams() -> None:
    page = _page(POOL_1848, tertile_representatives=True, subject_types=frozenset({CREAM}))
    assert not set(page) & MASKS
    assert page[0] == "mizon-collagen"  # top-1 neschimbat
    # scara de preț e în continuare acoperită, cu creme: 30 / 60 / 80-100 lei
    prices = {p["id"]: p["price"] for p in POOL_1848}
    lo, hi = 13.0, 100.0
    assert {ct._price_tertile(prices[i], lo, hi) for i in page} == {0, 1, 2}


def test_representative_is_the_most_relevant_of_its_tertile() -> None:
    """Varianta veche lua, după ultima terță, produsele de DUPĂ ea, nu pe cele sărite înainte."""
    pool = [
        _p("a0", "A", 10.0, None),
        _p("b1", "B", 100.0, None),
        _p("c2", "C", 95.0, None),
        _p("d3", "D", 90.0, None),
        _p("e4", "E", 50.0, None),
        _p("f5", "F", 12.0, None),
        _p("g6", "G", 11.0, None),
    ]
    assert _page(pool, 4) == ["a0", "b1", "e4", "f5"]  # vechi: c2 sărit, f5 luat
    assert _page(pool, 4, tertile_representatives=True) == ["a0", "b1", "c2", "e4"]


def test_only_the_requested_type_is_exempt_from_the_quota() -> None:
    """Scutirea e a tipului CERUT, nu o renunțare la cotă: celelalte tipuri rămân plafonate."""
    pool = [_p("c0", "C0", 50.0, CREAM)]
    pool += [_p(f"s{i}", f"S{i}", 50.0, "ser de fata") for i in range(1, 5)]
    pool += [_p(f"c{i}", f"C{i}", 50.0, CREAM) for i in range(5, 9)]
    page = ct.diversify_pool(
        pool, 6, max_per_type=2, subject_types=frozenset({CREAM}), tertile_representatives=True
    )[:6]
    types = Counter(ct._product_type(p) for p in page)
    assert types == {CREAM: 4, "ser de fata": 2}


def test_brand_quota_still_holds_for_the_requested_type() -> None:
    pool = [_p(f"a{i}", "A", 50.0 + i, CREAM) for i in range(5)]
    pool += [_p(f"x{i}", f"X{i}", 60.0 + i, CREAM) for i in range(5)]
    page = ct.diversify_pool(
        pool, 6, subject_types=frozenset({CREAM}), tertile_representatives=True
    )[:6]
    assert Counter(p["brand"] for p in page)["A"] == 2


def test_need_only_request_keeps_spreading_types() -> None:
    """Cazul pentru care există cota (NX-298, «scap de cosuri»): fără tip cerut, paleta rămâne."""
    cands = [_p(f"s{i}", chr(65 + i), 10.0 + i * 9, "ser") for i in range(6)]
    cands += [_p(f"c{i}", f"X{i}", 20.0 + i * 12, "crema") for i in range(3)]
    cands += [_p(f"m{i}", f"Y{i}", 15.0 + i * 15, "masca") for i in range(3)]
    page = ct.diversify_pool(cands, 6, tertile_representatives=True)[:6]
    types = Counter(ct._product_type(p) for p in page)
    assert page[0]["id"] == "s0" and types["ser"] <= 2 and len(types) == 3


def _legacy_diversify(candidates, limit, *, max_per_brand=2, max_per_type=2):
    """Oracolul: `diversify_pool` de dinainte de NX-358, copiat din `c380dd9^` (fără comentarii).
    Flagul stins trebuie să dea EXACT rezultatul lui (recenzia NX-358)."""
    n = len(candidates)
    if limit <= 0 or n <= limit:
        return list(candidates)
    prices = [p["price"] for p in candidates if p.get("price") is not None]
    lo, hi = (min(prices), max(prices)) if prices else (0.0, 0.0)
    tert = [
        None if p.get("price") is None else ct._price_tertile(float(p["price"]), lo, hi)
        for p in candidates
    ]
    present = {t for t in tert if t is not None}
    selected = [0]
    brand_count: dict = {}
    type_count: dict = {}
    covered: set = set()
    if candidates[0].get("brand"):
        brand_count[candidates[0]["brand"]] = 1
    if first_type := ct._product_type(candidates[0]):
        type_count[first_type] = 1
    if tert[0] is not None:
        covered.add(tert[0])
    for i in range(1, n):
        if len(selected) >= limit:
            break
        brand = candidates[i].get("brand")
        if brand and brand_count.get(brand, 0) >= max_per_brand:
            continue
        ptype = ct._product_type(candidates[i])
        if ptype and max_per_type is not None and type_count.get(ptype, 0) >= max_per_type:
            continue
        if tert[i] is None or tert[i] not in covered or present <= covered:
            selected.append(i)
            if brand:
                brand_count[brand] = brand_count.get(brand, 0) + 1
            if ptype:
                type_count[ptype] = type_count.get(ptype, 0) + 1
            if tert[i] is not None:
                covered.add(tert[i])
    if len(selected) < limit:
        chosen = set(selected)
        allow_brand = max_per_brand
        allow_type = max_per_type if max_per_type is not None else n
        for _ in range(n + 1):
            if len(selected) >= limit:
                break
            progressed = False
            for i in range(1, n):
                if len(selected) >= limit:
                    break
                if i in chosen:
                    continue
                brand = candidates[i].get("brand")
                if brand and brand_count.get(brand, 0) >= allow_brand:
                    continue
                ptype = ct._product_type(candidates[i])
                if ptype and type_count.get(ptype, 0) >= allow_type:
                    continue
                selected.append(i)
                chosen.add(i)
                progressed = True
                if brand:
                    brand_count[brand] = brand_count.get(brand, 0) + 1
                if ptype:
                    type_count[ptype] = type_count.get(ptype, 0) + 1
            if not progressed:
                allow_brand += 1
                allow_type += 1
    chosen_set = set(selected)
    front = [candidates[i] for i in sorted(chosen_set)]
    return front + [candidates[i] for i in range(n) if i not in chosen_set]


def test_flag_off_output_is_the_legacy_output() -> None:
    import random

    rng = random.Random(358)
    brands = [None, "", "A", "B", "C", "D"]
    types = [None, "", CREAM, MASK, "ser de fata"]
    for _ in range(3000):
        n = rng.randint(0, 14)
        pool = [
            _p(
                f"p{i}",
                rng.choice(brands),
                rng.choice([None, 0.0, 10.0, 25.0, 50.0, 99.0, float(rng.randint(1, 200))]),
                rng.choice(types),
            )
            for i in range(n)
        ]
        limit = rng.randint(-1, 8)
        mpt = rng.choice([None, 1, 2, 3])
        mpb = rng.choice([1, 2])
        got = ct.diversify_pool(pool, limit, max_per_brand=mpb, max_per_type=mpt)
        assert got == _legacy_diversify(pool, limit, max_per_brand=mpb, max_per_type=mpt)


def test_the_whole_pool_is_kept_for_pagination() -> None:
    out = ct.diversify_pool(
        POOL_1848, 6, subject_types=frozenset({CREAM}), tertile_representatives=True
    )
    assert sorted(p["id"] for p in out) == sorted(p["id"] for p in POOL_1848)


# --- apelul din `search_products` ----------------------------------------------------------------


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="ceva sub 100 lei"),
        conversation_id="conv",
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


@pytest.fixture
def spied(monkeypatch):
    calls: list[dict] = []
    real = ct.diversify_pool

    def spy(candidates, limit, **kw):
        calls.append(kw)
        return real(candidates, limit, **kw)

    async def fake_lex(conn, business_id, **kwargs):
        return [dict(p, availability="in_stock") for p in POOL_1848]

    async def no_vocab(deps, business_id):
        from src.catalog.vocabulary import CatalogVocabulary

        return CatalogVocabulary(business_id=business_id, dimensions={})

    monkeypatch.setattr(ct, "diversify_pool", spy)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    monkeypatch.setattr(ct, "get_vocabulary", no_vocab)
    return calls


def test_planned_search_gets_no_exemption(spied) -> None:
    """Recenzia NX-358: pe calea planificată `prefer.product_type` amestecă tipurile subiectului cu
    semnalele `inferred` ale interpretării (un tip ghicit de model). Scutirea ar fi fost a
    modelului."""
    args = ct.SearchArgs(query="ceva pentru cosuri", prefer={"product_type": ["ser de fata"]})
    asyncio.run(ct.run_planned_search(_ctx(), _deps(), args))
    assert spied[-1] == {"subject_types": frozenset(), "tertile_representatives": True}


def test_v1_search_exempts_the_subject_type(spied, monkeypatch) -> None:
    """Pe calea v1 singurul autor al lui `prefer` e NX-355, cu tipul subiectului din stare."""
    from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
    from src.conversation.subject import SUBJECT_KEY, ConversationSubject

    async def vocab(deps, business_id):
        types = (CREAM, MASK)
        return CatalogVocabulary(
            business_id=business_id,
            dimensions={"product_type": tuple(VocabEntry(key=t, label=t, count=9) for t in types)},
        )

    monkeypatch.setattr(ct, "get_vocabulary", vocab)
    ctx = _ctx()
    ctx.state.search_constraints = {
        SUBJECT_KEY: ConversationSubject(shelf_key="ten", product_type=CREAM).to_dict()
    }
    res = asyncio.run(run_tool(ctx, _deps(), "search_products", {"query": "crema hidratanta"}))
    assert spied[-1]["subject_types"] == frozenset({CREAM})
    assert not {p["id"] for p in res.products} & MASKS


def test_the_model_cannot_exempt_a_type(spied) -> None:
    """`prefer` e al plannerului (`PLANNER_ONLY_FIELDS`): trimis de model, se scoate înainte."""
    asyncio.run(
        run_tool(
            _ctx(),
            _deps(),
            "search_products",
            {"query": "crema de fata", "prefer": {"product_type": [MASK]}},
        )
    )
    assert spied[-1]["subject_types"] == frozenset()


def test_flag_off_is_the_old_call(spied, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "search_diversify_subject_aware_enabled", False)
    args = ct.SearchArgs(query="crema de fata", prefer={"product_type": [CREAM]})
    asyncio.run(ct.run_planned_search(_ctx(), _deps(), args))
    assert spied[-1] == {"subject_types": frozenset(), "tertile_representatives": False}
