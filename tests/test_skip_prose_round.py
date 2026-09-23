"""NX-312 felia 2 — runda de proză a buclei se sare pe un tur de recomandare „doar căutare".

Trei niveluri, fiecare cu ce poate dovedi:
1. adaptorul REAL (`LLMClient.run_tool_loop`) cu un OpenAI fals: numărul de apeluri de model
   scade de la 2 la 1 când predicatul spune „ajunge" (testul pică pe `origin/main`, unde parametrul
   nu există, iar bucla cere mereu runda de text);
2. predicatul PUR, pe tot vocabularul de motive;
3. stagiul agent, cap-coadă: rich picat + proză sărită ⇒ carduri + frază, ZERO apeluri de
   recompunere (P6), iar flagul stins ⇒ drumul de dinainte.
ZERO OpenAI, zero DB."""

from dataclasses import replace

import pytest

from src.agent.llm import LLMClient
from src.config import get_settings
from src.domain.loader import load_domain_pack
from src.worker.stages.agent import (
    PROSE_ROUND_REASONS,
    _prose_round_redundant,
    agent_stage,
)
from tests.test_agent import PRODUCTS, _ctx, _deps, _patch_search
from tests.test_llm_tool_loop import _FakeOpenAI, _Msg, _ToolCall

# --- 1. adaptorul real ---------------------------------------------------------------------


def _search_then_prose():
    return [
        _Msg(tool_calls=[_ToolCall("c1", "search_products", '{"query":"crema"}')]),
        _Msg(content="Proză pe care calea bogată n-o citește."),
    ]


async def _execute(name, args):
    return "rezultat"


async def test_loop_without_predicate_pays_the_prose_round():
    fake = _FakeOpenAI(_search_then_prose())
    out = await LLMClient(fake, model_agent="a").run_tool_loop("sys", "u", [{"t": 1}], _execute)
    assert out.startswith("Proză")
    assert len(fake.chat.completions.calls) == 2


async def test_predicate_true_stops_after_the_tool_round():
    fake = _FakeOpenAI(_search_then_prose())
    seen = []

    def stop(names):
        seen.append(list(names))
        return True

    out = await LLMClient(fake, model_agent="a").run_tool_loop(
        "sys", "u", [{"t": 1}], _execute, stop_after_tools=stop
    )
    assert out == ""
    assert len(fake.chat.completions.calls) == 1  # apelul 2 nu mai pleacă
    assert seen == [["search_products"]]


async def test_parallel_searches_reach_the_predicate_as_one_round():
    fake = _FakeOpenAI(
        [
            _Msg(
                tool_calls=[
                    _ToolCall("c1", "search_products", '{"query":"ruj"}'),
                    _ToolCall("c2", "search_products", '{"query":"fond de ten"}'),
                ]
            ),
            _Msg(content="nu trebuie cerut"),
        ]
    )
    executed = []

    async def execute(name, args):
        executed.append(args["query"])
        return "r"

    out = await LLMClient(fake, model_agent="a").run_tool_loop(
        "sys", "u", [{"t": 1}], execute, stop_after_tools=lambda names: len(names) == 2
    )
    assert out == "" and sorted(executed) == ["fond de ten", "ruj"]
    assert len(fake.chat.completions.calls) == 1


async def test_predicate_false_keeps_the_loop_unchanged():
    fake = _FakeOpenAI(_search_then_prose())
    out = await LLMClient(fake, model_agent="a").run_tool_loop(
        "sys", "u", [{"t": 1}], _execute, stop_after_tools=lambda names: False
    )
    assert out.startswith("Proză") and len(fake.chat.completions.calls) == 2


async def test_raising_predicate_does_not_lose_the_turn():
    fake = _FakeOpenAI(_search_then_prose())

    def boom(names):
        raise RuntimeError("predicat stricat")

    out = await LLMClient(fake, model_agent="a").run_tool_loop(
        "sys", "u", [{"t": 1}], _execute, stop_after_tools=boom
    )
    assert out.startswith("Proză")  # bucla continuă ca înainte


