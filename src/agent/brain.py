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
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from src.agent import tool_budget, turn_profile
from src.agent.answer_plan import (
    AnswerPlanContext,
    AnswerPlanV2,
    validate_answer_plan_v2,
)
from src.agent.answer_plan_runtime import (
    build_answer_plan_context,
    inject_server_owned,
    plan_schema_for_model,
    run_semantic_critic,
    safe_fallback,
    validate_revised_draft,
)
from src.agent.brain_models import BrainInput, UserParts
from src.agent.conversation_quality import evaluate_reply
from src.agent.evidence_bundle import EvidenceBundle, build_evidence_bundle
from src.agent.fallbacks import grounded_fallback_reply
from src.agent.grounding_guard import GroundedAnswer, ground_answer
from src.agent.llm import prompt_cache_scope
from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.agent.tool_definitions import tool_schemas
from src.agent.tool_executor import ToolRun, _safe_tool_args
from src.agent.voice import VOICE_RULES
from src.catalog.freshness import facts_sla_s
from src.config import get_settings
from src.conversation.needs import NeedVocabulary, corroborated_by, norm_key, normalize_need
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import active_needs
from src.domain import vocab_examples
from src.models import RetrievalResult, Route, TurnContext
from src.observability import turn_latency
from src.retrieval.port import deadline_from_turn, query_count_bucket
from src.retrieval.selector import build_port, select_provider
from src.runtime import deadline as turn_deadline
from src.runtime import turn_budget
from src.safety.policy import SafetyPolicy
from src.web.localization import DISPLAYABLE_NEEDS, format_need
from src.worker.context import build_brain_input

if TYPE_CHECKING:
    from src.agent.prompt_builder import PromptInputs
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

#: Versiunea promptului MainBrain — se schimbă la ORICE modificare de instrucțiuni.
BRAIN_PROMPT_VERSION = "main_brain.v1"

#: Bugetul de repair: UN singur repair bounded al aceluiași brain, apoi fallback determinist.
MAX_REPAIRS = 1

#: Câte rânduri de evidence intră în promptul de repair. Bugetul stă în cod (P4): un digest care
#: crește cu catalogul ar transforma reparația într-un al doilea apel scump.
MAX_REPAIR_EVIDENCE = 24

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
- Constrângerile HARD nu se relaxează NICIODATĂ, iar `relaxations` poate conține doar preferințe
  soft.
- `clarification`: cel mult UNA, doar dacă răspunsul ar schimba material rezultatul. Altfel
  răspunde best-effort și marchează assumption/unknown.
- Fără rezultate, pune `no_results` cu clasa ONESTĂ: no_match (am căutat, nu există),
  insufficient_data (nu putem verifica), dependency_unavailable (serviciu indisponibil).
- Nu confirma nicio acțiune care nu e în successful_action_ids, iar `action_intents` vin doar din
  registrul dat. Nu inventa produse, prețuri, linkuri sau stoc. Fără date personale în plan.
