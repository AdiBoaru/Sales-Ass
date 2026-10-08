"""NX-336 PR C (C1 + C2) și PR D (D1) — executorii turului interpretat, fără mutații.

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

**Bucla restrânsă (PR D1):** `delegate` (actul `other`), `faq` (`store_info`) și `order`
(`order_status`) rulează bucla de unelte a căii v1 (același system, același mesaj de user), cu
schema restrânsă la uneltele permise ∩ uneltele tenantului: `DELEGATE_TOOLS` pe `delegate`,
`faq_lookup` pe `faq`, `check_order` + `faq_lookup` pe `order` (setul v1 de comandă, FAQ întâi).
`execute` e ÎNVELIT într-un allowlist: un nume din
afara lui nu ajunge la `run_tool` (care execută orice unealtă înregistrată), se numără
(`delegate_tool_refused{name}`) și modelul primește un refuz structurat. Niciuna nu e mutație și
niciuna nu citește catalogul, deci niciun `product_id` nu vine de la model (I1, I10). Compunerea e a
căii v1 (`build_plan(kernel=True)` + `render`), pe proza buclei.

**Coșul și planurile multiple (PR D2):** `cart` adaugă produsele planului (țintele `exact`, I10)
prin `cart_add` (ambele căi ale coșului, cu idempotența lor), cu fraza pachetului `cart_added` /
`cart_failed`, verificate ÎNAINTEA oricărei scrieri. Cu complementare, cross-sell-ul căii v1
(reacție la produsul adăugat efectiv) pune el răspunsul. Două planuri = mutația întâi, apoi al
doilea plan, sărit dacă depinde de o mutație picată; fraza mutației vine ÎNAINTEA răspunsului
lui, iar după o mutație reușită turul NU mai cade pe v1 (ar dubla mutația): orice eșec al celui
de-al doilea plan lasă răspunsul mutației. O mutație refuzată de poartă fără întrebare
(`reply_only` cu motivul `mutation_unavailable` / `mutation_not_exact`) primește fraza pachetului
cu același nume.

**Rutina (PR D3):** `bundle` cu familia decisă de planner (`TurnPlan.family`, din subiect și
`routine_steps.family_by_shelf`) rulează `run_planned_routine`: nevoile dure, bugetul dur și
preferințele din `SearchArgs`-ul planului, ancora = ținta `exact`, fără re-judecarea argumentelor
pe textul recent. O rutină care nu se poate compune refuză (`False`), iar răspunsul e al căii v1.

Ce nu e legat aici întoarce `None` (turul rămâne pe calea v1, motivul `dark`): restul lui
`reply_only` (din `chitchat`, unde v1 răspunde în context), două planuri fără mutație și un `bundle`
fără familie declarată.
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

import copy
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.agent import deterministic as det
from src.agent.fallbacks import _card_products
from src.agent.observability import agent_prompt_event
from src.agent.tool_definitions import TOOL_NAMES
from src.agent.tool_executor import ToolRun
from src.agent.turn_planner import DELEGATE_TOOLS
from src.catalog.render_text import display_name
from src.config import get_settings
from src.conversation.answer_policy import dimension_label
from src.db.queries.catalog import get_products_by_ids
from src.domain.pack import kernel_sentence
from src.models import RetrievalResult
from src.safety.policy import SafetyPolicy

if TYPE_CHECKING:
    from src.agent.turn_planner import PlannedTurn
    from src.conversation.ambiguity_gate import GateOutcome
    from src.conversation.interpretation import AnswerPolicy, TurnPlan
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

#: Executorii legați (C1, C2, D1, D2). Oricare altul ⇒ `None` (calea v1).
READ_EXECUTORS: frozenset[str] = frozenset(
    {
        "search",
        "page",
        "detail",
        "compare",
        "link",
        "ask",
        "faq",
        "order",
        "delegate",
        "cart",
        "reply_only",
        "bundle",
    }
)
#: D3: singura unealtă de `bundle` pe care o știe kernelul (`DomainPack.bundle_executors`).
_ROUTINE_TOOL = "routine_plan"
#: Motivele porții pentru o mutație refuzată fără întrebare; fraza lor are același cod (D2).
_REFUSED_MUTATION: frozenset[str] = frozenset({"mutation_unavailable", "mutation_not_exact"})
#: Codurile fail-closed: SUNT răspunsul, deci fără frază turul nu e servit de kernel.
_REQUIRED = frozenset({"no_results"})
#: Evenimentele KERNELULUI emise în faza executorilor: rămân și pe un tur căzut (restul
#: evenimentelor executorilor se scot, ca turul căzut să fie turul cu flagul stins).
KERNEL_EXECUTOR_EVENTS: frozenset[str] = frozenset(
    {
        "kernel_sentence_missing",
        "kernel_similar_partner",
        "verdict_withheld",
        "delegate_tool_refused",
        "delegate_loop_failed",
        "kernel_second_plan_failed",
        "kernel_i10_blocked",
    }
)
#: Politica de răspuns a orchestratorului pe produsele unei comparații: (partenerii adăugați de
#: executor, rândurile încărcate) → `AnswerPolicy` sau `None` (nu e o judecată).
PolicyFor = Callable[[Sequence[str], Sequence[dict[str, Any]]], "AnswerPolicy | None"]
#: PR D1: uneltele pe care le poate chema bucla restrânsă, pe executor (∩ uneltele tenantului).
DELEGATED_TOOLS: dict[str, frozenset[str]] = {
    "delegate": DELEGATE_TOOLS,
    "faq": frozenset({"faq_lookup"}),
    # ca setul v1 de comandă (`tools.base._ORDER_TOOLS`, FAQ întâi): o întrebare de politică citită
    # ca `order_status` («cum fac retur la comandă?») are răspuns fără cont (recenzia D1)
    "order": frozenset({"check_order", "faq_lookup"}),
}
#: Ce primește modelul când cheamă o unealtă din afara allowlist-ului: structură, nu frază (P11).
_REFUSED_TOOL = json.dumps({"ok": False, "error": "tool_not_allowed"})
#: Câte produse arată o pagină a sesiunii (ca paginarea v1).
_PAGE_SIZE = 6


class NoSentence(Exception):
    """Pachetul n-are fraza unui cod fail-closed. Orchestratorul o numără ca `no_sentence`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _required_sentence(ctx: TurnContext, code: str) -> str:
    phrase = kernel_sentence(getattr(ctx.business, "domain_pack", None), ctx.language, code)
    if phrase is None:
        raise NoSentence(code)
    return phrase


