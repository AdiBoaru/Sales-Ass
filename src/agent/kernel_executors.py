"""NX-336 PR C (feliile C1 + C2) — executorii de CITIRE ai turului interpretat.

Planul turului (`PlannedTurn`, scris de `turn_planner`) spune CE se servește; aici se leagă la
executorii de azi, fără niciun apel de model în plus și fără a re-deduce intenția:

- `search`: `ToolRun.execute_planned(SearchArgs)`, apoi compunerea v1
  (`build_plan(kernel=True)` → `render`);
- `page`: `continue_search_session` pe sesiunea stării, apoi aceeași compunere;
- `detail`: un id ⇒ `serve_details`; ≥ 2 (`act_both`) ⇒ `serve_comparison`;
- `compare`: ≥ 2 id-uri ⇒ `serve_comparison`; o singură țintă ⇒ partenerul similar
  (`similar_candidates` → `pick_similar_partner`, ca chip-ul NX-319), apoi aceeași comparație;
- `link`: `_handle_link_intent(ids=…)`, niciodată `ids=None`;
- `ask`: întrebarea porții (`set_clarify`), cu candidații ca CARDURI (`_card_products`: identitate
  `product_id`, nume scurt), ca pe orice alt răspuns cu produse.

Ce nu e legat aici întoarce `None` (turul rămâne pe calea v1, motivul `dark`): `faq`/`order` (azi
răspunsul lor e proza modelului, iar planul nu poartă cuvintele clientului), `reply_only` (vine din
`chitchat`, unde v1 răspunde în context, sau dintr-o MUTAȚIE refuzată de poartă fără întrebare,
unde răspunsul e al mutației: PR D), mutațiile, `bundle`, `delegate` și planurile multiple (PR D).
O căutare care nu e o căutare nouă (aceeași amprentă, sesiune epuizată) refuză (`False`): răspunsul
„nu mai am" e al căii v1, nu „nu am găsit în catalog".

**I12 (C2), structural:** orice comparație primește `withhold`, care judecă politica de răspuns pe
produsele ÎNCĂRCATE (țintele + partenerul, `policy_for` al orchestratorului). Fără drept de verdict,
compunerea pierde sloturile de verdict (schemă și reguli, `compose_comparison(verdict=False)`), iar
închiderea devine fraza pachetului `verdict_unknown`, cu eticheta de rând a dimensiunii lipsă. Fără
frază sau fără etichetă, închiderea rămâne goală și se numără: verdictul tot nu se scrie.

**Confirmarea implicită (C2):** când poarta acționează și cere confirmarea unei nevoi `implicit`
(`asked_kind="noted"`), întrebarea e ULTIMA frază a răspunsului (`text`, plus câmpul citit de
widget), deci memoria ei se scrie (`note_asked`). Pe o căutare fără rezultate, întrebarea e
răspunsul.

Frazele kernelului vin DOAR din pachet (`DomainPack.kernel_sentences`, P11): fail-open pe o
dezvăluire (turul pleacă fără ea, `kernel_sentence_missing{code}`), fail-closed pe `no_results`
(`NoSentence` ⇒ orchestratorul cade pe v1 cu motivul `no_sentence`). O căutare cu zero rezultate
nu servește niciodată ecranul vechi (R3 e oprit prin `kernel=True`), nici „Îți recomand:" cu o listă
goală, și nici o dezvăluire în plus: fraza `no_results` spune deja tot.

Id-urile date executorilor vin din `TurnPlan.product_ids` (I1); singura excepție e partenerul unei
comparații cu o țintă, ales de interogarea de catalog, nu de model. Executorii nu scriu nevoi sau
subiect (I20): sesiunea de căutare și referințele trec prin commit-ul kernelului."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from src.agent import deterministic as det
from src.agent.fallbacks import _card_products
from src.agent.tool_executor import ToolRun
from src.config import get_settings
from src.conversation.answer_policy import dimension_label
from src.db.queries.catalog import get_products_by_ids
from src.models import RetrievalResult
from src.safety.policy import SafetyPolicy

if TYPE_CHECKING:
    from src.agent.turn_planner import PlannedTurn
    from src.conversation.ambiguity_gate import GateOutcome
    from src.conversation.interpretation import AnswerPolicy, TurnPlan
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

#: Executorii legați în C1. Oricare altul ⇒ `None` (calea v1).
READ_EXECUTORS: frozenset[str] = frozenset({"search", "page", "detail", "compare", "link", "ask"})
#: Codurile fail-closed: SUNT răspunsul, deci fără frază turul nu e servit de kernel.
_REQUIRED = frozenset({"no_results"})
#: Evenimentele KERNELULUI emise în faza executorilor: rămân și pe un tur căzut (restul
#: evenimentelor executorilor se scot, ca turul căzut să fie turul cu flagul stins).
KERNEL_EXECUTOR_EVENTS: frozenset[str] = frozenset(
    {"kernel_sentence_missing", "kernel_similar_partner", "verdict_withheld"}
)
#: Politica de răspuns a orchestratorului pe produsele unei comparații: (partenerii adăugați de
#: executor, rândurile încărcate) → `AnswerPolicy` sau `None` (nu e o judecată).
PolicyFor = Callable[[Sequence[str], Sequence[dict[str, Any]]], "AnswerPolicy | None"]
#: Câte produse arată o pagină a sesiunii (ca paginarea v1).
_PAGE_SIZE = 6


class NoSentence(Exception):
    """Pachetul n-are fraza unui cod fail-closed. Orchestratorul o numără ca `no_sentence`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def kernel_sentence(pack: Any, locale: str | None, code: str) -> str | None:
    """Fraza unui cod în limba turului (cu fallback pe limba de bază), sau `None`."""
    table = getattr(pack, "kernel_sentences", None) or {}
    lang = (locale or "").strip().lower()
    per_code = table.get(lang) or table.get(lang.split("-")[0]) or {}
    phrase = per_code.get(code)
    return phrase if isinstance(phrase, str) and phrase.strip() else None


