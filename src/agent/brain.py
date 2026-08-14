"""NX-239 — MainBrain: UN SINGUR writer semantic pentru turnurile nontriviale.

Bucla de tool-calling și planul structurat final sunt ale ACELUIAȘI model, în ACEEAȘI conversație
(`llm.run_tool_loop_structured`): nu mai există relay-ul triage-writer → tool-loop-writer → rich
writer → AnswerPlan-writer care pierde nuanța. În jurul brain-ului stă control plane-ul
determinist: obligațiile extrase din cod, validatorul V2 (evidence/tenant/hard constraints),
poarta de clarificare NX-235, criticul selectiv codes-only și UN singur repair bounded — apoi
fallback determinist non-gol (P6: niciodată tăcere).

Retrievalul trece EXCLUSIV prin portul NX-238: `selector.select_provider` decide providerul
(NOT-READY/NO-GO → `CurrentLiveRetrievalAdapter`, paritate prin construcție); brain-ul și promptul
NU știu și NU aleg providerul.

Model/prompt/tool-schema sunt VERSIONATE: hash-urile intră în evenimente (trace attrs), nu ca
labels high-cardinality. Modelul de runtime vine din settings — nu e hardcodat aici și nu se
schimbă decât prin eval blind (NX-246). Totul rulează DOAR sub `single_brain_enabled` (dark).
"""

from __future__ import annotations

import hashlib
import json
import logging
from time import perf_counter
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from src.agent.answer_plan import (
    AnswerPlanContext,
    AnswerPlanV2,
    validate_answer_plan_v2,
)
from src.agent.answer_plan_runtime import (
    ANSWER_PLAN_V2_SCHEMA,
    build_answer_plan_context,
    run_semantic_critic,
    safe_fallback,
    validate_revised_draft,
)
from src.agent.brain_models import BrainInput
from src.agent.conversation_quality import evaluate_reply
from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.agent.tool_executor import ToolRun, _safe_tool_args
from src.config import get_settings
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import active_needs
from src.models import TurnContext
from src.retrieval.selector import build_port, select_provider
from src.worker.context import build_brain_input

if TYPE_CHECKING:
    from src.agent.prompt_builder import PromptInputs
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

#: Versiunea promptului MainBrain — se schimbă la ORICE modificare de instrucțiuni.
BRAIN_PROMPT_VERSION = "main_brain.v1"

#: Bugetul de repair: UN singur repair bounded al aceluiași brain, apoi fallback determinist.
MAX_REPAIRS = 1