def _disclosure_text(
    ctx: TurnContext, planned: PlannedTurn, skip: frozenset[int] = frozenset()
) -> str:
    """Frazele dezvăluirilor planului, în ordine, fiecare o singură dată. Fail-open. `skip` =
    planurile care n-au servit: dezvăluirile lor nu descriu răspunsul (NX-374, recenzia: pe un coș
    picat, căutarea dependentă nu rulează, deci „n-am ținut cont la alegere" ar fi fals)."""
    pack = getattr(ctx.business, "domain_pack", None)
    out: list[str] = []
    for index, code in planned.disclosures:
        if code in _REQUIRED or index in skip:
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


def _append(ctx: TurnContext, text: str) -> None:
    """Dezvăluirile DUPĂ fraza unei mutații (fraza mutației e prima, recenzia D2)."""
    reply = ctx.reply
    if not text or reply is None:
        return
    if text not in (reply.text or ""):
        reply.text = f"{reply.text}\n\n{text}".strip() if reply.text else text
    rich = getattr(reply, "rich", None)
    if rich is not None and text not in (rich.intro or ""):
        # pe web, un răspuns bogat se citește din `rich`, nu din `text` (recenzia D2)
        rich.intro = f"{rich.intro}\n\n{text}".strip() if rich.intro else text
    reply.cacheable = False


