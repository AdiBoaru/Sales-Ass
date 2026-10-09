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
from src.conversation.ambiguity_gate import MAX_ACT_BOTH
from src.conversation.answer_policy import dimension_label
from src.conversation.references import name_in_results
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
        "kernel_name_resolved",
        "kernel_compare_single",
        "delegate_loop_failed",
        "kernel_second_plan_failed",
        "kernel_i10_blocked",
        "detail_question",  # NX-381: măsurătoarea răspunsului rămâne și pe un tur căzut
        "composer",  # NX-382: idem pentru compozitor
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
    ctx: TurnContext,
    planned: PlannedTurn,
    skip: frozenset[int] = frozenset(),
    drop: frozenset[str] = frozenset(),
) -> str:
    """Frazele dezvăluirilor planului, în ordine, fiecare o singură dată. Fail-open. `skip` =
    planurile care n-au servit: dezvăluirile lor nu descriu răspunsul (NX-374, recenzia: pe un coș
    picat, căutarea dependentă nu rulează, deci „n-am ținut cont la alegere" ar fi fals)."""
    pack = getattr(ctx.business, "domain_pack", None)
    out: list[str] = []
    for index, code in planned.disclosures:
        if code in _REQUIRED or index in skip or code in drop:
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
    ctx: TurnContext,
    deps: PipelineDeps,
    ids: list[str],
    policy_for: PolicyFor | None,
    *,
    found: tuple[str, ...] = (),
) -> bool | None:
    """≥ 2 ținte ⇒ comparația lor. O țintă (actul are una singură; orchestratorul lasă `dark` un act
    cu mai multe ținte din care una s-a pierdut, `_target_lost`):

    NX-386 (`kernel.v7.1`, turele wide-2026-10-07): partenerul din graf ieșea un prosop sau un
    produs pe care clientul nu-l văzuse. Acum, în ordine: (1) restul setului în care stă ținta
    (ecranul, apoi seturile de mai devreme, după starea porții), cel mult doi; (2) un partener din
    graf doar dacă e substitut curatoriat sau de ACELAȘI tip; (3) altfel detaliul țintei, fără
    partener inventat. Chip-ul nostru «compară-l cu un produs similar» rămâne pe scurtătura lui
    (NX-319)."""
    if len(ids) >= 2:
        withhold = _withhold(ctx, policy_for, found)
        return await det.serve_comparison(ctx, deps, ids, withhold=withhold)
    if not ids:
        return None
    partners = _set_partners(ctx, ids[0])
    if not partners and get_settings().compare_with_similar_enabled:
        async with deps.db("similar_candidates") as conn:
            candidates = await det.similar_candidates(conn, ctx.business.id, ids[0])
        kin = [c for c in candidates if c.get("is_substitute") or c.get("same_type")]
        partner = det.pick_similar_partner(kin)
        ctx.emit("kernel_similar_partner", found=partner is not None, n=len(kin))
        partners = (partner,) if partner is not None else ()
    if partners:
        served = await det.serve_comparison(
            ctx, deps, [ids[0], *partners], withhold=_withhold(ctx, policy_for, partners)
        )
        if served:
            return served
        # recenzia: o comparație refuzată (raft amestecat, siguranța) cade pe detaliul țintei
    ctx.emit("kernel_compare_single", served="detail")
    await det.serve_details(ctx, deps, ids[0])
    return ctx.reply is not None