#: Instrucțiunile V2, versionate (BRAIN_PROMPT_VERSION). Se ADAUGĂ system-ului generat din DB
#: (P9) — nu îl înlocuiesc. Fără nume de provider de retrieval, fără model hardcodat.
_PLAN_V2_SYSTEM = """
REGULI DE PLAN (AnswerPlanV2, schema_version=2):
- Răspunsul tău FINAL este planul structurat, nu proză liberă.
- `direct_answer`: răspunde ÎNTÂI la întrebarea principală, în limba clientului, scurt și natural,
  fără salut repetat, fără șabloane („Sigur!", „Desigur"), fără disclaimere repetitive.
- Acoperă TOATE obligațiile turului (lista din mesaj): un mesaj mixt primește toate
  sub-răspunsurile într-o ordine utilă.
- `claims`/`recommendations`: FIECARE afirmație factuală sau motiv de recomandare are evidence_ids
  din evidence-ul serverului. Motive CONCRETE (proprietate/review/fapt legat de nevoia clientului),
  zero motive generice. `need_ids` DOAR din nevoile date.
- UNKNOWN nu e MISMATCH: ce nu poți verifica intră în `unknowns`, nu se inventează.
- Constrângerile HARD nu se relaxează NICIODATĂ; `relaxations` poate conține doar preferințe soft.
- `clarification`: cel mult UNA, doar dacă răspunsul ar schimba material rezultatul; altfel
  răspunde best-effort și marchează assumption/unknown.
- Fără rezultate → `no_results` cu clasa ONESTĂ: no_match (am căutat, nu există),
  insufficient_data (nu putem verifica), dependency_unavailable (serviciu indisponibil).
- Nu confirma nicio acțiune care nu e în successful_action_ids; `action_intents` doar din
  registrul dat. Nu inventa produse, prețuri, linkuri sau stoc. Fără date personale în plan."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def brain_versions(system: str, tools: list[dict[str, Any]], model: str | None) -> dict[str, Any]:
    """Amprentele versionate ale rulării (trace attrs, nu labels): prompt/tool-schema/model."""
    return {
        "prompt_version": BRAIN_PROMPT_VERSION,
        "prompt_hash": _sha(system),
        "tool_schema_hash": _sha(json.dumps(tools, sort_keys=True, ensure_ascii=False)),
        "plan_schema": ANSWER_PLAN_V2_SCHEMA["name"],
        "model": model,
    }


def _rounds_bucket(rounds: int) -> str:
    if rounds <= 0:
        return "0"
    if rounds == 1:
        return "1"
    if rounds == 2:
        return "2"
    return "3+"


def _spec_from_args(ctx: TurnContext, args: dict[str, Any]) -> RuntimeQuerySpec:
    """Argumentele modelului pentru `search_products` → `RuntimeQuerySpec` 1:1, fără semantică
    nouă: exact câmpurile pe care adapterul le traduce înapoi în argumentele tool-ului
    (`current_live._search_args`). Brain-ul nu alege providerul — doar descrie cererea."""
    query = str(args.get("query") or "")
    constraints: list[Constraint] = []
    price_max = args.get("price_max")
    if isinstance(price_max, (int, float)) and not isinstance(price_max, bool):
        constraints.append(Constraint(facet="price", op="lte", value=float(price_max)))
    brand = args.get("brand")
    if isinstance(brand, str) and brand.strip():
        constraints.append(Constraint(facet="brand", op="eq", value=brand.strip()))
    for concern in args.get("concerns") or []:
        if isinstance(concern, str) and concern.strip():
            constraints.append(
                Constraint(facet="concern", op="contains", value=concern.strip(), strength="soft")
            )
    sort_mode = args.get("sort_mode")
    return RuntimeQuerySpec(
        raw_query=query,
        normalized_query=query.lower(),
        search_text=query,
        category=args.get("category") if isinstance(args.get("category"), str) else None,
        constraints=tuple(constraints),
        sort=sort_mode if isinstance(sort_mode, str) else "relevance",
    )


def _allowed_action_intents() -> tuple[str, ...]:
    """Registrul FINIT de acțiuni (NX-236). Absent (import eșuat) → tuple gol = fail-closed."""
    try:
        from src.web.action_models import KIND_REGISTRY  # noqa: PLC0415 — evită cuplaj la import

        return tuple(sorted(KIND_REGISTRY))
    except Exception:  # noqa: BLE001 — fără registru nu permitem niciun intent
        return ()


def _known_need_ids(brain_input: BrainInput) -> tuple[str, ...]:
    """Vocabularul de nevoi pe care planul le poate referi: nevoile active + sloturile standard."""
    base = {"budget_max", "concerns", "suitable_for", "brand"}
    base.update(brain_input.active_need_keys)
    return tuple(sorted(base))


def _no_results_text(no_results: Any, locale: str) -> str:
    """Formulare DETERMINISTĂ per clasă (nu a modelului): onestă despre CE fel de „nu" e."""
    texts = {
        "no_match": {
            "ro": "Nu am găsit produse care să respecte toate criteriile cerute.",
            "en": "I could not find products matching all the requested criteria.",
            "hu": "Nem találtam a kért feltételeknek megfelelő terméket.",
        },
        "insufficient_data": {
            "ro": "Nu pot verifica acum toate criteriile cerute — nu am datele necesare.",
            "en": "I cannot verify all the requested criteria right now — data is missing.",
            "hu": "Most nem tudom ellenőrizni az összes feltételt — hiányzanak az adatok.",
        },
        "dependency_unavailable": {
            "ro": "Căutarea nu e disponibilă momentan. Te rog încearcă din nou puțin mai târziu.",
            "en": "Search is temporarily unavailable. Please try again shortly.",
            "hu": "A keresés átmenetileg nem érhető el. Kérlek, próbáld újra kicsit később.",
        },
    }
    by_class = texts.get(getattr(no_results, "reason_class", ""), texts["insufficient_data"])
    return by_class.get(locale) or by_class["ro"]