def _message(ctx: TurnContext) -> str:
    """Cititorul DECLARAT al mesajului curent: pleacă neschimbat spre compunerea v1 și spre bucla
    restrânsă, ca pe calea de azi. Kernelul nu ramifică pe el: planul turului e deja decis."""
    return (ctx.message.body or "").strip()


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
        query=_message(ctx),
        history=conversation_transcript(ctx.history),
        tool_names=[],
        # Proza NU a existat pe calea kernelului (nicio buclă), deci compunerea picată nu cere o
        # recompunere pe un text gol, indiferent de `TOOL_LOOP_SKIP_PROSE_ENABLED` (cardul §5).
        prose_skipped=True,
        kernel=True,
    )
    if not plan.handled:
        await render(ctx, deps, plan)


async def _delegate(ctx: TurnContext, deps: PipelineDeps, allowed: frozenset[str]) -> bool | None:
    """Bucla restrânsă (PR D1): bucla v1 de unelte, cu schema și `execute` limitate la `allowed`
    ∩ uneltele tenantului. Fără nicio unealtă permisă ⇒ `None` (calea v1)."""
    from src.agent import prompt_builder  # noqa: PLC0415 — ciclul agent
    from src.agent.finalize import render  # noqa: PLC0415
    from src.agent.planner import build_plan  # noqa: PLC0415
    from src.worker.context import context_blocks, conversation_transcript  # noqa: PLC0415
    from src.worker.stages.agent import (  # noqa: PLC0415
        _filters_hint,
        _load_prompt_inputs,
        tool_loop_tools,
        tool_loop_user_parts,
    )

    names, schemas = tool_loop_tools(ctx.business, "sales", unrouted=True)
    allow = [n for n in names if n in allowed]
    if not allow:
        return None
    schemas = [s for s in schemas if s.get("function", {}).get("name") in allow]
    run = ToolRun(ctx, deps)

    async def execute(name: str, args: dict[str, Any]) -> str:
        if name not in allow:
            ctx.emit("delegate_tool_refused", name=name if name in TOOL_NAMES else "unknown")
            return _REFUSED_TOOL
        return await run.execute(name, args)

    inp = await _load_prompt_inputs(deps, ctx)
    history = conversation_transcript(ctx.history)
    query = _message(ctx)
    user = tool_loop_user_parts(
        language=ctx.language,
        history=history,
        hints=_filters_hint(ctx.state.search_constraints),
        context=context_blocks(ctx, consumer="agent"),
        query=query,
    ).legacy()
    system = prompt_builder.build_agent_system(inp)
    try:
        final = await deps.llm.run_tool_loop(system, user, schemas, execute)
    except Exception as e:  # noqa: BLE001 — P6: ca pe v1, fără o a doua buclă
        # Pe v1 o buclă picată lasă turul fără răspuns, iar `fallback_stage` pune întrebarea de
        # clarificare. Aici același răspuns, servit de kernel: căderea pe v1 ar rula bucla A DOUA
        # OARĂ exact pe turele pe care furnizorul e bolnav (recenzia D1).
        from src.worker.runner import fallback_stage  # noqa: PLC0415 — ciclul runner ↔ agent

        ctx.emit("delegate_loop_failed", error=type(e).__name__)
        await fallback_stage(ctx, deps)
        return ctx.reply is not None
    plan = await build_plan(
        ctx,
        deps,
        run,
        inp,
        final=final or "",
        retrieved=list(run.retrieved),
        # o vedere de comandă ajunsă din unealtă ⇒ ramura ORDER a lui `render` (vederea grounded a
        # comenzii), nu validatorul de proză de vânzare (recenzia D1)
        is_order=bool(run.order_views),
        show_more=False,
        query=query,
        history=history,
        tool_names=allow,
        kernel=True,
    )
    validation = None if plan.handled else await render(ctx, deps, plan)
    # ca pe v1: promptul (hash) și verdictul validatorului, pentru Turn Replay (P10, P12)
    ctx.emit(
        "agent_prompt",
        **agent_prompt_event(
            system,
            user,
            list(run.retrieved),
            store_prompt=get_settings().replay_store_prompt_enabled,
            validator=validation,
        ),
    )
    return ctx.reply is not None


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