# --- 2. predicatul pur ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        (dict(is_order=False, profile="recommend", called=["search_products"], has_products=True),
         (True, "search_only")),
        (dict(is_order=False, profile="recommend",
              called=["search_products", "search_products"], has_products=True),
         (True, "search_only")),
        (dict(is_order=True, profile="recommend", called=["search_products"], has_products=True),
         (False, "order")),
        (dict(is_order=False, profile="compare", called=["search_products"], has_products=True),
         (False, "not_recommend")),
        (dict(is_order=False, profile="recommend",
              called=["search_products", "get_product_details"], has_products=True),
         (False, "other_tool")),
        (dict(is_order=False, profile="recommend", called=["search_products"], has_products=False),
         (False, "no_products")),
    ],
)  # fmt: skip
def test_prose_round_redundant(kw, expected):
    assert _prose_round_redundant(**kw) == expected
    assert expected[1] in PROSE_ROUND_REASONS


# --- 3. stagiul agent, cap-coadă -----------------------------------------------------------


class _LoopLLM:
    """Fake care se poartă ca bucla REALĂ: rulează uneltele, întreabă predicatul, și întoarce proza
    doar dacă runda de text ar fi fost cerută. `complete_schema` = apelul rich, aici PICAT."""

    def __init__(self, *, tool_calls, final="Îți recomand Crema Hidratantă la 82.99 lei."):
        self._tool_calls = list(tool_calls)
        self._final = final
        self.complete_calls = 0
        self.got_predicate = False

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        self.complete_calls += 1
        return "recompunere"

    async def complete_schema(self, system, user, schema, **kw):
        raise TimeoutError("rich picat")

    async def run_tool_loop(self, system, user, tools, execute, **kw):
        for name, args in self._tool_calls:
            await execute(name, args)
        stop = kw.get("stop_after_tools")
        self.got_predicate = stop is not None
        if stop is not None and stop([name for name, _ in self._tool_calls]):
            return ""
        return self._final


def _events(ctx, kind):
    # `turn_id` îl injectează `emit` pe orice eveniment (NX-122); aici contează doar conținutul.
    return [
        {k: v for k, v in e.properties.items() if k != "turn_id"}
        for e in ctx.events
        if e.type == kind
    ]


_SEARCH = [("search_products", {"query": "crema", "price_max": None, "limit": 6})]


#: Un singur tip de produs pe ecran, ca pe turul măsurat (`d73822a3`: șase creme de față). E exact
#: cazul în care rezerva NX-299 NU ar fi scris nimic (ea cere ≥2 tipuri), deci ecranul ar fi rămas
#: fără nicio frază: premisa din card („rezerva dă mereu o frază") era falsă, testul a prins-o.
_ONE_TYPE = [{**p, "attributes": {"product_type": "crema de fata"}} for p in PRODUCTS]


def _ctx_with_pack(body):
    ctx = _ctx(body)
    ctx.business = replace(ctx.business, domain_pack=load_domain_pack(ctx.business))
    return ctx


async def test_search_only_turn_skips_prose_and_survives_a_failed_rich(monkeypatch):
    monkeypatch.setattr(get_settings(), "tool_loop_skip_prose_enabled", True, raising=False)
    monkeypatch.setattr(get_settings(), "rich_from_facts_enabled", True, raising=False)
    monkeypatch.setattr(get_settings(), "answer_shape_enabled", True, raising=False)
    _patch_search(monkeypatch, _ONE_TYPE)
    ctx = _ctx_with_pack("vreau o crema de hidratare")
    llm = _LoopLLM(tool_calls=_SEARCH)

    await agent_stage(ctx, _deps(llm))

    assert _events(ctx, "prose_round") == [{"skipped": True, "reason": "search_only"}]
    # P6: rich picat, proză absentă ⇒ carduri din catalog + o frază, niciodată tăcere…
    assert ctx.reply is not None and ctx.reply.rich is not None
    assert ctx.reply.rich.items
    assert "crema de fata" in (ctx.reply.rich.intro or "")
    assert _events(ctx, "rich_downgraded") == [{"reason": "structured-call-failed"}]
    # …și fără al doilea apel de model după unul care tocmai a eșuat.
    assert llm.complete_calls == 0