def _set_partners(ctx: TurnContext, target: str) -> tuple[str, ...]:
    """NX-386: restul primului set (ecranul, apoi seturile de mai devreme, după starea porții) care
    conține ținta, cel mult doi produse. Gol când ținta nu stă într-un set de cel puțin două."""
    view = getattr(ctx, "kernel_view", None)
    refs = getattr(getattr(view, "gate_state", None), "references", None)
    if refs is None:
        return ()
    sets = [list(refs.displayed_products), *(list(s) for s in refs.recent_sets)]
    for items in sets:
        found = [d.product_id for d in items if d.product_id]
        if target in found and len(found) >= 2:
            return tuple(p for p in dict.fromkeys(found) if p != target)[:2]
    return ()


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
        text = _confirmation(outcome) or await _composed_no_results(ctx, deps, plan)
        text = text or _required_sentence(ctx, "no_results")
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
    """Întrebarea porții. NX-382 faza 2: o formulează compozitorul (întrebarea porții și opțiunile
    ei sunt obligația `ask`), cu candidații ca carduri; pe orice eșec, șablonul pachetului. Memoria
    întrebării (I11) se scrie după `pending_question`, nu după text, deci formularea nu o atinge."""
    question = outcome.decision.question
    if not question:
        return False
    ids = list(plan.product_ids)
    rows: list[dict[str, Any]] = []
    if ids:
        async with deps.db("kernel_ask_candidates") as conn:
            rows = await get_products_by_ids(conn, ctx.business.id, ids, limit=len(ids))
        rows = SafetyPolicy.for_turn(ctx).gate(ctx, rows, purpose="kernel_ask")[0]
    text = question
    if get_settings().composer_ask_enabled:
        from src.agent import composer  # noqa: PLC0415 — ciclul agent ↔ executori
        from src.worker.context import conversation_transcript  # noqa: PLC0415

        inp = composer.ComposeInput(
            task="ask",
            products=rows,
            obligations=[composer.Obligation("ask", {"question_to_ask": question})],
        )
        history = conversation_transcript(ctx.history, consumer="composer")
        composed, _reason = await composer.compose(ctx, deps, inp, history=history)
        if composed is not None:
            text = composed.served
    ctx.set_clarify(text, field="kernel", resume_route="sales")
    if rows and ctx.reply is not None:
        ctx.reply.products = _card_products(rows, n=len(rows))
    return True


async def _composed_no_results(ctx: TurnContext, deps: PipelineDeps, plan: TurnPlan) -> str | None:
    """NX-382 faza 2: „n-am găsit" scris de compozitor, din ce s-a căutat (textul, raftul,
    nevoile, bugetul planului), ca să spună concret ce lipsește și încotro s-o ia. `None` = fraza
    pachetului (flag stins sau model picat)."""
    if not get_settings().composer_no_results_enabled or plan.search_args is None:
        return None
    from src.agent import composer  # noqa: PLC0415 — ciclul agent ↔ executori
    from src.worker.context import conversation_transcript  # noqa: PLC0415

    args = plan.search_args
    # cuvintele cererii, nu cheile de catalog (un cod de raft sau de nevoie ar ajunge la client)
    searched = {
        k: v
        for k, v in {
            "words": args.query,
            "brand": args.brand,
            "price_max": args.price_max,
            "cheaper_half_only": True if args.price_band else None,
            "excluded": sorted({str(x) for vals in (args.exclude or {}).values() for x in vals}),
        }.items()
        if v not in (None, "", [])
    }
    inp = composer.ComposeInput(
        task="no_results", obligations=[composer.Obligation("nothing_found", searched)]
    )
    history = conversation_transcript(ctx.history, consumer="composer")
    composed, _reason = await composer.compose(ctx, deps, inp, history=history)
    return composed.served if composed is not None else None


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
    if get_settings().composer_cart_enabled and await _compose_cart(ctx, deps, mutation):
        return True
    # cross-sell doar pe o mutație reușită integral: un eșec parțial se spune (recenzia D2)
    if mutation.added and not mutation.failed and await _cross_sell(ctx, deps, mutation):
        return True
    _set_cart_reply(ctx, mutation)
    return True


async def _cart_complements(
    ctx: TurnContext, deps: PipelineDeps, mutation: _Mutation
) -> list[dict[str, Any]]:
    """Complementarele produsului adăugat EFECTIV (aceleași condiții și aceeași citire ca
    cross-sell-ul v1, `planner.maybe_cross_sell`: graful de relații, fără ce e deja în coș, prin
    poarta de siguranță). Doar CANDIDAȚI: pe care îi arată, sau niciunul, decide compozitorul
    (wide-2026-10-07: 22 de ture cu complemente fără legătură, 8 fără nicio potrivire)."""
    from src.agent.planner import (  # noqa: PLC0415 — ciclul agent
        _cart_followup_products,
        _current_cart_lines,
    )

    added = mutation.run.added_product
    if (
        not mutation.added
        or mutation.failed
        or added is None
        or mutation.run.generated_links
        or not get_settings().cross_sell_enabled
    ):
        return []
    if get_settings().conversation_cart_enabled:
        lines = await _current_cart_lines(ctx, deps, mutation.run, fetch=False)
    else:
        lines = list(ctx.state.cart or []) + list(ctx.state_patch.get("cart") or [])
    exclude = [str(line.get("product_id")) for line in lines if line.get("product_id")]
    exclude += [str(p.get("id")) for p in mutation.added]
    rows, _label = await _cart_followup_products(ctx, deps, str(added["id"]), exclude)
    return SafetyPolicy.for_turn(ctx).gate(ctx, list(rows), purpose="cross_sell")[0]