def _seen_by_subject(ctx: TurnContext) -> tuple[str, ...]:
    """NX-378: ce a văzut clientul, după starea PORȚII: ecranul (pe o reluare, reducerul a pus
    înapoi setul subiectului parcat) și seturile de mai devreme. Vederea v1 (`displayed_products`)
    arată încă ecranul subiectului de dinainte de reluare: pe k1 T4 («înapoi la seruri, mai
    arată-mi altele») căutarea exclusese șampoanele, aducea aceleași seruri, iar compunerea le
    refuza ca deja văzute (zero carduri)."""
    view = getattr(ctx, "kernel_view", None)
    return seen_in_state(getattr(view, "gate_state", None))


def seen_in_state(state: Any) -> tuple[str, ...]:
    """Ecranul și seturile de mai devreme ale unei stări v2, ca id-uri, în ordine, fără dubluri.
    PUR. Îl folosesc și calea servită, și cea dark (`interpreted_turn._dark_search`)."""
    refs = getattr(state, "references", None)
    if refs is None:
        return ()
    ids = [d.product_id for d in refs.displayed_products]
    ids += [d.product_id for s in refs.recent_sets for d in s]
    return tuple(dict.fromkeys(i for i in ids if i))


async def _search(
    ctx: TurnContext,
    deps: PipelineDeps,
    plan: TurnPlan,
    outcome: GateOutcome,
    exclude_shown: bool = False,
) -> bool:
    from src.tools import catalog_tools  # noqa: PLC0415 — ciclul unelte ↔ agent

    if plan.search_args is None:
        return False
    run = ToolRun(ctx, deps)
    result = await run.execute_planned(
        plan.search_args,
        exclude_shown=exclude_shown,
        seen_extra=(
            _seen_by_subject(ctx)
            if exclude_shown and get_settings().search_resume_excludes_subject_seen_enabled
            else ()
        ),
    )
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


@dataclass(frozen=True)
class _Mutation:
    """Rezultatul executorului `cart`: produsele adăugate (rândurile uneltei), câte au picat și
    `ToolRun`-ul lui (cross-sell-ul citește produsul adăugat de acolo)."""

    added: tuple[dict[str, Any], ...]
    failed: int
    run: ToolRun


async def _cart(ctx: TurnContext, deps: PipelineDeps, plan: TurnPlan) -> _Mutation:
    """`cart_add` pe fiecare id al planului (ținte `exact`, I10), prin `ToolRun`: aceeași unealtă
    ca v1, care alege singură `CartService` (idempotent per tur și comandă) sau coșul din stare.

    Reușita se citește din unealtă (o acțiune reușită în plus), nu din `added_product`, care
    rămâne de la id-ul anterior: un replay idempotent al serviciului întoarce `ok` FĂRĂ produse, iar
    produsul lui se recitește din catalog (recenzia D2)."""
    run = ToolRun(ctx, deps)
    added: list[dict[str, Any]] = []
    failed = 0
    for pid in plan.product_ids:
        before, previous = len(run.successful_action_ids), run.added_product
        await run.execute("cart_add", {"product_id": pid})
        if len(run.successful_action_ids) == before:
            failed += 1
            continue
        row = run.added_product if run.added_product is not previous else None
        if row is None or str(row.get("id")) != pid:
            async with deps.db("kernel_cart_product") as conn:
                rows = await get_products_by_ids(conn, ctx.business.id, [pid], limit=1)
            row = rows[0] if rows else {"id": pid, "name": ""}
        added.append(row)
    return _Mutation(added=tuple(added), failed=failed, run=run)


def _cart_lead(ctx: TurnContext, mutation: _Mutation) -> str:
    """Fraza mutației: un `cart_added` pe produs adăugat (numele scurt) și `cart_failed` dacă a
    picat ceva. Frazele au fost verificate înaintea scrierii (`_require_cart_sentences`)."""
    pack = _pack(ctx)
    added = kernel_sentence(pack, ctx.language, "cart_added") or ""
    parts = [
        added.replace("{product}", display_name(str(p.get("name") or "")), 1)
        for p in mutation.added
    ]
    if mutation.failed:
        parts.append(kernel_sentence(pack, ctx.language, "cart_failed") or "")
    return " ".join(p for p in parts if p)


def _require_cart_sentences(ctx: TurnContext) -> None:
    """Fail-closed ÎNAINTEA oricărei scrieri: după o mutație turul nu mai are voie să cadă pe v1."""
    for code in ("cart_added", "cart_failed"):
        _required_sentence(ctx, code)