async def test_single_type_framing_is_only_for_the_sole_text_case(monkeypatch):
    """Pragul de 2 tipuri al slotului NX-299 rămâne neatins când proza EXISTĂ (flag stins)."""
    monkeypatch.setattr(get_settings(), "tool_loop_skip_prose_enabled", False, raising=False)
    monkeypatch.setattr(get_settings(), "rich_from_facts_enabled", True, raising=False)
    monkeypatch.setattr(get_settings(), "answer_shape_enabled", True, raising=False)
    _patch_search(monkeypatch, _ONE_TYPE)
    ctx = _ctx_with_pack("vreau o crema de hidratare")
    llm = _LoopLLM(tool_calls=_SEARCH, final="")  # modelul n-a scris nimic, dar nu DELIBERAT

    await agent_stage(ctx, _deps(llm))

    assert llm.complete_calls == 1  # recompunerea de dinainte rămâne pe drumul vechi
    assert "crema de fata" not in (ctx.reply.text or "")


async def test_zero_products_keeps_the_prose_round(monkeypatch):
    monkeypatch.setattr(get_settings(), "tool_loop_skip_prose_enabled", True, raising=False)
    _patch_search(monkeypatch, [])
    ctx = _ctx("vreau o crema de hidratare")
    llm = _LoopLLM(tool_calls=_SEARCH, final="Nu am găsit exact asta, spune-mi mai multe.")

    await agent_stage(ctx, _deps(llm))

    assert _events(ctx, "prose_round") == [{"skipped": False, "reason": "no_products"}]
    assert ctx.reply is not None and "spune-mi" in ctx.reply.text


async def test_flag_off_is_the_old_loop(monkeypatch):
    monkeypatch.setattr(get_settings(), "tool_loop_skip_prose_enabled", False, raising=False)
    monkeypatch.setattr(get_settings(), "rich_from_facts_enabled", True, raising=False)
    _patch_search(monkeypatch, PRODUCTS)
    ctx = _ctx("vreau o crema de hidratare")
    llm = _LoopLLM(tool_calls=_SEARCH)

    await agent_stage(ctx, _deps(llm))

    assert llm.got_predicate is False  # bucla primește exact argumentele de dinainte
    assert _events(ctx, "prose_round") == []
    # proza validă a modelului rămâne intro-ul rezervei, ca înainte de NX-312
    assert ctx.reply is not None and "82.99" in ctx.reply.text


async def test_failed_rich_on_show_more_does_not_recompose(monkeypatch):
    """`show_more` rula deja cu `final=""`, deci avea defectul latent: rich picat ⇒ recompunere."""
    monkeypatch.setattr(get_settings(), "tool_loop_skip_prose_enabled", True, raising=False)
    monkeypatch.setattr(get_settings(), "rich_from_facts_enabled", True, raising=False)
    from src.agent import finalize as finalize_mod
    from src.agent.planner import ResponsePlan

    llm = _LoopLLM(tool_calls=[])
    ctx = _ctx("mai arata-mi")
    plan = ResponsePlan(products=list(PRODUCTS), final="", query="mai arata-mi", prose_skipped=True)
    monkeypatch.setattr(finalize_mod.prompt_builder, "build_rich_system", lambda *a, **k: "s")
    monkeypatch.setattr(finalize_mod.prompt_builder, "build_reco_system", lambda *a, **k: "s")

    await finalize_mod.render(ctx, _deps(llm), plan)

    assert llm.complete_calls == 0
    assert ctx.reply is not None and ctx.reply.rich is not None and ctx.reply.rich.items