def _required_sentence(ctx: TurnContext, code: str) -> str:
    phrase = kernel_sentence(getattr(ctx.business, "domain_pack", None), ctx.language, code)
    if phrase is None:
        raise NoSentence(code)
    return phrase


def _disclosure_text(ctx: TurnContext, planned: PlannedTurn) -> str:
    """Frazele dezvăluirilor planului, în ordine, fiecare o singură dată. Fail-open."""
    pack = getattr(ctx.business, "domain_pack", None)
    out: list[str] = []
    for _index, code in planned.disclosures:
        if code in _REQUIRED:
            continue
        phrase = kernel_sentence(pack, ctx.language, code)
        if phrase is None:
            ctx.emit("kernel_sentence_missing", code=code)
            continue
        if phrase not in out:
            out.append(phrase)
    return " ".join(out)


def _lead(text: str, current: str | None) -> str:
    return f"{text}\n\n{current}".strip() if current else text


def _prefix(ctx: TurnContext, text: str) -> None:
    """Pune dezvăluirile ÎNAINTEA răspunsului, o dată, pe tiparul frazei de siguranță NX-173: în
    `text` (istoricul) și în câmpul pe care îl citește widgetul pe fiecare formă (`rich.intro` pe
    recomandare, `comparison.intro` pe comparație). Un răspuns relativ la tur nu intră în cache."""
    reply = ctx.reply
    if not text or reply is None:
        return
    if text not in (reply.text or ""):
        reply.text = _lead(text, reply.text)
    for shown in (getattr(reply, "rich", None), getattr(reply, "comparison", None)):
        if shown is not None and text not in (shown.intro or ""):
            shown.intro = _lead(text, shown.intro)
    reply.cacheable = False


async def _compose(
    ctx: TurnContext,
    deps: PipelineDeps,
    run: ToolRun,
    products: list[dict[str, Any]],
    *,
    page: bool,
) -> None:
    """Compunerea v1 pe setul executorului (tiparul paginării din `agent_stage`): fără rundă de
    proză, iar `build_plan(kernel=True)` nu mai re-deduce intenția."""
    from src.agent.finalize import render  # noqa: PLC0415 — ciclul finalize ↔ agent
    from src.agent.planner import build_plan  # noqa: PLC0415
    from src.worker.context import conversation_transcript  # noqa: PLC0415
    from src.worker.stages.agent import _load_prompt_inputs  # noqa: PLC0415

    inp = await _load_prompt_inputs(deps, ctx)
    plan = await build_plan(
        ctx,
        deps,
        run,
        inp,
        final="",
        retrieved=products,
        is_order=False,
        show_more=page,
        query=(ctx.message.body or "").strip(),
        history=conversation_transcript(ctx.history),
        tool_names=[],
        # Proza NU a existat pe calea kernelului (nicio buclă), deci compunerea picată nu cere o
        # recompunere pe un text gol, indiferent de `TOOL_LOOP_SKIP_PROSE_ENABLED` (cardul §5).
        prose_skipped=True,
        kernel=True,
    )
    if not plan.handled:
        await render(ctx, deps, plan)