async def _compose_cart(ctx: TurnContext, deps: PipelineDeps, mutation: _Mutation) -> bool:
    """NX-382 faza 2c: confirmarea coșului o scrie compozitorul (obligația `cart_change`: ce s-a
    adăugat, pe numele scurt, și câte au picat), iar complementarele sunt candidați pe care modelul
    îi arată doar dacă chiar completează ce a luat clientul. Mutația a rulat deja: orice eșec al
    modelului lasă răspunsul de azi (`False`), niciodată o a doua scriere.

    Recenzia 2c: ORICE excepție de după mutație (compunerea, cardurile, aplatizarea) întoarce
    `False`, ca cross-sell-ul de azi: o excepție care urcă la orchestrator ar face turul să cadă pe
    v1, iar instantaneul ar anula coșul din `state_patch` și confirmarea."""
    try:
        return await _compose_cart_reply(ctx, deps, mutation)
    except Exception as e:  # noqa: BLE001 — după mutație: răspunsul coșului de azi (P6)
        ctx.emit("composer", task="cart", outcome="cart_reply_failed", error=type(e).__name__)
        return False


async def _compose_cart_reply(ctx: TurnContext, deps: PipelineDeps, mutation: _Mutation) -> bool:
    from src.agent import composer  # noqa: PLC0415 — ciclul agent ↔ executori
    from src.worker import compose as wc  # noqa: PLC0415
    from src.worker.context import conversation_transcript  # noqa: PLC0415

    try:
        complements = await _cart_complements(ctx, deps, mutation)
    except Exception as e:  # noqa: BLE001 — complementarele sunt opționale; confirmarea rămâne
        ctx.emit("composer", task="cart", outcome="complements_failed", error=type(e).__name__)
        complements = []
    change = {
        "added": [display_name(str(p.get("name") or "")) for p in mutation.added],
        "failed": mutation.failed,
    }
    inp = composer.ComposeInput(
        task="cart",
        products=complements,
        obligations=[composer.Obligation("cart_change", change)],
    )
    history = conversation_transcript(ctx.history, consumer="composer")
    composed, _reason = await composer.compose(ctx, deps, inp, history=history)
    if composed is None:
        return False
    rich = composer.rich_reply(ctx, composed, complements)
    if rich is not None:
        chosen = {it.product_id for it in rich.items}
        # doar complementarele ALESE sunt ale turului; un coș nu închide sesiunea de căutare
        ctx.retrieval = RetrievalResult(
            products=[p for p in complements if str(p.get("id")) in chosen],
            source="composer_cart",
            catalog_read=False,
        )
        ctx.set_rich_reply(
            rich, text=wc.flatten(rich, ctx.language), products=wc.card_products(rich.items)
        )
        ctx.reply.cacheable = False
        ctx.emit("cross_sell", added=str(mutation.added[0].get("id")), n=len(rich.items))
        return True
    products = list(mutation.added)
    ctx.retrieval = RetrievalResult(products=products, source="kernel_cart", catalog_read=False)
    ctx.set_reply(
        composed.served,
        products=_card_products(products, n=len(products)) or None,
        cacheable=False,
    )
    return True


async def _compose_chitchat(ctx: TurnContext, deps: PipelineDeps) -> bool | None:
    """NX-382 faza 2c: un salut, o mulțumire sau un rămas-bun (un tur doar `chitchat`) primește
    răspunsul compozitorului, cu istoricul în față, fără unelte (pe v1, «ok pa» ajunsese la o
    abonare la stoc, NX-383). `None` = calea v1 (fără unelte de mutație), ca azi. Turul n-a citit
    catalogul: e o paranteză (NX-326), sesiunea de căutare rămâne."""
    from src.agent import composer  # noqa: PLC0415 — ciclul agent ↔ executori
    from src.worker.context import conversation_transcript  # noqa: PLC0415

    history = conversation_transcript(ctx.history, consumer="composer")
    composed, _reason = await composer.compose(
        ctx, deps, composer.ComposeInput(task="chitchat"), history=history
    )
    if composed is None:
        return None
    ctx.retrieval = RetrievalResult(products=[], source="composer_chitchat", catalog_read=False)
    ctx.set_reply(composed.served, cacheable=False)
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


