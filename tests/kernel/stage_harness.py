"""NX-336 PR A — convertorul journey → tur REAL prin `agent_stage`.

Pașii 1-5 au rulat journey-urile prin funcțiile pure (`fixture_catalog.kernel_step`). Aici același
tur trece prin stagiul REAL: `agent_stage` cu `INTERPRETED_TURN_ENABLED`, ramura, orchestratorul și,
după ea, calea v1 (în PR A ramura întoarce mereu `False`). Interpretarea turului e eticheta
journey-ului, întoarsă de un client de model REAL (`LLMClient.complete_schema_raw`) peste un
transport fals: rândul `per_call` cu `purpose="interpret"` se scrie exact ca în producție, deci I13
se numără pe ce numără și producția, nu pe un contor al testului.

Ce e stub (ca la `tests/test_nx329_shortcut_resolver.py`), pe pachetul journey-ului:

- `fetch_reference_facts` → faptele catalogului de fixture (id-urile de fixture nu sunt UUID, iar
  funcția reală le-ar arunca), vocabularul, meniul de rafturi, `get_products_by_ids`,
  `continue_search_session`;
- calea v1 de după ramură: bucla de model scriptată (`ScriptedLLM`, fără unelte, proză goală),
  citirile promptului generat (`list_category_names`, `list_routing_aliases`).

Accesele la DB trec prin `deps.db(operație)` și se înregistrează pe etichetă, ca testul de fallback
să poată compara calea v1 cu flagul ON și OFF, fără citirile declarate ale kernelului.

Pachetul SOLE primește catalogul SINTETIC din `fixture_sole_catalog`; celelalte, catalogul lor de
fixture. Nimic de aici nu atinge un DB sau un model real."""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace as NS
from typing import Any

from src.agent import usage
from src.agent.llm import LLMClient
from src.catalog.vocabulary import CatalogVocabulary
from src.conversation.interpretation import TurnInterpretation
from src.conversation.kernel_trace import state_view
from src.conversation.references import CatalogLookup, ReferenceFacts
from src.conversation.state_v2 import ConversationStateV2, project_v1
from src.domain.pack import DomainPack
from src.evals.scripted_llm import ScriptedLLM
from src.models import (
    Author,
    BusinessConfig,
    Contact,
    ConversationState,
    Direction,
    InboundMessage,
    Message,
    TurnContext,
)
from src.worker.runner import PipelineDeps
from tests.kernel import fixture_catalog, fixture_sole_catalog, replay

PACKS: tuple[str, ...] = (*replay.FIXTURE_PACKS, "sole-ro")
#: Profilul de flaguri al ramurii (poarta de boot, §8): stările v2 și scurtăturile v2 aprinse.
FLAGS: dict[str, bool] = {
    "interpreted_turn_enabled": True,
    "conversation_state_v2_enabled": True,
    "conversation_state_v2_write_enabled": True,
    "reference_resolver_v2_shortcuts_enabled": True,
    "named_shortcut_targets_enabled": True,
    "refinement_guard_enabled": True,
    "single_brain_enabled": False,
}


# --- catalogul pe pachet --------------------------------------------------------------------------


@dataclass(frozen=True)
class Catalog:
    name: str
    pack: DomainPack
    vocab: CatalogVocabulary
    menu: tuple[tuple[str, int], ...]
    items: Mapping[str, dict[str, Any]]
    facts: Callable[[CatalogLookup | None], ReferenceFacts]


def catalog(name: str) -> Catalog:
    if name == fixture_sole_catalog.NAME:
        return Catalog(
            name=name,
            pack=fixture_catalog.pack(name),
            vocab=fixture_sole_catalog.vocabulary(),
            menu=fixture_sole_catalog.category_menu(),
            items=fixture_sole_catalog.products(),
            facts=fixture_sole_catalog.facts,
        )
    return Catalog(
        name=name,
        pack=fixture_catalog.pack(name),
        vocab=fixture_catalog.vocabulary(name),
        menu=fixture_catalog.category_menu(name),
        items=fixture_catalog.products(name),
        facts=lambda lookup=None: fixture_catalog.facts(name, lookup),
    )