def _pack(ctx: TurnContext) -> Any:
    return getattr(ctx.business, "domain_pack", None)


def _verdict_note(ctx: TurnContext, missing: Sequence[str]) -> str:
    """Închiderea unei comparații fără verdict. Cu eticheta de rând a dimensiunii: fraza
    `verdict_unknown`, care o citează ca atare (o etichetă de rând nu e mereu un substantiv:
    „Potrivit pentru"). Fără etichetă (ratingul nu e o fațetă): fraza generică
    `verdict_unknown_any`. Fără frază ⇒ închidere goală, numărată; verdictul tot nu se scrie."""
    label = dimension_label(_pack(ctx), missing[0], ctx.language) if missing else None
    code = "verdict_unknown" if label is not None else "verdict_unknown_any"
    template = kernel_sentence(_pack(ctx), ctx.language, code)
    if template is None:
        ctx.emit("kernel_sentence_missing", code=code)
        return ""
    return template.replace("{dimension}", label, 1) if label is not None else template


def _withhold(
    ctx: TurnContext, policy_for: PolicyFor | None, partners: Sequence[str] = ()
) -> Callable[[list[dict[str, Any]]], str | None] | None:
    """`serve_comparison(withhold=)`: politica pe produsele încărcate (după siguranță). `None` ⇒
    verdictul e permis sau nu e o judecată; altfel închiderea care îl înlocuiește."""
    if policy_for is None:
        return None

    def withhold(rows: list[dict[str, Any]]) -> str | None:
        policy = policy_for(tuple(partners), rows)
        if policy is None or policy.verdict_allowed:
            return None
        ctx.emit("verdict_withheld", missing=list(policy.missing))
        return _verdict_note(ctx, policy.missing)

    return withhold


async def _compare(
    ctx: TurnContext, deps: PipelineDeps, ids: list[str], policy_for: PolicyFor | None
) -> bool | None:
    """≥ 2 ținte ⇒ comparația lor. O țintă ⇒ partenerul similar (NX-319), sub același kill-switch
    ca pe v1; ajunge aici doar când ACTUL are o singură țintă (orchestratorul lasă `dark` un act cu
    mai multe ținte din care una s-a pierdut, `_target_lost`)."""
    if len(ids) >= 2:
        return await det.serve_comparison(ctx, deps, ids, withhold=_withhold(ctx, policy_for))
    if not ids or not get_settings().compare_with_similar_enabled:
        return None
    async with deps.db("similar_candidates") as conn:
        candidates = await det.similar_candidates(conn, ctx.business.id, ids[0])
    partner = det.pick_similar_partner(candidates)
    ctx.emit("kernel_similar_partner", found=partner is not None, n=len(candidates))
    if partner is None:
        return False
    return await det.serve_comparison(
        ctx, deps, [ids[0], partner], withhold=_withhold(ctx, policy_for, (partner,))
    )


def _confirm(ctx: TurnContext, question: str) -> None:
    """Întrebarea de confirmare ca ULTIMA frază (C2): în `text` (istoricul și memoria întrebării) și
    în câmpul citit de widget (`rich.intro`, ultimul paragraf al comparației)."""
    reply = ctx.reply
    if reply is None or question in (reply.text or ""):
        return
    reply.text = f"{reply.text}\n\n{question}".strip() if reply.text else question
    rich = getattr(reply, "rich", None)
    if rich is not None:
        rich.intro = f"{rich.intro} {question}".strip() if rich.intro else question
    comparison = getattr(reply, "comparison", None)
    if comparison is not None:
        comparison.closing = [*(comparison.closing or []), question]
    reply.cacheable = False


async def _search(
    ctx: TurnContext, deps: PipelineDeps, plan: TurnPlan, outcome: GateOutcome
) -> bool:
    from src.tools import catalog_tools  # noqa: PLC0415 — ciclul unelte ↔ agent

    if plan.search_args is None:
        return False
    run = ToolRun(ctx, deps)
    result = await run.execute_planned(plan.search_args)
    if not run.retrieved:
        if result.llm_view == catalog_tools._NO_MORE_VIEW:
            # aceeași amprentă ca sesiunea activă, pool epuizat: nu e „nu am găsit în catalog"
            return False
        # confirmarea unei nevoi implicite, cu setul gol, e chiar răspunsul (cardul §5)
        text = _confirmation(outcome) or _required_sentence(ctx, "no_results")
        ctx.retrieval = RetrievalResult(products=[], source="kernel", catalog_read=True)
        ctx.set_reply(text, cacheable=False)
        return True
    await _compose(ctx, deps, run, list(run.retrieved), page=False)
    return True


