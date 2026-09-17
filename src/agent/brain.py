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
from collections.abc import Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from src.agent import speculative_retrieval, tool_budget, turn_profile
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
from src.agent.brain_rich import card_refs, rich_from_plan
from src.agent.conversation_quality import evaluate_reply
from src.agent.deterministic import _comparison_facets
from src.agent.evidence_bundle import EvidenceBundle, build_evidence_bundle
from src.agent.fallbacks import grounded_fallback_reply
from src.agent.grounding_guard import GroundedAnswer, ground_answer
from src.agent.llm import prompt_cache_scope
from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.agent.tool_definitions import tool_schemas
from src.agent.tool_executor import ToolRun, _safe_tool_args
from src.agent.voice import VOICE_RULES
from src.analytics.demand import clean_ids
from src.catalog.freshness import facts_sla_s
from src.config import get_settings
from src.conversation.needs import NeedVocabulary, corroborated_by, norm_key, normalize_need
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import active_needs
from src.domain import vocab_examples
from src.models import Offer, RetrievalResult, Route, TurnContext
from src.observability import turn_latency
from src.retrieval.port import deadline_from_turn, query_count_bucket
from src.retrieval.selector import build_port, select_provider
from src.runtime import deadline as turn_deadline
from src.runtime import turn_budget
from src.safety.policy import SafetyPolicy
from src.web.localization import DISPLAYABLE_NEEDS, format_need
from src.worker import compose
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

