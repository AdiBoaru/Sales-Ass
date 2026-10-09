"""NX-392 — un tur mixt (o întrebare despre magazin + o cerere de produse) e servit de kernel.

Setul nevăzut `mixed-2026-10-09`, m25 T1: «livrati si in chisinau? si as vrea un toner pt ten
gras». Interpretarea și planul au fost corecte (`store_info` + `find` ⇒ planurile `faq` și
`search`), dar două planuri fără mutație cădeau pe calea v1: bucla a căutat doar tonere, iar fraza
compozitorului despre livrare a fost tăiată ca „parte de magazin” (NX-373). Clientul n-a aflat nimic
despre livrare.

Turul se recunoaște din PLANURI (actele scrise de model), nu din cuvinte: răspunsul de magazin îl
scrie compozitorul din toate regulile active (modelul alege regula), apoi planul de produse, cu nota
că partea de magazin e răspunsă separat. Zero model real, zero DB."""

from __future__ import annotations

import pytest

from src.agent import kernel_executors as kx
from src.agent.turn_planner import PlannedTurn
from src.config import get_settings
from tests.test_interpreted_turn_c import _ctx, _outcome
from tests.test_nx382_composer import (
    RULES,
    _deps,
    _detail,
    _faq_plan,
    _reply,
    catalog,  # noqa: F401 — fixture
    electronics,  # noqa: F401 — fixture
)

STORE = "Livrarea costa 15 lei, gratuita peste 149 lei."  # citat exact din regulă (poarta NX-382)
DETAIL = "Crema se aplică seara, de 3 ori pe săptămână la început."


class SeqLLM:
    """Compozitorul scriptat pe sarcină: răspunsul de magazin și cel de produs."""

    def __init__(self, store=STORE, item=DETAIL):
        self.replies = {"store_info": store, "detail": item}
        self.calls: list[tuple[str, str]] = []

    async def complete_schema(self, system, user, schema, **kw):
        task = next(t for t in self.replies if f"TASK {t}:" in system)
        self.calls.append((task, user))
        reply = self.replies[task]
        if isinstance(reply, Exception):
            raise reply
        # un model care respectă contractul bifează obligațiile primite (enumul schemei)
        codes = schema["schema"]["properties"]["obligations_met"]["items"].get("enum", [])
        return _reply(reply, met=codes)


@pytest.fixture
def rules(monkeypatch, electronics):  # noqa: F811
    from src.tools import faq_tools

    monkeypatch.setattr(get_settings(), "composer_store_info_enabled", True)

    async def load(ctx, deps):
        return [dict(r) for r in RULES]

    monkeypatch.setattr(faq_tools, "load_rules", load)


def _mixed(*, faq_first: bool = True) -> PlannedTurn:
    plans = (_faq_plan(), _detail(None)) if faq_first else (_detail(None), _faq_plan())
    return PlannedTurn(plans=plans, primary=1 if faq_first else 0)


@pytest.mark.parametrize("faq_first", [True, False])
async def test_the_store_part_and_the_item_part_are_both_answered(
    rules,
    electronics,  # noqa: F811
    catalog,  # noqa: F811
    faq_first,
):
    llm = SeqLLM()
    ctx = _ctx(electronics, "x")
    served = await kx.execute_read_plans(ctx, _deps(llm), _mixed(faq_first=faq_first), _outcome())
    assert served is True
    text = ctx.reply.text
    assert text.startswith(STORE) and DETAIL in text
    assert [p["product_id"] for p in ctx.reply.products] == ["p1"]
    assert [task for task, _ in llm.calls] == ["store_info", "detail"]
    _, item_user = llm.calls[1]
    assert "STORE PART" in item_user, (
        "compozitorul de produse află că partea de magazin e a altcuiva"
    )
    _, store_user = llm.calls[0]
    assert "STORE PART" not in store_user
    assert {"outcome": "served", "turn_id": "t0"} in [
        e.properties for e in ctx.events if e.type == "kernel_store_and_read"
    ]


async def test_without_a_store_answer_the_item_reply_says_a_part_is_left(
    monkeypatch,
    rules,
    electronics,  # noqa: F811
    catalog,  # noqa: F811
):
    from src.tools import faq_tools

    async def none(ctx, deps):
        return []

    monkeypatch.setattr(faq_tools, "load_rules", none)
    llm = SeqLLM()
    ctx = _ctx(electronics, "x")
    assert await kx.execute_read_plans(ctx, _deps(llm), _mixed(), _outcome()) is True
    assert ctx.reply.text.startswith(DETAIL)
    [(task, user)] = llm.calls
    assert task == "detail" and "dropped_act" in user and "STORE PART" not in user


async def test_a_rejected_store_answer_is_not_served(
    rules,
    electronics,  # noqa: F811
    catalog,  # noqa: F811
):
    """O sumă care nu e în reguli: răspunsul de magazin pică la poartă, nu ajunge la client."""
    llm = SeqLLM(store="Livrarea e gratuită peste 99 lei.")
    ctx = _ctx(electronics, "x")
    assert await kx.execute_read_plans(ctx, _deps(llm), _mixed(), _outcome()) is True
    assert "99 lei" not in ctx.reply.text and ctx.reply.text.startswith(DETAIL)


@pytest.mark.parametrize(
    "plans",
    [
        (_faq_plan(), _faq_plan()),
        (_faq_plan(),),
    ],
)
def test_only_a_store_plan_beside_one_read_plan_is_a_mixed_turn(plans):
    assert kx._store_and_read(PlannedTurn(plans=plans)) is None


def test_the_mixed_turn_is_recognised_from_the_plans():
    assert kx._store_and_read(_mixed(faq_first=True)) == 1
    assert kx._store_and_read(_mixed(faq_first=False)) == 0