async def _page(ctx: TurnContext, deps: PipelineDeps) -> bool:
    from src.tools import catalog_tools  # noqa: PLC0415 — ciclul unelte ↔ agent

    sess = ctx.state.active_search
    if not sess:
        return False
    result = await catalog_tools.continue_search_session(ctx, deps, sess, _PAGE_SIZE)
    if not result.products:
        return False  # pool epuizat: fraza „nu mai am" e a căii v1 (paginarea deterministă)
    run = ToolRun(ctx, deps)
    run.called.append("search_products")
    run.retrieved.extend(result.products)
    await _compose(ctx, deps, run, list(result.products), page=True)
    return True


async def _ask(ctx: TurnContext, deps: PipelineDeps, plan: TurnPlan, outcome: GateOutcome) -> bool:
    question = outcome.decision.question
    if not question:
        return False
    ctx.set_clarify(question, field="kernel", resume_route="sales")
    ids = list(plan.product_ids)
    if ids and ctx.reply is not None:
        async with deps.db("kernel_ask_candidates") as conn:
            rows = await get_products_by_ids(conn, ctx.business.id, ids, limit=len(ids))
        rows = SafetyPolicy.for_turn(ctx).gate(ctx, rows, purpose="kernel_ask")[0]
        if rows:
            ctx.reply.products = _card_products(rows, n=len(rows))
    return True


def _confirmation(outcome: GateOutcome) -> str | None:
    question = outcome.decision.question
    return question if outcome.asked_kind == "noted" and question else None


async def _run_plan(
    ctx: TurnContext,
    deps: PipelineDeps,
    plan: TurnPlan,
    outcome: GateOutcome,
    policy_for: PolicyFor | None,
) -> bool | None:
    kind, ids = plan.executor, list(plan.product_ids)
    if kind == "search":
        return await _search(ctx, deps, plan, outcome)
    if kind == "page":
        return await _page(ctx, deps)
    if kind == "ask":
        return await _ask(ctx, deps, plan, outcome)
    if kind == "link":
        if not ids:
            return False
        await det._handle_link_intent(ctx, deps, ids=ids)  # niciodată `ids=None`
        return True
    if kind == "detail":
        if len(ids) == 1:
            await det.serve_details(ctx, deps, ids[0])
            return ctx.reply is not None
        return await _compare(ctx, deps, ids, policy_for) if len(ids) >= 2 else False
    if kind == "compare":
        return await _compare(ctx, deps, ids, policy_for)
    return None


async def execute_read_plans(
    ctx: TurnContext,
    deps: PipelineDeps,
    planned: PlannedTurn,
    outcome: GateOutcome,
    policy_for: PolicyFor | None = None,
) -> bool | None:
    """Rulează planul turului. `None` = niciun executor legat (calea v1, `dark`), `False` =
    executorul a refuzat, `True` = a servit. `NoSentence` urcă la orchestrator. `policy_for` =
    politica de răspuns a orchestratorului, judecată pe produsele unei comparații (I12)."""
    if len(planned.plans) != 1:
        return None  # multi-act: PR D
    plan = planned.plans[0]
    if plan.executor not in READ_EXECUTORS:
        return None
    verdict = await _run_plan(ctx, deps, plan, outcome, policy_for)
    if not verdict:
        return verdict
    no_results = (
        plan.executor == "search" and ctx.retrieval is not None and not ctx.retrieval.products
    )
    if not no_results:
        # pe `no_results` fraza spune deja tot: o dezvăluire în plus ar contrazice-o
        _prefix(ctx, _disclosure_text(ctx, planned))
        question = _confirmation(outcome)
        if question is not None and plan.executor != "ask":
            _confirm(ctx, question)
    return verdict


__all__ = [
    "KERNEL_EXECUTOR_EVENTS",
    "READ_EXECUTORS",
    "NoSentence",
    "execute_read_plans",
    "PolicyFor",
    "kernel_sentence",
]