#: Câte rânduri de evidence se atașează la UN rezultat de tool. Bugetul stă în cod (P4), la fel ca
#: la repair: blocul însoțește fiecare apel, deci un plafon lipsă l-ar înmulți cu numărul de runde.
MAX_TOOL_EVIDENCE = 32

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
- `evidence_ids` se COPIAZĂ din blocurile EVIDENCE primite după rezultatele tool-urilor, exact cum
  sunt scrise. Nu construi id-uri după un tipar ghicit. Fiecare produs din `selected_products` are
  cel puțin un id AL LUI; dacă numești o variantă, adaugă și un id legat de acea variantă.
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
        },
        "insufficient_data": {
            "ro": "Nu pot verifica acum toate criteriile cerute, nu am datele necesare.",
            "en": "I cannot verify all the requested criteria right now, data is missing.",
        },
        "dependency_unavailable": {
            "ro": "Căutarea nu e disponibilă momentan. Te rog încearcă din nou puțin mai târziu.",
            "en": "Search is temporarily unavailable. Please try again shortly.",
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


async def _clarify_chips(ctx: TurnContext, deps: Any) -> tuple[str, ...]:
    """Chips-urile turului, din MENIUL ÎNCHIS al catalogului (NX-295) — nu din output-ul modelului.

    Pe v1 chips-urile erau frazele scrise de modelul rich, iar NX-295 a arătat de ce asta e o
    promisiune apăsabilă fără acoperire: un magazin de COSMETICE oferea «Pentru consola, cablu USB
    de date». Planul creierului unic n-are câmp de sugestii, și e bine că n-are — meniul se compune
    din catalogul REAL, deci fiecare frază se rezolvă înapoi prin `resolve_any`, pe o cheie cu
    produse. Un chip nu e o părere, e un mesaj pe care clientul îl retrimite.

    `ground_suggestions` primește lista GOALĂ deliberat: fără sugestii de filtrat, tot ce rămâne e
    COMPLETAREA din meniu, adică exact opțiunile oferibile. Refolosim funcția în loc să citim
    `menu.phrases()` direct fiindcă ea deține deja plafonul și dedupe-ul; două locuri care aleg
    câte chips-uri încap ar diverge la primul cap schimbat.

    Meniu indisponibil (flag stins, DB jos, vocabular gol) ⇒ zero chips, adică exact starea de
    dinainte de această felie. Fail-open: o clipeală de DB n-are voie să devină comportament nou.
    """
    from src.catalog.clarify_menu import ground_suggestions, menu_for_turn  # noqa: PLC0415

    menu = await menu_for_turn(ctx, deps)
    if not menu.usable:
        return ()
    kept, _ = ground_suggestions((), menu)
    return kept


def _attach_checkout_offer(ctx: TurnContext, run: ToolRun) -> None:
    """NX-137, pe calea creierului unic: linkul de checkout creat în ACEST tur ajunge garantat la
    client, ca CTA neutru de canal.

    Pe v1 asta se cheamă din `finalize` pe ambele ramuri. Brain-ul nu o chema deloc, deci un tur
    care crea linkul (`checkout_link_created` în DB) putea ieși fără buton: validatorul verifică
    doar că linkurile SCRISE sunt din `generated_links`, niciodată că cel CREAT a fost scris. Un
    link creat și nerostit e exact bug-ul pe care NX-137 l-a reparat o dată. Floor-ul din
    `set_offer` nu dublează un URL pe care proza îl conține deja.
    """
    url = getattr(run, "checkout_url", None)
    if not url or ctx.reply is None:
        return
    from src.agent.finalize import _checkout_label  # noqa: PLC0415 — evită ciclul

    ctx.set_offer(Offer(kind="open_url", label=_checkout_label(ctx.language), url=url))
    ctx.emit("checkout_offer_attached")


async def _set_brain_reply(
    ctx: TurnContext, deps: Any, plan: AnswerPlanV2, run: ToolRun, text: str
) -> None:
    """Punctul UNIC prin care planul devine reply: comparație, recomandare bogată sau proză.

    Ordinea ramurilor e a lui v1 (`finalize.render`) și nu e arbitrară: comparația PRECEDE calea
    bogată, altfel un „compară primele două" ar RE-RECOMANDA în loc să compare.

    Reply-urile brain sunt specifice contextului (obligații/nevoi/istoric) → necacheabile în v1.

    `text` pleacă neatins ca floor (`messages.body`, canale fără randare bogată), NU aplatizarea
    `compose.flatten`: pe v1 intro-ul era un framing scurt peste o enumerare făcută de cod, dar aici
    proza creierului E deja răspunsul complet, cu produsele numite în ea. Aplatizată peste ea,
    enumerarea ar spune totul de două ori.

    ORDER nu ajunge aici: `agent_stage` cheamă creierul unic doar pe `not is_order`.
    """
    settings = get_settings()
    chips = await _clarify_chips(ctx, deps) if settings.brain_chips_enabled else ()

    if settings.brain_rich_reply_enabled:
        # ── COMPARAȚIE ────────────────────────────────────────────────────────────────────────
        # `run.compared` e populat de `compare_products` pe ACELAȘI `ToolRun` ca pe v1 — brain-ul
        # doar nu-l citea, deci o comparație cerută modelului (nu prinsă de poarta deterministă
        # dinaintea buclei) ieșea ca proză, fără tabel. Tabelul e DETERMINIST: fiecare celulă e un
        # fapt din retrieval, zero text de model.
        #
        # Ce NU facem, deliberat: `compose_comparison` (narativul v1). El REscrie leadul cu un al
        # doilea apel de model, iar aici proza creierului există deja și a trecut toate porțile —
        # un al doilea writer semantic e exact ce interzice D1. Tabelul rămâne al datelor, leadul
        # rămâne al creierului.
        if run.compared:
            comparison = compose.build_comparison(
                run.compared, ctx.language, _comparison_facets(ctx)
            )
            if comparison is not None:
                comparison.intro = text
                ctx.set_comparison_reply(
                    comparison,
                    text=text,
                    products=compose.comparison_cards(comparison),
                    chips=list(chips) or None,
                )
                ctx.emit("agent_compared", n=len(comparison.columns))
                _attach_checkout_offer(ctx, run)
                return

        # ── RECOMANDARE BOGATĂ ────────────────────────────────────────────────────────────────
        rich = rich_from_plan(ctx, plan, run.retrieved, text=text, suggestions=chips)
        if rich is not None:
            ctx.set_rich_reply(rich, text=text, products=compose.card_products(rich.items))
            # Paritate de OBSERVABILITATE cu v1 (`finalize`): fără el, aprinderea creierului unic
            # golea tăcut `agent_recommended`, adică exact seria pe care se citește ce recomandă
            # botul și ce se cere (NX-163/164). Doar id-uri, ca acolo.
            ctx.emit(
                "agent_recommended",
                n=len(rich.items),
                rich=True,
                product_ids=clean_ids(it.product_id for it in rich.items),
            )
            _attach_checkout_offer(ctx, run)
            return

    # ── PROZĂ ─────────────────────────────────────────────────────────────────────────────────
    # Fără carduri (clarificare pură, refuz onest) sau cu felia stinsă. `card_refs` și nu rândurile
    # brute nici aici — kill-switch-ul e pentru FORMA bogată, nu pentru dreptul de a trimite carduri
    # fără `product_id`.
    cards = card_refs(_plan_products(plan, run.retrieved))
    ctx.set_reply(text, products=cards or None, cacheable=False)
    if cards:
        # Ca pe ramura de proză a lui v1 (`finalize`): fără `rich=True`, dar cu aceleași ref-uri.
        # Seria „ce recomandă botul" (NX-163/164) nu are voie să depindă de ce ramură a servit.
        ctx.emit(
            "agent_recommended",
            n=len(cards),
            product_ids=clean_ids(c["product_id"] for c in cards),
        )
    # Chips și pe ramura săracă: pe v1, un no-result de sales primea căi de continuare
    # (`_attach_no_result_alternatives`). Aici vin din meniul catalogului, deci sunt servabile, nu
    # copy generic — dar rolul e același: un răspuns fără rezultate nu e o fundătură.
    if chips and ctx.reply is not None:
        ctx.reply.suggestions = list(chips)
    _attach_checkout_offer(ctx, run)


async def _generate_plan(
    ctx: TurnContext,
    deps: PipelineDeps,
    *,
    system: str,
    user: str,
    tools: list[dict[str, Any]],
    execute: Any,
    model: str | None = None,
    seed: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Bucla structurată; eșecul (JSON invalid/API) devine `(None, rounds)` — caller-ul repară."""
    try:
        with prompt_cache_scope(_cache_key(ctx)):
            raw, rounds = await deps.llm.run_tool_loop_structured(
                system, user, tools, execute, plan_schema_for_model(), model=model, seed=seed
            )
        return raw, rounds
    except Exception as e:  # noqa: BLE001 — model/JSON/API: vizibil, nu fatal (repair/fallback)
        log.warning("main_brain: bucla structurată a eșuat (%s)", type(e).__name__)
        return None, 0


def _serve_exhausted(ctx: TurnContext, run: ToolRun) -> None:
    """Ce spunem când planul s-a epuizat: faptele reale dacă le avem, refuzul onest dacă nu.

    Paritate cu v1 (`_finalize`): ACEEAȘI formă de răspuns, produsă din ACELEAȘI produse
    grounded. Fără asta, creierul unic răspundea „nu pot confirma recomandarea" cu retrieval-ul
    plin — o degradare vizibilă exact pe inputurile adversariale, unde v1 răspundea util.

    CARDURILE merg cu textul, și nu e cosmetică: `displayed_products` se scrie din
    `reply.products` (`worker/processor.py`), nu din `ctx.retrieval`. Fără ele, un tur servit din
    fallback lăsa starea GOALĂ, deci turul următor pierdea referința — „dă-mi linkul" după un
    „ceva mai ieftin" reușit nu mai avea la ce să se refere, iar clientul vedea produsul pe ecran
    și botul răspundea că nu știe despre care e vorba. Se atașează DOAR pe ramura grounded: pe
    refuzul onest textul nu numește niciun produs, iar niște carduri sub el ar fi un răspuns care
    se contrazice singur."""
    grounded = grounded_fallback_reply(run.retrieved)
    if grounded is not None:
        # Cardurile sunt EXACT produsele pe care le numește textul. Înainte, textul enumera 3
        # (`_deterministic_reply`) iar aici se atașau 6: clientul vedea șase carduri și o listă de
        # trei nume care nu explica de ce apar celelalte trei. Două plafoane independente peste
        # aceeași listă nu pot rămâne de acord, așa că acum există unul singur.
        text, named = grounded
        # `card_refs`, nu rândurile brute: retrievalul scrie `id`, randorul web citește
        # `product_id`, deci cardurile de fallback plecau la widget cu identitate NULL.
        # Starea scăpa (`_displayed_product_refs` are fallback pe `id`), sârma nu.
        ctx.set_reply(text, products=card_refs(named) or None, cacheable=False)
        return
    ctx.set_reply(safe_fallback(ctx.language), cacheable=False)


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


def _product_key(product: dict[str, Any]) -> str:
    """Id-ul rândului, sub ambele nume pe care le poartă produsele în drumul lor prin tool-uri."""
    return str(product.get("id") or product.get("product_id") or "")


def _evidence_block(evidence: Sequence[Any], *, header: str, limit: int) -> str:
    """Rândurile de evidence ca text, în SINGURUL format pe care îl vede modelul.

    Un singur proprietar al formei, fiindcă sunt două locuri care i-o arată (rezultatul de tool și
    promptul de repair) și două id-uri scrise diferit ar fi exact defectul pe care îl reparăm.

    Trunchierea se DECLARĂ: un digest tăiat în tăcere l-ar face să creadă că restul nu există și
    ar produce un plan invalid, din alt motiv."""
    usable = [item for item in evidence if item.current]
    rows = [
        f"{item.evidence_id} | {item.kind} | {item.product_id} | {item.value}"
        for item in usable[:limit]
    ]
    if not rows:
        return ""
    hidden = len(usable) - len(rows)
    tail = f"\n(+{hidden} nelistate, folosește doar id-urile de mai sus)" if hidden else ""
    return header + "\n".join(rows) + tail


def _evidence_digest(context: AnswerPlanContext, limit: int = MAX_REPAIR_EVIDENCE) -> str:
    """Evidence-ul deja colectat al turului, ca text scurt pentru repair.

    Repair-ul rulează pe `complete_schema`, adică în AFARA conversației în care s-au văzut
    rezultatele tool-urilor. Fără digestul ăsta i se cerea să citeze `evidence_ids` pe care nu le
    mai avea în față: singurele planuri reparabile erau cele care nu depindeau de evidence, adică
    aproape niciunul după o rundă de căutare — deci reparația era decorativă exact acolo unde
    conta. Îi dăm înapoi strict ce a validat deja serverul (id + tip + produs + valoare), nu
    payload brut de tool."""
    return _evidence_block(
        context.evidence,
        header="\nEVIDENCE DISPONIBIL (evidence_id | tip | product_id | valoare):\n",
        limit=limit,
    )


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
        """Rezultatul tool-ului, PLUS id-urile de evidence ale produselor pe care tocmai le-a adus.

        De ce aici: `build_answer_plan_context` rula abia DUPĂ bucla de tool-calling, deci în
        momentul în care modelului i se cerea planul nu văzuse niciun `evidence_id` — deși
        instrucțiunile îi cer să citeze „evidence_ids din evidence-ul serverului". Nu avea ce
        respecta, așa că le inventa (`search-1`, `search:<product_id>`), iar validatorul respingea
        planul cu `unknown_evidence`. Măsurat în producție: 6 din 6 id-uri emise la primul apel
        erau inventate, adică FIECARE tur cu produse plătea obligatoriu un repair, iar când și
        acela rata, clientul primea fallback-ul determinist în loc de recomandare.

        Calea v1 nu avea defectul (`_plan_prompt` pune evidence-ul în promptul de la primul apel);
        creierul unic l-a introdus. Blocul îl repune pe același traseu, dar acolo unde aparține pe
        calea structurată: lipit de rezultatul care l-a produs.

        Se atașează DOAR evidence-ul produselor NOI din acest apel: rândurile deja arătate sunt în
        conversație, iar relistarea lor ar crește cu fiecare rundă exact partea care e plătită de
        fiecare dată."""
        before = {_product_key(p) for p in self.run.retrieved}
        view = await self._dispatch(name, args)
        return self._with_evidence(view, before)

    def _with_evidence(self, view: str, before: set[str]) -> str:
        fresh: list[dict[str, Any]] = []
        seen = set(before)
        for product in self.run.retrieved:
            key = _product_key(product)
            if not key or key in seen:
                continue
            seen.add(key)
            fresh.append(product)
        if not fresh:
            return view
        # ACELAȘI constructor ca la validare, deci id-urile arătate sunt exact cele acceptate.
        # Un al doilea loc care le-ar compune după aceeași regulă ar putea diverge în tăcere.
        evidence = build_answer_plan_context(
            business_id=self.ctx.business.id,
            locale=self.ctx.language,
            products=fresh,
        ).evidence
        block = _evidence_block(
            evidence,
            header=(
                "\nEVIDENCE pentru produsele de mai sus (evidence_id | tip | product_id | valoare)."
                "\nCitează aceste id-uri în `claims`/`recommendations`: pentru fiecare produs ales,"
                " cel puțin un id AL LUI; dacă numești o variantă, și un id legat de acea variantă."
                " Nu inventa id-uri.\n"
            ),
            limit=MAX_TOOL_EVIDENCE,
        )
        return f"{view}\n{block}" if block else view

    async def _dispatch(self, name: str, args: dict[str, Any]) -> str:
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


async def _speculative_seed(
    ctx: TurnContext, profile: Any, execute: Any, query: str
) -> list[dict[str, Any]] | None:
    """NX-275 felia 6: căutarea rulată ÎNAINTE de primul apel, sau None.

    Trece prin ACELAȘI `execute` ca orice tool al buclei, deci prin portul NX-238, prin safety
    (NX-173) și prin admission/buget (NX-241). Nu e o scurtătură pe lângă porți, e aceeași cale
    chemată mai devreme.

    Emite `speculative_retrieval{outcome}` pe fiecare ramură de refuz, cu MOTIVUL: fără el, o felie
    stinsă de fapt (toate turele sar peste seed dintr-un motiv sau altul) ar arăta la fel ca una
    aprinsă care nu nimerește niciodată."""
    if not getattr(get_settings(), "speculative_retrieval_enabled", False):
        return None
    reason = speculative_retrieval.skip_reason(
        speculative_profile=bool(profile is not None and profile.speculative_retrieval),
        has_action=getattr(ctx, "action", None) is not None,
        has_anchor=bool(getattr(ctx, "resolved_reference", None)),
        is_pagination=bool(getattr(ctx, "show_more", False)),
        message=query,
        locale=ctx.language,
    )
    if reason is not None:
        ctx.emit("speculative_retrieval", outcome="skipped", reason=reason)
        return None
    messages, outcome = await speculative_retrieval.seed_messages(
        turn_id=ctx.turn_id, message=query, locale=ctx.language, execute=execute
    )
    if messages is None:
        ctx.emit("speculative_retrieval", outcome="skipped", reason=outcome)
    return messages


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
    forced_seed: list[dict[str, Any]] | None = None,
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
    pack = getattr(ctx.business, "domain_pack", None)
    registry = getattr(pack, "relation_kinds", None)
    families = tuple(getattr(getattr(pack, "routine_steps", None), "families", {}) or ())
    # NX-292: două flag-uri, două domenii. `TURN_PROFILES_ENABLED` aprinde toate cele cinci profile
    # (deci schimbă sufixul pentru TOT traficul, și se decide pe golden). `ROUTINE_ENABLED` aprinde
    # exclusiv profilul `routine` — singurul unde răspunsul de azi e garantat degradat, fiindcă
    # poarta `routine_evidence_required` cere o dovadă pe care nicio unealtă oferită n-o poate
    # produce. Ambele stinse ⇒ `profile is None` și nimic nu se schimbă.
    profiles_on = bool(getattr(settings, "turn_profiles_enabled", False))
    routine_on = bool(getattr(settings, "routine_enabled", False))
    profile = None
    if profiles_on or routine_on:
        candidate = turn_profile.select(turn_class, obligations, has_routine=bool(families))
        profile = candidate if profiles_on or candidate.name == "routine" else None
    if profile is not None:
        ctx.emit("turn_profile", name=profile.name, turn_class=turn_class.value)
        have = {s.get("function", {}).get("name") for s in tools}
        extra = [t for t in profile.extra_tools if t not in have]
        if extra:
            # Enumurile sunt ale TENANTULUI. La relații trimitem tipurile DECLARATE (nu doar
            # secvențele): o rutină poate avea nevoie și de un complement, iar un tip nedeclarat e
            # refuzat de tool oricum (`unknown_relation`).
            declared = tuple(getattr(registry, "specs", {})) if registry is not None else ()
            examples = vocab_examples.from_pack(pack)
            moments = tuple(getattr(getattr(pack, "routine_steps", None), "time_markers", {}) or ())
            tools = [*tools, *tool_schemas(extra, examples, declared, families, moments)]

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
    # Un seed FORȚAT bate speculația, și nu e o prioritate arbitrară: speculativul GHICEȘTE că
    # turul are nevoie de o căutare (poate rata, și atunci costă un apel în plus), pe când seed-ul
    # forțat poartă un set pe care CODUL l-a ales determinist și pentru care există o garanție de
    # respectat — azi «ceva mai ieftin» (`planner.resolve_cheaper_followup`), unde baseline-ul e
    # minimul a ceea ce clientul a VĂZUT. A lăsa speculativul să caute din nou peste el ar readuce
    # exact riscul pe care calea deterministă îl elimină: un set ales de model, care poate conține
    # produse mai scumpe decât ce s-a arătat deja.
    seed = forced_seed or await _speculative_seed(ctx, profile, execute, query)
    raw, rounds = await _generate_plan(
        ctx,
        deps,
        system=brain_system,
        user=brain_user,
        tools=tools,
        execute=execute,
        model=model,
        seed=seed,
    )
    ctx.emit("main_brain_tool_rounds_bucket", bucket=_rounds_bucket(rounds))
    if seed is not None and forced_seed is None:
        # HIT = modelul a produs planul din candidații seedați, fără să mai caute (rounds == 0),
        # adică am scos un apel întreg. MISS = a căutat din nou, deci seed-ul a costat un apel în
        # plus. Raportul dintre ele decide dacă felia merită: pragul e 43% (vezi design §6).
        #
        # Seed-ul FORȚAT nu intră în raportul ăsta: el n-a fost un pariu pe care să-l câștigăm sau
        # să-l pierdem, ci o căutare pe care codul trebuia s-o facă oricum. Numărat aici, ar fi
        # umflat rata de hit cu ture în care speculația nici n-a rulat — adică exact metrica pe
        # baza căreia se decide dacă felia 6 rămâne aprinsă.
        ctx.emit("speculative_retrieval", outcome="hit" if rounds == 0 else "miss")
    # NX-256: planul BRUT al modelului, înainte de validare — singurul loc unde există. Un plan
    # respins la validare e exact cazul pe care vrei să-l citești, iar el nu ajunge nici în
    # `ctx.answer_plan`, nici în reply.
    _trace(ctx, "brain_plan_raw", raw)

    # R3 — plasa de grounding pe produsele DEJA arătate. Trăia în `build_plan`, care nu mai rulează
    # pe calea asta, iar lipsa ei nu se vedea ca o eroare: bucla se termina fără produse, planul
    # cita ce avea clientul pe ecran, validatorul îl respingea ca `unknown_product` și turul ieșea
    # cu refuzul generic — exact pe follow-up-urile neclasificate („care e cea mai bună?"), unde v1
    # răspundea util. Rulează DOAR când bucla n-a adus nimic: un set proaspăt are întâietate, iar
    # rehidratarea nu are voie să dilueze o căutare reușită.
    if not run.retrieved and ctx.state.displayed_products:
        from src.agent.planner import rehydrate_displayed  # noqa: PLC0415 — evită ciclul

        recovered = await rehydrate_displayed(ctx, deps, policy=SafetyPolicy.for_turn(ctx))
        if recovered:
            run.retrieved.extend(recovered)
            ctx.emit("brain_rehydrated_displayed", n=len(recovered))

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
        _serve_exhausted(ctx, run)
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
        _serve_exhausted(ctx, run)
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
        _serve_exhausted(ctx, run)
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
            _serve_exhausted(ctx, run)
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
            _serve_exhausted(ctx, run)
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
            _serve_exhausted(ctx, run)
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
    await _set_brain_reply(ctx, deps, plan, run, text)
    if ask_clarification:
        _persist_clarification(ctx, plan)


__all__ = [
    "BRAIN_PROMPT_VERSION",
    "MAX_REPAIRS",
    "brain_versions",
    "render_plan_text",
    "run_main_brain",
]