def _set_cart_reply(ctx: TurnContext, mutation: _Mutation) -> None:
    products = list(mutation.added)
    ctx.retrieval = RetrievalResult(products=products, source="kernel_cart", catalog_read=False)
    ctx.set_reply(
        _cart_lead(ctx, mutation),
        products=_card_products(products, n=len(products)) or None,
        cacheable=False,
    )


async def _cross_sell(ctx: TurnContext, deps: PipelineDeps, mutation: _Mutation) -> bool:
    """Cross-sell-ul căii v1 după un produs adăugat EFECTIV (nu o intenție dedusă): complementarele
    ca carduri. Intro-ul lui (confirmarea din codul v1, cu numele întreg al ULTIMULUI produs) devine
    fraza pachetului pe TOATE produsele adăugate. Orice eșec ⇒ `False` (răspunsul coșului),
    niciodată o cădere pe v1 după mutație."""
    from src.agent.fallbacks import _cart_confirm_msg  # noqa: PLC0415
    from src.agent.planner import maybe_cross_sell  # noqa: PLC0415 — ciclul agent
    from src.worker.context import conversation_transcript  # noqa: PLC0415
    from src.worker.stages.agent import _load_prompt_inputs  # noqa: PLC0415

    try:
        inp = await _load_prompt_inputs(deps, ctx)
        served = await maybe_cross_sell(
            ctx,
            deps,
            run=mutation.run,
            inp=inp,
            history=conversation_transcript(ctx.history),
            policy=SafetyPolicy.for_turn(ctx),
        )
    except Exception as e:  # noqa: BLE001 — după mutație: răspunsul coșului (P6)
        ctx.emit("kernel_second_plan_failed", error=type(e).__name__)
        return False
    reply = ctx.reply
    if not served or reply is None:
        return False
    old = _cart_confirm_msg(mutation.run.added_product or {}, ctx.language)
    _replace_lead(reply, old, _cart_lead(ctx, mutation))
    return True


def _replace_lead(reply: Any, old: str, lead: str) -> None:
    """Confirmarea din codul v1 → fraza pachetului, în `text` și în `rich.intro`. Lipsă: fraza
    pachetului se pune înainte."""
    rich = getattr(reply, "rich", None)
    if rich is not None:
        intro = rich.intro or ""
        rich.intro = intro.replace(old, lead, 1) if old and old in intro else _lead(lead, intro)
    text = reply.text or ""
    reply.text = text.replace(old, lead, 1) if old and old in text else _lead(lead, text)
    reply.cacheable = False


async def _serve_cart(ctx: TurnContext, deps: PipelineDeps, plan: TurnPlan) -> bool:
    _require_cart_sentences(ctx)
    mutation = await _cart(ctx, deps, plan)
    # cross-sell doar pe o mutație reușită integral: un eșec parțial se spune (recenzia D2)
    if mutation.added and not mutation.failed and await _cross_sell(ctx, deps, mutation):
        return True
    _set_cart_reply(ctx, mutation)
    return True


@dataclass(frozen=True)
class _SecondPlan:
    """Ce poate scrie al doilea plan în context înainte să știm dacă servește: sesiunea (în
    `state_patch`), propunerile și evenimentele. Se anulează dacă nu servește (recenzia D2)."""

    patch: dict[str, Any]
    proposals: list[Any]
    events: int
    routine: Any

    @classmethod
    def take(cls, ctx: TurnContext) -> _SecondPlan:
        return cls(
            patch=copy.deepcopy(ctx.state_patch),
            proposals=list(ctx.state_proposals),
            events=len(ctx.events),
            routine=ctx.routine,
        )

    def restore(self, ctx: TurnContext) -> None:
        _restore_second_plan(ctx, self.patch, self.proposals, self.events)
        ctx.routine = self.routine  # o rutină a planului care nu servește nu se randează