def subject_pairs_of(cat: Catalog) -> list[tuple[str, str]]:
    """NX-348: perechile (raft, tip) ale catalogului de fixture, ca `subject_type_pairs` pe DB
    (raftul fixture n-are cale, deci cheia ține loc de cale, ca în `pair_exists`)."""
    out = set()
    for item in cat.items.values():
        kind = (item.get("attributes") or {}).get("product_type")
        if item.get("category") and isinstance(kind, str):
            out.add((item["category"], kind))
    return sorted(out)


def product_rows(cat: Catalog, ids: list[str]) -> list[dict[str, Any]]:
    """`get_products_by_ids` pe catalogul de fixture, în ordinea cerută."""
    out = []
    for pid in ids:
        p = cat.items.get(pid)
        if p is None:
            continue
        out.append(
            {
                "id": pid,
                "name": p["name"],
                "price": float(p["price"]),
                "url": f"https://example.test/{pid}",
                "availability": p.get("availability", "in_stock"),
                "attributes": dict(p.get("attributes") or {}),
                "rating": p.get("rating"),
            }
        )
    return out


# --- modelul: interpretarea prin clientul REAL, restul scriptat -----------------------------------


class Transport:
    """`chat.completions` fals: întoarce interpretarea scriptată. `fault` alege un eșec de
    furnizor (refuz, tăiere, JSON invalid, schemă greșită, excepție)."""

    def __init__(self) -> None:
        self.content: str = "{}"
        self.fault: str | None = None
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content, refusal, finish = self.content, None, "stop"
        if self.fault == "provider_error":
            raise RuntimeError("furnizor indisponibil")
        if self.fault == "refused":
            refusal = "no"
        elif self.fault == "truncated":
            finish = "length"
        elif self.fault == "invalid_json":
            content = "{not json"
        elif self.fault == "schema_violation":
            content = json.dumps({"thread": "continue"})
        return NS(
            choices=[NS(message=NS(content=content, refusal=refusal), finish_reason=finish)],
            usage=NS(
                prompt_tokens=10,
                completion_tokens=5,
                prompt_tokens_details=None,
                completion_tokens_details=NS(reasoning_tokens=0),
            ),
        )


class StageLLM(ScriptedLLM):
    """`ScriptedLLM` pentru calea v1; interpretarea trece prin `LLMClient.complete_schema_raw`."""

    def __init__(self, interpretation: TurnInterpretation | None = None) -> None:
        self.fx: dict[str, Any] = {"final": "", "tool_calls": []}
        super().__init__(lambda: self.fx)
        self.transport = Transport()
        if interpretation is not None:
            self.transport.content = json.dumps(interpretation.model_dump(mode="json"))
        self.real = LLMClient(NS(chat=NS(completions=self.transport)), model_agent="gpt-6-luna")
        self.loops: list[tuple[str, str]] = []

    async def complete_schema_raw(self, system, user, schema, **kw):  # type: ignore[override]
        return await self.real.complete_schema_raw(system, user, schema, **kw)

    async def run_tool_loop(self, system, user, tools, execute, **kw):  # type: ignore[override]
        self.loops.append((system, user))
        return await super().run_tool_loop(system, user, tools, execute, **kw)


# --- DB-ul: un provider care înregistrează eticheta fiecărei operații -----------------------------


class RecordingDb:
    def __init__(self) -> None:
        self.ops: list[str] = []
        self.fail: set[str] = set()

    def __call__(self, operation: str = "unlabeled") -> Any:
        @asynccontextmanager
        async def _cm() -> AsyncIterator[Any]:
            self.ops.append(operation)
            if operation in self.fail:
                raise ConnectionError(f"{operation} indisponibil")
            yield object()

        return _cm()