"""
# VOCE: `direct_answer` e proza pe care o citește clientul, deci contractul de voce e parte din
# instrucțiuni, nu o rafinare de ton lăsată la latitudinea modelului.
_PLAN_V2_SYSTEM += VOICE_RULES


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _trace(ctx: TurnContext, key: str, value: Any) -> None:
    """Depune un diagnostic în `ctx.trace` (NX-256), sub prefixul `brain_`.

    De ce e nevoie: pe calea creierului unic, `agent_stage` iese la `run_main_brain` ÎNAINTE de
    `finalize`, deci `rich_raw` nu se scrie niciodată. Planul e singurul lucru care decide ce
    randează frontendul (tip de bloc, variantă de text, tabel de comparație, CTA-uri) — vezi
    `_attach_grounding` → `ground_answer` → `channels/web/render_v2`. Fără captura asta, întrebarea
    „de ce a ieșit cardul așa" n-are unde să primească răspuns: planul moare în memorie.

    `getattr`: ctx-urile fake din unit-teste (SimpleNamespace) n-au câmpul — un câmp de diagnoză nu
    are voie să transforme o suită verde într-una roșie (tiparul aftercare, ca în `compose`)."""
    trace = getattr(ctx, "trace", None)
    if trace is not None:
        trace[key] = value


def brain_versions(
    system: str,
    tools: list[dict[str, Any]],
    model: str | None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Amprentele versionate ale rulării (trace attrs, nu labels): prompt/tool-schema/model."""
    return {
        "prompt_version": BRAIN_PROMPT_VERSION,
        "prompt_hash": _sha(system),
        "tool_schema_hash": _sha(json.dumps(tools, sort_keys=True, ensure_ascii=False)),
        "plan_schema": plan_schema_for_model()["name"],
        "model": model,
        # NX-275 felia 4: care direcție a rulat. Fără ea, două ture cu prompturi diferite ar
        # arăta identic în trace, iar `prompt_hash` ar diferi fără să spună de ce.
        "turn_profile": profile,
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
            "ro": "Nu pot verifica acum toate criteriile cerute, nu am datele necesare.",
            "en": "I cannot verify all the requested criteria right now, data is missing.",
            "hu": "Most nem tudom ellenőrizni az összes feltételt, hiányzanak az adatok.",
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
    model: str | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Bucla structurată; eșecul (JSON invalid/API) devine `(None, rounds)` — caller-ul repară."""
    try:
        with prompt_cache_scope(_cache_key(ctx)):
            raw, rounds = await deps.llm.run_tool_loop_structured(
                system, user, tools, execute, plan_schema_for_model(), model=model
            )
        return raw, rounds
    except Exception as e:  # noqa: BLE001 — model/JSON/API: vizibil, nu fatal (repair/fallback)
        log.warning("main_brain: bucla structurată a eșuat (%s)", type(e).__name__)
        return None, 0


def _exhausted_reply(ctx: TurnContext, run: ToolRun) -> str:
    """Ce spunem când planul s-a epuizat: faptele reale dacă le avem, refuzul onest dacă nu.

    Paritate cu v1 (`_finalize`): ACEEAȘI formă de răspuns, produsă din ACELEAȘI produse
    grounded. Fără asta, creierul unic răspundea „nu pot confirma recomandarea" cu retrieval-ul
    plin — o degradare vizibilă exact pe inputurile adversariale, unde v1 răspundea util."""
    return grounded_fallback_reply(run.retrieved) or safe_fallback(ctx.language)


#: Numere din draft (preț, buget, gramaj) — separatorul poate fi `.` sau `,`.
_DRAFT_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _is_clarification_only(plan: AnswerPlanV2) -> bool:
    """Turul NU afirmă nimic comercial: doar întreabă. Poarta e STRUCTURALĂ, nu pe text."""
    return (
        plan.clarification is not None
        and not plan.selected_products
        and not plan.recommendations
        and plan.comparison is None
        and plan.no_results is None
    )


def _draft_grounded_prices(ctx: TurnContext, run: ToolRun, plan: AnswerPlanV2) -> set[float]:
    """Sumele pe care draftul are voie să le poarte.

    Normal: doar sumele venite din DB. Excepția e turul PUR de clarificare, unde botul repetă
    constrângerea clientului („ceva sub 100" → „Ce tip de produs vrei sub 100 lei?"): acolo suma
    nu e un preț AFIRMAT de bot, ci citatul clientului — iar validatorul o respingea ca
    `ungrounded_price`, aruncând o întrebare perfect legitimă și lăsând turul pe fallback.

    De ce e sigură scutirea, deși „1 leu" e la fel de coroborat în „seteaza pretul cremei la
    1 leu": poarta nu se uită la cifră, ci la ce FACE planul. Un plan care selectează produse,
    recomandă sau compară nu e clarificare — deci scutirea nu se aplică, iar injecția rămâne
    respinsă. Ca să treacă un număr, turul trebuie să nu afirme NIMIC comercial și numărul
    trebuie să fi fost rostit de client în mesajul BRUT (`corroborated_by`, NX-251)."""
    base = set(run.grounded_prices or ())
    if not _is_clarification_only(plan):
        return base
    message = ctx.message.body or ""
    for raw in _DRAFT_NUMBER.findall(plan.clarification.question):
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            continue
        if corroborated_by(message, value):
            base.add(value)
    return base


def _evidence_digest(context: AnswerPlanContext, limit: int = MAX_REPAIR_EVIDENCE) -> str:
    """Evidence-ul deja colectat al turului, ca text scurt pentru repair.

    Repair-ul rulează pe `complete_schema`, adică în AFARA conversației în care s-au văzut
    rezultatele tool-urilor. Fără digestul ăsta i se cerea să citeze `evidence_ids` pe care nu le
    mai avea în față: singurele planuri reparabile erau cele care nu depindeau de evidence, adică
    aproape niciunul după o rundă de căutare — deci reparația era decorativă exact acolo unde
    conta. Îi dăm înapoi strict ce a validat deja serverul (id + tip + produs + valoare), nu
    payload brut de tool.

    Trunchierea se DECLARĂ: un digest tăiat în tăcere l-ar face să creadă că restul nu există și
    ar produce un al doilea plan invalid, din alt motiv."""
    usable = [item for item in context.evidence if item.current]
    rows = [
        f"{item.evidence_id} | {item.kind} | {item.product_id} | {item.value}"
        for item in usable[:limit]
    ]
    if not rows:
        return ""
    hidden = len(usable) - len(rows)
    tail = f"\n(+{hidden} nelistate, folosește doar id-urile de mai sus)" if hidden else ""
    header = "\nEVIDENCE DISPONIBIL (evidence_id | tip | product_id | valoare):\n"
    return header + "\n".join(rows) + tail


async def _repair_plan(
    ctx: TurnContext,
    deps: PipelineDeps,
    *,
    system: str,
    user: str,
    failures: tuple[str, ...],
    context: AnswerPlanContext,
    model: str | None = None,
) -> dict[str, Any] | None:
    """UN repair bounded al ACELUIAȘI brain: aceleași instrucțiuni + codurile de validare +
    evidence-ul turului (fără el, un plan care depinde de tool results nu poate fi reparat)."""
    feedback = (
        f"\nPLANUL ANTERIOR A FOST INVALID. Corectează DOAR: {', '.join(failures)}"
        f"{_evidence_digest(context)}"
    )
    try:
        with prompt_cache_scope(_cache_key(ctx)):
            return await deps.llm.complete_schema(
                system, user + feedback, plan_schema_for_model(), model=model
            )
    except Exception as e:  # noqa: BLE001 — repair eșuat → fallback determinist
        log.warning("main_brain: repair eșuat (%s)", type(e).__name__)
        return None


def _validate(
    ctx: TurnContext,
    brain_input: BrainInput,
    raw: dict[str, Any] | None,
    context: AnswerPlanContext,
    required: tuple[tuple[str, str], ...],
) -> tuple[AnswerPlanV2 | None, tuple[str, ...]]:
    """Parsare + validare V2. Întoarce `(plan, failures)`; plan None = nici măcar parsabil.

    NX-275 felia 2: câmpurile server-owned se INJECTEAZĂ aici, într-un singur loc, fiindcă toate
    cele trei căi (plan inițial, repair, repair-ul de pe ramura de critic) trec pe aici. Injectarea
    e idempotentă, deci merge identic cu flagul stins, când modelul le-a emis oricum."""
    raw = inject_server_owned(
        raw,
        business_id=ctx.business.id,
        locale=ctx.language,
        obligations=[{"kind": kind, "key": key} for kind, key in required],
    )
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


def _retrieval_annotations(
    bundles: list[Any],
) -> tuple[dict[str, str], dict[str, tuple[Any, ...]]]:
    """Verdictele providerului de retrieval, pe produs. Prima apariție câștigă: dacă două căutări
    din același tur au judecat același produs, judecata care a produs lista pe care o vede
    clientul e prima — a doua ar rescrie retroactiv un verdict deja afișat."""
    classes: dict[str, str] = {}
    constraints: dict[str, tuple[Any, ...]] = {}
    for bundle in bundles:
        for candidate in getattr(bundle, "candidates", ()) or ():
            product_id = str(getattr(candidate, "product_id", "") or "")
            if not product_id or product_id in classes:
                continue
            classes[product_id] = str(getattr(candidate, "match_class", "exact"))
            constraints[product_id] = tuple(getattr(candidate, "constraint_results", ()) or ())
    return classes, constraints


def _memory_criteria(ctx: TurnContext, locale: str) -> tuple[str, ...]:
    """Criteriile ACTIVE, ca text afișabil. Doar sloturile cu formă onestă (`DISPLAYABLE_NEEDS`):
    un slug de vocabular („ten_gras") pe ecran ar fi memoria noastră internă scursă în UI."""
    values = {need.key: need.normalized_value for need in active_needs(ctx)}
    out: list[str] = []
    for key in DISPLAYABLE_NEEDS:
        value = values.get(key)
        text = format_need(key, value, "RON", locale) if value is not None else None
        if text:
            out.append(text)
    return tuple(out)


def _bucket(count: int) -> str:
    """Bandă low-cardinality pentru numărători (P10/P12: o metrică nu crește cu catalogul)."""
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 3:
        return "2-3"
    if count <= 6:
        return "4-6"
    return "7+"


def _freshness_bucket(fact: Any) -> str:
    """Banda de prospețime a unui fapt. `unverified` e o categorie proprie, nu „vechi": un fapt
    fără `verified_at` nu e stale, e neverificat — două cauze diferite, două fixuri diferite."""
    if fact.verified_at is None:
        return "unverified"
    age = fact.age_s or 0
    if age < 3600:
        return "<1h"
    if age < 86400:
        return "<1d"
    if age < 7 * 86400:
        return "<7d"
    return "7d+"


def _emit_grounding_telemetry(
    ctx: TurnContext, bundle: EvidenceBundle, answer: GroundedAnswer
) -> None:
    """Observabilitatea cardului, low-cardinality. Fără id-uri de produs, fără text, fără sume —
    numai câmpuri din vocabular ÎNCHIS și benzi."""
    coverage = bundle.coverage()
    total = max(1, len(bundle.products))
    known_price = coverage["price"]["known"]
    ctx.emit(
        "evidence_bundle",
        outcome="ok" if answer.ok else "rejected",
        product_bucket=_bucket(len(bundle.products)),
        source_coverage_bucket=(
            "full" if known_price == total else ("partial" if known_price else "none")
        ),
    )
    ctx.emit("evidence_query_count_bucket", bucket=query_count_bucket(bundle.query_count))
    for product in bundle.products:
        for name in ("price", "availability", "rating", "delivery_promise"):
            fact = product.fact(name)
            ctx.emit(
                "commercial_fact",
                field=name,
                status=fact.status,
                freshness_bucket=_freshness_bucket(fact),
            )
    for failure in answer.failures:
        ctx.emit("grounding_claim", type="prose", outcome="rejected", reason=failure)
    for omission in answer.omissions:
        if omission.field == "commerce_cta":
            ctx.emit("commerce_cta_omitted", reason=omission.reason)
        else:
            ctx.emit("view_field_omitted", field=omission.field, reason=omission.reason)


def _attach_grounding(
    ctx: TurnContext,
    run: ToolRun,
    plan: AnswerPlanV2,
    execute: Any,
    *,
    ask_clarification: bool,
) -> None:
    """Îngheață faptele turului și trece planul prin `GroundingGuard`. DOAR sub flag; cu flagul
    stins `ctx.grounded` rămâne None, deci marginea web persistă exact ce persista înainte.

    Rulează după validarea planului și după validarea prozei: guardul e ultima poartă, nu prima —
    ce respinge el a trecut deja de tot restul, deci un refuz aici e un semnal real, nu zgomot."""
    settings = get_settings()
    if not settings.web_view_v2_projector_enabled:
        return
    classes, constraints = _retrieval_annotations(getattr(execute, "bundles", []) or [])
    bundle = build_evidence_bundle(
        business_id=ctx.business.id,
        locale=ctx.language,
        rows=run.retrieved,
        now=datetime.now(UTC),
        # Pragul aparține TENANTULUI, nu mediului: un catalog alimentat de feed live și unul
        # importat o dată nu se pot judeca cu aceeași cifră (`src/catalog/freshness.py`).
        sla_s=facts_sla_s(ctx.business.settings, default=settings.commerce_facts_sla_s),
        match_class_by_product=classes,
        constraints_by_product=constraints,
        cart=getattr(run, "cart_snapshot", None),
        # Bugetul de query-uri al bundle-ului e ZERO prin construcție: se hidratează din rândurile
        # deja retrievate. Contorul raportează câte căutări au alimentat faptele, nu câte a făcut
        # builderul — altfel ar raporta mereu 0 și n-ar detecta nimic.
        query_count=len(getattr(execute, "bundles", []) or []),
    )
    # NX-241: grounding-ul e o FAZĂ (validare), măsurată din afară ca guardul să rămână determinist.
    with turn_latency.span("validation"):
        answer = ground_answer(
            plan,
            bundle,
            locale=ctx.language,
            ask_clarification=ask_clarification,
            memory_criteria=_memory_criteria(ctx, ctx.language),
            commerce_enabled=settings.conversation_cart_enabled,
        )
    _emit_grounding_telemetry(ctx, bundle, answer)
    ctx.grounded = answer if answer.ok else None


def _plan_source(ctx: TurnContext, vocab: NeedVocabulary, proposal: Any) -> str:
    """Cine AFIRMĂ faptul propus de plan: clientul sau modelul?

    Modelul nu-și poate alege sursa (ar fi D7 pe cuvântul lui). O propune codul, dintr-o singură
    întrebare verificabilă: valoarea asta chiar apare în ce a scris clientul ACUM? Dacă da, e
    afirmația lui și poate deveni `hard`; dacă nu, e o inferență și rămâne `soft`.

    Poarta contează cel mai mult când triajul nu mai rulează sincron: extracția de sloturi
    `user_explicit` venea de la nano (`stages/agent._filter_proposals`), iar fără un înlocuitor
    TOATE nevoile ar coborî la `soft` și noțiunea de constrângere inviolabilă ar dispărea tăcut.

    Limita ei, explicit: coroborarea confirmă că valoarea a fost ROSTITĂ, nu că modelul a
    interpretat-o corect — un „200ml" citit ca buget rămâne o eroare a modelului, exact ca la
    extracția din triaj. Ce garantează e că nimic ne-rostit nu devine fapt al clientului."""
    if proposal.op == "set_need":
        normalized = normalize_need(proposal.key, proposal.value, vocab)
        value = normalized.value if normalized is not None else None
    elif proposal.op == "revoke":
        # Revocarea se coroborează pe valoarea DIN MEMORIE, nu pe una propusă: „de fapt accept
        # Sony" conține exact faptul care se retrage. Un „bugetul nu mai contează" nu conține
        # „200", deci nu se coroborează — și tocmai de asta nu poate șterge un plafon declarat de
        # client (reducerul îl respinge cu `unsupported_revoke`, vizibil în metrici).
        key = norm_key(proposal.key)
        need = next((n for n in active_needs(ctx) if n.key == key), None)
        value = need.normalized_value if need is not None else None
    else:
        return "model_inferred"
    return "user_explicit" if corroborated_by(ctx.message.body or "", value) else "model_inferred"


def _state_proposals_from_plan(ctx: TurnContext, plan: AnswerPlanV2) -> list[StateUpdateProposal]:
    """Propunerile planului → propuneri typed pentru reducer, cu sursa stabilită de COD."""
    vocab = NeedVocabulary.from_pack(getattr(ctx.business, "domain_pack", None))
    out: list[StateUpdateProposal] = []
    for proposal in plan.state_update_proposals:
        if proposal.op == "set_topic":
            out.append(
                StateUpdateProposal(
                    "set_topic",
                    category_key=proposal.key,
                    source="model_inferred",
                    turn_id=ctx.turn_id,
                )
            )
            continue
        source = _plan_source(ctx, vocab, proposal)
        ctx.emit("state_proposal_source", op=proposal.op, source=source)
        out.append(
            StateUpdateProposal(
                proposal.op,
                key=proposal.key,
                value=proposal.value,
                source=source,
                turn_id=ctx.turn_id,
            )
        )
    return out


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


def _persist_clarification(ctx: TurnContext, plan: AnswerPlanV2) -> None:
    """O întrebare PUSĂ trebuie să poată fi reluată la turul următor și numărată împotriva buclei.

    Calea de triaj face asta de mult (`set_clarify` → `pending_question` → `clarify_resume_stage`).
    Brain-ul doar concatena întrebarea în text: răspunsul scurt care urma („sub 200") repornea de
    la zero, fiindcă nimic nu ținea minte CE s-a întrebat, iar `asked_questions` nu creștea
    niciodată — deci anti-bucla NX-235 număra 0 la infinit și aceeași întrebare se putea repeta
    tur după tur. Cu poarta de gain stinsă (defaultul), nimic nu o oprea.

    Scriem în AMBELE reprezentări fiindcă ambele au cititori: `reply.pending_question` (v1,
    persistat de processor, citit de `clarify_resume_stage` și de marginea web pentru tokenul de
    acțiune NX-236) și propunerea typed (v2, unde trăiesc `question_id` și `attempts`)."""
    clarification = plan.clarification
    if clarification is None or ctx.reply is None:
        return
    field = norm_key(clarification.target_need) or "intent"
    previous = ctx.state.pending_question if isinstance(ctx.state.pending_question, dict) else None
    same_slot = bool(previous) and previous.get("field") == field
    attempts = int(previous.get("attempts") or 0) + 1 if same_slot else 1
    ctx.reply.pending_question = {
        "field": field,
        "resume_route": Route.SALES.value,
        "asked_at": datetime.now(UTC).isoformat(),
        "attempts": attempts,
    }
    ctx.state_proposals.append(
        StateUpdateProposal(
            "set_pending_question",
            key=field,
            source="policy",
            turn_id=ctx.turn_id,
            reason="missing_required",
            resume_route=Route.SALES.value,
            options_refs=tuple(o for o in clarification.options if o)[:6],
        )
    )
    ctx.emit("clarify_asked", field=field, attempts=attempts, source="main_brain")


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
        # NX-241: căutarea prin port trece prin ACELEAȘI porți ca orice tool — admission (plafon de
        # apeluri + timp rămas) și poarta read/mutation. Altfel `search_products` ar fi singurul
        # tool nebugetat, adică fix cel pe care modelul îl cheamă în buclă.
        ledger, d = turn_budget.current(), turn_deadline.current()
        if ledger is None and d is None:
            async with self.run._execution_lock:  # noqa: SLF001 — aceeași serializare ca ToolRun
                return await self._search(name, args)
        seq = self.run._take_ticket()  # noqa: SLF001 — aceeași ordonare ca ToolRun (NX-241)
        try:
            admission = tool_budget.admit(name, ledger=ledger, deadline=d)
            if not admission:
                self.ctx.emit(
                    "tool_budget",
                    name=name,
                    outcome="rejected",
                    reason=admission.reason or "unknown",
                )
                return admission.refusal or tool_budget.REFUSAL_BUDGET
            async with self.run._tool_gate().hold(name):  # noqa: SLF001 — poarta lui ToolRun
                return await self._search(name, args, seq=seq)
        finally:
            if seq is not None:
                await self.run._finish_ticket(seq)  # noqa: SLF001

    async def _search(self, name: str, args: dict[str, Any], *, seq: int | None = None) -> str:
        ctx, run = self.ctx, self.run
        started = perf_counter()
        spec = _spec_from_args(ctx, args)
        budget = turn_budget.current()
        cap_ms = budget.budget.retrieval_ms if budget else get_settings().retrieval_deadline_ms
        try:
            with turn_latency.span("retrieval"):
                bundle = await self.port.retrieve(
                    ctx.snapshot,
                    spec,
                    active_needs(ctx),
                    deadline=deadline_from_turn(cap_ms),
                )
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
        if seq is not None:
            # Acumularea (bundles + `retrieved`) se aplică în ordinea APELURILOR, nu în ordinea în
            # care a răspuns providerul — altfel aceleași două căutări ar da carduri în ordini
            # diferite de la o rulare la alta.
            await run._await_ticket(seq)  # noqa: SLF001
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


def _cache_key(ctx: TurnContext) -> str:
    """Partiția de cache a turului: tenant + versiunea de prompt (NX-275 felia 3).

    Tenantul, fiindcă prefixul (system generat din DB + tool-uri) e al lui. Versiunea, fiindcă la
    o schimbare de prompt vrei să NU cauți într-un cache al formei vechi. Nimic per conversație:
    ar face fiecare conversație propria partiție, adică fix opusul scopului."""
    return f"{ctx.business.id}:{BRAIN_PROMPT_VERSION}"


def _compose_user(user: str, parts: UserParts | None, brain_blocks: str) -> str:
    """Mesajul USER al brain-ului, compus într-UN singur loc (NX-275 felia 3).

    Stins (implicit) sau fără părți: exact ordinea de azi — blocurile brain-ului, apoi `user`
    (care e deja `per_turn + istoric + mesaj`). Byte-identic, deci nimic nu se mișcă.

    Aprins: istoricul URCĂ în față. Motivul e mecanic, nu estetic: prompt cachingul se prinde pe
    un prefix identic, iar orice octet care se schimbă mai devreme îl invalidează pe tot ce
    urmează. Cu obligațiile și hint-urile turului scrise ÎNAINTEA istoricului, istoricul (partea
    care crește cel mai mult și e stabilă în interiorul unei conversații) e mereu precedat de
    octeți diferiți, deci nu are cum să fie servit din cache. Inversând ordinea, turul 2+ al
    aceleiași conversații retrimite un prefix pe care furnizorul l-a mai văzut.

    Conținutul e IDENTIC în ambele ramuri — se schimbă doar poziția. De asta felia are flag
    propriu: reordonarea poate schimba comportamentul modelului chiar dacă nu schimbă informația,
    iar asta se măsoară pe golden, nu se presupune.
    """
    if parts is None or not getattr(get_settings(), "prompt_cache_layout_enabled", False):
        return f"{brain_blocks}{user}"
    return parts.cache_first(brain_blocks=brain_blocks)


async def run_main_brain(
    ctx: TurnContext,
    deps: PipelineDeps,
    *,
    run: ToolRun,
    inp: PromptInputs,
    tools: list[dict[str, Any]],
    system: str,
    user: str,
    user_parts: UserParts | None = None,
    query: str,
) -> None:
    """Turul MainBrain: plan structurat în aceeași buclă → validare → (un repair) → render →
    critic selectiv → reply. ÎNTOTDEAUNA setează un reply non-gol (P6) — fallback determinist la
    orice epuizare. Chemat de `agent_stage` DOAR sub `single_brain_enabled`."""
    settings = get_settings()
    brain_input = build_brain_input(ctx)  # snapshot/state SAFE — fără conn, fără frontend facts
    obligations = brain_input.obligations
    required = tuple((o.kind, o.key) for o in obligations)

    # Modelul turului: escaladăm la cel puternic DOAR pentru turele complicate (comparație, mesaj
    # mixt, mutație). Clasa vine din obligațiile DETERMINISTE — niciun model nu decide aici, altfel
    # am plăti un apel ca să aflăm dacă merită să plătim un apel. Fără `model_agent_complex`
    # configurat, totul rămâne pe `model_agent`: comportamentul de dinainte, bit cu bit.
    turn_class = turn_budget.turn_class_for(obligations)
    escalate = turn_class in (turn_budget.TurnClass.COMPLEX, turn_budget.TurnClass.MUTATION)
    model = (settings.model_agent_complex.strip() if escalate else "") or settings.model_agent
    ctx.emit("model_tier", turn_class=turn_class.value, escalated=escalate)

    # NX-238: selectorul decide providerul; fără GO semnat → current live, întotdeauna.
    selection = select_provider(business_id=ctx.business.id, conversation_id=ctx.conversation_id)
    port = build_port(ctx, deps, selection)
    ctx.emit(
        "retrieval_gate",
        decision=selection.provider_version,
        reason=selection.reason,
        blocking_code=selection.blocking_code,
    )

    # NX-275 felia 4: direcția de răspuns, aleasă de COD din obligații + clasa de tur. Adaugă un
    # sufix la FINALUL system-ului (prefixul rămâne byte-identic, deci cache-ul ține) și, cel mult,
    # tool-uri în plus. OFF → `profile is None` și nimic nu se schimbă.
    profile = (
        turn_profile.select(turn_class, obligations)
        if getattr(settings, "turn_profiles_enabled", False)
        else None
    )
    if profile is not None:
        ctx.emit("turn_profile", name=profile.name, turn_class=turn_class.value)
        have = {s.get("function", {}).get("name") for s in tools}
        extra = [t for t in profile.extra_tools if t not in have]
        if extra:
            examples = vocab_examples.from_pack(getattr(ctx.business, "domain_pack", None))
            tools = [*tools, *tool_schemas(extra, examples)]

    brain_system = f"{system}\n{_PLAN_V2_SYSTEM}"
    if profile is not None:
        brain_system = f"{brain_system}\n{profile.suffix}"
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
    brain_user = _compose_user(user, user_parts, f"{obligations_block}{needs_block}{signals_block}")
    versions = brain_versions(brain_system, tools, model, profile.name if profile else None)

    execute = _PortedExecute(ctx, deps, run, port)
    raw, rounds = await _generate_plan(
        ctx, deps, system=brain_system, user=brain_user, tools=tools, execute=execute, model=model
    )
    ctx.emit("main_brain_tool_rounds_bucket", bucket=_rounds_bucket(rounds))
    # NX-256: planul BRUT al modelului, înainte de validare — singurul loc unde există. Un plan
    # respins la validare e exact cazul pe care vrei să-l citești, iar el nu ajunge nici în
    # `ctx.answer_plan`, nici în reply.
    _trace(ctx, "brain_plan_raw", raw)

    # NX-173 (P0) ENFORCEMENT FINAL + contractul `ctx.retrieval`, exact ca pe calea v1
    # (`planner.build_plan`). Brain-ul returnează înainte de `build_plan`, deci fără asta nimeni
    # nu mai scria câmpul: `run.retrieved` avea produsele, dar `ctx.retrieval` rămânea gol — iar
    # din el se alimentează validatorul de proză, cardurile, `displayed_products` (deci referința
    # „primul"/„acesta" din turul URMĂTOR) și analytics. Gate-ul e idempotent pe un set deja
    # gate-uit: backstop-ul din `ToolRun` rămâne, ăsta e ultimul punct înainte de consumatori.
    run.retrieved[:] = SafetyPolicy.for_turn(ctx).gate(
        ctx, run.retrieved, purpose="retrieval_final"
    )[0]
    ctx.retrieval = RetrievalResult(products=list(run.retrieved), source="tools")

    context = build_answer_plan_context(
        business_id=ctx.business.id,
        locale=ctx.language,
        products=run.retrieved,
        successful_action_ids=run.successful_action_ids,
        known_need_ids=_known_need_ids(brain_input),
    )
    plan, failures = _validate(ctx, brain_input, raw, context, required)
    repairs = 0
    if plan is None or failures:
        _trace(ctx, "brain_plan_failures", list(failures or ("unknown_evidence",)))
        repairs = 1
        repaired = await _repair_plan(
            ctx,
            deps,
            system=brain_system,
            user=brain_user,
            failures=failures or ("unknown_evidence",),
            context=context,
            model=model,
        )
        _trace(ctx, "brain_repair_raw", repaired)
        plan, failures = _validate(ctx, brain_input, repaired, context, required)
        ctx.emit("repair", outcome="ok" if plan is not None and not failures else "exhausted")

    if plan is None or failures:
        ctx.emit(
            "answer_plan_validation",
            outcome="fallback",
            reason=failures[0] if failures else "unparseable",
            **versions,
        )
        ctx.emit("main_brain_call", phase="plan", outcome="fallback")
        _trace(ctx, "brain_plan_fallback", failures[0] if failures else "unparseable")
        ctx.set_reply(_exhausted_reply(ctx, run), cacheable=False)
        return

    ctx.emit("answer_plan_validation", outcome="ok", reason=None, **versions)
    ctx.answer_plan = plan
    # Planul VALIDAT — cel din care se derivă grounding-ul și, prin el, blocurile pe care le
    # randează frontendul. `by_alias`: exact forma din schemă, ca să se poată compara cu `raw`.
    _trace(ctx, "brain_plan", plan.model_dump(mode="json", by_alias=True))
    if plan.no_results is not None:
        ctx.emit("no_results", reason_class=plan.no_results.reason_class)
    _emit_constraint_handling(ctx, brain_input, plan)

    # Propunerile de state ale brain-ului → reducerul NX-235 decide. Sursa NU e a modelului: o
    # declară codul, după ce confruntă valoarea cu mesajul BRUT al clientului (vezi `_plan_source`).
    ctx.state_proposals.extend(_state_proposals_from_plan(ctx, plan))

    ask_clarification = _clarification_allowed(ctx, plan)
    text = render_plan_text(plan, ctx.language, ask_clarification=ask_clarification)
    if not text.strip():
        ctx.emit("main_brain_call", phase="render", outcome="empty_fallback")
        ctx.set_reply(_exhausted_reply(ctx, run), cacheable=False)
        return

    draft_validation = validate_revised_draft(
        text,
        products=run.retrieved,
        generated_links=run.generated_links,
        grounded_prices=_draft_grounded_prices(ctx, run, plan),
        plan=plan.to_v1(),
    )
    if not draft_validation.ok:
        # Motivul călătorește cu evenimentul, ca pe calea v1 (`validator_reasons`). Fără el,
        # `draft_invalid` spune că răspunsul a fost aruncat, dar nu și de ce — adică exact
        # întrebarea pe care o pui în incident.
        ctx.emit(
            "main_brain_call",
            phase="render",
            outcome="draft_invalid",
            reason=draft_validation.reasons[0] if draft_validation.reasons else "unknown",
        )
        ctx.set_reply(_exhausted_reply(ctx, run), cacheable=False)
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
            ctx.set_reply(_exhausted_reply(ctx, run), cacheable=False)
            return
        repairs += 1
        repaired = await _repair_plan(
            ctx,
            deps,
            system=brain_system,
            user=brain_user,
            failures=critic.failures,
            context=context,
            model=model,
        )
        plan2, failures2 = _validate(ctx, brain_input, repaired, context, required)
        ctx.emit("repair", outcome="ok" if plan2 is not None and not failures2 else "exhausted")
        if plan2 is None or failures2:
            ctx.set_reply(_exhausted_reply(ctx, run), cacheable=False)
            return
        plan = plan2
        ctx.answer_plan = plan
        # Poarta se re-evaluează pe planul NOU, o singură dată: `_clarification_allowed` emite
        # `clarification_decision`, deci a o chema de două ori ar dubla evenimentul.
        ask_clarification = _clarification_allowed(ctx, plan)
        text = render_plan_text(plan, ctx.language, ask_clarification=ask_clarification)
        draft_validation = validate_revised_draft(
            text,
            products=run.retrieved,
            generated_links=run.generated_links,
            grounded_prices=_draft_grounded_prices(ctx, run, plan),
            plan=plan.to_v1(),
        )
        if not text.strip() or not draft_validation.ok:
            ctx.set_reply(_exhausted_reply(ctx, run), cacheable=False)
            return

    previous = tuple(
        (m.body or "")
        for m in ctx.history
        if getattr(m, "direction", "") == "outbound" and (m.body or "").strip()
    )
    for check in evaluate_reply(text, plan=plan, previous_bot_texts=previous):
        ctx.emit("conversation_quality", check=check.check, outcome=check.outcome)

    # NX-240: faptele se îngheață AICI, după ce planul și proza au trecut toate porțile. Ce iese
    # de aici e ce va proiecta `render_v2` — și nimic din catalog nu-l mai poate schimba.
    _attach_grounding(ctx, run, plan, execute, ask_clarification=ask_clarification)

    ctx.emit("main_brain_call", phase="final", outcome="ok", **versions)
    # Reply-urile brain sunt specifice contextului (obligații/nevoi/istoric) → necacheabile în v1.
    ctx.set_reply(text, products=_plan_products(plan, run.retrieved) or None, cacheable=False)
    if ask_clarification:
        _persist_clarification(ctx, plan)


__all__ = [
    "BRAIN_PROMPT_VERSION",
    "MAX_REPAIRS",
    "brain_versions",
    "render_plan_text",
    "run_main_brain",
]