def _restore_second_plan(
    ctx: TurnContext, patch: dict[str, Any], proposals: list[Any], events: int
) -> None:
    """Anularea scrierilor celui de-al doilea plan (nu o scriere nouă de stare): `state_patch` și
    propunerile revin la valoarea de după mutație, iar evenimentele lui se scot, în afara celor ale
    kernelului. Explicit, câmp cu câmp, ca poarta I3 să-l vadă."""
    ctx.state_patch.clear()
    ctx.state_patch.update(copy.deepcopy(patch))
    ctx.state_proposals[:] = list(proposals)
    kept = [e for e in ctx.events[events:] if e.type in KERNEL_EXECUTOR_EVENTS]
    ctx.events[events:] = kept


async def _serve_mutation_then(
    ctx: TurnContext,
    deps: PipelineDeps,
    planned: PlannedTurn,
    outcome: GateOutcome,
    policy_for: PolicyFor | None,
) -> bool:
    """Două planuri, mutația întâi (plannerul le ordonează). Al doilea se sare dacă depinde de o
    mutație picată; fraza mutației vine ÎNAINTEA răspunsului lui. După o mutație reușită nimic nu
    mai cade pe v1: un al doilea plan refuzat sau picat lasă răspunsul mutației, iar ce scrisese el
    în context (sesiunea de căutare, propunerile, evenimentele) se anulează."""
    first, second = planned.plans
    _require_cart_sentences(ctx)
    mutation = await _cart(ctx, deps, first)
    served = False
    saved = _SecondPlan.take(ctx)
    if not (second.depends_on == 0 and not mutation.added):
        try:
            served = bool(
                await _run_plan(
                    ctx,
                    deps,
                    second,
                    outcome,
                    policy_for,
                    exclude_shown=1 in planned.excludes_shown,
                )
            )
        except NoSentence:
            served = False
        except Exception as e:  # noqa: BLE001 — o mutație picată n-a scris nimic: v1 poate relua
            if not mutation.added:
                raise
            ctx.emit("kernel_second_plan_failed", error=type(e).__name__)
            served = False
    no_results = (
        second.executor == "search" and ctx.retrieval is not None and not ctx.retrieval.products
    )
    unserved = frozenset() if served and ctx.reply is not None else frozenset({1})
    disclosure = "" if no_results else _disclosure_text(ctx, planned, unserved)
    if served and ctx.reply is not None:
        # ordinea citită de client: mutația, apoi dezvăluirile, apoi răspunsul celui de-al doilea
        _prefix(ctx, disclosure)
        _prefix(ctx, _cart_lead(ctx, mutation))
        question = _confirmation(outcome)
        if question is not None and second.executor != "ask":
            _confirm(ctx, question)
    else:
        saved.restore(ctx)
        _set_cart_reply(ctx, mutation)
        _append(ctx, disclosure)
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
    mutating: bool = False,
    exclude_shown: bool = False,
) -> bool | None:
    """`exclude_shown` (NX-370) = planul e o căutare născută dintr-un act `show_more`
    (`PlannedTurn.excludes_shown`): prima pagină sare produsele de pe ecran."""
    kind, ids = plan.executor, list(plan.product_ids)
    if kind == "reply_only":
        # Doar răspunsul unei MUTAȚII oprite de poartă fără întrebare (orice motiv: epuizat, țintă
        # neexactă, fără opțiuni, fără șablon, întrebare deja pusă, ținta care numește o
        # proprietate). Căderea pe v1 ar da turul unei bucle care are `cart_add`, exact pe turul
        # pe care poarta l-a oprit (I10/I11, recenzia D2). NX-383: pe orice verdict, nu doar
        # `must_ask`: regula 0 a porții scoate coșul cu verdictul `act`. Restul lui `reply_only`
        # (din `chitchat`) rămâne pe v1, fără unelte de mutație (`ToolRun.mutations_allowed`).
        if not mutating:
            return None
        reason = outcome.decision.reason
        code = reason if reason in _REFUSED_MUTATION else "mutation_not_exact"
        ctx.set_reply(_required_sentence(ctx, code), cacheable=False)
        return True
    if kind == "cart":
        return await _serve_cart(ctx, deps, plan) if ids else False
    if kind == "search":
        return await _search(ctx, deps, plan, outcome, exclude_shown)
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
    if kind in DELEGATED_TOOLS:
        return await _delegate(ctx, deps, DELEGATED_TOOLS[kind])
    if kind == "bundle":
        return await _bundle(ctx, deps, plan)
    return None