def install(
    monkeypatch: Any,
    cat: Catalog,
    *,
    flags: Mapping[str, bool] = FLAGS,
    executors: bool = False,
) -> None:
    """Stub-urile pe pachet + profilul de flaguri. Fiecare citire trece prin `deps.db`.

    `executors=False` (implicit) ține seam-ul executorilor DARK, ca în PR A/B: testele lor verifică
    lanțul, traceul și fallback-ul, nu servirea. PR C (`executors=True`) lasă executorii de citire
    de producție să ruleze."""
    from src.agent import deterministic as det  # noqa: PLC0415
    from src.agent import interpreted_turn as it  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415
    from src.tools import catalog_tools  # noqa: PLC0415
    from src.worker.stages import agent as agent_mod  # noqa: PLC0415

    settings = get_settings()
    for name, value in flags.items():
        monkeypatch.setattr(settings, name, value)

    async def fetch(deps, business_id, lookup, *, op="reference_facts"):
        async with deps.db(op):
            pass
        return cat.facts(lookup)

    async def vocabulary(deps, business_id, *, op="load_vocabulary", fail_open=True):
        async with deps.db(op):
            pass
        return cat.vocab

    async def menu(conn, business_id):
        return list(cat.menu)

    async def by_ids(conn, business_id, ids, **kw):
        return product_rows(cat, list(ids))

    async def compose_comparison(llm, ctx, comparison, products, **kw):
        return comparison

    async def roots(conn, business_id, ids):
        return {}

    async def no_candidates(conn, business_id, anchor_id):
        return []

    async def categories(conn, business_id):
        return [c for c, _ in cat.menu]

    async def aliases(conn, business_id, **kw):
        return []

    async def planner_by_ids(conn, business_id, ids, **kw):
        return []

    async def next_page(ctx, deps, sess, limit):
        return NS(products=[])

    async def nothing_cheaper(conn, business_id, *a, **kw):
        return []

    monkeypatch.setattr(it, "fetch_reference_facts", fetch)

    async def pairs(conn, business_id):
        return subject_pairs_of(cat)

    monkeypatch.setattr(it, "subject_type_pairs", pairs)
    monkeypatch.setattr(it, "get_vocabulary", vocabulary)
    monkeypatch.setattr(it, "list_category_menu", menu)
    monkeypatch.setattr(det, "fetch_reference_facts", fetch)
    monkeypatch.setattr(det, "get_vocabulary", vocabulary)
    monkeypatch.setattr(det, "get_products_by_ids", by_ids)
    monkeypatch.setattr(det, "compose_comparison", compose_comparison)
    monkeypatch.setattr(det, "product_category_roots", roots)
    monkeypatch.setattr(det, "similar_candidates", no_candidates)
    monkeypatch.setattr(agent_mod, "list_category_names", categories)
    monkeypatch.setattr(agent_mod, "list_category_menu", menu)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", aliases)
    monkeypatch.setattr("src.agent.planner.get_products_by_ids", planner_by_ids)
    monkeypatch.setattr("src.agent.planner.search_cheaper_than", nothing_cheaper)
    monkeypatch.setattr(agent_mod, "get_vocabulary", vocabulary)
    monkeypatch.setattr(catalog_tools, "continue_search_session", next_page)
    if executors:
        from src.agent import kernel_executors  # noqa: PLC0415

        monkeypatch.setattr(kernel_executors, "get_products_by_ids", by_ids)
    else:

        async def dark(ctx, deps, planned, outcome, *_):
            return None

        monkeypatch.setattr(it, "execute_plans", dark)


# --- turul ---------------------------------------------------------------------------------------


def build_ctx(
    cat: Catalog,
    state: ConversationStateV2,
    text: str,
    previous: tuple[str, ...] = (),
    *,
    turn_id: str = "t0",
    locale: str = "ro",
) -> TurnContext:
    """Contextul turului: starea v2 hidratată (proprietarul ei e procesorul), vederea v1 din
    proiecție, istoricul cu mesajul CURENT ultimul (`turn_uow` îl inserează înainte de citire)."""
    business = BusinessConfig(
        id=f"b-{cat.name}", slug=cat.name, name=cat.name, domain_pack=cat.pack
    )
    ctx = TurnContext(
        turn_id=turn_id,
        business=business,
        contact=Contact(id="c", business_id=business.id),
        message=InboundMessage(provider_msg_id=f"m-{turn_id}", body=text),
        conversation_id="conv",
    )
    ctx.language = locale
    ctx.state_v2 = state
    ctx.state = ConversationState.from_jsonb(project_v1(state))
    ctx.history = [
        Message(direction=Direction.INBOUND, author=Author.CONTACT, body=t)
        for t in (*previous, text)
    ]
    return ctx