def render_plan_text(plan: AnswerPlanV2, locale: str, *, ask_clarification: bool) -> str:
    """Proza servită DIN plan, determinist: direct answer → no-results onest → disclosures →
    (cel mult) întrebarea de clarificare. NX-240 va înlocui asta cu proiecția ViewModel; până
    atunci textul rămâne validat de `validate_revised_draft` contra produselor retrievate."""
    parts: list[str] = []
    if plan.direct_answer.strip():
        parts.append(plan.direct_answer.strip())
    if plan.no_results is not None:
        parts.append(_no_results_text(plan.no_results, locale))
    parts.extend(d.strip() for d in plan.disclosures if d.strip())
    if ask_clarification and plan.clarification is not None:
        parts.append(plan.clarification.question.strip())
    return "\n\n".join(p for p in parts if p)


def _plan_products(plan: AnswerPlanV2, retrieved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cardurile compacte: DOAR produsele pe care planul le selectează, din setul retrievat."""
    by_id = {str(p.get("id") or p.get("product_id") or ""): p for p in retrieved}
    return [by_id[pid] for pid in (p.product_id for p in plan.selected_products) if pid in by_id][
        :6
    ]


async def _generate_plan(
    ctx: TurnContext,
    deps: PipelineDeps,
    *,
    system: str,
    user: str,
    tools: list[dict[str, Any]],
    execute: Any,
) -> tuple[dict[str, Any] | None, int]:
    """Bucla structurată; eșecul (JSON invalid/API) devine `(None, rounds)` — caller-ul repară."""
    try:
        raw, rounds = await deps.llm.run_tool_loop_structured(
            system, user, tools, execute, ANSWER_PLAN_V2_SCHEMA
        )
        return raw, rounds
    except Exception as e:  # noqa: BLE001 — model/JSON/API: vizibil, nu fatal (repair/fallback)
        log.warning("main_brain: bucla structurată a eșuat (%s)", type(e).__name__)
        return None, 0


async def _repair_plan(
    ctx: TurnContext,
    deps: PipelineDeps,
    *,
    system: str,
    user: str,
    failures: tuple[str, ...],
) -> dict[str, Any] | None:
    """UN repair bounded al ACELUIAȘI brain: aceleași instrucțiuni + codurile de validare."""
    feedback = f"\nPLANUL ANTERIOR A FOST INVALID. Corectează DOAR: {', '.join(failures)}"
    try:
        return await deps.llm.complete_schema(system, user + feedback, ANSWER_PLAN_V2_SCHEMA)
    except Exception as e:  # noqa: BLE001 — repair eșuat → fallback determinist
        log.warning("main_brain: repair eșuat (%s)", type(e).__name__)
        return None


def _validate(
    brain_input: BrainInput,
    raw: dict[str, Any] | None,
    context: AnswerPlanContext,
    required: tuple[tuple[str, str], ...],
) -> tuple[AnswerPlanV2 | None, tuple[str, ...]]:
    """Parsare + validare V2. Întoarce `(plan, failures)`; plan None = nici măcar parsabil."""
    if raw is None:
        return None, ("unknown_evidence",)
    try:
        plan = AnswerPlanV2.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        return None, ("unknown_evidence",)
    validation = validate_answer_plan_v2(
        plan,
        context,
        required_obligations=required,
        revoked_need_keys=brain_input.revoked_need_keys,
        hard_constraint_keys=brain_input.hard_need_keys,
        allowed_action_intents=_allowed_action_intents(),
    )
    return plan, validation.failures


def _emit_constraint_handling(
    ctx: TurnContext, brain_input: BrainInput, plan: AnswerPlanV2
) -> None:
    """`constraint_handling{strength,outcome}` — hard păstrat / soft relaxat (low-cardinality)."""
    for _key in brain_input.hard_need_keys:
        ctx.emit("constraint_handling", strength="hard", outcome="kept")
    for _key in plan.relaxations:
        ctx.emit("constraint_handling", strength="soft", outcome="relaxed")


def _clarification_allowed(ctx: TurnContext, plan: AnswerPlanV2) -> bool:
    """Poarta DETERMINISTĂ peste clarificarea propusă de brain (NX-235: anti-buclă + gain).
    Refuzul nu e tăcere: rămâne direct answer-ul best-effort din plan."""
    if plan.clarification is None:
        return False
    from src.worker.stages.clarify import clarification_gate  # noqa: PLC0415 — evită ciclu

    decision = clarification_gate(
        ctx,
        plan.clarification.target_need,
        reason="missing_required",
        total_candidates=None,
    )
    ctx.emit(
        "clarification_decision",
        reason=decision.reason,
        gain_bucket=decision.gain_bucket,
        source="main_brain",
    )
    return decision.ask


class _PortedExecute:
    """Callback-ul buclei: tool-urile normale trec prin `ToolRun.execute` (neatins); DOAR
    `search_products` e rutat prin portul NX-238 — selectorul a ales providerul, brain-ul nu-l
    cunoaște. Vederea pentru model și efectele de state vin din `last_result` (traseul live =
    paritate completă); candidatul, fără `last_result`, primește vederea derivată din bundle."""

    def __init__(self, ctx: TurnContext, deps: PipelineDeps, run: ToolRun, port: Any) -> None:
        self.ctx = ctx
        self.deps = deps
        self.run = run
        self.port = port
        self.bundles: list[Any] = []

    async def __call__(self, name: str, args: dict[str, Any]) -> str:
        if name != "search_products":
            return await self.run.execute(name, args)
        async with self.run._execution_lock:  # noqa: SLF001 — aceeași serializare ca ToolRun
            return await self._search(name, args)

    async def _search(self, name: str, args: dict[str, Any]) -> str:
        ctx, run = self.ctx, self.run
        started = perf_counter()
        spec = _spec_from_args(ctx, args)
        try:
            bundle = await self.port.retrieve(ctx.snapshot, spec, active_needs(ctx))
        except Exception as e:  # noqa: BLE001 — degradare VIZIBILĂ modelului, nu tăcere
            ctx.emit(
                "tool_call",
                name=name,
                ok=False,
                args=_safe_tool_args(name, args),
                n_results=0,
                latency_ms=round((perf_counter() - started) * 1000, 1),
                error=type(e).__name__,
            )
            return "Căutarea nu e disponibilă momentan (dependency_unavailable)."
        self.bundles.append(bundle)
        products = run._safe_products(list(bundle.products))  # noqa: SLF001 — backstop NX-173
        run.retrieved.extend(products)
        result = getattr(self.port, "last_result", None)
        if result is not None:
            if getattr(result, "relevance", None) is not None:
                run.search_relevance = result.relevance
            run.generated_links.update(getattr(result, "links", ()) or ())
            run.grounded_prices.update(getattr(result, "prices", ()) or ())
            state_patch = getattr(result, "state_patch", None)
            if state_patch:
                ctx.state_patch.update(state_patch)
            view = getattr(result, "llm_view", None)
        else:
            view = None
        if not view:
            from src.tools.catalog_tools import _brief  # noqa: PLC0415 — aceeași proiecție

            view = _brief(products, getattr(ctx.business, "domain_pack", None), ctx.language)
        ctx.emit(
            "tool_call",
            name=name,
            ok=bool(products) or not bundle.degradations,
            args=_safe_tool_args(name, args),
            n_results=len(products),
            latency_ms=round((perf_counter() - started) * 1000, 1),
            error=None,
        )
        return view or "(fără rezultat)"


async def run_main_brain(
    ctx: TurnContext,
    deps: PipelineDeps,
    *,
    run: ToolRun,
    inp: PromptInputs,
    tools: list[dict[str, Any]],
    system: str,
    user: str,
    query: str,
) -> None:
    """Turul MainBrain: plan structurat în aceeași buclă → validare → (un repair) → render →
    critic selectiv → reply. ÎNTOTDEAUNA setează un reply non-gol (P6) — fallback determinist la
    orice epuizare. Chemat de `agent_stage` DOAR sub `single_brain_enabled`."""
    settings = get_settings()
    brain_input = build_brain_input(ctx)  # snapshot/state SAFE — fără conn, fără frontend facts
    obligations = brain_input.obligations
    required = tuple((o.kind, o.key) for o in obligations)

    # NX-238: selectorul decide providerul; fără GO semnat → current live, întotdeauna.
    selection = select_provider(business_id=ctx.business.id, conversation_id=ctx.conversation_id)
    port = build_port(ctx, deps, selection)
    ctx.emit(
        "retrieval_gate",
        decision=selection.provider_version,
        reason=selection.reason,
        blocking_code=selection.blocking_code,
    )

    brain_system = f"{system}\n{_PLAN_V2_SYSTEM}"
    obligations_block = (
        "Obligațiile turului (acoperă-le pe TOATE în plan): "
        + "; ".join(f"{o.kind}:{o.key}" for o in obligations)
        + "\n"
        if obligations
        else ""
    )
    signals_block = "".join(f"[context {s.stage}] {s.text}\n" for s in brain_input.signals)
    needs_block = (
        "Nevoi cunoscute (need_ids valide): " + ", ".join(_known_need_ids(brain_input)) + "\n"
    )
    brain_user = f"{obligations_block}{needs_block}{signals_block}{user}"
    versions = brain_versions(brain_system, tools, getattr(deps.llm, "model_agent", None))

    execute = _PortedExecute(ctx, deps, run, port)
    raw, rounds = await _generate_plan(
        ctx, deps, system=brain_system, user=brain_user, tools=tools, execute=execute
    )
    ctx.emit("main_brain_tool_rounds_bucket", bucket=_rounds_bucket(rounds))

    context = build_answer_plan_context(
        business_id=ctx.business.id,
        locale=ctx.language,
        products=run.retrieved,
        successful_action_ids=run.successful_action_ids,
        known_need_ids=_known_need_ids(brain_input),
    )
    plan, failures = _validate(brain_input, raw, context, required)
    repairs = 0
    if plan is None or failures:
        repairs = 1
        repaired = await _repair_plan(
            ctx,
            deps,
            system=brain_system,
            user=brain_user,
            failures=failures or ("unknown_evidence",),
        )
        plan, failures = _validate(brain_input, repaired, context, required)
        ctx.emit("repair", outcome="ok" if plan is not None and not failures else "exhausted")

    if plan is None or failures:
        ctx.emit(
            "answer_plan_validation",
            outcome="fallback",
            reason=failures[0] if failures else "unparseable",
            **versions,
        )
        ctx.emit("main_brain_call", phase="plan", outcome="fallback")
        ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
        return

    ctx.emit("answer_plan_validation", outcome="ok", reason=None, **versions)
    ctx.answer_plan = plan
    if plan.no_results is not None:
        ctx.emit("no_results", reason_class=plan.no_results.reason_class)
    _emit_constraint_handling(ctx, brain_input, plan)

    # Propunerile de state ale brain-ului → reducerul NX-235 decide (sursă model_inferred: un
    # `hard` propus de model nu poate rescrie un hard al clientului — reducerul refuză).
    for proposal in plan.state_update_proposals:
        if proposal.op == "set_topic":
            ctx.state_proposals.append(
                StateUpdateProposal(
                    "set_topic",
                    category_key=proposal.key,
                    source="model_inferred",
                    turn_id=ctx.turn_id,
                )
            )
        else:
            ctx.state_proposals.append(
                StateUpdateProposal(
                    proposal.op,
                    key=proposal.key,
                    value=proposal.value,
                    source="model_inferred",
                    turn_id=ctx.turn_id,
                )
            )

    ask_clarification = _clarification_allowed(ctx, plan)
    text = render_plan_text(plan, ctx.language, ask_clarification=ask_clarification)
    if not text.strip():
        ctx.emit("main_brain_call", phase="render", outcome="empty_fallback")
        ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
        return

    draft_validation = validate_revised_draft(
        text,
        products=run.retrieved,
        generated_links=run.generated_links,
        grounded_prices=run.grounded_prices,
        plan=plan.to_v1(),
    )
    if not draft_validation.ok:
        ctx.emit("main_brain_call", phase="render", outcome="draft_invalid")
        ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
        return

    # Critic SELECTIV, codes-only (reuse NX-211): rulează doar pe triggeri; „unavailable" nu e
    # fail-open — validatorul determinist a trecut deja, criticul doar mai poate RETRAGE.
    critic = await run_semantic_critic(
        deps.llm,
        plan=plan.to_v1(),
        context=context,
        draft=text,
        enabled=settings.answer_plan_critic_enabled,
        comparison=plan.comparison is not None,
        coverage_threshold=settings.answer_plan_critic_coverage_threshold,
        max_quality=settings.answer_plan_max_quality,
    )
    ctx.emit(
        "critic_triggered",
        reason=critic.triggers[0] if critic.triggers else None,
        outcome=critic.status,
    )
    if critic.status == "rejected":
        if repairs >= MAX_REPAIRS:
            ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
            return
        repairs += 1
        repaired = await _repair_plan(
            ctx, deps, system=brain_system, user=brain_user, failures=critic.failures
        )
        plan2, failures2 = _validate(brain_input, repaired, context, required)
        ctx.emit("repair", outcome="ok" if plan2 is not None and not failures2 else "exhausted")
        if plan2 is None or failures2:
            ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
            return
        plan = plan2
        ctx.answer_plan = plan
        text = render_plan_text(
            plan, ctx.language, ask_clarification=_clarification_allowed(ctx, plan)
        )
        draft_validation = validate_revised_draft(
            text,
            products=run.retrieved,
            generated_links=run.generated_links,
            grounded_prices=run.grounded_prices,
            plan=plan.to_v1(),
        )
        if not text.strip() or not draft_validation.ok:
            ctx.set_reply(safe_fallback(ctx.language), cacheable=False)
            return

    previous = tuple(
        (m.body or "")
        for m in ctx.history
        if getattr(m, "direction", "") == "outbound" and (m.body or "").strip()
    )
    for check in evaluate_reply(text, plan=plan, previous_bot_texts=previous):
        ctx.emit("conversation_quality", check=check.check, outcome=check.outcome)

    ctx.emit("main_brain_call", phase="final", outcome="ok", **versions)
    # Reply-urile brain sunt specifice contextului (obligații/nevoi/istoric) → necacheabile în v1.
    ctx.set_reply(text, products=_plan_products(plan, run.retrieved) or None, cacheable=False)


__all__ = [
    "BRAIN_PROMPT_VERSION",
    "MAX_REPAIRS",
    "brain_versions",
    "render_plan_text",
    "run_main_brain",
]
