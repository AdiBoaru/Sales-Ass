"""NX-343 — instrucțiunile magazinului numesc produsul pe care modelul îl vede, și pe cel întrebat.

Două defecte care se compun, cu `HOWTO_FROM_CATALOG_ENABLED` aprins și handle-urile NX-324 aprinse
(implicitul):

1. directiva numea produsul prin UUID-ul brut, lângă un bundle scris `[P1]…[Pk]`, deci
   instrucțiunile erau legate de un id care nu apărea nicăieri altundeva în mesaj;
2. directiva lua mereu `products[0]`, deci pe setul afișat reîncărcat «cum se folosește a doua?»
   primea instrucțiunile primului card.

Testele vechi nu le puteau prinde: id-ul de fixture era `"p1"` (diferit de handle-ul `"P1"` doar
prin majusculă), iar testul byte-identic stingea handle-urile. Aici id-urile au forma UUID."""

from __future__ import annotations

import re

import pytest

from src.agent import answer_shape
from src.agent.finalize import bundle_ref, rich_handles
from src.config import get_settings
from src.models import ProductRef
from tests.test_explain_shape import _LLM as _BaseLLM
from tests.test_explain_shape import INP, PACK, USAGE, _ctx

U1 = "3f2a9c1e-7b4d-4e8a-9c21-5a6b7c8d9e01"
U2 = "3f2a9c1e-7b4d-4e8a-9c21-5a6b7c8d9e02"
U3 = "3f2a9c1e-7b4d-4e8a-9c21-5a6b7c8d9e03"
OTHER_USAGE = "Aplica seara pe tenul curat, doua picaturi. Nu folosi pe pielea iritata."


def _p(pid: str, name: str, usage: str | None) -> dict:
    sections = [{"kind": "summary", "body": "x"}]
    if usage:
        sections.append({"kind": "usage", "body": usage})
    return {
        "id": pid,
        "name": name,
        "price": 80.0,
        "url": f"https://shop/{pid}",
        "availability": "in_stock",
        "ai_summary": f"{name} pentru ten.",
        "sections": sections,
    }


A = _p(U1, "Crema Yuja", USAGE)
B = _p(U2, "Ser Niacin", OTHER_USAGE)
C = _p(U3, "Toner Centella", None)


class _LLM(_BaseLLM):
    def __init__(self) -> None:
        super().__init__()
        self._j["items"] = [{"product_id": "P1", "pro_index": 0, "fit_clause": "ușoară"}]


@pytest.fixture
def howto_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "howto_from_catalog_enabled", True)
    assert get_settings().rich_item_handles_enabled  # implicitul de producție


async def _render(ctx, products: list[dict]) -> str:
    from src.agent.finalize import render
    from src.agent.planner import ResponsePlan
    from src.worker.runner import PipelineDeps

    llm = _LLM()
    plan = ResponsePlan(
        handled=False,
        products=[dict(p) for p in products],
        final="",
        is_order=False,
        query=ctx.message.body,
        history="",
        inp=INP,
        mode="rich",
    )
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), plan)
    return llm.calls[0][1]


def _directive_ref(user: str) -> str:
    m = re.search(r"(?:Instrucțiunile magazinului pentru|pentru produsul) \[([^\]]+)\]", user)
    assert m, "directiva lipsește"
    return m.group(1)


def _bundle_refs(user: str) -> set[str]:
    return set(re.findall(r"^\[([^\]]+)\] ", user, flags=re.M))


def _shown(*products: dict) -> list[ProductRef]:
    return [ProductRef(product_id=p["id"], name=p["name"], price=p["price"]) for p in products]


# --- 1. handle-ul ---------------------------------------------------------------------------------


async def test_the_directive_names_the_product_by_its_handle(howto_on):
    user = await _render(_ctx("cum se foloseste?"), [A])
    assert _directive_ref(user) == "P1"  # pe `main`: UUID-ul
    assert U1 not in user.split("Turul ăsta cere instrucțiuni")[1].split("\n")[0]
    assert _directive_ref(user) in _bundle_refs(user)


async def test_a_product_without_instructions_is_named_by_its_handle_too(howto_on):
    ctx = _ctx("cum se foloseste?")
    user = await _render(ctx, [C])
    assert "nu are instrucțiuni pentru produsul [P1]" in user


def test_bundle_ref_and_rich_handles_share_one_owner(monkeypatch):
    assert rich_handles([A, B]) == {"P1": U1, "P2": U2}
    assert bundle_ref([A, B], U2) == "P2"
    monkeypatch.setattr(get_settings(), "rich_item_handles_enabled", False)
    assert rich_handles([A, B]) is None and bundle_ref([A, B], U2) == U2


# --- 2. ținta -------------------------------------------------------------------------------------


async def test_the_second_card_gets_its_own_instructions(howto_on):
    """Setul afișat reîncărcat (R3): «cum se folosește a doua?» ⇒ instrucțiunile lui B, cu
    handle-ul lui B din bundle (pe `main`: ale lui A, prin UUID)."""
    ctx = _ctx("cum se foloseste a doua?")
    ctx.state.displayed_products = _shown(A, B, C)
    user = await _render(ctx, [A, B, C])
    assert OTHER_USAGE in user and USAGE not in user
    assert _directive_ref(user) == "P2"
    event = next(e for e in ctx.events if e.type == "howto_instructions")
    assert event.properties["target"] == "reference"


async def test_an_ambiguous_question_on_the_shown_set_gets_no_directive(howto_on):
    ctx = _ctx("cum se foloseste?")
    ctx.state.displayed_products = _shown(A, B, C)
    user = await _render(ctx, [A, B, C])
    assert "Turul ăsta cere instrucțiuni" not in user
    event = next(e for e in ctx.events if e.type == "howto_instructions")
    assert event.properties == {"reason": "no_target", "target": "ambiguous", "turn_id": "t"}


async def test_substitutes_after_the_detail_product_do_not_steal_the_directive(howto_on):
    """`get_product_details` pe un produs epuizat adaugă substitute DUPĂ el: ținta rămâne primul."""
    ctx = _ctx("cum se foloseste?")
    user = await _render(ctx, [A, B, C])  # ecran gol: produsele vin din detaliu, nu din ecran
    assert USAGE in user and _directive_ref(user) == "P1"


def test_howto_target_rules_in_order():
    shown = _shown(A, B, C)
    assert answer_shape.howto_target([A, B, C], shown, "a treia", "ro") == (C, "reference")
    assert answer_shape.howto_target([A, B, C], shown, "cum se aplica", "ro") == (None, "ambiguous")
    assert answer_shape.howto_target([A, B], [], "cum se aplica", "ro") == (A, "detail")
    assert answer_shape.howto_target([B], _shown(B), "cum se aplica", "ro") == (B, "reference")
    assert answer_shape.howto_target([], shown, "a doua", "ro") == (None, "no_products")


# --- flag stins: nicio directivă, nimic schimbat --------------------------------------------------


async def test_with_howto_off_no_target_is_computed():
    ctx = _ctx("cum se foloseste a doua?")
    ctx.state.displayed_products = _shown(A, B, C)
    user = await _render(ctx, [A, B, C])
    assert "Turul ăsta cere instrucțiuni" not in user
    assert not [e for e in ctx.events if e.type == "howto_instructions"]


def test_the_pack_used_here_declares_usage_as_instructions():
    assert "usage" in PACK.howto_sections