@dataclass
class StageRun:
    ctx: TurnContext
    llm: StageLLM
    db: RecordingDb
    per_call: list[dict[str, Any]]
    reached: bool = False
    before_branch: dict[str, Any] | None = None
    after_branch: dict[str, Any] | None = None
    branch_result: bool | None = None
    #: NX-353: fereastra citirilor făcute ÎN ramură (`db.ops[start:end]`): citirile kernelului
    branch_ops: tuple[int, int] | None = None

    def ops_outside_branch(self) -> list[str]:
        """Citirile căii v1: tot ce nu s-a citit în ramură (acolo citește doar kernelul)."""
        if self.branch_ops is None:
            return list(self.db.ops)
        start, end = self.branch_ops
        return [*self.db.ops[:start], *self.db.ops[end:]]

    @property
    def interpret_rows(self) -> list[dict[str, Any]]:
        return [r for r in self.per_call if r.get("purpose") == "interpret"]

    def kernel_ops(self) -> list[str]:
        return list(self.db.ops)


#: Câmpurile contextului pe care ramura le poate atinge DECLARAT: evenimentele și traceul.
DECLARED_KERNEL_FIELDS: frozenset[str] = frozenset({"events", "trace"})


def context_fields(ctx: TurnContext) -> dict[str, Any]:
    """TOATE câmpurile contextului, copiate, în afara celor declarate ale kernelului."""
    return {
        f.name: copy.deepcopy(getattr(ctx, f.name))
        for f in dataclasses.fields(ctx)
        if f.name not in DECLARED_KERNEL_FIELDS
    }


async def run_turn(
    monkeypatch: Any,
    cat: Catalog,
    ctx: TurnContext,
    llm: StageLLM,
    *,
    db: RecordingDb | None = None,
) -> StageRun:
    """Un tur prin `agent_stage`, cu ramura spionată: se reține dacă a fost atinsă și contextul
    predat căii v1 (înainte și după orchestrator)."""
    from src.agent import interpreted_turn as it  # noqa: PLC0415
    from src.worker.stages.agent import agent_stage  # noqa: PLC0415

    db = db or RecordingDb()
    run = StageRun(ctx=ctx, llm=llm, db=db, per_call=[])
    original = it.run_interpreted_turn

    async def spy(ctx_, deps_, **kw):
        run.reached = True
        run.before_branch = context_fields(ctx_)
        start = len(db.ops)
        run.branch_result = await original(ctx_, deps_, **kw)
        run.branch_ops = (start, len(db.ops))
        run.after_branch = context_fields(ctx_)
        return run.branch_result

    monkeypatch.setattr(it, "run_interpreted_turn", spy)
    acc, token = usage.push()
    try:
        await agent_stage(ctx, PipelineDeps(db=db, llm=llm))
    finally:
        usage.pop(token)
    run.per_call = list(acc.call_rows)
    return run


# --- lanțul pur, pe același catalog ---------------------------------------------------------------


@dataclass(frozen=True)
class ChainTurn:
    index: int
    turn: replay.JourneyTurn
    state_before: ConversationStateV2
    previous: tuple[str, ...]
    step: fixture_catalog.KernelStep


def chain(journey: replay.Journey, cat: Catalog) -> Iterator[ChainTurn]:
    """Turele journey-ului prin `kernel_step`, în lanț, pe catalogul harnessului: starea de
    dinaintea fiecărui tur și pasul pur (referința pe straturi)."""
    state = ConversationStateV2()
    for index, turn in enumerate(journey.turns):
        previous = tuple(t.user_input for t in journey.turns[:index])
        step = fixture_catalog.kernel_step(
            journey.pack,
            state,
            turn.expect["interpretation"],
            turn.user_input,
            earlier=previous[::-1],
            shown_ids=turn.shown,
            turn_id=f"t{index}",
            locale=journey.locale,
            loaded=cat.pack,
            vocab=cat.vocab,
            catalog=cat.facts,
            answer_pending=True,
            pairs=subject_pairs_of(cat),
        )
        yield ChainTurn(index, turn, state, previous, step)
        state = step.state_after


# --- PR B: un executor SINTETIC și commit-ul real între ture --------------------------------------

#: Executorii care citesc catalogul (`fixture_catalog._CATALOG_EXECUTORS`, aceeași mulțime).
CATALOG_EXECUTORS: frozenset[str] = frozenset(fixture_catalog._CATALOG_EXECUTORS)