async def _answer_detail(
    ctx: TurnContext, deps: PipelineDeps, product_id: str, question: str
) -> bool:
    """NX-381: întrebarea clientului despre UN produs primește răspunsul compus din fișă
    (`detail_answer`), cu cardul produsului dedesubt. Produsul se citește și trece prin poarta de
    siguranță O SINGURĂ dată, ca în `serve_details` (I1: id-ul vine din plan); orice eșec servește
    fișa de azi pe ACELAȘI produs (`gated`), deci nicio a doua citire și niciun eveniment de
    siguranță dublat. Pe un claim medical respins, fișa vine cu trimiterea la medic sau farmacist
    înainte: întrebarea era despre sănătate, iar fișa singură ar ocoli-o."""
    from src.agent import detail_answer  # noqa: PLC0415 — ciclul agent ↔ executori
    from src.safety.messages import refer_sentence  # noqa: PLC0415
    from src.worker.context import conversation_transcript  # noqa: PLC0415

    async with deps.db("detail_question_product") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, [product_id], limit=1)
    products = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose="detail_intent")[0]
    if products:
        history = conversation_transcript(ctx.history, consumer="detail_question")
        result = await detail_answer.answer_question(ctx, deps, products[0], question, history)
        if result.answer is not None:
            ctx.retrieval = RetrievalResult(products=products, source="detail_question")
            ctx.set_reply(result.answer, products=_card_products(products, n=1), cacheable=False)
            copy = det._detail_copy(ctx.language)
            ctx.reply.suggestions = [copy["review_chip"], copy["link_chip"], copy["compare_chip"]]
            return True
        if result.reason == "medical_claim":
            refer = refer_sentence(ctx.language)
            await det.serve_details(ctx, deps, product_id, lead=lambda _p: refer, gated=products)
            return ctx.reply is not None
    await det.serve_details(ctx, deps, product_id, gated=products)
    return ctx.reply is not None


#: NX-382: pașii următori oferiți sub un detaliu; modelul îi formulează în limba clientului.
_DETAIL_MOVES = (
    "see what customers say about it",
    "get the link to this product",
    "compare it with a similar product",
)


async def _compose_detail(
    ctx: TurnContext, deps: PipelineDeps, product_id: str, question: str | None
) -> bool:
    """NX-382 faza 1: un `detail` pe UN produs, scris de compozitorul unic (cu sau fără
    întrebare). Produsul se citește și trece prin poarta de siguranță O SINGURĂ dată (I1: id-ul vine
    din plan). Orice eșec al modelului servește fișa de azi pe ACELAȘI produs (`gated`); pe un
    claim medical respins, cu trimiterea la medic sau farmacist înainte."""
    from src.agent import composer  # noqa: PLC0415 — ciclul agent ↔ executori
    from src.safety.messages import refer_sentence  # noqa: PLC0415
    from src.worker.context import conversation_transcript  # noqa: PLC0415

    async with deps.db("composer_detail_product") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, [product_id], limit=1)
    products = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose="detail_intent")[0]
    if products:
        inp = composer.ComposeInput(
            task="detail", products=products, question=question, offered_moves=_DETAIL_MOVES
        )
        history = conversation_transcript(ctx.history, consumer="composer")
        composed, reason = await composer.compose(ctx, deps, inp, history=history)
        if composed is not None:
            ctx.retrieval = RetrievalResult(products=products, source="composer_detail")
            ctx.set_reply(composed.served, products=_card_products(products, n=1), cacheable=False)
            ctx.reply.suggestions = list(composed.suggestions)
            return True
        # trimiterea la medic doar când clientul a ÎNTREBAT ceva (recenzia NX-382: fișa unui
        # produs pentru acnee nu e o întrebare de sănătate)
        if reason == "medical_claim" and question:
            refer = refer_sentence(ctx.language)
            await det.serve_details(ctx, deps, product_id, lead=lambda _p: refer, gated=products)
            return ctx.reply is not None
    await det.serve_details(ctx, deps, product_id, gated=products)
    return ctx.reply is not None


