"""Faza F — render + guard + recover (NX-144 felia 1a). Extras din `agent.py`.

Transformă rezultatul buclei (text brut al modelului / produse retrievate) în răspunsul final:
  • `_finalize`          — proză SALES: validează (preț/link) → 1 retry cu feedback → fallback.
  • `_finalize_grounded` — status comandă (fapte grounded): validează → retry → fallback sigur.
  • `_finalize_rich`     — recomandare STRUCTURATĂ (model iZi): apel structurat → `assemble`.

`_rich_bundle`/`_rich_facets` construiesc inputul rich; `_no_result_msg` = fallback per-rută.
Grounding-ul rămâne la `validator` (P2: modelul propune, codul dispune); textele deterministe la
`fallbacks`; flattening-ul la `compose`. Aici trăiește DOAR regia render→validate→recover.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from src.agent import prompt_builder
from src.agent.brain_rich import rich_from_facts
from src.agent.compare_narrative import compose_comparison
from src.agent.deterministic import _comparison_facets
from src.agent.fallbacks import (
    _card_products,
    _checkout_label,
    _compare_chips,
    _deterministic_reply,
    _products_brief,
    _thin_path_chips,
)
from src.agent.validator import (
    ValidationResult,
    _allowed_prices,
    _bad_bare_numbers,
    _claims_ok,
    _stock_claim_ok,
    _valid,
    validate_prose,
)
from src.analytics.demand import clean_ids, product_ids_from_dicts
from src.config import card_slots, chip_slots, get_settings
from src.models import MAX_OFFERED_CHIPS, Offer, RichReply, TurnContext
from src.web.localization import amount_text
from src.worker import compose
from src.worker.order_gate import login_required_for_ctx, web_unidentified

if TYPE_CHECKING:
    from src.agent.planner import ResponsePlan
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# Schema strict pentru `complete_schema` (mini-ul folosește deja strict:true în tool-uri).
# NB: fără maxItems/minimum — keyword-uri nesuportate de structured outputs strict; capul (6) și
# range-ul pro_index se impun în compose.
_RICH_SCHEMA: dict[str, Any] = {
    "name": "sales_recommendation",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["intro", "items", "pick", "education", "suggestions"],
        "properties": {
            "intro": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["product_id", "pro_index", "fit_clause"],
                    "properties": {
                        "product_id": {"type": "string"},
                        "pro_index": {"type": "integer"},
                        "fit_clause": {"type": "string"},
                    },
                },
            },
            "pick": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["product_id", "justification"],
                "properties": {
                    "product_id": {"type": "string"},
                    "justification": {"type": "string"},
                },
            },
            "education": {"type": ["string", "null"]},
            # Mesaje de follow-up din partea CLIENTULUI (voce de client → fără scrub, contextuale).
            "suggestions": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    },
}


def _with_question(base: dict[str, Any]) -> dict[str, Any]:
    return {
        **base,
        "schema": {
            **base["schema"],
            "required": [*base["schema"]["required"], "question"],
            "properties": {
                **base["schema"]["properties"],
                "question": {"type": ["string", "null"]},
            },
        },
    }


#: NX-315 felia 2: aceeași schemă plus câmpul întrebării de îngustare. Se folosește DOAR pe turele
#: cu ofertă, deci orice alt tur trimite exact `_RICH_SCHEMA` (byte-identic). Două obiecte, nu o
#: schemă mutată pe loc: `_RICH_SCHEMA` e o constantă de modul folosită concurent de toate turele.
_RICH_SCHEMA_WITH_QUESTION: dict[str, Any] = _with_question(_RICH_SCHEMA)

#: Câmpurile de SCHEMĂ dintre omisiunile lui `prompt_builder.RICH_OMITTABLE` (`categories` e doar în
#: system, nu e un câmp de output).
_RICH_SCHEMA_FIELDS = frozenset({"pick", "suggestions"})

_slim_logged = False


def rich_omissions(settings: Any = None) -> frozenset[str]:
    """NX-312 felia 4 — ce NU mai cere apelul de compunere bogată, derivat din configurație.

    Un câmp iese doar când ce produce modelul pe el e aruncat oricum: `suggestions` când mutările
    de chip le suprascriu (`chip_moves_v1_enabled`), `pick` când linia nu se arată pe niciun canal
    (`rich_pick_web_enabled=false`, citit de `compose.flatten` și `compose.flatten_framing`,
    singurii care îl transformă în text). `categories` iese mereu: modelul alege dintr-o listă
    fixă de produse. Aceeași mulțime pleacă spre system (`build_rich_system(omit=)`) și spre schemă
    (`_rich_schema`), deci promptul nu poate numi un câmp pe care schema nu-l are.

    Flagul stins ⇒ mulțimea vidă ⇒ schema și system-ul de dinainte, byte-identice. Constantă per
    proces în producție (flagurile se citesc o dată), deci prefixul de cache nu variază pe ture."""
    global _slim_logged
    s = settings or get_settings()
    if not getattr(s, "rich_schema_slim_enabled", False):
        return frozenset()
    omit = {"categories"}
    if getattr(s, "chip_moves_v1_enabled", False):
        omit.add("suggestions")
    if not getattr(s, "rich_pick_web_enabled", False):
        omit.add("pick")
    out = frozenset(omit)
    if not _slim_logged:
        # Configurație, nu comportament: o dată per proces, în log, nu un eveniment per tur.
        log.info("rich_schema_slim removed=%s", sorted(out))
        _slim_logged = True
    return out


@lru_cache(maxsize=16)
def _rich_schema(omit: frozenset[str] = frozenset(), *, question: bool = False) -> dict[str, Any]:
    """Schema apelului de compunere, fără câmpurile din `omit`. Fără omisiuni întoarce CHIAR
    constantele de modul (identitate, nu doar egalitate). Cache-uit: aceleași omisiuni ⇒ același
    obiect, deci nimeni nu construiește o schemă nouă pe fiecare tur."""
    drop = omit & _RICH_SCHEMA_FIELDS
    if not drop:
        return _RICH_SCHEMA_WITH_QUESTION if question else _RICH_SCHEMA
    body = _RICH_SCHEMA["schema"]
    slim = {
        **_RICH_SCHEMA,
        "schema": {
            **body,
            "required": [k for k in body["required"] if k not in drop],
            "properties": {k: v for k, v in body["properties"].items() if k not in drop},
        },
    }
    return _with_question(slim) if question else slim


async def _finalize(
    llm,
    reco_system: str,
    query: str,
    text: str,
    products: list[dict[str, Any]],
    language: str,
    history: str,
    allowed_links: set[str] | None = None,
    allowed_prices: set[float] | None = None,
    *,
    recompose: bool = True,
) -> tuple[str, ValidationResult]:
    """Validează textul final (preț + link). Invalid → 1 retry (recompune din produse cu
    prețuri permise) → fallback determinist. Invariantul: zero prețuri/linkuri inventate.

    NX-312: `recompose=False` + text GOL ⇒ direct fallback-ul determinist, fără retry. Textul
    lipsește atunci deliberat (runda de proză sărită), iar apelantul tocmai a pierdut apelul rich:
    un al doilea apel de model, posibil după un timeout, ar fi latență plătită pentru o frază pe
    care rezerva din catalog o dă oricum. Un text PREZENT și invalid se recompune ca înainte
    (adevărul nu se negociază).
    `reco_system` = system-ul de recompunere generat din DB (NX-78). `allowed_links`/
    `allowed_prices` = linkuri/sume grounded de bot (checkout_link/check_order). Întoarce textul
    servit ÎMPREUNĂ cu `ValidationResult` (NX-146 felia 2 fix — corelat în `agent_prompt` pt
    Turn Replay): pe fallback determinist, `reasons` arată DE CE a picat proza LLM-ului.

    Poarta de trecere/eșec rămâne `_valid` (shim monkeypatch-uit de proba anti-teatru NX-121 —
    `test_golden.test_injection_case_fails_without_its_guard`); `validate_prose` se cheamă
    SEPARAT doar pe calea de eșec, ca să raporteze motivele fără să schimbe gating-ul testat."""
    if text and _valid(text, products, allowed_links, allowed_prices):
        return text, ValidationResult(ok=True, reasons=[])
    if not text and not recompose:
        return _deterministic_reply(products), ValidationResult(ok=False, reasons=["empty_text"])

    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    prices = _allowed_prices(products) + sorted(allowed_prices or set())
    allowed = ", ".join(f"{amount_text(p, language)} lei" for p in prices)
    user = (
        f"Limba clientului: {language}\n{history_block}"
        f"Întrebare: {query}\nProduse:\n{_products_brief(products, language)}\n\n"
        f"FOLOSEȘTE EXACT doar aceste prețuri: {allowed}. Niciun alt preț, niciun link inventat."
    )
    try:
        reply2 = await llm.complete(reco_system, user)
    except Exception as e:  # noqa: BLE001 — retry eșuat → fallback determinist
        log.warning("agent: retry compunere eșuat (%s)", type(e).__name__)
        reply2 = ""
    if reply2 and _valid(reply2, products, allowed_links, allowed_prices):
        return reply2, ValidationResult(ok=True, reasons=[])

    log.warning("agent: validator a eșuat → fallback determinist")
    failed_text = reply2 or text
    reasons = (
        validate_prose(
            failed_text,
            products=products,
            generated_links=allowed_links,
            grounded_prices=allowed_prices,
        ).reasons
        if failed_text
        else ["empty_text"]
    )
    return _deterministic_reply(products), ValidationResult(ok=False, reasons=reasons)


async def _finalize_grounded(
    llm,
    text: str,
    facts: str,
    language: str,
    allowed_links: set[str],
    allowed_prices: set[float],
) -> tuple[str, ValidationResult]:
    """Cale fără produse, dar cu date grounded (status comandă): validează textul; invalid →
    1 retry order-shaped (din `facts` + sume permise) → fallback SIGUR (non-tăcere, fără numere,
    NU forma de produs `_deterministic_reply`). Întoarce textul servit + `ValidationResult`
    (NX-146 felia 2 fix). Gating pe `_valid` (monkeypatch-uit de proba NX-121), motivele de eșec
    raportate separat prin `validate_prose` — vezi docstring-ul `_finalize`."""
    # NX-117: ORDER → fără claims-check (faptele de livrare/stoc din check_order sunt grounded).
    if text and _valid(
        text, [], allowed_links, allowed_prices, check_bare=False, check_claims=False
    ):
        return text, ValidationResult(ok=True, reasons=[])

    allowed = (
        ", ".join(f"{amount_text(p, language)} lei" for p in sorted(allowed_prices))
        or "(fără sume)"
    )
    user = (
        f"Limba clientului: {language}\nDate comandă:\n{facts}\n\n"
        f"FOLOSEȘTE EXACT doar aceste sume: {allowed}. Niciun alt număr, AWB sau link inventat."
    )
    try:
        reply2 = await llm.complete(prompt_builder.ORDER_RECO_SYSTEM, user)
    except Exception as e:  # noqa: BLE001 — retry eșuat → fallback sigur
        log.warning("agent: retry status comandă eșuat (%s)", type(e).__name__)
        reply2 = ""
    if reply2 and _valid(
        reply2, [], allowed_links, allowed_prices, check_bare=False, check_claims=False
    ):
        return reply2, ValidationResult(ok=True, reasons=[])

    log.warning("agent: validator status comandă a eșuat → fallback sigur")
    failed_text = reply2 or text
    reasons = (
        validate_prose(
            failed_text,
            products=[],
            generated_links=allowed_links,
            grounded_prices=allowed_prices,
            check_bare=False,
            check_claims=False,
        ).reasons
        if failed_text
        else ["empty_text"]
    )
    return (
        "Ți-am verificat comanda 🙂 Îți confirm imediat detaliile exacte, revin la tine.",
        ValidationResult(ok=False, reasons=reasons),
    )


def _no_result_msg(is_order: bool) -> str:
    if is_order:
        return "N-am găsit nicio comandă pe contul tău. Îmi dai numărul comenzii?"
    return (
        "Momentan n-am găsit produse potrivite. Îmi spui mai exact ce cauți (tip de produs, buget)?"
    )


def _attach_no_result_alternatives(ctx: TurnContext) -> None:
    """NX-159 felia 2: pe un no-result de SALES, mesajul are deja o întrebare, dar atașăm chips
    deterministe cu căi CONCRETE de continuare (popular / alt buget / altă categorie) → nu fundătură
    generică. DOAR sales (order-ul are propriul flux de câmp cerut). Gated + best-effort."""
    if ctx.reply is None or not get_settings().no_result_alternatives_enabled:
        return
    ctx.reply.suggestions = _thin_path_chips(ctx.language)


def _rich_facets(ctx: TurnContext) -> tuple:
    """Tier 2b: fațetele de domeniu (DomainPack.comparison_facets) pentru BUNDLE-ul rich, gated de
    kill-switch propriu. OFF / fără pack → () → bundle ca înainte (doar descriere)."""
    if not get_settings().rich_facets_enabled:
        return ()
    pack = getattr(ctx.business, "domain_pack", None)
    return pack.comparison_facets if pack else ()


def _rich_bundle(
    products: list[dict[str, Any]], facets: tuple = (), language: str | None = None
) -> str:
    """Lista de produse pentru apelul structurat: id + preț + rating + avantaje INDEXATE
    (pentru `pro_index`) + DESCRIERE (ai_summary) + FAȚETE (Tier 2b). Modelul VEDE prețul (ca să
    ordoneze/aleagă) dar NU-l emite.

    PR-3 (IZI-parity consultativ): `descriere` aduce caracteristicile REALE ale produsului
    (componente cheie / ingrediente / pentru ce ten/uz, ce conține ai_summary-ul) ca modelul să
    scrie un fit SPECIFIC („cu acid hialuronic, pentru ten uscat"), NU tautologic („hidratant care
    hidratează"). Tier 2b: `fațete` aduce ACELEAȘI atribute STRUCTURATE ca tabelul de comparație
    (Ingrediente cheie/Beneficiu/Potrivit pentru din `attributes`) — mai precise decât proza
    din ai_summary („ingrediente TIPICE precum") → fit grounded pe ce e REAL în formulă. GENERIC pe
    vertical (config DomainPack). Gol (date sărace / fără fațete) → degradare lină."""
    lines = []
    for p in products:
        raw = p.get("top_pros") or ([p["review_pro"]] if p.get("review_pro") else [])
        pros = [s.strip() for s in raw if isinstance(s, str) and s.strip()][:3]
        pros_str = "; ".join(f"{i}) {pr}" for i, pr in enumerate(pros)) or "(fără avantaje listate)"
        # DETALII (IZI): minusurile reale → modelul poate scrie un AVERTISMENT onest grounded
        # («de luat în calcul») în deep-dive-ul de produs. Prezent mai ales pe get_product_details
        # (get_products_by_ids întoarce top_cons); pe listă (search) e des absent → fără linie.
        raw_cons = p.get("top_cons") or []
        cons = [s.strip() for s in raw_cons if isinstance(s, str) and s.strip()][:2]
        cons_str = f" | de_luat_in_calcul: {'; '.join(cons)}" if cons else ""
        rating = f"{float(p['rating']):.1f}★" if p.get("rating") else "-"
        desc = " ".join((p.get("ai_summary") or "").split())[:160]
        desc_str = f" | descriere: {desc}" if desc else ""
        fac = compose.facet_summary(p, facets, language) if facets else ""
        fac_str = f" | fațete: {fac}" if fac else ""
        lines.append(
            f"[{p['id']}] {p['name']} | preț {amount_text(p['price'], language)} lei | "
            f"rating {rating} | avantaje: {pros_str}{cons_str}{desc_str}{fac_str}"
        )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _TurnShape:
    """NX-315 — ce FORMĂ cere turul ăsta compunerii rich, decisă de server înaintea apelului.

    `block` = liniile în plus din mesajul turului (gol ⇒ mesajul e byte-identic cu înainte).
    Restul sunt ce trebuie verificat DUPĂ apel: dacă slotul cerut a ieșit, dacă întrebarea e pe
    fațeta oferită. Valoare locală a stagiului, nu câmp pe `TurnContext` (P3)."""

    block: str = ""
    guidance_required: bool = False
    offer: Any = None  # `NarrowingOffer | None`
    offer_phrases: tuple[str, ...] = ()
    offer_reason: str | None = None  # de ce NU s-a oferit (vocabular închis), None dacă s-a oferit
    grounded_numbers: frozenset[str] = frozenset()


def _known_facets(ctx: TurnContext, pack: Any) -> frozenset[str]:
    """Fațetele pe care clientul ni le-a spus deja, deci pe care nu le mai întrebăm.

    Două surse, amândouă ale CLIENTULUI, nu ale modelului: constrângerile turului (cheile stivei
    NX-133) și ce a SCRIS el, în turul ăsta sau într-unul recent. Pentru a doua, frazele vin din
    pachet (aliasurile fațetei + harta de nevoi, pe valorile fațetei), iar potrivirea e
    `uttered_by_client` (NX-299), deci nicio listă de cuvinte românești aici (P11). Un fals
    „știut" costă o întrebare nepusă, adică exact comportamentul de azi; un fals „neștiut" l-ar
    pune pe client să repete, deci dubiul merge spre „știut"."""
    from src.conversation.state_v2 import active_needs  # noqa: PLC0415
    from src.tools.catalog_tools import uttered_by_client  # noqa: PLC0415

    known: set[str] = set()
    for source in (
        getattr(ctx.state, "search_constraints", None),
        getattr(ctx.state, "constraints", None),
    ):
        if isinstance(source, dict):
            known |= {str(k) for k, v in source.items() if v not in (None, "", [], {})}
    known |= {str(n.key) for n in active_needs(ctx)}
    concern_map = getattr(pack, "concern_map", None) or {}
    for facet in getattr(pack, "facets", ()) or ():
        if getattr(facet, "binding", "") != "partitioning" or facet.key in known:
            continue
        values = set(getattr(facet, "values", ()) or ())
        phrases = [a for a, target in (getattr(facet, "aliases", None) or {}).items()]
        phrases += [a for a, target in concern_map.items() if target in values]
        phrases += [v.replace("_", " ") for v in values if v.isalpha() and len(v) >= 4]
        if phrases and uttered_by_client(ctx, *phrases):
            known.add(facet.key)
    return frozenset(known)


async def _turn_shape(
    ctx: TurnContext, deps: PipelineDeps, products: list[dict[str, Any]]
) -> _TurnShape:
    """NX-315 — decide, înaintea apelului rich, ce formă cere turul. Toate flagurile OFF ⇒
    `_TurnShape()` gol, adică mesaj și schemă byte-identice.

    Trei felii, fiecare pe obligația ei (cardul: „forma e azi aceeași indiferent ce a cerut
    clientul"): `recommend` primește „cum alegi" obligatoriu și, eventual, o întrebare de
    îngustare; `explain` (profilul `howto`) primește instrucțiunile magazinului și forma
    „răspunsul întâi". O rutină are regulile ei (`_ROUTINE_RICH_RULES`) și nu se atinge.

    Best-effort: orice eșec întoarce ce s-a calculat până atunci, fiindcă o formă mai săracă nu e
    un motiv să pierzi răspunsul (P6)."""
    from src.agent import answer_shape, turn_profile  # noqa: PLC0415

    s = get_settings()
    guidance_on = bool(getattr(s, "guidance_required_enabled", False))
    narrowing_on = bool(getattr(s, "narrowing_question_enabled", False))
    howto_on = bool(getattr(s, "howto_from_catalog_enabled", False))
    if not (guidance_on or narrowing_on or howto_on) or not products:
        return _TurnShape()

    pack = getattr(ctx.business, "domain_pack", None)
    profile = turn_profile.name_for_turn(ctx)
    routine = getattr(ctx, "routine", None) is not None
    lines: list[str] = []
    guidance_required = False
    offer = None
    phrases: tuple[str, ...] = ()
    offer_reason: str | None = None
    grounded: frozenset[str] = frozenset()

    if profile == "recommend" and not routine:
        if guidance_on:
            facets = tuple(getattr(pack, "comparison_facets", ()) or ()) if pack else ()
            axes = compose.decision_axes(products, facets, ctx.language)
            shape = answer_shape.shape_for(
                n_items=min(len(products), card_slots()),
                product_types=answer_shape.distinct_types(products),
                n_axes=len(axes),
            )
            directive = (
                answer_shape.guidance_directive(answer_shape.axis_names(axes))
                if answer_shape.SLOT_CLOSING in shape.required
                else None
            )
            if directive:
                lines.append(directive)
                guidance_required = True
        if narrowing_on:
            offer, phrases, offer_reason = await _narrowing_offer(ctx, deps, pack, products)
            if offer is not None:
                label = next(
                    (
                        f.label(ctx.language, fallback_locale=None)
                        for f in (getattr(pack, "facets", ()) or ())
                        if f.key == offer.facet
                    ),
                    offer.facet,
                )
                directive = answer_shape.narrowing_directive(label, phrases)
                if directive:
                    lines.append(directive)
                else:
                    offer, phrases, offer_reason = None, (), "unphrasable"
    elif profile == "howto" and howto_on:
        product = products[0]
        instructions, reason = answer_shape.howto_instructions(product, pack)
        ctx.emit("howto_instructions", reason=reason)
        if reason in ("instructions", "no_instructions"):
            lines.append(answer_shape.howto_directive(str(product.get("id") or ""), instructions))
            if instructions:
                import re  # noqa: PLC0415

                grounded = frozenset(re.findall(r"\d+", instructions))

    return _TurnShape(
        block="\n".join(lines) + "\n" if lines else "",
        guidance_required=guidance_required,
        offer=offer,
        offer_phrases=phrases,
        offer_reason=offer_reason,
        grounded_numbers=grounded,
    )


async def _narrowing_offer(
    ctx: TurnContext, deps: PipelineDeps, pack: Any, products: list[dict[str, Any]]
) -> tuple[Any, tuple[str, ...], str | None]:
    """`(ofertă, frazele opțiunilor, motivul refuzului)`. Alegerea e PURĂ
    (`narrowing_candidate`); aici se adaugă doar frazele, din meniul închis NX-295, cu round-trip.
    Vocabular indisponibil ⇒ nicio ofertă (fail-open: exact turul de azi)."""
    from src.catalog.clarify_menu import value_phrases  # noqa: PLC0415
    from src.catalog.vocabulary_cache import get_vocabulary  # noqa: PLC0415
    from src.conversation.clarification_policy import narrowing_candidate  # noqa: PLC0415

    if pack is None:
        return None, (), "no_partitioning_facet"
    asked = frozenset(str(k) for k in (getattr(ctx.state, "asked_intents", None) or ()))
    verdict = narrowing_candidate(
        tuple(getattr(pack, "facets", ()) or ()),
        products,
        known=_known_facets(ctx, pack),
        asked=asked,
    )
    if verdict.offer is None:
        return None, (), verdict.reason
    try:
        vocab = await get_vocabulary(deps, ctx.business.id)
    except Exception:  # noqa: BLE001 — fără vocabular nu există fraze verificate
        log.warning("finalize: vocabular indisponibil pentru oferta de îngustare")
        return None, (), "vocabulary_unavailable"
    phrased = value_phrases(
        vocab, pack, verdict.offer.facet, verdict.offer.values, locale=ctx.language or "ro"
    )
    phrases = tuple(phrased[v] for v in verdict.offer.values if v in phrased)
    if len(phrases) < 2:
        return None, (), "unphrasable"
    return verdict.offer, phrases, None


def _apply_turn_shape(
    ctx: TurnContext, rich: RichReply, j: dict[str, Any], shape: _TurnShape
) -> None:
    """NX-315 — verifică, DUPĂ apel, ce a cerut `_turn_shape`. Nu scrie text nou: doar păstrează
    sau scoate ce a scris modelul, iar eșecul are nume și cod.

    • „cum alegi" cerut și absent ⇒ `guidance_dropped` (`missing`: modelul n-a scris; `scrubbed`:
      a scris, dar scrub-ul l-a omorât propoziție cu propoziție). Fără retry: slotul e util, nu
      obligatoriu pentru adevăr, iar o rundă în plus costă secunde pe care clientul le simte.
    • întrebarea de îngustare pleacă doar dacă numește opțiunile OFERITE și trece scrub-ul de
      propoziție. Atunci devine ultima frază a `intro`-ului (contractul v1 n-are alt loc, iar
      iZi o pune exact acolo: înaintea cardurilor), iar alte întrebări din `intro`/`education` se
      scot, ca turul să aibă cel mult una.
    """
    from src.agent import answer_shape  # noqa: PLC0415

    if shape.guidance_required and not rich.education:
        raw = j.get("education")
        reason = "scrubbed" if isinstance(raw, str) and raw.strip() else "missing"
        ctx.emit("guidance_dropped", reason=reason)

    offer = shape.offer
    if offer is None:
        if shape.offer_reason is not None:
            ctx.emit(
                "narrowing_offer",
                facet=None,
                values_in_set=0,
                gain=0.0,
                asked=False,
                rejected_reason=shape.offer_reason,
            )
        return
    question, reason = answer_shape.judge_question(j.get("question"), shape.offer_phrases)
    if question is not None:
        safe = compose.scrub_sentence(question, compose._allowed_client_numbers(ctx))
        if safe is None:
            question, reason = None, "unsafe"
        else:
            question = safe
    if question is not None:
        intro = compose.strip_questions(rich.intro)
        rich.intro = f"{intro} {question}" if intro else question
        rich.education = compose.strip_questions(rich.education)
        asked = [k for k in (ctx.state.asked_intents or []) if k != offer.facet]
        ctx.state.asked_intents[:] = [*asked, offer.facet][-8:]
    ctx.emit(
        "narrowing_offer",
        facet=offer.facet,
        values_in_set=len(offer.values),
        gain=round(float(offer.gain), 2),
        asked=question is not None,
        rejected_reason=reason,
    )


@dataclass(frozen=True, slots=True)
class _RichOutcome:
    """Ce a produs apelul structurat — și, când n-a produs carduri, DE CE.

    `model_items` e numărul de produse pe care le-a NUMIT modelul, înainte de poarta de
    apartenență. Fără el, două eșecuri diferite arătau identic în analytics: „modelul a emis
    id-uri străine de retrieval" (defect de model, produsele sunt bune) și „modelul n-a selectat
    niciun produs" (refuz deliberat: pe 2026-09-16 a refuzat, corect, un set de farduri de obraz
    la o cerere de produse de păr). Ambele se etichetau `all-items-dropped-by-membership`, deși în
    al doilea caz nu se dropase nimic — deci nu se putea număra nici măcar cât de des se întâmplă.
    """

    reply: RichReply | None
    model_items: int = 0


def _drop_dead_moves(
    ctx, candidates: list, *, n_cards: int, obligation_kinds=None, cards=()
) -> tuple[list, int]:
    """NX-316: aceeași regulă pe AMBELE căi care construiesc chips (v1 aici, creierul unic în
    `brain._turn_chips`).

    Chips-urile MOARTE (nevoie rostită, valoare pe toate cardurile, detaliu pe un card) se scot
    sub `CHIP_DROP_DEAD_ENABLED` (ON), fiindcă sunt risipă măsurată, nu o capabilitate de câștigat
    pe golden. Coborârea lui `pivot_shelf` (`obligation_kinds`, felia 3) rămâne sub
    `CHIP_MOVES_V2_ENABLED`. Ambele stinse ⇒ lista neatinsă. `cards` = cardurile afișate CU
    `attributes`; primul tur al subiectului vine din starea v1 (`subject_is_new`)."""
    s = get_settings()
    v2 = bool(getattr(s, "chip_moves_v2_enabled", False))
    if not v2 and not getattr(s, "chip_drop_dead_enabled", False):
        return candidates, 0
    from src.conversation import chip_moves  # noqa: PLC0415
    from src.conversation.subject import spoken_needs, subject_is_new  # noqa: PLC0415

    return chip_moves.drop_dead(
        candidates,
        spoken_needs=spoken_needs(ctx.state),
        n_cards=n_cards,
        obligation_kinds=obligation_kinds if v2 else None,
        first_subject_turn=subject_is_new(ctx.state, ctx.turn_id),
        cards=cards,
    )


def _role_order(obligation_kinds) -> tuple[str, ...]:
    """`roles_for` cu ordinea feliei 3 pe explain/answer DOAR sub `CHIP_MOVES_V2_ENABLED`."""
    from src.conversation import chip_moves  # noqa: PLC0415

    return chip_moves.roles_for(
        list(obligation_kinds),
        graph_lateral=bool(getattr(get_settings(), "chip_moves_v2_enabled", False)),
    )


async def _relation_moves(ctx, deps, cards: list[dict[str, Any]]) -> tuple[list, dict[str, int]]:
    """NX-316 felia 3: `routine_next` + `similar_to` din graful de relații, pe AMBELE căi.

    UN query agregat pe ancorele afișate (`relation_type_counts`), într-un checkout SCURT
    `chip_relations`, DUPĂ apelul de model (NX-231: nicio conexiune ținută peste model). Frazele
    tipurilor vin din vocabular (cache-uit), doar pentru produsul discutat. Query sau vocabular
    picat ⇒ `([], {"relations_error": 1})`: mutările din graf lipsesc, restul chips-urilor se emit
    (fail-open), iar `chip_moves` numără eroarea. Flag stins ⇒ `([], {})` fără nicio citire."""
    if not getattr(get_settings(), "chip_moves_v2_enabled", False) or not cards:
        return [], {}
    from src.catalog.clarify_menu import value_phrases  # noqa: PLC0415
    from src.catalog.vocabulary_cache import get_vocabulary  # noqa: PLC0415
    from src.conversation import chip_moves  # noqa: PLC0415
    from src.db.queries.catalog import relation_type_counts  # noqa: PLC0415

    ids = [
        str(c.get("product_id") or c.get("id")) for c in cards if c.get("product_id") or c.get("id")
    ]
    try:
        async with deps.db("chip_relations") as conn:
            rows = await relation_type_counts(conn, ctx.business.id, ids)
        pack = getattr(ctx.business, "domain_pack", None)
        types = chip_moves.relation_types_wanted(rows, cards)
        phrases: dict[str, str] = {}
        if types:
            vocab = await get_vocabulary(deps, ctx.business.id)
            phrases = value_phrases(
                vocab, pack, chip_moves.PRODUCT_TYPE_DIMENSION, types, locale=ctx.language or "ro"
            )
        moves = chip_moves.from_relations(
            rows,
            cards,
            phrases,
            offered_before=tuple(getattr(ctx.state, "offered_chips", ()) or ()),
            unique_anchor=getattr(get_settings(), "unique_name_prefix_enabled", False),
            locale=ctx.language,
        )
        return chip_moves.renderable(moves, pack, ctx.language), {}
    except Exception as e:  # noqa: BLE001 — chips-urile nu sunt răspunsul (P6)
        log.warning("finalize: mutările din graf au eșuat (%s)", type(e).__name__)
        return [], {"relations_error": 1}


async def _facet_moves(ctx, deps, cards: list[dict[str, Any]]) -> list:
    """NX-316 felia 2: mutările `choose_within` + `fit_question` pe setul AFIȘAT, pe AMBELE căi
    (v1 aici, creierul unic în `brain._set_brain_reply`). `cards` trebuie să poarte `attributes`.

    Fațetele „știute" sunt aceleași ca la întrebarea de îngustare (`_known_facets`), deci
    chips-urile nu propun alegerea pe o fațetă pe care clientul a spus-o deja. Singura citire e
    vocabularul (cache-uit per tenant), și doar când planul are ce fraza. Best-effort: orice eșec
    ⇒ `[]`, adică chips-urile de dinainte (P6). Flag stins ⇒ `[]` fără nicio muncă (byte-identic).
    """
    if not getattr(get_settings(), "chip_moves_v2_enabled", False) or not cards:
        return []
    try:
        from src.catalog.clarify_menu import value_phrases  # noqa: PLC0415
        from src.catalog.vocabulary_cache import get_vocabulary  # noqa: PLC0415
        from src.conversation import chip_moves  # noqa: PLC0415
        from src.conversation.subject import spoken_needs  # noqa: PLC0415

        pack = getattr(ctx.business, "domain_pack", None)
        facets = tuple(getattr(pack, "facets", ()) or ())
        plan = chip_moves.plan_facets(
            cards, facets, known=_known_facets(ctx, pack), spoken=spoken_needs(ctx.state)
        )
        wanted = plan.wanted()
        if not wanted:
            return []
        vocab = await get_vocabulary(deps, ctx.business.id)
        phrases = {
            (facet, key): phrase
            for facet, keys in wanted.items()
            for key, phrase in value_phrases(
                vocab, pack, facet, keys, locale=ctx.language or "ro"
            ).items()
        }
        moves = chip_moves.from_facets(
            cards,
            plan,
            facets,
            phrases,
            offered_before=tuple(getattr(ctx.state, "offered_chips", ()) or ()),
            unique_anchor=getattr(get_settings(), "unique_name_prefix_enabled", False),
            locale=ctx.language,
        )
        return chip_moves.renderable(moves, pack, ctx.language)
    except Exception as e:  # noqa: BLE001 — chips-urile nu sunt răspunsul (P6)
        log.warning("finalize: mutările pe fațete au eșuat (%s)", type(e).__name__)
        return []


def _cards_with_attributes(
    cards: list[dict[str, Any]], products: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Cardurile turului (ordinea de pe ecran) cu `attributes` din setul de retrieval."""
    by_id = {str(p.get("id") or p.get("product_id") or ""): p for p in products or ()}
    return [
        {**c, "attributes": (by_id.get(str(c.get("product_id"))) or {}).get("attributes") or {}}
        for c in cards
    ]


async def _apply_move_chips(ctx, deps, rich) -> None:
    """NX-297 felia 5 — chips-urile v1 devin MUTĂRI cu dovadă (NX-296), nu fraze scrise liber.

    Pe v1 textul chip-ului ESTE comanda: apăsarea îl retrimite ca mesaj NOU al clientului. Scris
    liber de modelul rich, putea numi orice — exact producătorul pe care `CHIP_PRODUCERS` îl
    declară NEANCORAT, și clasa de defect măsurată la NX-295 («Pentru consola, cablu USB de date»
    într-un magazin de cosmetice).

    Sursa devine aceeași ca pe calea creierului unic: meniul ÎNCHIS al catalogului plus cardurile
    turului. Diferența e că aici nu există `chip_labels` — modelul n-are unde să reformuleze — deci
    fiecare mutare iese cu ȘABLONUL tenantului. Nu e o degradare, e ramura fail-OPEN a lui NX-296:
    „modelul tace ⇒ rămâne șablonul". Textul e ancorat prin construcție.

    Rolurile vin din obligațiile DETERMINISTE ale mesajului (`extract_obligations`, cod pur), nu
    dintr-un plan: pe un tur factual n-ai nevoie de cinci îngustări, ai nevoie de continuări pe
    produsul discutat.

    Best-effort: orice eșec lasă chips-urile compuse de `compose`, adică exact comportamentul de
    azi. Un chip mai puțin nu e un motiv să pierzi răspunsul (P6).
    """
    if not getattr(get_settings(), "chip_moves_v1_enabled", False):
        return
    try:
        from src.agent.brain_models import extract_obligations  # noqa: PLC0415 — evită ciclul
        from src.catalog.clarify_menu import menu_for_turn  # noqa: PLC0415
        from src.conversation import chip_moves  # noqa: PLC0415

        pack = getattr(ctx.business, "domain_pack", None)
        offered_before = tuple(getattr(ctx.state, "offered_chips", ()) or ())
        menu = await menu_for_turn(ctx, deps)
        candidates = []
        if menu.usable:
            candidates += chip_moves.renderable(
                chip_moves.from_menu(menu, offered_before=offered_before), pack, ctx.language
            )
        cards = [
            {"product_id": it.product_id, "name": it.name, "price": it.price} for it in rich.items
        ]
        anchor_stats: dict[str, int] = {}
        candidates += chip_moves.renderable(
            chip_moves.from_cards(
                cards,
                offered_before=offered_before,
                unique_anchor=getattr(get_settings(), "unique_name_prefix_enabled", False),
                locale=ctx.language,
            ),
            pack,
            ctx.language,
            stats=anchor_stats,
        )
        retrieved = list(ctx.retrieval.products) if ctx.retrieval is not None else []
        with_attrs = _cards_with_attributes(cards, retrieved)
        candidates += await _facet_moves(ctx, deps, with_attrs)
        graph, graph_stats = await _relation_moves(ctx, deps, with_attrs)
        candidates += graph
        anchor_stats.update(graph_stats)
        obligations = extract_obligations(ctx.message.body or "")
        kinds = [o.kind for o in obligations]
        candidates, dead = _drop_dead_moves(
            ctx, candidates, n_cards=len(cards), obligation_kinds=kinds, cards=with_attrs
        )
        if dead:
            anchor_stats["dropped_dead"] = dead
        picked = chip_moves.select(
            candidates,
            slots=chip_slots(get_settings()),
            role_order=_role_order(kinds),
            offered_before=offered_before,
        )
        if not picked:
            return
        texts, _ = chip_moves.apply_labels(picked, {}, pack, ctx.language)
        if not texts:
            return
        rich.chips = compose._suggestion_chips(list(texts))
        ctx.emit(
            "chip_moves",
            n=len(texts),
            kinds=sorted({m.kind for m in picked}),
            roles=sorted({m.role for m in picked}),
            offered=len(candidates),
            path="v1",
            **anchor_stats,
        )
        previous = [str(m) for m in offered_before]
        merged = previous + [m.move_id for m in picked if m.move_id not in previous]
        ctx.state_patch["offered_chips"] = merged[-MAX_OFFERED_CHIPS:]
    except Exception as e:  # noqa: BLE001 — chips-urile nu sunt răspunsul (P6)
        log.warning("finalize: chips ca mutări au eșuat (%s)", type(e).__name__)


async def _finalize_rich(
    llm,
    rich_system: str,
    query: str,
    products: list[dict[str, Any]],
    ctx,
    history: str,
    notes: str = "",
    shape: _TurnShape | None = None,
) -> _RichOutcome:
    """Compune recomandarea STRUCTURATĂ (model iZi). Modelul emite intro + referințe
    product_id/pro_index/fit_clause + pick + education + chip_intents (enum închis); codul
    (compose) hidratează faptele. `rich_system` = system generat din DB (NX-78). `notes` =
    context per-tur din bucla de tool-uri (NX-137: ex. checkout eșuat → fără chips de coș).
    Întoarce `_RichOutcome`; `reply is None` → fallback pe proză."""
    shape = shape or _TurnShape()
    user = rich_user_message(ctx, query, products, history, notes=notes, shape=shape)
    schema = _rich_schema(rich_omissions(), question=shape.offer is not None)
    trace = getattr(ctx, "trace", None)  # fake-urile din teste n-au câmpul nou (tiparul aftercare)
    try:
        j = await llm.complete_schema(rich_system, user, schema)
    except Exception as e:  # noqa: BLE001 — apel structurat eșuat → fallback pe proză
        log.warning("agent: finalize structured eșuat (%s)", type(e).__name__)
        if trace is not None:
            trace["rich_error"] = type(e).__name__  # NX-256: în captura de diagnoză
        return _RichOutcome(reply=None)
    # NX-256: JSON-ul BRUT al modelului, înainte de membership/scrub — singurul loc unde există.
    # Incidentul din 24 aug: rich-ul degradase pe „all-items-dropped-by-membership" și nu aveam
    # cum să aflăm ce id-uri emisese modelul, fiindcă `j` murea aici, în memorie. Merge DOAR în
    # `ctx.trace` (→ `conversation_traces`, sub flag), NU în analytics (P12: acolo contoare).
    if trace is not None:
        trace["rich_raw"] = j
    emitted = [it for it in (j.get("items") or []) if isinstance(it, dict) and it.get("product_id")]
    rich = compose.assemble(ctx, j, products, grounded_numbers=shape.grounded_numbers)
    if rich.items:
        # Doar pe un răspuns care chiar pleacă: pe refuz (zero carduri) turul coboară pe proză, iar
        # a raporta acolo un „cum alegi" lipsă ar număra un eșec care nu i-a fost arătat nimănui.
        _apply_turn_shape(ctx, rich, j, shape)
    return _RichOutcome(reply=rich, model_items=len(emitted))


def rich_user_message(
    ctx,
    query: str,
    products: list[dict[str, Any]],
    history: str,
    *,
    notes: str = "",
    shape: _TurnShape | None = None,
) -> str:
    """Mesajul de USER al apelului de compunere bogată. Extras din `_finalize_rich` ca să aibă UN
    singur autor: replay-ul de raționament (NX-312 felia 5) îl reface pe ture reale prin aceeași
    funcție, deci nu măsoară o copie care a divergit de producție."""
    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    notes_block = f"NB: {notes}\n" if notes else ""
    # NX-139: axele pe care VARIAZĂ setul (fațete DomainPack cu dispersie + interval de preț) —
    # input grounded ca intro-ul să numească axe REALE (tip de ten/fitment/material, per vertical),
    # nu superficiale („cremă vs stick"), iar education să segmenteze pe ele. Gated; gol → fără.
    axes_block = ""
    if get_settings().decision_axes_enabled:
        axes = compose.decision_axes(products, _rich_facets(ctx), ctx.language)
        if axes:
            ctx.emit(
                "decision_axes",
                n_axes=len(axes),
                keys=[a.split(":", 1)[0] for a in axes],  # cheile, nu valorile (minimal, P12)
            )
            axes_block = (
                "Axe pe care variază setul (folosește-le în intro și la segmentare): "
                + " | ".join(axes)
                + "\n"
            )
    shape = shape or _TurnShape()
    user = (
        f"Limba clientului: {ctx.language}\n{notes_block}{history_block}"
        f"Nevoia clientului: {query}\n{axes_block}{shape.block}\n"
        f"Produse disponibile (alege dintre acestea):\n"
        f"{_rich_bundle(products, _rich_facets(ctx), ctx.language)}"
    )
    return user


def _attach_checkout_offer(ctx: TurnContext, url: str | None) -> None:
    """NX-137: linkul de checkout creat în ACEST tur ajunge GARANTAT la client, pe orice cale de
    compunere. Root cause (găsit live pe sim): pe calea RICH (web) modelul are INTERZIS structural
    să scrie linkuri (regulile rich) → linkul era creat în DB (`checkout_link_created`) și apoi
    murea tăcut — reply fără URL. Offer e neutru de canal (NX-114): marginile bogate randează
    buton/CTA; floor-ul din `set_offer` lipește URL-ul la text DOAR dacă nu e deja acolo (proza
    îl poate conține deja — fără dublare)."""
    if not url or ctx.reply is None:
        return
    ctx.set_offer(Offer(kind="open_url", label=_checkout_label(ctx.language), url=url))
    ctx.emit("checkout_offer_attached")


async def render(
    ctx: TurnContext, deps: PipelineDeps, plan: ResponsePlan
) -> ValidationResult | None:
    """Faza F: dispatch pe `ResponsePlan` → răspuns final. Byte-identic cu vechiul bloc din
    `agent_stage`: comparație → recomandare (rich→proză) → status comandă / clarificare / fallback.
    Păstrează fall-through-urile: `build_comparison` None → cade pe produse; rich eșuat → proză.
    Grounding-ul rămâne la `validator` (P2); un singur punct de ieșire e Sender via `ctx.set_*`.

    Întoarce `ValidationResult`-ul validatorului de PROZĂ care a decis reply-ul servit (NX-146
    felia 2 fix — `agent_stage` îl corelează în `agent_prompt` pt Turn Replay). `None` pe căile
    FĂRĂ proză validată structural (comparație/rich/no-result/login): acolo grounding-ul vine
    din compose (membership) sau nu există text de validat, nu din `validate_prose`."""
    is_order = plan.is_order
    products = plan.products
    final = plan.final

    # IZI-compare: modelul a chemat compare_products → turul e o COMPARAȚIE, nu o recomandare.
    # `build_comparison` dă tabelul DETERMINIST din setul comparat (ordinea cerută păstrată, fapte
    # din retrieval) — el rămâne plasa. Peste el, `compare_narrative` compune axele de decizie și
    # îndrumarea, cu porți de grounding pe fiecare celulă; ce nu trece cade înapoi exact aici.
    # Web randează tabelul; canalele text primesc floor-ul aplatizat. Precede calea rich de
    # recomandare (altfel ar re-RECOMANDA în loc să compare — bug-ul „Compară primele două" care
    # doar re-lista produsele). Sare peste rich/proză pentru acest tur.
    if plan.compared and not is_order:
        facets = _comparison_facets(ctx)
        comparison = compose.build_comparison(plan.compared, ctx.language, facets)
        if comparison is not None:
            # Ca pe calea deterministă: tabelul determinist e plasa, narativul îl înlocuiește
            # doar dacă trece porțile de grounding.
            comparison = await compose_comparison(
                deps.llm, ctx, comparison, plan.compared, facets=facets, query=plan.query
            )
            ctx.set_comparison_reply(
                comparison,
                text=compose.flatten_comparison(comparison, ctx.language),
                products=compose.comparison_cards(comparison),
                chips=_compare_chips(comparison.columns, ctx.language),
            )
            ctx.emit("agent_compared", n=len(comparison.columns))
            return None

    if products:
        # Motivul pentru care calea bogată a cedat, sau `None` dacă n-a fost încercată (ORDER).
        # Trăiește aici, nu în blocul de mai jos, fiindcă îl citește și recuperarea de după
        # `_finalize` (NX-302) — o variabilă definită într-o ramură și citită în alta e exact
        # felul de legătură care se rupe tăcut la următoarea refactorizare.
        downgrade_reason: str | None = None
        # Calea BOGATĂ (model iZi): recomandare structurată → compose. Doar pe SALES.
        # Orice eșec (apel structurat, zero items după membership) → fallback pe proză.
        if not is_order:
            outcome = await _finalize_rich(
                deps.llm,
                prompt_builder.build_rich_system(
                    plan.inp,
                    routine=getattr(ctx, "routine", None) is not None,
                    omit=rich_omissions(),
                ),
                plan.query,
                products,
                ctx,
                plan.history,
                notes=plan.commerce_note,
                shape=await _turn_shape(ctx, deps, products),
            )
            rich = outcome.reply
            if rich is not None and rich.items:
                await _apply_move_chips(ctx, deps, rich)
                ctx.set_rich_reply(
                    rich,
                    text=compose.flatten(rich, ctx.language),
                    products=compose.card_products(rich.items),
                )
                # NX-137: regulile rich INTERZIC linkuri în proza modelului → fără atașarea asta,
                # linkul de checkout creat în acest tur nu ajungea NICIODATĂ la client pe web.
                _attach_checkout_offer(ctx, plan.checkout_url)
                # NX-163: ce a recomandat botul, ca ref-uri (P8) — „ce se cere/convertește"
                # (NX-164). rich.items poartă product_id structural; doar id-uri, fără PII.
                ctx.emit(
                    "agent_recommended",
                    n=len(rich.items),
                    rich=True,
                    product_ids=clean_ids(it.product_id for it in rich.items),
                )
                return None
            # NX-122: downgrade tăcut rich → proză, acum vizibil. Trei motive, nu două: apelul
            # structurat a eșuat; modelul a numit produse și TOATE au picat la poarta de
            # apartenență (defect de model); modelul n-a numit niciunul (refuz — setul nu i s-a
            # părut un răspuns). Ultimul se eticheta „dropped-by-membership" deși nu se dropa
            # nimic, deci nu exista cifră pentru el. Pur observabilitate (downgrade-ul exista, P6).
            if rich is None:
                reason = "structured-call-failed"
            elif outcome.model_items:
                reason = "all-items-dropped-by-membership"
            else:
                reason = "no-items-selected"
            downgrade_reason = reason
            ctx.emit("rich_downgraded", reason=reason)
            if getattr(ctx, "trace", None) is not None:
                ctx.trace["rich_downgraded"] = reason  # NX-256: lângă `rich_raw`, în captura full
        # NX-91: dacă textul brut al modelului are cifre bare negroundate, semnalează (P12: doar
        # contorul, NU corpul). _finalize declanșează retry-ul/fallback-ul pe baza validării.
        bare = _bad_bare_numbers(final, products, plan.grounded_prices) if final else []
        if bare:
            ctx.emit("validator_rejected", kind="bare_number", n=len(bare))
        # NX-117: claim ne-numeric neverificabil pe proză → semnalează (P12: doar contorul).
        if final and not _claims_ok(final):
            ctx.emit("validator_rejected", kind="claim")
        # NX-118: claim de stoc nefondat (niciun produs pe stoc) → semnalează (P12: doar contorul).
        if final and not _stock_claim_ok(final, products):
            ctx.emit("validator_rejected", kind="stock_claim")
        reply, result = await _finalize(
            deps.llm,
            prompt_builder.build_reco_system(plan.inp),
            plan.query,
            final,
            products,
            ctx.language,
            plan.history,
            plan.generated_links,
            plan.grounded_prices,
            recompose=not plan.prose_skipped,
        )
        # NX-306: un set pe care modelul l-a REFUZAT, și pe care retrievalul îl dăduse deja ca
        # pe un COMPROMIS, nu se servește — nici ca fapte, nici ca text.
        #
        # Turul real `78b347fa` (`sole-ro`, «cat cost una?» după ce botul oferise o mănușă de
        # aplicare): căutarea a nimerit raftul greșit, modelul a scris ONEST „nu am găsit în
        # catalog o mănușă, rezultatele sunt pensule de machiaj, nu sunt potrivite", iar codul a
        # atașat oricum cele șase produse. Clientul a primit patru carduri de 1.300 lei sub un text
        # care le nega. `validator_ok: true` pe tot turul, fiindcă produsele și prețurile ERAU
        # reale: stagiul 8 și `grounding_guard` sunt porți de ADEVĂR, nu de POTRIVIRE.
        #
        # Poarta cere AMBELE semnale, și fiecare pentru alt motiv.
        #
        # `no-items-selected` e singura formă de refuz măsurată de model ÎNSUȘI: apelul structurat a
        # REUȘIT, modelul a văzut setul și n-a numit niciun produs. Nu e ghicit dintr-un regex pe
        # proză, deci nu introduce nicio listă de cuvinte românești pentru „nu am găsit" (P11).
        #
        # `relevance.relaxed` spune că setul a ieșit doar fiindcă scara de relaxare a RENUNȚAT la
        # filtre. Singur, refuzul nu ajunge: suita a prins de ce. Pe follow-up-ul „ceva mai ieftin"
        # (`test_cheaper_followup_e2e`), setul e ales DETERMINIST de `cheaper_intent`, iar modelul
        # poate foarte bine să nu-l numească — acolo serverul știe mai bine decât modelul ce a cerut
        # clientul, și a suprima ar însemna să ascundem exact răspunsul corect. Un set găsit STRICT
        # și refuzat rămâne deci pe ecran; unul obținut prin renunțare la filtre și refuzat, nu.
        #
        # Pe refuz, setul servibil devine ce NUMEȘTE proza. De obicei nimic, și atunci nu se
        # afișează nimic. Dacă modelul a numit totuși un produs în proză după ce nu-l numise în
        # structură, ăla e chiar produsul despre care vorbește textul.
        #
        # Filtrul se aplică ÎNAINTE de `rich_from_facts`, deliberat. NX-302 recuperează FORMA bogată
        # din catalog, deci fără poarta asta refuzul ar fi ieșit AGRAVAT: aceleași pensule, dar acum
        # cu badge, rating și un motiv de sub card, adică un ecran care arată exact ca o recomandare
        # convinsă, sub un text care o neagă.
        servable = products
        _relevance = getattr(getattr(ctx, "retrieval", None), "relevance", None)
        if downgrade_reason == "no-items-selected" and getattr(_relevance, "relaxed", False):
            servable = compose.named_products(reply, products)
            ctx.emit("refused_set_withheld", retrieved=len(products), named=len(servable))
        # NX-302: degradarea e PARȚIALĂ, nu totală. Modelul rich lipsește, FAPTELE nu — deci
        # cardurile se construiesc din catalog (motiv din `best_for`, rating, badge, preț de listă,
        # variante, gramaj) și clientul primește contractul bogat, minus proza narată.
        #
        # Măsurat pe traficul real al lui `sole-ro` (`conversation_traces`, 56 de ture): 14 din
        # turele cu produse ajungeau la client cu `rich = null`, adică un sfert. Pe turul `57fa9fbe`
        # («vreau o rutina de cosuri») cauza a fost `APITimeoutError` pe apelul pe care NX-300 îl
        # măsurase la 86,7 s dintr-un tur de 95,2 s. Ce vedea clientul: patru carduri mute și trei
        # nume cu prețuri sub ele.
        #
        # Vine DUPĂ `_finalize`, deliberat. Recuperarea asta e despre FORMA răspunsului; retry-ul de
        # recompunere e despre ADEVĂRUL lui (un preț inventat se prinde și se rescrie). Sunt două
        # preocupări, iar a le amesteca ar fi însemnat ca o îmbunătățire de formă să dezactiveze
        # tăcut o poartă de adevăr. Proza validată devine `intro`; cea nevalidată (adică
        # `_deterministic_reply`) NU — sub carduri care poartă aceleași prețuri, o listă de nume cu
        # prețuri nu e o încadrare, e chiar contradicția din turul măsurat. Slotul rămas gol îl
        # umple rezerva de încadrare a serverului (NX-299), deci clientul primește tot o frază.
        salvaged = (
            rich_from_facts(
                ctx,
                servable,
                intro=reply if result.ok else None,
                sole_text=plan.prose_skipped,
            )
            if get_settings().rich_from_facts_enabled and servable
            else None
        )
        if salvaged is not None and salvaged.items:
            await _apply_move_chips(ctx, deps, salvaged)
            ctx.set_rich_reply(
                salvaged,
                text=compose.flatten(salvaged, ctx.language),
                products=compose.card_products(salvaged.items),
            )
            _attach_checkout_offer(ctx, plan.checkout_url)
            ctx.emit(
                "agent_recommended",
                n=len(salvaged.items),
                rich=True,
                product_ids=clean_ids(it.product_id for it in salvaged.items),
            )
            ctx.emit("rich_from_facts", reason=downgrade_reason, prose=result.ok)
            if getattr(ctx, "trace", None) is not None:
                ctx.trace["rich_from_facts"] = {"reason": downgrade_reason, "prose": result.ok}
            return result
        # NX-302: `cacheable` urmează VALIDATORUL. Un text care a picat validarea e
        # `_deterministic_reply`, adică un răspuns de avarie — scris în `semantic_cache`, el s-ar
        # re-servi la fiecare query similar, sărind agentul. Ramura de no-result de mai jos se apăra
        # de asta din 2026-06 (`cacheable=False`, „hit_count=9 pe demo"); ramura cu produse nu, iar
        # turul măsurat `57fa9fbe` a ieșit din producție cu `cacheable: true` pe un răspuns născut
        # dintr-un timeout. Pe calea bogată problema nu există prin construcție: `set_rich_reply`
        # are `cacheable=False` implicit.
        #
        # NX-306: un tur din care setul a fost reținut NU e cacheabil, oricât de validă ar fi proza.
        # „Nu am găsit o mănușă" scris în `semantic_cache` s-ar re-servi la fiecare întrebare
        # similară, sărind agentul — exact otrăvirea pe care ramura de no-result o evită din 2026-06
        # (`hit_count=9` pe demo). Aici textul e chiar un no-result, doar că a ajuns pe altă ramură.
        ctx.set_reply(
            reply,
            products=_card_products(servable),
            cacheable=result.ok and bool(servable),
        )
        # NX-137: pe proză modelul POATE scrie linkul (validat prin generated_links), dar dacă
        # l-a omis, Offer-ul îl garantează (floor-ul din set_offer nu dublează un URL deja în text).
        _attach_checkout_offer(ctx, plan.checkout_url)
        # NX-306: fără carduri, turul e o fundătură — exact starea în care clientul are cea mai
        # mare nevoie de o cale de continuare. Ramurile de no-result atașează chips de mult
        # (NX-159 felia 2); ramura asta nu le atașa niciodată, fiindcă „are produse" era judecat
        # pe RETRIEVAL, nu pe ce ajunge la client.
        if not servable:
            _attach_no_result_alternatives(ctx)
        # NX-163: ce a recomandat botul, ca ref-uri (P8) — vezi enrich-ul rich de mai sus.
        ctx.emit("agent_recommended", n=len(servable), product_ids=product_ids_from_dicts(servable))
        return result
    elif final:
        # Fără produse, dar avem text: îl VALIDĂM (nu servire oarbă). Forma de recuperare diferă
        # pe rută — nu trecem o întrebare de vânzare prin fallback-ul de status comandă.
        if is_order:
            # ORDER: fără bare-check (numere DB legitime: dată/AWB/cantitate) — vezi validate_prose.
            reply, result = await _finalize_grounded(
                deps.llm,
                final,
                "\n".join(plan.order_views),
                ctx.language,
                plan.generated_links,
                plan.grounded_prices,
            )
            ctx.set_reply(reply)
            return result
        # Gating pe `_valid` (monkeypatch-uit de proba anti-teatru NX-121); motivele raportate
        # separat prin `validate_prose`, fără să schimbe gating-ul testat (vezi `_finalize`).
        if _valid(final, [], plan.generated_links, plan.grounded_prices):
            # SALES: text fără produse și fără sumă inventată (clarificare) → servim
            ctx.set_reply(final)
            return ValidationResult(ok=True, reasons=[])
        # SALES: preț negroundat fără produse care să-l susțină → mesaj sigur de vânzare.
        # NU cacheabil: altfel „n-am găsit" otrăvește semantic_cache și se re-servește la
        # fiecare query similar, sărind agentul (bug găsit live: hit_count=9 pe demo).
        ctx.set_reply(_no_result_msg(is_order=False), cacheable=False)
        _attach_no_result_alternatives(ctx)  # NX-159 felia 2: chips de continuare
        return validate_prose(
            final,
            products=[],
            generated_links=plan.generated_links,
            grounded_prices=plan.grounded_prices,
        )
    elif is_order and web_unidentified(ctx):
        # ORDER pe web anonim, fără rezultat (modelul n-a chemat un tool) → login, NU „dă-mi numărul
        # comenzii" (ar relua bucla NX-128 pe un canal unde lookup-ul nu poate reuși).
        ctx.set_reply(login_required_for_ctx(ctx), cacheable=False)
        return None
    else:
        ctx.set_reply(_no_result_msg(is_order), cacheable=False)
        if not is_order:  # NX-159 felia 2: chips de continuare doar pe sales (order cere numărul)
            _attach_no_result_alternatives(ctx)
        return None
