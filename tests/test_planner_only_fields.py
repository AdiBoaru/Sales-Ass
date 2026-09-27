"""NX-333 felia 4b-3 — câmpurile DOAR ale plannerului (`rank_terms`, `prefer`) nu vin de la model.

`SearchArgs` e configurat să accepte câmpuri noi fără eroare, deci din ziua în care `rank_terms` și
`prefer` există pe model, `SearchArgs(**args)` le-ar LUA din argumentele modelului. Un model care
le-ar ghici ar ocoli plannerul (I2): ar ordona catalogul după cuvinte pe care nu le-a spus clientul.
Intrarea modelului (`search_products_tool`) le scoate înainte de validare și le numără
(`planner_field_from_model{field}`, numele câmpului, niciodată valoarea: P12).

Toate locurile unde argumentele modelului devin `SearchArgs` trec prin `search_products_tool`:
bucla de unelte (`run_tool` → registru), adaptorul de retrieval NX-238 (`current_live`) și
scurtăturile deterministe; testul de derivare de mai jos le caută mecanic, pe AST, în `src/`."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from src.agent import tool_definitions
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = {"id": "p1", "name": "Telefon A", "price": 100.0, "availability": "in_stock"}


def _ctx(body: str = "vreau un telefon") -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []
    fused: list[dict] = []

    async def fake_lex(conn, business_id, **kwargs):
        calls.append(kwargs)
        return [dict(PRODUCT)]

    real_fuse = ct.fuse_candidates

    def spy_fuse(lexical, vector, **kwargs):
        fused.append(kwargs)
        return real_fuse(lexical, vector, **kwargs)

    async def no_vocab(deps, business_id):
        from src.catalog.vocabulary import CatalogVocabulary

        return CatalogVocabulary(business_id=business_id, dimensions={})

    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    monkeypatch.setattr(ct, "fuse_candidates", spy_fuse)
    monkeypatch.setattr(ct, "get_vocabulary", no_vocab)
    return calls, fused


def _events(ctx, kind):
    return [e for e in ctx.events if e.type == kind]


def test_the_model_schema_does_not_offer_the_planner_fields():
    """`tool_definitions.py` rămâne neatins: schema văzută de model n-are câmpurile plannerului."""
    params = tool_definitions._SCHEMAS["search_products"]["function"]["parameters"]["properties"]
    assert not set(ct.PLANNER_ONLY_FIELDS) & set(params)


@pytest.mark.parametrize(
    "extra",
    [
        {"rank_terms": ["gaming"]},
        {"prefer": {"color": ["negru"]}},
        {"rank_terms": ["gaming"], "prefer": {"color": ["negru"]}},
    ],
)
def test_the_model_cannot_send_planner_only_fields(captured, extra):
    """Scoase înainte de validare, numărate pe NUME, iar căutarea e byte-identică cu cea fără
    ele."""
    calls, fused = captured
    base = {"query": "telefon"}
    ctx_clean, ctx_sent = _ctx(), _ctx()
    asyncio.run(run_tool(ctx_clean, _deps(), "search_products", dict(base)))
    clean_calls, clean_fused = list(calls), list(fused)
    calls.clear()
    fused.clear()
    res = asyncio.run(run_tool(ctx_sent, _deps(), "search_products", {**base, **extra}))
    assert res.ok
    assert calls == clean_calls and calls
    assert all("rank_terms" not in c for c in calls)
    assert fused == clean_fused
    events = _events(ctx_sent, "planner_field_from_model")
    assert sorted(e.properties["field"] for e in events) == sorted(extra)
    # P12: numele câmpului, niciodată valoarea trimisă de model
    assert not any("gaming" in str(e.properties) or "negru" in str(e.properties) for e in events)
    assert not _events(ctx_clean, "planner_field_from_model")
    assert (
        ctx_sent.state_patch["active_search"]["fp"] == ctx_clean.state_patch["active_search"]["fp"]
    )


def test_a_malformed_planner_field_from_the_model_is_dropped_not_rejected(captured):
    """Scos înainte de validare: nici măcar un `rank_terms` invalid nu poate strica turul."""
    res = asyncio.run(
        run_tool(_ctx(), _deps(), "search_products", {"query": "telefon", "rank_terms": 7})
    )
    assert res.ok


def test_the_planner_entry_keeps_them(captured):
    """Contraproba: intrarea PLANNERULUI (`run_planned_search`) le duce până la SQL și fuziune."""
    calls, fused = captured
    args = ct.SearchArgs(query="telefon", rank_terms=["gaming"], prefer={"color": ["negru"]})
    res = asyncio.run(ct.run_planned_search(_ctx(), _deps(), args))
    assert res.ok
    assert calls[0]["rank_terms"] == ["gaming"]
    assert fused[0]["prefer"] == {"color": ["negru"]}
    assert args.rank_terms == ["gaming"]  # planul nu e mutat de unealtă (copie)


def _search_args_builders() -> list[str]:
    """Locurile din `src/` unde se construiește `SearchArgs`, găsite pe AST (nu prin grep)."""
    out = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            value = getattr(func, "value", None)
            owner = value.id if isinstance(value, ast.Name) else getattr(value, "attr", None)
            if name == "SearchArgs" or (
                name in ("model_validate", "model_construct") and owner == "SearchArgs"
            ):
                rel = path.relative_to(ROOT).as_posix()
                out.append(f"{rel}:{node.lineno}")
    return out


def test_every_search_args_built_from_model_output_goes_through_the_guard():
    """Derivat mecanic: în `src/` există exact doi constructori de `SearchArgs`, intrarea modelului
    (`search_products_tool`, păzită) și plannerul (`_search_args`, singurul autor pe calea
    interpretată). Un al treilea constructor e un drum pe care garda nu-l vede."""
    builders = _search_args_builders()
    files = sorted({b.split(":")[0] for b in builders})
    assert files == ["src/agent/turn_planner.py", "src/tools/catalog_tools.py"], builders
    assert len(builders) == 2, builders
    tool = (ROOT / "src/tools/catalog_tools.py").read_text(encoding="utf-8")
    assert "SearchArgs(**_model_args(ctx, args))" in tool