def synthetic_executor(cat: Catalog, shown: tuple[str, ...]) -> Callable[..., Any]:
    """`interpreted_turn.execute_plans` pentru testele PR-ului B: servește turul scriind în context
    EXACT câmpurile pe care le scriu executorii de producție și pe care commit-ul le citește prin
    `processor._sender_tail`: răspunsul (cu produsele `shown` ca carduri), `ctx.retrieval` (cu
    `catalog_read` pe executorii de catalog, NX-326), sesiunea de căutare în `state_patch` (o
    căutare cu produse o deschide, o paginare o rescrie), iar întrebarea porții ca `set_clarify`
    (`ask`) sau ca ultima frază a răspunsului (confirmarea, `noted`), ca memoria ei să fie scrisă.
    Nu scrie nevoi sau subiect: acelea sunt ale reducerului (I20)."""
    from src.models import RetrievalResult  # noqa: PLC0415

    async def execute(ctx: TurnContext, deps: Any, planned: Any, outcome: Any, *_: Any) -> bool:
        executors = {p.executor for p in planned.plans}
        rows = product_rows(cat, list(shown))
        question = outcome.decision.question
        if "ask" in executors:
            # executorul `ask` arată candidații întrebării (cardul, §4: id-urile = candidații)
            ctx.set_clarify(question or "?", field="kernel", resume_route="sales")
            if rows:
                ctx.reply.products = rows
            return True
        text = f"ok. {question}" if outcome.asked_kind == "noted" and question else "ok"
        if rows:
            ctx.set_reply(text, products=rows, cacheable=False)
        else:
            ctx.set_reply(text, cacheable=False)
        if executors & CATALOG_EXECUTORS or rows:
            ctx.retrieval = RetrievalResult(
                products=rows,
                source="kernel",
                catalog_read=bool(executors & CATALOG_EXECUTORS),
            )
        if "search" in executors and rows:
            ctx.state_patch["active_search"] = {
                "fp": f"fixture:{cat.name}",
                "pool": list(shown),
                "cursor": 0,
                "page": 0,
            }
        elif "page" in executors and ctx.state_v2.active_search:
            ctx.state_patch["active_search"] = dict(ctx.state_v2.active_search)
        return True

    return execute


def commit(cat: Catalog, ctx: TurnContext, base: ConversationStateV2) -> ConversationStateV2:
    """Commit-ul REAL al procesorului (`_build_new_state`, cu scrierea v2 aprinsă) peste documentul
    stării de dinaintea turului, apoi hidratarea lui, ca la turul următor (`_attach_state_v2`)."""
    from src.conversation.needs import NeedVocabulary  # noqa: PLC0415
    from src.conversation.state_v2 import hydrate_state_v2, serialize  # noqa: PLC0415
    from src.worker.processor import _build_new_state  # noqa: PLC0415

    reply = ctx.reply
    assert reply is not None
    doc = _build_new_state(
        serialize(base)[0],
        ctx,
        is_rich=reply.rich is not None,
        has_products=bool(reply.products),
    )
    return hydrate_state_v2(doc, NeedVocabulary.from_pack(cat.pack))


def expected_layers(step: fixture_catalog.KernelStep) -> dict[str, Any]:
    """Straturile lanțului pur, în forma etichetelor `first_divergence`. `reducer` = starea PORȚII
    (în PR A niciun executor nu rulează, deci traceul nu are ecranul turului)."""
    planned = step.planned
    assert planned is not None
    return {
        "interpretation": step.interpretation,
        "checked": list(step.checked),
        "resolver": list(step.resolved),
        "reducer": state_view(step.gate_state),
        "ambiguity": step.outcome.decision,
        "plan": planned.plans[planned.primary],
        "answer_policy": step.answer_policy,
    }


__all__ = [
    "CATALOG_EXECUTORS",
    "DECLARED_KERNEL_FIELDS",
    "FLAGS",
    "PACKS",
    "Catalog",
    "ChainTurn",
    "RecordingDb",
    "StageLLM",
    "StageRun",
    "Transport",
    "build_ctx",
    "catalog",
    "chain",
    "commit",
    "context_fields",
    "expected_layers",
    "install",
    "product_rows",
    "run_turn",
    "synthetic_executor",
]