async def _compose_store_info(ctx: TurnContext, deps: PipelineDeps) -> bool:
    """NX-382 faza 2: o întrebare despre magazin, scrisă de compozitor din regulile ACTIVE (aduse
    de cod, întregi, ca unealta `faq_lookup`). `False` = bucla restrânsă de azi (nicio regulă,
    citire picată, model picat sau răspuns respins). Turul n-a citit catalogul (paranteza
    NX-326)."""
    from src.agent import composer  # noqa: PLC0415 — ciclul agent ↔ executori
    from src.tools.faq_tools import load_rules  # noqa: PLC0415
    from src.worker.context import conversation_transcript  # noqa: PLC0415

    try:
        rows = await load_rules(ctx, deps)
    except Exception as e:  # noqa: BLE001 — P6: bucla de azi rămâne răspunsul
        ctx.emit("composer", task="store_info", outcome="rules_failed", error=type(e).__name__)
        return False
    if not rows:
        return False
    rules = [f"{r['question'].strip()} -> {r['answer'].strip()}" for r in rows]
    inp = composer.ComposeInput(task="store_info", store_rules=rules)
    history = conversation_transcript(ctx.history, consumer="composer")
    composed, _reason = await composer.compose(ctx, deps, inp, history=history)
    if composed is None:
        return False
    ctx.retrieval = RetrievalResult(
        products=[],
        source="composer_store_info",
        catalog_read=False,
        read_beyond_catalog=True,
        store_only=True,
        store_read_ok=True,
    )
    ctx.set_reply(composed.served, cacheable=False)
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
    social: bool = False,
) -> bool | None:
    """`exclude_shown` (NX-370) = planul e o căutare născută dintr-un act `show_more`
    (`PlannedTurn.excludes_shown`): prima pagină sare produsele de pe ecran. `social` (NX-382
    faza 2c) = turul e doar `chitchat`: `reply_only` îl răspunde compozitorul."""
    kind, ids = plan.executor, list(plan.product_ids)
    if kind == "reply_only" and social and not mutating:
        if get_settings().composer_chitchat_enabled:
            return await _compose_chitchat(ctx, deps)
        return None
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
            if get_settings().composer_detail_enabled:
                return await _compose_detail(ctx, deps, ids[0], plan.question)
            if plan.question and get_settings().detail_question_answer_enabled:
                return await _answer_detail(ctx, deps, ids[0], plan.question)
            await det.serve_details(ctx, deps, ids[0])
            return ctx.reply is not None
        return await _compare(ctx, deps, ids, policy_for) if len(ids) >= 2 else False
    if kind == "compare":
        return await _compare(ctx, deps, ids, policy_for)
    if kind == "faq" and get_settings().composer_store_info_enabled:
        if await _compose_store_info(ctx, deps):
            return True
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
        steps=list(plan.steps) or None,
    )
    run = ToolRun(ctx, deps)
    result = await run.execute_planned_routine(routine, prefer=dict(args.prefer or {}) or None)
    if not result.ok or not run.retrieved:
        return False
    await _compose(ctx, deps, run, list(run.retrieved), page=False)
    return ctx.reply is not None