def _routine_moment(ctx: TurnContext, args: Any) -> str | None:
    """Momentul rutinei (recenzia D3): o valoare din `routine_steps.time_markers` al pachetului
    (`am`/`pm`) printre nevoile sau preferințele planului. O rutină de seară nu mai cere protecția
    solară, ca pe v1, unde modelul pune `moment`. Fără potrivire: rutina de zi întreagă."""
    markers = getattr(getattr(_pack(ctx), "routine_steps", None), "time_markers", None) or {}
    values = [
        *(args.concerns or []),
        *(args.features or []),
        *(v for vals in (args.prefer or {}).values() for v in vals),
    ]
    return next((str(v) for v in values if str(v) in markers), None)


async def _bundle(ctx: TurnContext, deps: PipelineDeps, plan: TurnPlan) -> bool | None:
    """Rutina planificată (D3). Fără familie, fără argumente sau cu altă unealtă de `bundle` în
    pachet ⇒ `None` (calea de azi); o rutină necompusă ⇒ `False` (calea v1 o spune)."""
    from src.tools.routine_tools import RoutineArgs  # noqa: PLC0415 — ciclul unelte ↔ agent

    args = plan.search_args
    tools = set((getattr(_pack(ctx), "bundle_executors", None) or {}).values())
    if plan.family is None or args is None or tools != {_ROUTINE_TOOL}:
        return None
    needs = [str(v) for v in [*(args.concerns or []), *(args.features or [])] if v]
    routine = RoutineArgs(
        family=plan.family,
        concerns=needs,
        budget_max=args.price_max,
        anchor_id=plan.product_ids[0] if plan.product_ids else None,
        moment=_routine_moment(ctx, args),
    )
    run = ToolRun(ctx, deps)
    result = await run.execute_planned_routine(routine, prefer=dict(args.prefer or {}) or None)
    if not result.ok or not run.retrieved:
        return False
    await _compose(ctx, deps, run, list(run.retrieved), page=False)
    return ctx.reply is not None


async def execute_read_plans(
    ctx: TurnContext,
    deps: PipelineDeps,
    planned: PlannedTurn,
    outcome: GateOutcome,
    policy_for: PolicyFor | None = None,
    mutating: bool = False,
    dropped_request: bool = False,
) -> bool | None:
    """Rulează planul turului. `mutating` = interpretarea turului a cerut o scriere (coșul);
    `dropped_request` = poarta a scos și o cerere care nu scrie (NX-383: dezvăluirea ei rămâne).
    `None` = niciun executor legat (calea v1, `dark`), `False` =
    executorul a refuzat, `True` = a servit. `NoSentence` urcă la orchestrator. `policy_for` =
    politica de răspuns a orchestratorului, judecată pe produsele unei comparații (I12)."""
    plans = planned.plans
    if len(plans) == 2 and plans[0].executor == "cart" and plans[0].product_ids:
        return await _serve_mutation_then(ctx, deps, planned, outcome, policy_for)
    if len(plans) != 1:
        return None  # două planuri fără mutație: calea v1
    plan = plans[0]
    if plan.executor not in READ_EXECUTORS:
        return None
    verdict = await _run_plan(
        ctx, deps, plan, outcome, policy_for, mutating, exclude_shown=0 in planned.excludes_shown
    )
    if not verdict:
        return verdict
    if plan.executor == "reply_only":
        # NX-383: refuzul mutației e răspunsul turului. Dezvăluirea `invalid_target` rămâne doar
        # dacă poarta a scos și o cerere care nu scrie; altfel ar spune refuzul a doua oară.
        if dropped_request:
            _prefix(ctx, _disclosure_text(ctx, planned))
        return verdict
    no_results = (
        plan.executor == "search" and ctx.retrieval is not None and not ctx.retrieval.products
    )
    if plan.executor == "cart":
        # fraza mutației e prima; dezvăluirile după ea
        _append(ctx, _disclosure_text(ctx, planned))
    elif not no_results:
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