async def _serve_named(
    ctx: TurnContext,
    deps: PipelineDeps,
    plan: TurnPlan,
    policy_for: PolicyFor | None,
) -> bool | None:
    """NX-386 (`kernel.v7.1`): actul cerut pe un produs NUMIT pe care resolverul nu l-a găsit scris
    întreg. Fiecare nume se caută (căutarea planificată, cu poarta de siguranță a uneltei), iar
    rezultatele se confruntă cu numele pe treptele precise ale resolverului (`name_in_results`).
    Fiecare nume pe 1..`MAX_ACT_BOTH` produse ⇒ actul (`detail`, `link`, `compare`) pe ele, lângă
    țintele deja `exact`; altfel `None`, iar planul rulează ca azi (căutarea primului nume, cu
    dezvăluirea). Căutările de rezolvare nu lasă sesiune și nici evenimente: nu ele au fost
    arătate clientului. Turul real `w4_alergie_sarcina_nu#1`: «beauty of joseon relief sun contine
    alcool?» primea „Nu am găsit exact produsul" deasupra exact acelui produs."""
    from src.tools import catalog_tools  # noqa: PLC0415 — ciclul unelte ↔ agent

    args = plan.search_args
    if args is None or plan.then is None or not plan.names:
        return None
    ids = list(dict.fromkeys(plan.product_ids))
    saved = _SecondPlan.take(ctx)
    try:
        # un `find` (NX-375) devine detaliu doar pe UN produs; o citire, pe cel mult `MAX_ACT_BOTH`
        cap = 1 if plan.then == "find" else MAX_ACT_BOTH
        for n, name in enumerate(plan.names):
            update = {"query": name, "product_name": name}
            current = args if n == 0 else args.model_copy(update=update)
            result = await catalog_tools.run_planned_search(ctx, deps, current)
            hits = name_in_results(name, result.products or [], ctx.language)
            if not hits or len(hits) > cap:
                return None
            ids += [h for h in hits if h not in ids]
    finally:
        saved.restore(ctx)
    ctx.emit("kernel_name_resolved", act=plan.then, names=len(plan.names), ids=len(ids))
    if plan.then == "link":
        await det._handle_link_intent(ctx, deps, ids=ids)
        return True
    if plan.then in ("detail", "find") and len(ids) == 1:
        if get_settings().composer_detail_enabled:
            # NX-382: ca orice detaliu pe un produs, îl scrie compozitorul (cu sau fără întrebare)
            return await _compose_detail(ctx, deps, ids[0], plan.question)
        if plan.question and get_settings().detail_question_answer_enabled:
            # NX-381 pe actul promovat: «beauty of joseon relief sun conține alcool?» primește
            # răspunsul la întrebare, nu fișa standard
            return await _answer_detail(ctx, deps, ids[0], plan.question)
        await det.serve_details(ctx, deps, ids[0])
        return ctx.reply is not None
    # `detail` pe mai mulți candidați = comparația lor (ca `act_both` pe o citire). Recenzia (I12):
    # produsele găsite prin căutare intră la politica de răspuns ca parteneri, altfel politica nu
    # le vedea și verdictul trecea pe o dimensiune necunoscută.
    found = tuple(i for i in ids if i not in plan.product_ids)
    return await _compare(ctx, deps, ids, policy_for, found=found)


async def execute_read_plans(
    ctx: TurnContext,
    deps: PipelineDeps,
    planned: PlannedTurn,
    outcome: GateOutcome,
    policy_for: PolicyFor | None = None,
    mutating: bool = False,
    dropped_request: bool = False,
    *,
    social: bool = False,
) -> bool | None:
    """Rulează planul turului. `mutating` = interpretarea turului a cerut o scriere (coșul);
    `dropped_request` = poarta a scos și o cerere care nu scrie (NX-383: dezvăluirea ei rămâne);
    `social` = turul e doar `chitchat` (NX-382 faza 2c: îl răspunde compozitorul).
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
    if (
        plan.executor == "reply_only"
        and not mutating
        and any(code == "no_target" for _, code in planned.disclosures)
    ):
        # NX-387: o citire pe o țintă pe care n-o are nimic (un ordinal fără listă pe ecran) nu
        # cade pe bucla v1, care alegea singură un produs («Da, primul conține ulei de cocos»
        # despre un produs pe care clientul nu-l văzuse): fraza pachetului o spune.
        ctx.set_reply(_required_sentence(ctx, "no_target"), cacheable=False)
        return True
    if plan.executor == "search" and plan.then is not None:
        named = await _serve_named(ctx, deps, plan, policy_for)
        if named is not None:
            if named:
                # produsul numit E răspunsul: „nu am găsit exact” ar spune contrariul
                _prefix(ctx, _disclosure_text(ctx, planned, drop=frozenset({"not_exact_match"})))
                question = _confirmation(outcome)
                if question is not None:
                    _confirm(ctx, question)
            return named
    verdict = await _run_plan(
        ctx,
        deps,
        plan,
        outcome,
        policy_for,
        mutating,
        exclude_shown=0 in planned.excludes_shown,
        social=social,
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
