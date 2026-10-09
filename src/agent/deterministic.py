"""Faza B — intenții deterministe PRE-loop (NX-143). Early-exit ÎNAINTE de bucla LLM, cost $0 de
inferență: cererea se poate servi din setul deja afișat (`ctx.state.displayed_products` / sesiunea
activă), fără să chemăm modelul.

Trei intenții:
  • `link`    — „trimite-mi linkul" pe produs afișat → `Offer(open_url)` + card (NX-131).
  • `compare` — „compară primele două" → tabel structurat determinist (IZI-parity G2).
  • `show_more` — „mai arată-mi" pe o sesiune activă → paginare (predicatul e aici; paginarea
    propriu-zisă rămâne cablată în `stage.py`/GENERATE via `continue_search_session`, NX-119b).

Contract: `try_pre_intents(ctx, deps) -> bool` (True = tratat, `stage.py` face return) + predicatul
pur `is_show_more(ctx)`. Gating pur (predicat pe `ctx` + regex) + handler care setează `ctx.reply`
(P5). `_CHEAPER_RE` trăiește AICI și e partajat cu shaping-ul post-loop (planner, NX-144) — un
follow-up de preț («mai ieftin») NU e link/compare/paginare, ci re-căutare (cheaper_intent).
"""

from __future__ import annotations

import contextvars
import inspect
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from src.agent.compare_narrative import compose_comparison
from src.agent.fallbacks import (
    _card_products,
    _compare_chips,
    _link_lead,
    _no_link_msg,
    _view_label,
    fit_chip,
)
from src.agent.reference_resolver import (
    ANY_ORDINAL_RE,
    ActionAnchor,
    NamedTargets,
    ReferenceRequest,
    ReferenceResolution,
    expresses_ordinal,
    named_targets,
    normalize_for_match,
    page_anchor_from_snapshot,
    resolve_from_displayed,
    resolve_product_reference,
    resolve_reference,
    shortcut_references,
)
from src.catalog.query_terms import fold, formula_fillers, stopwords
from src.catalog.reference_facts import fetch_reference_facts
from src.catalog.render_text import cut_at_sentence, display_name
from src.catalog.vocabulary_cache import get_vocabulary
from src.config import get_settings
from src.conversation.interpretation import Act, Reference, ResolvedRef
from src.conversation.references import (
    ReferenceSources,
    ShownItem,
    gate_act_targets,
    plan_lookup,
    resolve_references,
    zoomed_list,
)
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import ConversationStateV2
from src.db.queries.catalog import (
    get_products_by_ids,
    product_category_roots,
    similar_candidates,
)
from src.domain.constraints import extract_constraints
from src.models import Offer, ProductRef, RetrievalResult, Route, TurnContext
from src.safety.policy import SafetyPolicy
from src.web.action_models import action_command, action_kind
from src.worker import compose
from src.worker.text_scrub import has_medical_claim

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# P1 (ARCH-product-retrieval): follow-up de PREȚ pe un set deja afișat → re-căutare DETERMINISTĂ a
# produselor strict mai ieftine (search_cheaper_than), NU re-rank pe setul afișat (R3). Precizie
# mare RO/EN (comparativ/superlativ de preț). Un miss cade grațios pe comportamentul vechi (R3).
# Partajat cu planner-ul (NX-144): gating-ul link/compare/show_more exclude «mai ieftin».
_CHEAPER_RE = re.compile(
    r"\bmai\s+ieftin\w*|\bcea\s+mai\s+ieftin\w*|\bmai\s+accesibil\w*"
    r"|\bpre[țt]\s+mai\s+mic|\bmai\s+mic\s+la\s+pre[țt]|\bbuget\s+mai\s+mic"
    r"|\bprea\s+scump\w*|\bcam\s+scump\w*"
    r"|\bcheaper\b|\bcheapest\b",
    re.IGNORECASE,
)

# IZI-parity (Tier 1, G2): intenție de COMPARAȚIE pe un set deja afișat → tabel DETERMINIST (ca
# cheaper/show_more/link), fără să depindem de modelul care cheamă `compare_products`. RO/EN,
# agnostic de vertical. ÎNALTĂ PRECIZIE deliberat (gate-ul n-are recurs la model pe fals-pozitiv):
# DOAR verbul „a compara" + „versus/vs". `compar[aăie]\w*` prinde compara/compară/
# comparați/comparație ȘI EN compare/comparison/comparing, dar NU „compartiment" (compar+t). Frazele
# laxe („ce diferență e între ele") cad pe calea model-driven (modelul cheamă compare_products) — nu
# le prindem determinist ca să nu confundăm „diferența dintre garanție și retur" cu o comparație.
_COMPARE_RE = re.compile(
    r"\bcompar[aăie]\w*|\bversus\b|\bvs\.?\b",
    re.IGNORECASE,
)
# Numărul explicit din cerere controlează comparația; implicit rămâne perechea dominantă.
_THREE_RE = re.compile(r"\btrei\b|\bthree\b|\b3\b|\bh[áa]rom\b", re.IGNORECASE)
_FOUR_RE = re.compile(r"\bpatru\b|\bfour\b|\b4\b|\bn[ée]gy\b", re.IGNORECASE)

# NX-119b: „mai arată-mi"/„show more" = INTENȚIE de PAGINARE (NU un tool nou). Pe o sesiune activă
# → pagina următoare DETERMINIST, fără bucla LLM. NU prinde „mai ieftin" (= cheaper_intent). Ancorat
# pe sensul de paginare: „mai multe" PLURAL bare/terminal sau + obiect de listă — NU „mai multă/mult
# X" (rafinare „mai multă hidratare") și nu „și alte INGREDIENTE" (rafinare). Rafinările cu cuvânt
# „more" sunt prinse oricum de gate-ul `not route.filters` (cad pe bucla LLM → sesiune nouă).
_MORE_RE = re.compile(
    r"\bmai\s+arat\w*"  # „mai arată-mi"
    r"|\bmai\s+multe\b(?!\s+\w)"  # „mai multe" bare/terminal (NU „mai multă/mult X")
    r"|\bmai\s+multe\s+(?:produse|op[țt]iuni|variante|rezultate|exemple)\b"
    r"|\balte\s+(?:op[țt]iuni|variante|produse)\b|\b[șs]i\s+alte\s+(?:op[țt]iuni|variante|produse)\b"
    r"|\baltele\b|\bmai\s+vreau\b"
    r"|\bshow\s+more\b|\bmore\s+(?:options|products|results)\b|\bother\s+(?:options|ones)\b",
    re.IGNORECASE,
)

# NX-131: cerere de LINK la un produs DEJA arătat („trimite-mi linkul / dă-mi link direct / unde-l
# cumpăr"). Intenție DETERMINISTĂ (ca _CHEAPER_RE/_MORE_RE): calea rich INTERZICE structural
# modelului linkurile (regulile rich) → o cerere de link cădea în re-randarea bogată cu coaching
# repetat (bug live: „partea asta e foarte repetitiva"). Ancorat pe „link" (link/linkul/linkuri,
# RO/EN) + fraze de cumpărare. `\blink\w*` NU prinde „hyperlink"/„blink" (fără boundary înainte).
# Gated în `try_pre_intents` pe displayed_products + SALES + fără filtre noi (cu filtre = fresh).
_LINK_RE = re.compile(
    r"\blink\w*"
    r"|\bunde\s+(?:o\s+|[îi]l\s+|le\s+)?(?:pot\s+)?(?:cump[ăa]r|comand|g[ăa]sesc)\w*"
    r"|\bwhere\s+(?:can\s+i\s+|to\s+)?(?:buy|get|find)\b",
    re.IGNORECASE,
)

# Follow-up de recenzii pe un set deja afișat. Rulează pe text normalizat fără diacritice, astfel
# încât aceeași intenție să funcționeze în RO/EN și când clientul tastează fără diacritice.
_REVIEW_RE = re.compile(r"(?<![a-z0-9])(?:recenzi|parer|opini|review)[a-z]*", re.IGNORECASE)
_DETAIL_RE = re.compile(
    r"\b(?:spune|zi)-?mi\s+mai\s+multe\b"
    r"|\bmai\s+multe\s+detali\w*\b|\bdetali\w*\s+(?:despre|pentru)\b"
    r"|\b(?:vreau|as vrea)\s+detali\w*\b|\bmore\s+(?:details|about)\b"
    r"|\btell\s+me\s+more\b",
    re.IGNORECASE,
)


# NX-234: ordinalele + potrivirea pe nume s-au mutat în `src/agent/reference_resolver.py`
# (API comun, ca ancora paginii să folosească EXACT aceleași reguli ca setul afișat).
def _norm_followup(text: str) -> str:
    return normalize_for_match(text)


def _page_anchor_ref(ctx: TurnContext) -> ProductRef | None:
    """NX-234 — ancora paginii ca `ProductRef`, sau None.

    Numele și prețul vin din snapshotul REHIDRATAT server-side, nu din browser: `ProductRef` e
    tipul pe care îl consumă deja handlerele deterministe, deci ancora intră pe același drum ca
    un produs afișat, fără o a doua cale de rezolvare (P3)."""
    anchor = page_anchor_from_snapshot(getattr(ctx, "snapshot", None))
    if anchor is None:
        return None
    return ProductRef(
        product_id=anchor.product_id,
        name=anchor.name or "",
        price=float(anchor.price) if anchor.price is not None else 0.0,
    )


def _anchor_refs(ctx: TurnContext) -> list[ProductRef]:
    """Setul de produse pe care un follow-up îl poate ancora: cele AFIȘATE + produsul PAGINII
    (dacă nu e deja acolo). Ordinea contează — deixis-ul ordinal („a doua") se referă la lista
    afișată, deci ancora paginii se adaugă la coadă, nu în față."""
    refs = list(ctx.state.displayed_products)
    page = _page_anchor_ref(ctx)
    if page is not None and all(r.product_id != page.product_id for r in refs):
        refs.append(page)
    return refs


def _resolve_review_product(
    query: str, refs: list[ProductRef], locale: str | None = None
) -> ProductRef | None:
    """Rezolvă deixis-ul fără LLM: produs unic, ordinal, nume complet sau tokeni unici.

    NX-234: logica trăiește acum în `src/agent/reference_resolver.py` (API comun, ca ancora
    paginii să folosească EXACT aceleași reguli). Semantica rămâne neschimbată."""
    resolution = resolve_from_displayed(query, refs, locale=locale)
    return refs[resolution.index] if resolution.index is not None else None


def _signed_anchor(ctx: TurnContext) -> ActionAnchor | None:
    """NX-236 — produsul purtat de acțiunea curentă, ca ancoră semnată.

    `revision=0` (nu se compară cu revizia listei): o acțiune NUMEȘTE produsul explicit, deci o
    reordonare a cardurilor între emitere și click nu o poate face să aleagă altceva. Ancorele care
    chiar depind de listă (paginarea) își verifică prospețimea în kernel, pe `active_search.fp`."""
    command = action_command(ctx)
    if command is None or not command.args.product_ref:
        return None
    return ActionAnchor(product_id=command.args.product_ref, revision=0, valid=True)


def _resolve_anchor(ctx: TurnContext, query: str) -> ProductRef | None:
    """Produsul ancorei (vezi `_anchor`); semnătura de azi, pentru apelanții care nu cer sursa."""
    return _anchor(ctx, query)[0]


def _anchor(ctx: TurnContext, query: str) -> tuple[ProductRef | None, ReferenceResolution]:
    """Rezolvarea referinței, prin precedența UNICĂ din `reference_resolver`, plus DECIZIA ei
    (`source`/`reason`): NX-336, a doua recenzie, ca trecerea `exact_only` să deosebească o țintă
    exactă (acțiune, ordinal, nume unic) de o ancoră ghicită (singurul card, pagina ca fallback).

    NX-234 (flag stins): `ordinal` > `named` > `page` > `single`, cu pagina ca fallback
    necondiționat. NX-235 (`reference_precedence_v2_enabled`): precedența completă —
    `action` > `named` > `ordinal` > `page(deictic)` > `selected` > `single`, iar o ancoră stale
    sau un ordinal imposibil se întorc ca `ambiguous`, nu ca alt produs.

    Fără snapshot/ancoră (orice canal non-web, flag stins, pagină fără produs) cade exact pe
    `_resolve_review_product` — comportamentul de dinainte, bit cu bit."""
    refs = list(ctx.state.displayed_products)
    page = _page_anchor_ref(ctx)
    v2 = get_settings().reference_precedence_v2_enabled
    if page is None and not v2:
        # `_resolve_review_product`, cu decizia păstrată (aceeași `resolve_from_displayed`).
        legacy = resolve_from_displayed(query, refs, locale=ctx.language)
        return (refs[legacy.index] if legacy.index is not None else None), legacy
    if v2:
        state_v2 = getattr(ctx, "state_v2", None)
        references = getattr(state_v2, "references", None)
        resolution = resolve_reference(
            ReferenceRequest(
                query=query,
                refs=tuple(refs),
                page=page_anchor_from_snapshot(ctx.snapshot),
                # NX-236: ancora SEMNATĂ bate tot (precedența 1). Clientul n-a descris produsul,
                # l-a arătat cu degetul — iar tokenul dovedește pe care.
                anchor=_signed_anchor(ctx),
                selected_product=getattr(references, "selected_product", None),
                displayed_revision=getattr(references, "displayed_revision", 0),
                locale=ctx.language,
            )
        )
    else:
        resolution = resolve_product_reference(
            query,
            refs,
            page=page_anchor_from_snapshot(ctx.snapshot),
            locale=ctx.language,
        )
    ctx.emit(
        "web_reference_resolved",
        source=resolution.source,
        outcome=resolution.outcome,
        reason=resolution.reason,
    )
    if v2 and resolution.resolved:
        # NX-235: o referință rezolvată E o selecție explicită. Fără s-o memorăm, „cât costă?" de
        # la turul următor ar redeveni ambiguu, deși clientul tocmai a arătat despre ce vorbește.
        ctx.state_proposals.append(
            StateUpdateProposal(
                "set_references",
                source="user_explicit",
                turn_id=ctx.turn_id,
                payload={"selected_product": resolution.product_id},
            )
        )
    if resolution.index is not None:
        return refs[resolution.index], resolution
    if resolution.product_id is None:
        return None, resolution
    if page is not None and resolution.product_id == page.product_id:
        return page, resolution
    # Rezolvat pe un id pe care NU-l putem hidrata aici (produs selectat într-un tur anterior, ieșit
    # din setul afișat): apelantul întreabă. Nu inventăm un `ProductRef` fără nume/preț canonice.
    return next((r for r in refs if r.product_id == resolution.product_id), None), resolution


def _review_copy(language: str) -> dict[str, str]:
    if language == "en":
        return {
            "which": "Which product would you like reviews for?",
            "summary": "Review summary for {name}: {value}",
            "pros": "What customers liked: {value}.",
            "cons": "Worth considering: {value}.",
            "rating": "Rating: {rating}/5 from {count} reviews.",
            "empty": "I don't have enough review data for {name} yet.",
            "chip": "Show me the reviews for {name}",
        }
    return {
        "which": "Pentru care produs vrei să vezi recenziile?",
        "summary": "Rezumatul recenziilor pentru {name}: {value}",
        "pros": "Ce au apreciat clienții: {value}.",
        "cons": "De luat în calcul: {value}.",
        "rating": "Rating: {rating}/5 din {count} recenzii.",
        "empty": "Nu am încă suficiente date din recenzii pentru {name}.",
        "chip": "Arată-mi recenziile la {name}",
    }


def _safe_review_text(value: object) -> str | None:
    text = str(value or "").strip().rstrip(".")
    return text if text and not has_medical_claim(text) else None


def _review_answer(product: dict, language: str) -> tuple[str, bool]:
    copy = _review_copy(language)
    # NX-319: numele SCURT (NX-301). Cel întreg e nume + frază de marketing, iar aici intra de două
    # ori în același paragraf.
    name = display_name(str(product.get("name") or "produs"))
    lines: list[str] = []
    summary = _safe_review_text(product.get("review_summary"))
    pros = [_safe_review_text(value) for value in (product.get("top_pros") or [])]
    cons = [_safe_review_text(value) for value in (product.get("top_cons") or [])]
    # NX-319: rezumatul NX-279 e construit din aceleași teme ca `top_pros`, deci o temă pe care o
    # spune deja nu se mai repetă ca „ce au apreciat" («dă rezultate vizibile», de două ori).
    said = fold(summary or "")
    pros = [value for value in pros if value and fold(value) not in said]
    cons = [value for value in cons if value and fold(value) not in said]
    if summary:
        lines.append(copy["summary"].format(name=name, value=summary) + ".")
    if pros:
        lines.append(copy["pros"].format(value=", ".join(pros[:3])))
    if cons:
        lines.append(copy["cons"].format(value=", ".join(cons[:2])))
    rating = product.get("rating")
    count = int(product.get("review_count") or 0)
    if rating is not None and count > 0:
        rating_text = f"{float(rating):.1f}"
        if language != "en":
            rating_text = rating_text.replace(".", ",")
        lines.append(copy["rating"].format(rating=rating_text, count=count))
    has_evidence = bool(lines)
    return (chr(10).join(lines) if lines else copy["empty"].format(name=name), has_evidence)


def _review_choice_chips(refs: list[ProductRef], language: str) -> list[str]:
    """Opțiunile de dezambiguizare („pentru care produs?"). Ordinalul rămâne în față: ancora din
    `reference_resolver` se rezolvă pe el, deci e parte din rutare, nu decor."""
    template = _review_copy(language)["chip"]
    return [
        fit_chip(template, f"{index}. {ref.name}") for index, ref in enumerate(refs[:4], start=1)
    ]


def _review_next_steps(language: str) -> list[str]:
    """Pașii de după un răspuns din recenzii, ca mesaje de client. Cuvintele-cheie rămân cele pe
    care le prind `_DETAIL_RE` / `_LINK_RE` — altfel chip-ul sună mai bine și rutează mai prost."""
    if language == "en":
        return ["Tell me more about it", "Send me the product link", "Add it to my cart"]
    return ["Spune-mi mai multe despre el", "Trimite-mi linkul la produs", "Adaugă-l în coș"]


async def serve_reviews(
    ctx: TurnContext, deps: PipelineDeps, product_id: str, name: str = ""
) -> None:
    """Recenziile unui produs DEJA rezolvat. Extras ca API TYPED (NX-236): o acțiune opacă poartă
    `product_ref`, deci nu are ce „rezolva" — sare peste deixis și intră direct aici, pe exact
    aceleași porți (safety gate, evidence, carduri). Un al doilea drum ar fi un al doilea set de
    reguli de siguranță."""
    async with deps.db("review_intent_product") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, [product_id], limit=1)
    products = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose="review_intent")[0]
    if not products:
        ctx.set_reply(_review_copy(ctx.language)["empty"].format(name=name), cacheable=False)
        ctx.emit("review_intent", served=0, reason="unavailable")
        return

    product = products[0]
    text, has_evidence = _review_answer(product, ctx.language)
    ctx.retrieval = RetrievalResult(products=products, source="review_intent")
    ctx.set_reply(text, products=_card_products(products, n=1), cacheable=False)
    ctx.reply.suggestions = _review_next_steps(ctx.language)
    ctx.emit("review_intent", served=1, has_evidence=has_evidence)


async def _handle_review_intent(ctx: TurnContext, deps: PipelineDeps, query: str) -> None:
    """Răspunde din recenziile produsului afișat; nu lasă modelul să aleagă ancora."""
    refs = _anchor_refs(ctx)  # NX-234: setul afișat + produsul PAGINII (dacă există)
    selected = (await _memo(ctx, ("anchor",), lambda: _anchor(ctx, query)))[0]
    if selected is None:
        ctx.set_clarify(
            _review_copy(ctx.language)["which"],
            field="product_for_reviews",
            resume_route=Route.SALES.value,
            suggestions=_review_choice_chips(refs, ctx.language),
        )
        ctx.emit("review_intent", served=0, reason="ambiguous", candidates=len(refs))
        return
    await serve_reviews(ctx, deps, selected.product_id, selected.name)


def _detail_copy(language: str) -> dict[str, str]:
    if language == "en":
        return {
            "which": "Which product would you like more details about?",
            "why": "Why I recommend it",
            "features": "Main features",
            "usage": "How to use it",
            "reviews": "What customers say",
            "empty_features": "I don't have additional specifications for this product yet.",
            "unavailable": "That product is no longer available, so I can't show reliable details.",
            "chip": "Tell me more about {name}",
            "review_chip": "What do the reviews say about it?",
            "link_chip": "Send me the product link",
            "compare_chip": "Compare it with a similar product",
        }
    return {
        "which": "Pentru care produs vrei mai multe detalii?",
        "why": "De ce ți-l recomand",
        "features": "Caracteristici principale",
        "usage": "Cum se folosește",
        "reviews": "Ce spun clienții",
        "empty_features": "Nu am încă specificații suplimentare pentru acest produs.",
        "unavailable": "Produsul nu mai este disponibil, așa că nu îți pot arăta detalii sigure.",
        "chip": "Spune-mi mai multe despre {name}",
        "review_chip": "Ce spun recenziile despre el?",
        "link_chip": "Trimite-mi linkul la produs",
        "compare_chip": "Compară-l cu un produs similar",
    }


def _detail_choice_chips(refs: list[ProductRef], language: str) -> list[str]:
    """Vezi `_review_choice_chips`: ordinalul e ancora, numele cedează primul la scurtare."""
    template = _detail_copy(language)["chip"]
    return [fit_chip(template, f"{i}. {ref.name}") for i, ref in enumerate(refs[:4], 1)]


def _section_text(product: dict, pack: Any, kinds: Sequence[str]) -> str | None:
    """Prima secțiune de fișă din `kinds` (în ordinea dată), tăiată la plafonul din pachet. PURĂ.

    NX-319: plafonul vine din `detail_sections`, ca în vederea de detaliu a modelului
    (`catalog_tools._detail`), deci clientul și modelul citesc aceeași porție din fișă. Textul cu
    claim medical nu iese (aceeași poartă ca rezumatul, stagiul 8)."""
    caps = {s.kind: s.max_chars for s in (getattr(pack, "detail_sections", ()) or ())}
    by_kind: dict[str, str] = {}
    for sec in product.get("sections") or []:
        if isinstance(sec, dict) and sec.get("body") and sec.get("kind"):
            by_kind.setdefault(str(sec["kind"]), str(sec["body"]))
    for kind in kinds:
        body = " ".join(by_kind.get(kind, "").split())
        if not body:
            continue
        text = cut_at_sentence(body, caps.get(kind, _DEFAULT_SECTION_CAP))
        if text and not has_medical_claim(text):
            return text
    return None


#: Plafonul unei secțiuni pe care pachetul nu o declară în `detail_sections`.
_DEFAULT_SECTION_CAP = 240


def _detail_answer(product: dict, ctx: TurnContext) -> str:
    """Build a product deep-dive exclusively from catalog facets and review aggregates.

    NX-319: „de ce" cădea pe NUMELE ÎNTREG al produsului (`ai_summary` e gol pe tot catalogul
    SOLE), adică o frază de marketing de 100+ caractere sub un titlu care promite un motiv. Acum
    vine din secțiunea `summary` a fișei, iar numele e cel scurt (`display_name`, NX-301). Se
    adaugă „cum se folosește" din `howto_sections` (NX-315), când fișa îl are."""
    copy = _detail_copy(ctx.language)
    pack = getattr(ctx.business, "domain_pack", None)
    summary = " ".join(str(product.get("ai_summary") or "").split()).strip()
    if not summary or has_medical_claim(summary):
        summary = _section_text(product, pack, ("summary",)) or display_name(
            str(product.get("name") or "Produs")
        )

    facet_text = compose.facet_summary(
        product, (pack.comparison_facets if pack else ()), ctx.language
    )
    facts = [part.strip() for part in facet_text.split(";") if part.strip()][:6]
    feature_lines = [f"✓ {fact}" for fact in facts]
    features = "\n".join(feature_lines) or copy["empty_features"]
    reviews, _ = _review_answer(product, ctx.language)
    howto = _section_text(product, pack, tuple(getattr(pack, "howto_sections", ()) or ()))
    usage = f"{copy['usage']}\n\n{howto}\n\n" if howto else ""
    return (
        f"{copy['why']}\n\n{summary}\n\n"
        f"{copy['features']}\n\n{features}\n\n"
        f"{usage}"
        f"{copy['reviews']}\n\n{reviews}"
    )


async def serve_details(
    ctx: TurnContext,
    deps: PipelineDeps,
    product_id: str,
    *,
    lead: Callable[[dict[str, Any]], str | None] | None = None,
    gated: list[dict[str, Any]] | None = None,
) -> None:
    """Detaliile unui produs DEJA rezolvat (vezi `serve_reviews` pentru de ce e extras).

    `lead` (NX-316 `fit_question`): o frază calculată din produsul PROASPĂT citit, pusă înaintea
    detaliilor. Primește produsul după safety gate, deci nu poate răspunde despre unul exclus.

    `gated` (NX-381): produsul DEJA citit și trecut prin poarta de siguranță în acest tur (de
    răspunsul la întrebare); fără el, a doua citire ar re-emite evenimentul de blocare."""
    if gated is None:
        async with deps.db("detail_intent_product") as conn:
            products = await get_products_by_ids(conn, ctx.business.id, [product_id], limit=1)
        products = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose="detail_intent")[0]
    else:
        products = list(gated)
    if not products:
        ctx.set_reply(_detail_copy(ctx.language)["unavailable"], cacheable=False)
        ctx.emit("detail_intent", served=0, reason="unavailable")
        return

    product = products[0]
    copy = _detail_copy(ctx.language)
    ctx.retrieval = RetrievalResult(products=products, source="detail_intent")
    answer = _detail_answer(product, ctx)
    first = lead(product) if lead is not None else None
    if first:
        answer = f"{first}\n\n{answer}"
    ctx.set_reply(answer, products=_card_products(products, n=1), cacheable=False)
    ctx.reply.suggestions = [copy["review_chip"], copy["link_chip"], copy["compare_chip"]]
    ctx.emit("detail_intent", served=1)


async def _handle_detail_intent(ctx: TurnContext, deps: PipelineDeps, query: str) -> None:
    refs = _anchor_refs(ctx)  # NX-234: setul afișat + produsul PAGINII (dacă există)
    selected = (await _memo(ctx, ("anchor",), lambda: _anchor(ctx, query)))[0]
    if selected is None:
        ctx.set_clarify(
            _detail_copy(ctx.language)["which"],
            field="product_for_details",
            resume_route=Route.SALES.value,
            suggestions=_detail_choice_chips(refs, ctx.language),
        )
        ctx.emit("detail_intent", served=0, reason="ambiguous", candidates=len(refs))
        return
    await serve_details(ctx, deps, selected.product_id)


async def _handle_link_intent(
    ctx: TurnContext, deps: PipelineDeps, ids: list[str] | None = None
) -> None:
    """Servește o cerere de LINK pe produsele DEJA arătate, FĂRĂ bucla LLM (NX-131) — ca
    show_more/cheaper. State ține doar ref-uri (P8) → fetch `product_url` PROASPĂT din catalog
    (sursa de adevăr). Link real → Offer(open_url) + card(uri); `product_url` NULL (gaură de date
    demo) → mesaj ONEST, NU link inventat (PP-F4). Mereu setează un reply (P6, niciodată tăcere).

    NX-234: „unde-l cumpăr?" pe o pagină de produs, fără nimic afișat înainte, avea zero ancore —
    ancora paginii intră în set, cu URL-ul luat tot din catalog (`product_url`), niciodată din
    browser (un URL afirmat de host ar fi exact linkul pe care nu-l putem valida).

    NX-316: `ids` date = produsele NUMITE de un chip recunoscut. Fără ele, handlerul servea
    linkurile tuturor produselor afișate la „Trimite-mi linkul la X"."""
    if ids is None:
        ids = [p.product_id for p in _anchor_refs(ctx)]
    async with deps.db("link_intent_products") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, ids, limit=6)
    # NX-173 (P0): `displayed_products` e STATE VECHI — poate conține produse afișate ÎNAINTE ca
    # clientul să declare contextul („arată-mi seruri" → „sunt însărcinată, dă-mi linkul"). Calea
    # asta iese înainte de tool loop, deci backstop-ul din executor n-o vede: gate aici, ori deloc.
    products = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose="link")[0]
    ctx.retrieval = RetrievalResult(products=products, source="link_intent")
    with_url = [p for p in products if p.get("url")]
    if not with_url:
        # Fără product_url → onest + pasul care există. NU re-afișăm cardul (ar repeta exact ce a
        # frustrat clientul în bucla veche); doar mesajul onest. cacheable=False (context-specific).
        ctx.emit("link_intent", served=0, in_context=len(products))
        ctx.set_reply(_no_link_msg(ctx.language), cacheable=False)
        return
    ctx.emit("link_intent", served=len(with_url))
    cards = _card_products(with_url, n=6)
    if len(with_url) == 1:
        # Un singur produs țintă → buton CTA (open_url). set_reply ÎNTÂI (creează reply-ul), apoi
        # set_offer (îl mută pe el — ordinea contează: set_offer cere un reply deja setat).
        ctx.set_reply(_link_lead(ctx.language, many=False), products=cards, cacheable=False)
        ctx.set_offer(
            Offer(kind="open_url", label=_view_label(ctx.language), url=with_url[0]["url"])
        )
    else:
        # Mai multe → cardurile SUNT linkurile (fiecare cu url-ul lui); fără un buton unic arbitrar.
        ctx.set_reply(_link_lead(ctx.language, many=True), products=cards, cacheable=False)


def _comparison_facets(ctx: TurnContext) -> tuple:
    """Tier 2 (IZI-parity): fațetele de DOMENIU din DomainPack pentru tabelul de comparație
    (finish/acoperire/potrivit-pentru/..., din products.attributes), gated de kill-switch. OFF /
    fără pack → () → tabelul are doar rândurile generice (preț/rating/avantaje/brand), ca azi."""
    if not get_settings().comparison_facets_enabled:
        return ()
    pack = getattr(ctx.business, "domain_pack", None)
    return pack.comparison_facets if pack else ()


async def _handle_compare_intent(ctx: TurnContext, deps: PipelineDeps, query: str) -> bool:
    """Servește o COMPARAȚIE pe produsele DEJA afișate, FĂRĂ bucla LLM (G2, IZI-parity) — ca
    link/show_more/cheaper. State ține doar ref-uri (P8) → re-fetch detaliile proaspete (preț/
    rating/avantaje/minusuri/disponibilitate/brand) → tabel structurat. „Fără bucla LLM" rămâne
    adevărat pentru RETRIEVAL (nimic nu se re-caută), dar tabelul NU mai e integral determinist:
    `build_comparison` produce plasa, iar `compare_narrative` compune peste ea axele de decizie și
    îndrumarea, cu porți de grounding pe fiecare celulă. Implicit primele 2 (perechea = cazul
    dominant „compară primele două"); un număr explicit poate extinde tabelul la 3 sau 4.
    `get_products_by_ids` păstrează ORDINEA afișată (deixis ordinal corect). <2 valide → False →
    cade pe bucla LLM (caută/compară fresh). True = a servit turul."""
    n = 4 if _FOUR_RE.search(query) else (3 if _THREE_RE.search(query) else 2)
    ids = [p.product_id for p in ctx.state.displayed_products][:n]
    return await _memo(ctx, ("compare", tuple(ids)), lambda: serve_comparison(ctx, deps, ids))


async def serve_comparison(
    ctx: TurnContext,
    deps: PipelineDeps,
    ids: list[str],
    *,
    withhold: Callable[[list[dict[str, Any]]], str | None] | None = None,
) -> bool:
    """Tabelul de comparație pe ID-uri DEJA rezolvate (extras pentru NX-236, ca `serve_reviews`).

    Aceleași porți ca pe calea text: safety gate, coerență de categorie, `build_comparison`. O
    acțiune opacă poartă `product_refs` explicite, deci reordonarea listei afișate între emitere
    și click NU poate schimba ce se compară — exact invariantul din failure matrix.

    `withhold` (NX-336 C2, I12, calea kernelului): judecă produsele încărcate și întoarce `None`
    când verdictul e permis, altfel închiderea care îl înlocuiește (fraza care numește ce lipsește,
    sau `""` fără frază). Verdictul reținut ⇒ compunerea fără sloturi de verdict. Fără `withhold` ⇒
    comportamentul de azi."""
    n = max(2, len(ids))
    async with deps.db("compare_intent_products") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, ids, limit=n)
    # NX-173 (P0): ca la link — set vechi din state, cale care ocolește tool loop-ul. Dacă
    # gate-ul taie sub 2, întoarcem False → cade pe bucla LLM, care caută FRESH (deja filtrat de
    # policy) — nu comparăm tăcut ce a mai rămas și nu prezentăm produsul exclus.
    products = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose="compare_intent")[0]
    if len(products) < 2:
        return False
    # NX-167 (C): nu compara produse din ramuri INCOERENTE (root-branch diferit din
    # `categories.path`, ex. machiaj vs. par) — tabelul „fond vs. accesoriu de păr" e absurd.
    # Fail-open la `path` lipsă (produse fără root nu contează). ≥2 root-uri distincte → `return
    # False` → cade pe bucla LLM (poate re-căuta coerent), nu un mesaj-perete. Kill-switch OFF →
    # comportamentul vechi (compară orice 2 afișate).
    if get_settings().compare_coherence_guard_enabled and len(products) >= 2:
        # Lipsa metadatelor este fail-open, la fel ca produse fără path: comparația rămâne utilă
        # chiar dacă query-ul opțional de coerență nu poate rula într-un adaptor degradat.
        try:
            async with deps.db("product_category_roots") as conn:
                roots = await product_category_roots(
                    conn, ctx.business.id, [p["id"] for p in products]
                )
        except Exception:  # noqa: BLE001 — metadate opționale; produsele sunt deja grounded
            roots = {}
            ctx.emit("compare_coherence_unavailable")
        distinct = {r for r in roots.values() if r}
        if len(distinct) >= 2:
            ctx.emit("compare_incoherent_blocked", n=len(products), root_branches=len(distinct))
            return False
    facets = _comparison_facets(ctx)
    comparison = compose.build_comparison(products, ctx.language, facets)
    if comparison is None:
        return False
    ctx.retrieval = RetrievalResult(products=products, source="compare_intent")
    note = withhold(products) if withhold is not None else None
    # Tabelul determinist e plasa; peste el, agentul compune axele pe care perechea chiar se
    # desparte + îndrumarea de sub tabel (respins/eșuat → exact tabelul de mai sus, P6).
    if note is None:
        comparison = await compose_comparison(
            deps.llm, ctx, comparison, products, facets=facets, query=(ctx.message.body or "")
        )
    else:
        comparison = await compose_comparison(
            deps.llm,
            ctx,
            comparison,
            products,
            facets=facets,
            query=(ctx.message.body or ""),
            verdict=False,
        )
        comparison = replace(comparison, closing=[note] if note else [])
    ctx.set_comparison_reply(
        comparison,
        text=compose.flatten_comparison(comparison, ctx.language),
        products=compose.comparison_cards(comparison),
        chips=_compare_chips(comparison.columns, ctx.language),
    )
    ctx.emit("agent_compared", n=len(comparison.columns), deterministic=True)
    return True


def is_compare_with_similar(query: str, language: str | None) -> bool:
    """NX-319: mesajul e exact chip-ul NOSTRU «Compară-l cu un produs similar»? PUR.

    Nu e o intenție dedusă din text, e recunoașterea unui buton pe care serverul l-a emis sub
    detaliu (`_detail_copy`), ca la NX-316: textul re-randat trebuie să fie IDENTIC, după aceeași
    normalizare ca restul follow-up-urilor. O formulare liberă („compar-o cu altceva") nu intră
    aici și rămâne a modelului, fiindcă poate numi chiar produsul cu care vrea comparația."""
    wanted = _norm_followup(_detail_copy(language)["compare_chip"]).strip(" ?.!")
    return bool(query) and _norm_followup(query).strip(" ?.!") == wanted


def pick_similar_partner(candidates: list[dict[str, Any]]) -> str | None:
    """NX-319: primul candidat care NU e geamăn al ancorei. PURĂ.

    Geamăn = același nume AFIȘAT (nuanțe, gramaje, NX-313) sau același brand la același preț
    (culori listate ca produse separate: «GESKE SmartAppGuided Sonic Facial Brush | 5 in 1
    Magenta» e peria din turul real în altă culoare, cu alt nume afișat). O comparație între doi
    gemeni n-are axe: tabelul ar ieși cu toate rândurile egale. Ordinea candidaților e a
    interogării (substitut, tip, asemănare, preț)."""
    for c in candidates:
        anchor_name = display_name(str(c.get("anchor_name") or "")).casefold()
        if display_name(str(c.get("name") or "")).casefold() == anchor_name:
            continue
        same_brand = c.get("brand_id") is not None and c.get("brand_id") == c.get("anchor_brand_id")
        price, anchor_price = c.get("price"), c.get("anchor_price")
        same_price = (
            price is not None and anchor_price is not None and abs(price - anchor_price) < 0.005
        )
        if same_brand and same_price:
            continue
        return str(c["id"])
    return None


async def serve_compare_with_similar(ctx: TurnContext, deps: PipelineDeps, anchor_id: str) -> bool:
    """NX-319: comparația ancorei cu cel mai apropiat înlocuitor. False ⇒ bucla de model (P6)."""
    async with deps.db("similar_candidates") as conn:
        candidates = await similar_candidates(conn, ctx.business.id, anchor_id)
    partner = pick_similar_partner(candidates)
    if partner is None:
        ctx.emit("compare_with_similar", served=False, reason="no_partner", n=len(candidates))
        return False
    served = await serve_comparison(ctx, deps, [anchor_id, partner])
    ctx.emit(
        "compare_with_similar",
        served=served,
        reason=None if served else "comparison_refused",
        n=len(candidates),
    )
    return served


# Cuvintele care fac parte din FORMULA unei scurtături trăiesc în `query_terms.formula_fillers`
# (mutate de NX-329, fiindcă și extractorul de referințe le consumă). Ce se întâmplă când lista e
# incompletă (și va fi, la orice volum de clienți): cuvântul necunoscut rămâne în reziduu, turul
# pleacă la model și clientul primește răspunsul corect, plătit cu o inferență. Asta e toată miza
# formei alese — enumerăm mulțimea ÎNCHISĂ (cum se cere o scurtătură), nu pe cea DESCHISĂ (cum se
# rostește o constrângere), iar necunoscutul cade spre model, nu spre tăcere.
_formula_fillers = formula_fillers


def carries_new_constraints(ctx: TurnContext, trigger: re.Pattern[str]) -> bool:
    """Mesajul CURENT cere ALTCEVA decât scurtătura pe care a declanșat-o `trigger`? PUR, zero
    LLM/DB.

    Ăsta e predicatul care desparte o SCURTĂTURĂ („mai arată-mi", „dă-mi linkul", „compară-le") de
    o RAFINARE („mai arată-mi, dar sub 100 lei"). Prima se poate servi determinist din setul deja
    afișat; a doua trebuie să ajungă la model. Confuzia dintre ele nu produce o eroare vizibilă:
    paginăm pool-ul căutării vechi, produsele sunt reale, prețurile există în `ctx.retrieval`, deci
    validatorul (stagiul 8) și grounding guardul trec — sunt porți de ADEVĂR, nu de POTRIVIRE.
    Clientul primește un răspuns corect la altă întrebare, iar analytics-ul nu are ce raporta.

    Predicatul avea un singur producător: sloturile extrase de triaj (`RouteDecision.filters`). Cu
    triajul scos de pe drumul sincron (NX-251) ruta o construiește `agent_stage` cu `filters` gol la
    FIECARE tur, deci gardul `not route.filters` era permanent deschis și toate cele trei scurtături
    înghițeau rafinările.

    **Forma răspunsului contează mai mult decât acoperirea lui.** Un gard care ar enumera
    CONSTRÂNGERILE („sub N", „fără parfum", concern din pack) enumeră o mulțime DESCHISĂ: clienții
    nu scriu la fel, lista rămâne mereu în urmă, iar fiecare frază neprevăzută devine un răspuns
    greșit TĂCUT. Așa că întrebăm invers, pe mulțimea ÎNCHISĂ: **ce rămâne din mesaj după ce scoatem
    formula scurtăturii?** Scoatem potrivirea declanșatorului, referințele (ordinalele NX-234),
    cuvintele funcționale ale locale-i (`query_terms`, tabelul care hrănește deja căutarea lexicală)
    și `_FORMULA_FILLERS`. Orice rămâne = clientul a cerut ceva în plus, oricum ar fi scris-o:
    „dar sub 100 lei", „dar de la Cerave", „pentru ten gras", „ceva ce nu mă lucește" — niciuna
    dintre ele nu e prevăzută nicăieri, toate lasă reziduu.

    Măsurat pe un corpus de 24 de fraze, cele două forme ratează la fel de des, dar în direcții
    OPUSE: enumerarea constrângerilor ratează 7 rafinări (răspuns greșit, tăcut), reziduul ratează
    4 scurtături pure (o inferență în plus, răspuns corect).

    Locale necunoscută → fără stopwords și fără fillers → aproape orice text lasă reziduu → totul
    pleacă la model. Scurtăturile se sting, adevărul nu. E direcția corectă de degradare (P6) și
    motivul pentru care nu hardcodăm româna ca implicit (P11).

    **Aplicat DOAR pe paginare.** Pe porțile ANCORATE (link/compare) cuvintele în plus sunt de
    obicei o REFERINȚĂ la produsul deja arătat („linkul către crema asta"), pe care le interpretează
    `resolve_reference`; un reziduu lexical nu le poate deosebi de o rafinare, iar o cădere pe model
    ar risca regresia NX-131 (calea rich interzice modelului linkurile). Pe paginare nu există
    ambiguitatea asta: căderea pe model înseamnă o căutare nouă, adică exact ce a cerut clientul."""
    route = ctx.route
    if route is not None and route.filters:
        # Triajul a extras sloturi (calea de dinainte de NX-251) — nimic de recalculat.
        return True
    if not get_settings().refinement_guard_enabled:
        return False
    body = (ctx.message.body or "").strip()
    if not body:
        # Acțiune opacă (NX-236): mesajul e gol prin construcție, comanda e DECLARATĂ, nu dedusă.
        return False
    return bool(_shortcut_residue(body, trigger, ctx.language))


def _shortcut_residue(body: str, trigger: re.Pattern[str], locale: str | None) -> list[str]:
    """Ce rămâne din mesaj după scăderea formulei. Pur; expus pentru teste și pentru diagnostic.

    Ordinea scăderilor nu e arbitrară: întâi declanșatorul (el poate conține cuvinte purtătoare de
    sens — „alte opțiuni"), apoi referințele, apoi vocabularul funcțional."""
    text = fold(body)
    text = trigger.sub(" ", text)
    for pattern in ANY_ORDINAL_RE:
        text = pattern.sub(" ", text)
    skip = stopwords(locale) | _formula_fillers(locale)
    return [t for t in re.split(r"[^0-9a-z]+", text) if t and t not in skip]


def show_more_phrase(query: str) -> bool:
    """Textul CERE paginare („mai arată-mi"), independent de starea conversației. Pur.

    Extras din `is_show_more` ca apelantul să poată distinge „n-a cerut" de „a cerut, dar mesajul
    rafinează" — fără distincția asta, reparația de mai jos ar fi invizibilă în analytics."""
    return _MORE_RE.search(query) is not None and _CHEAPER_RE.search(query) is None


def turn_has_new_constraints(ctx: TurnContext, route: Any) -> bool:
    """Turul cere ceva ÎN PLUS față de ce se vede pe ecran? (NX-297 felia 3)

    Porțile ANCORATE (link, comparație, superlativ pe setul afișat) au nevoie de predicatul ăsta ca
    să nu servească tăcut produsul de bază când clientul a adăugat o condiție: «compară-le, dar sub
    100 lei» nu e o comparație pe setul afișat, e o căutare nouă.

    Avea un singur producător: sloturile triajului. Fără nano, `route.filters` e gol la fiecare tur
    și poarta ar rămâne permanent deschisă — aceeași clasă de defect ca la paginare (NX-251), doar
    pe alte TREI porți: `link_intent` și `compare_intent` de aici, plus `attr_query` din
    `planner.py`. Public (nu `_`) exact din motivul ăsta: a treia a rămas pe `not route.filters`
    într-o primă rundă fiindcă stătea în alt modul, iar o clasă reparată pe două din trei nu e
    reparată — e una în care defectul rămas e mai greu de găsit.

    A doua sursă e DETERMINISTĂ și, spre deosebire de reziduul lexical de la paginare, e precisă:
    `extract_constraints` (NX-266) scoate din mesajul BRUT valorile cu unitate (preț, volum, indici
    declarați), folosind registrul de unități al TENANTULUI. „linkul la crema asta" n-are niciun
    număr cu unitate, deci poarta rămâne deschisă — corect. „dar sub 100 lei" are, deci se închide.

    **Ce NU acoperă, declarat:** rafinările NE-numerice pe o poartă ancorată («compară-le, dar doar
    cele fără parfum»). Reziduul lexical le-ar prinde, dar pe porțile ancorate nu poate fi folosit:
    nu deosebește o referință („crema asta") de o rafinare, iar căderea pe model ar risca regresia
    NX-131. Rămâne consecința ASUMATĂ de la NX-251, nici lărgită, nici restrânsă de felia asta.
    """
    if route is not None and getattr(route, "filters", None):
        return True
    pack = getattr(ctx.business, "domain_pack", None)
    units = getattr(pack, "units", None)
    if units is None or not getattr(units, "specs", None):
        # Tenant fără tabel de unități: nu putem deosebi o cifră de o valoare („am 2 copii" nu e
        # un buget). Fail-OPEN, ca azi — poarta rămâne pe comportamentul pre-NX-297.
        return False
    message = (ctx.message.body or "").strip()
    if not message:
        return False  # acțiune opacă (NX-236): comanda e DECLARATĂ, nu dedusă din text
    spoken, _ = extract_constraints(message, units=units, locale=ctx.language)
    return bool(spoken)


def fit_lead(
    ctx: TurnContext, facet: str, key: str, phrase: str
) -> Callable[[dict[str, Any]], str | None]:
    """NX-316 `fit_question`: răspunsul la „X merge pentru {valoare}?" din FIȘA produsului.

    Da/nu vin din `attributes` (sursa fațetei declarată în pachet), nu din model: valoarea e în
    listă ⇒ `fit_yes`, lipsește dintr-o listă CUNOSCUTĂ ⇒ `fit_no`. Un „nu" cunoscut e un răspuns
    bun, nu o eroare. Fațetă necunoscută pe produs (fișa s-a schimbat între ture) sau pachet fără
    șablon ⇒ `None` ⇒ doar detaliile, fără o afirmație pe care n-o putem susține. Copy-ul e în
    pachet (`answer_shape_templates`, P11), iar valoarea e fraza chip-ului, deci aceleași
    cuvinte."""
    from src.agent.answer_shape import pack_template  # noqa: PLC0415
    from src.conversation.chip_moves import partitioning_sources  # noqa: PLC0415
    from src.conversation.clarification_policy import values_of  # noqa: PLC0415

    pack = getattr(ctx.business, "domain_pack", None)
    source = partitioning_sources(getattr(pack, "facets", ()) or ()).get(facet)

    def _lead(product: dict[str, Any]) -> str | None:
        if source is None:
            return None
        values = values_of(product, source)
        if not values:
            ctx.emit("fit_question", verdict="unknown")
            return None
        verdict = "yes" if key.lower() in values else "no"
        template = pack_template(pack, f"fit_{verdict}", ctx.language)
        ctx.emit("fit_question", verdict=verdict)
        return template.format(slot=phrase) if template else None

    return _lead


async def serve_chip_move(ctx: TurnContext, deps: PipelineDeps, move: Any) -> bool:
    """NX-316: servește apăsarea unui chip recunoscut. True = turul e servit; False = mergi mai
    departe pe drumul obișnuit (handler stins, sau comparația refuzată de porțile ei).

    Fiecare fel intră pe ACELAȘI handler ca varianta lui din text (`serve_details`,
    `serve_reviews`, `_handle_link_intent`, `serve_comparison`), deci porțile de siguranță, de
    coerență și de disponibilitate sunt aceleași. Se schimbă doar DE UNDE vin produsele."""
    from src.conversation.chip_press import facet_of, product_ids  # noqa: PLC0415 — ciclul

    settings = get_settings()
    ids = list(product_ids(move))
    handler = "agent"
    served = False
    if move.kind in ("choose_within", "routine_next", "similar_to"):
        # NX-316 felia 2/3: nu e un răspuns determinist, e un SET determinist (afișatele cu
        # valoarea, pasul de rutină, substitutele). Setul îl aduce planul (v1) sau seed-ul
        # creierului, care citesc `ctx.chip_move`; aici doar numărăm apăsarea.
        handler = move.kind
    elif not ids:
        pass
    elif move.kind == "fit_question" and getattr(settings, "detail_intent_enabled", True):
        facet_key = facet_of(move)
        if facet_key is not None:
            lead = fit_lead(ctx, *facet_key, move.slot_map().get("slot", ""))
            await serve_details(ctx, deps, ids[0], lead=lead)
            handler, served = "fit_question", True
    elif move.kind == "detail" and getattr(settings, "detail_intent_enabled", True):
        await serve_details(ctx, deps, ids[0])
        handler, served = "detail_intent", True
    elif move.kind == "reviews" and getattr(settings, "review_intent_enabled", True):
        await serve_reviews(ctx, deps, ids[0], move.slot_map().get("slot", ""))
        handler, served = "review_intent", True
    elif move.kind == "link" and settings.link_intent_enabled:
        await _handle_link_intent(ctx, deps, ids=ids[:1])
        handler, served = "link_intent", True
    elif move.kind == "compare" and settings.compare_intent_enabled and len(ids) >= 2:
        served = await serve_comparison(ctx, deps, ids[:2])
        handler = "agent_compared" if served else "agent"
    ctx.emit("chip_pressed", kind=move.kind, recognized=True, handler=handler)
    return served


@dataclass
class ShortcutMemo:
    """NX-336 (recenzia, constatarea 2): deciziile trecerii `exact_only`, refolosite de trecerea
    COMPLETĂ de după un `False` al kernelului, ca un tur căzut pe v1 să fie turul cu flagul stins:
    fără citiri dublate și fără evenimente dublate.

    Trecerea exactă ÎNREGISTREAZĂ fiecare pas cu I/O sau cu efecte (verdictul `_v2_shortcut`,
    rezolvarea ancorei, comparația cu un similar, comparația servită și refuzată): rezultatul și
    efectele lui (evenimentele și propunerile adăugate în context). Dacă trecerea nu servește
    turul, efectele se DETAȘEAZĂ din context (contextul rămâne cel de dinainte) și se păstrează pe
    cheie; trecerea completă, ajunsă la același pas, primește rezultatul și REDĂ efectele în locul
    în care le-ar fi produs ea, fără să recitească. Fiecare cheie se consumă o dată. Fără memo
    (flag stins) pașii rulează ca azi, byte cu byte."""

    recording: bool = True
    entries: dict[Any, tuple[Any, list[Any], list[Any]]] = field(default_factory=dict)
    ranges: list[tuple[Any, Any, int, int, int, int]] = field(default_factory=list)

    def detach(self, ctx: TurnContext, events_from: int, proposals_from: int) -> None:
        """Scoate din context efectele trecerii exacte și le păstrează pe cheie."""
        for key, result, e0, e1, p0, p1 in self.ranges:
            self.entries[key] = (result, ctx.events[e0:e1], ctx.state_proposals[p0:p1])
        del ctx.events[events_from:]
        del ctx.state_proposals[proposals_from:]
        self.ranges.clear()


_ACTIVE_MEMO: contextvars.ContextVar[ShortcutMemo | None] = contextvars.ContextVar(
    "shortcut_memo", default=None
)


async def _memo(ctx: TurnContext, key: Any, compute: Callable[[], Any]) -> Any:
    """Un pas memorat al scurtăturilor (vezi `ShortcutMemo`). Fără memo activ: `compute()`."""
    memo = _ACTIVE_MEMO.get()
    if memo is None:
        result = compute()
        return await result if inspect.isawaitable(result) else result
    if not memo.recording and key in memo.entries:
        result, events, proposals = memo.entries.pop(key)
        ctx.events.extend(events)
        ctx.state_proposals.extend(proposals)
        return result
    e0, p0 = len(ctx.events), len(ctx.state_proposals)
    result = compute()
    result = await result if inspect.isawaitable(result) else result
    if memo.recording:
        memo.ranges.append((key, result, e0, len(ctx.events), p0, len(ctx.state_proposals)))
    return result


async def _serve_exact_anchor(
    ctx: TurnContext,
    deps: PipelineDeps,
    query: str,
    route: Any,
    serve: Callable[[ProductRef], Any],
    trigger: re.Pattern[str],
) -> bool:
    """NX-336 (`exact_only`): recenzii / detaliu DOAR pe o ancoră rezolvată determinist și fără
    constrângeri noi în mesaj (`turn_has_new_constraints`, predicatul porților ancorate). Altfel
    `False`: nicio întrebare (întrebarea e a porții kernelului); rezolvarea e memorată, iar
    efectele ei (propunerea de selecție, evenimentul) se detașează și se redau pe trecerea
    completă."""
    if turn_has_new_constraints(ctx, route):
        return False
    selected, resolution = await _memo(ctx, ("anchor",), lambda: _anchor(ctx, query))
    if selected is None or not _anchor_is_exact(query, trigger, ctx.language, selected, resolution):
        return False
    await serve(selected)
    return True


#: Decizii ale resolverului care NUMESC ținta: acțiunea semnată, un ordinal pe lista afișată, un
#: nume unic pe ecran. `single` / `selected` / `page` sunt ancore IMPLICITE: exacte doar când
#: mesajul nu numește altceva (vezi `_anchor_is_exact`).
_NAMING_SOURCES: frozenset[str] = frozenset({"action", "ordinal", "named"})


def _own_chip_texts(language: str | None) -> tuple[str, ...]:
    """Textele chip-urilor NOASTRE fără nume de produs (sub un detaliu / sub recenzii), re-randate
    ca la `is_compare_with_similar`: o apăsare pe ele e o mutare a serverului, nu o descriere."""
    copy = _detail_copy(language)
    return (copy["review_chip"], copy["link_chip"], copy["compare_chip"]) + tuple(
        _review_next_steps(language)
    )


def _anchor_is_exact(
    query: str,
    trigger: re.Pattern[str],
    language: str | None,
    selected: ProductRef,
    resolution: ReferenceResolution,
) -> bool:
    """NX-336, a doua recenzie: ținta recenziilor/detaliului e EXACTĂ fără model? PUR.

    O decizie care numește ținta (acțiune, ordinal în listă, nume unic) e exactă. Una implicită
    (singurul card, focusul, pagina, inclusiv fallback-ul NX-234 pe pagină) e exactă DOAR dacă
    mesajul nu numește altceva: după scăderea formulei (`_shortcut_residue`, aceleași tabele per
    locale ca gardul de rafinare) nu rămâne nimic, sau rămân doar cuvinte din numele ANCOREI
    (chip-ul nostru «Spune-mi mai multe despre X»), sau mesajul e chiar textul unui chip al nostru.
    Altfel («ce părere au clienții despre Cerave?» cu COSRX pe ecran) turul e al interpretării.
    Conservator, declarat: un cuvânt al formulei pe care tabelele nu-l știu («clienții») trimite și
    un tur exact la interpretare (o inferență în plus, nu un răspuns greșit)."""
    if resolution.source in _NAMING_SOURCES:
        return True
    residue = _shortcut_residue(query, trigger, language)
    if not residue:
        return True
    asked = _norm_followup(query).strip(" ?.!")
    if any(asked == _norm_followup(chip).strip(" ?.!") for chip in _own_chip_texts(language)):
        return True
    name_words = set(re.split(r"[^0-9a-z]+", fold(selected.name or "")))
    return set(residue) <= name_words


def is_pure_pagination(ctx: TurnContext) -> bool:
    """NX-336 §1.1: «mai arată-mi» FĂRĂ reziduu rămâne pe v1, întreg. E exact predicatul paginării
    de azi (`is_show_more`, care include `carries_new_constraints`, NX-251): o paginare cu reziduu
    pleacă la interpretare. Numit separat ca ramura kernelului să spună ce întreabă."""
    return is_show_more(ctx)


async def try_pre_intents(
    ctx: TurnContext,
    deps: PipelineDeps,
    *,
    exact_only: bool = False,
    memo: ShortcutMemo | None = None,
) -> bool:
    """Faza B: intenții deterministe PRE-loop (link + compare). True = tratat (early-exit din
    `stage.py`); False = lasă bucla LLM. Doar SALES; toate exclud «mai ieftin» (cheaper_intent) și
    o căutare nouă (`route.filters`).

    NX-336: `exact_only=True` (ramura turului interpretat) păstrează DOAR ramurile al căror rezultat
    nu depinde de ghicit: chip-ul recunoscut și chip-ul nostru «compară-l cu un produs similar»,
    recenzii/detaliu pe o ancoră rezolvată fără constrângeri noi, link/comparație pe verdictul
    `served` al resolverului v2 (și `fallback`-ul lor când ecranul are o singură ancoră, respectiv
    exact două carduri). Restul întorc `False` fără efecte în context: pașii cu I/O se MEMOREAZĂ
    (`memo`), iar trecerea completă de după un `False` al kernelului, chemată cu ACELAȘI `memo`,
    îi refolosește fără să recitească și fără să re-emită. Fără memo și fără `exact_only` e funcția
    de azi, neatinsă."""
    if exact_only and memo is None:
        memo = ShortcutMemo()
    if memo is not None and not exact_only:
        # A doua recenzie (constatarea 5): DOAR trecerea exactă înregistrează. Un memo dat unei
        # treceri complete doar REDĂ ce a înregistrat trecerea exactă; altfel detașarea de la final
        # i-ar fi șters propriile evenimente.
        memo.recording = False
    if memo is None:
        return await _pre_intents(ctx, deps, exact_only=exact_only)
    token = _ACTIVE_MEMO.set(memo)
    events_from, proposals_from = len(ctx.events), len(ctx.state_proposals)
    try:
        served = await _pre_intents(ctx, deps, exact_only=exact_only)
    finally:
        _ACTIVE_MEMO.reset(token)
    if memo.recording:
        if not served:
            memo.detach(ctx, events_from, proposals_from)
        memo.ranges.clear()
        memo.recording = False
    return served


async def _pre_intents(ctx: TurnContext, deps: PipelineDeps, *, exact_only: bool) -> bool:
    route = ctx.route
    if route is None or route.route != Route.SALES:
        return False
    query = (ctx.message.body or "").strip()
    if not query:
        return False
    if exact_only and expresses_ordinal(query) and _zoom_screen(ctx):
        # kernel.v6.0 (verificarea independentă): pe un ecran de detaliu intrat dintr-o listă,
        # ordinalul numără LISTA (`references.zoomed_list`). Scurtăturile ar fi numărat ecranul de
        # un card, deci turul e al kernelului, care aplică regula. Calea v1 (fără `exact_only`)
        # rămâne neatinsă (I16). Urma stă în trace, nu în evenimente: trecerea exactă care nu
        # servește își scoate evenimentele (`ShortcutMemo.detach`), deci un eveniment n-ar ajunge
        # niciodată în analytics (recenzia finală v6.0).
        ctx.trace["shortcut_deferred_to_kernel"] = "zoomed_ordinal"
        return False

    # NX-316: un chip RECUNOSCUT (`ctx.chip_move`, scris de agent_stage) e o comandă declarată, nu
    # o intenție de dedus: produsele vin din `move_id`, deci „Compară A cu B" compară A și B, iar
    # „linkul la X" trimite linkul lui X, nu pe al tuturor produselor de pe ecran.
    move = getattr(ctx, "chip_move", None)
    if move is not None and await serve_chip_move(ctx, deps, move):
        return True

    # NX-319: chip-ul de sub detaliu, «Compară-l cu un produs similar». Ancora trebuie să fie
    # UNICĂ (detaliul arată un singur card); altfel „-l" nu are un referent sigur și turul rămâne
    # al modelului, ca înainte.
    settings = get_settings()
    if (
        getattr(settings, "compare_with_similar_enabled", False)
        and getattr(settings, "compare_intent_enabled", False)
        and is_compare_with_similar(query, ctx.language)
    ):
        refs = _anchor_refs(ctx)
        anchor = refs[0].product_id if len(refs) == 1 else None
        if anchor is not None and await _memo(
            ctx, ("similar", anchor), lambda: serve_compare_with_similar(ctx, deps, anchor)
        ):
            return True

    # Un follow-up de recenzii se referă la setul deja afișat chiar dacă triajul a propagat filtre
    # istorice. Cu mai multe produse, handlerul cere ancora în loc să aleagă primul card.
    # NX-234: `anchorable` include și produsul PAGINII — pe un PDP, „ce părere ai despre acesta?"
    # e ancorabil chiar cu zero produse afișate (exact cazul pe care cardul îl repară).
    displayed = ctx.state.displayed_products
    anchorable = _anchor_refs(ctx)
    raw_pending = getattr(ctx.state, "pending_question", None)
    pending = raw_pending if isinstance(raw_pending, dict) else {}
    explicit_review = _REVIEW_RE.search(_norm_followup(query)) is not None
    resolves_pending_review = (
        pending.get("field") == "product_for_reviews"
        and _resolve_review_product(query, displayed, ctx.language) is not None
    )
    review_intent = (
        getattr(get_settings(), "review_intent_enabled", True)
        and bool(anchorable)
        and (explicit_review or resolves_pending_review)
    )
    if review_intent:
        if exact_only:
            return await _serve_exact_anchor(
                ctx,
                deps,
                query,
                route,
                lambda p: serve_reviews(ctx, deps, p.product_id, p.name),
                _REVIEW_RE,
            )
        await _handle_review_intent(ctx, deps, query)
        return True

    explicit_detail = _DETAIL_RE.search(_norm_followup(query)) is not None
    resolves_pending_detail = (
        pending.get("field") == "product_for_details"
        and _resolve_review_product(query, displayed, ctx.language) is not None
    )
    detail_intent = (
        getattr(get_settings(), "detail_intent_enabled", True)
        and bool(anchorable)
        and (explicit_detail or resolves_pending_detail)
    )
    if detail_intent:
        if exact_only:
            return await _serve_exact_anchor(
                ctx,
                deps,
                query,
                route,
                lambda p: serve_details(ctx, deps, p.product_id),
                _DETAIL_RE,
            )
        await _handle_detail_intent(ctx, deps, query)
        return True

    # NX-131: cerere de LINK pe un produs deja arătat = intenție DETERMINISTĂ, NU re-recomandare.
    # Calea rich interzice modelului linkurile → cererea de link cădea în re-randarea bogată cu
    # coaching repetat. O servim direct din state → product_url proaspăt → Offer(open_url) + card.
    #
    # GARDUL DE RAFINARE NU E APLICAT AICI, deliberat (vezi `carries_new_constraints`). Pe o poartă
    # ANCORATĂ, cuvintele în plus sunt de obicei REFERINȚĂ, nu cerere nouă: „linkul către crema
    # asta" numește produsul, iar `resolve_reference` e cel care le interpretează. Un reziduu
    # calculat lexical nu poate deosebi „crema asta" (referință) de „varianta de 200ml" (rafinare),
    # iar căderea pe model ar risca exact regresia pe care NX-131 a reparat-o. Consecința ASUMATĂ:
    # „dă-mi linkul, dar la varianta de 200ml" servește linkul produsului de bază, tăcut. Se închide
    # cu vocabularul de categorii al tenantului (P9, `categories`), care cere un citit de DB pe
    # calea asta — decizie de măsurat, nu de ghicit.
    link_intent = (
        get_settings().link_intent_enabled
        and bool(anchorable)
        and not turn_has_new_constraints(ctx, route)
        and _LINK_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )
    if link_intent:
        if _resolver_v2_enabled():
            decision = await _memo(
                ctx, ("v2", "link"), lambda: _v2_shortcut(ctx, deps, query, "link")
            )
            if decision is not None:
                if decision.action == "model":
                    return False
                if exact_only and decision.action != "served" and len(anchorable) != 1:
                    # `fallback` = toate ancorele: o ghicire, cu excepția unei singure ancore
                    # (recenzia NX-336, constatarea 3: «Trimite-mi linkul» sub un detaliu).
                    return False
                await _handle_link_intent(
                    ctx, deps, list(decision.ids) if decision.action == "served" else None
                )
                return True
        if exact_only:
            return False  # `_link_targets` (NX-326) ghicește pe cuvinte, fără catalog
        ids = _link_targets(ctx, query, anchorable)
        if ids is None:
            await _handle_link_intent(ctx, deps)  # apelul de dinainte, neschimbat
        else:
            await _handle_link_intent(ctx, deps, ids)
        return True

    # IZI-parity G2: COMPARAȚIE pe setul afișat → tabel structurat determinist, fără să depindem de
    # model. ≥2 afișate; fără filtre noi; NU «mai ieftin». <2 valide la fetch → handler False.
    # Aceeași excepție ca la link: poartă ancorată, gardul de rafinare nu se aplică.
    compare_intent = (
        get_settings().compare_intent_enabled
        and len(ctx.state.displayed_products) >= 2
        and not turn_has_new_constraints(ctx, route)
        and _COMPARE_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )
    if not compare_intent:
        return False
    if _resolver_v2_enabled():
        decision = await _memo(
            ctx, ("v2", "compare"), lambda: _v2_shortcut(ctx, deps, query, "compare")
        )
        if decision is not None:
            if decision.action == "served":
                ids = list(decision.ids)[:4]
                return await _memo(
                    ctx, ("compare", tuple(ids)), lambda: serve_comparison(ctx, deps, ids)
                )
            if decision.action == "model":
                return False
            if exact_only and len(ctx.state.displayed_products) != 2:
                # `fallback` = primele două afișate: o ghicire, cu excepția a exact două
                # carduri (recenzia NX-336, constatarea 3).
                return False
            return await _handle_compare_intent(ctx, deps, query)
    if exact_only:
        return False  # calea NX-326 (`named_targets`) și „primele două" ghicesc
    if getattr(get_settings(), "named_shortcut_targets_enabled", False):
        displayed_refs = list(ctx.state.displayed_products)
        targets = named_targets(query, displayed_refs, locale=ctx.language)
        if targets.source == "named" and len(targets.indices) >= 2:
            ids = [displayed_refs[i].product_id for i in targets.indices][:4]
            _emit_shortcut_targets(ctx, "compare", targets, outcome="served")
            return await serve_comparison(ctx, deps, ids)
        if targets.source != "none":
            # O singură țintă («compară X cu celelalte») sau un cuvânt comun mai multor produse: nu
            # există un set sigur de comparat, iar „primele două" ar fi exact defectul B2.
            _emit_shortcut_targets(ctx, "compare", targets, outcome="model")
            return False
        _emit_shortcut_targets(ctx, "compare", targets, outcome="fallback")
    return await _handle_compare_intent(ctx, deps, query)


def _emit_shortcut_targets(
    ctx: TurnContext, gate: str, targets: NamedTargets, *, outcome: str
) -> None:
    ctx.emit(
        "shortcut_targets",
        gate=gate,
        source=targets.source,
        n=len(targets.candidates or targets.indices),
        outcome=outcome,
    )


def _resolver_v2_enabled() -> bool:
    settings = get_settings()
    return bool(
        getattr(settings, "named_shortcut_targets_enabled", False)
        and getattr(settings, "reference_resolver_v2_shortcuts_enabled", False)
    )


@dataclass(frozen=True)
class ShortcutDecision:
    """NX-329 — ce face o scurtătură cu referințele rezolvate de resolverul v2.

    `served` = țintele sunt sigure (`ids`, în ordinea mesajului); `fallback` = mesajul nu numește
    nimic, deci comportamentul de dinainte (toate ancorele / primele N); `model` = o referință nu
    se poate onora pe scurtătură (negăsită, ștearsă, ambiguă în afara ecranului, o proprietate), iar
    modelul poate căuta. `reason` e al primei referințe care a trimis turul la model."""

    action: Literal["served", "fallback", "model"]
    ids: tuple[str, ...] = ()
    reason: str | None = None


#: Unde poate trăi o ambiguitate pe care linkul o servește ca atare (candidații sunt pe ecran).
_ON_SCREEN: frozenset[str] = frozenset({"shown_now", "page"})


def decide_shortcut(
    gate: str, refs: Sequence[Reference], resolved: Sequence[ResolvedRef]
) -> ShortcutDecision:
    """Politica scurtăturilor pe verdictele resolverului. PURĂ (tabelul din cardul NX-329 §5).

    Link: `exact` servește; `ambiguous` servește candidații DOAR dacă sunt pe ecran (act read-only:
    răspunsul despre toți bate o întrebare); orice altceva pleacă la model. Comparație: cere ≥ 2
    produse DISTINCTE `exact`, altfel modelul. Fără nicio referință: comportamentul de dinainte."""
    if not refs:
        return ShortcutDecision("fallback")
    act = Act(kind=gate, targets=[r.id for r in refs], query=None)  # type: ignore[arg-type]
    for check in gate_act_targets([act], resolved):
        if check.verdict != "ok":
            return ShortcutDecision("model", reason=check.verdict)
    if gate == "compare" and (paired := _paired_ambiguity(resolved)):
        return ShortcutDecision("served", paired)
    ids: list[str] = []
    for r in resolved:
        read_only_ambiguity = gate == "link" and r.outcome == "ambiguous" and r.source in _ON_SCREEN
        if r.outcome != "exact" and not read_only_ambiguity:
            return ShortcutDecision("model", reason=r.reason or r.outcome)
        ids += [pid for pid in r.product_ids if pid not in ids]
    if gate == "compare" and len(ids) < 2:
        return ShortcutDecision("model", reason="single_target")
    return ShortcutDecision("served", tuple(ids))


def _paired_ambiguity(resolved: Sequence[ResolvedRef]) -> tuple[str, ...]:
    """Referințe ambigue care, ÎMPREUNĂ, numesc exact un set. PURĂ.

    «Compară MUZIGAE MANSION Objet Water cu MUZIGAE MANSION…» pe un ecran cu două carduri cu
    nume identic: fiecare referință e ambiguă între cele două, dar două referințe pe exact două
    produse, într-o comparație (care cere produse DISTINCTE), nu lasă nicio alegere. Găsit de sonda
    NX-329 pe chip-uri reale; calea NX-326 le compara, iar v2 le trimitea la model. Doar pe ecran
    (candidații sunt cei văzuți) și doar când numărul candidaților e EXACT numărul referințelor:
    șase carduri identice pentru două referințe rămân ale modelului. Ordinea e cea din resolver.
    DOAR ambiguitatea de NUME (`name_shared`): una de ordinal în afara ecranului sau fără focus
    («compară-l pe al treilea cu celălalt» pe două carduri) cere un produs care nu există, deci
    perechea de pe ecran nu e ce a cerut clientul."""
    if len(resolved) < 2:
        return ()
    first = resolved[0].product_ids
    same = all(
        r.outcome == "ambiguous"
        and r.reason == "name_shared"
        and r.source in _ON_SCREEN
        and set(r.product_ids) == set(first)
        for r in resolved
    )
    return tuple(first) if same and len(set(first)) == len(resolved) else ()


def page_source(ctx: TurnContext) -> ShownItem | None:
    """Produsul PAGINII (NX-234) ca sursă a resolverului, din snapshotul rehidratat al turului.
    O singură construcție pentru scurtăturile v2 și pentru turul interpretat (NX-336), care o
    adaugă la `references.sources_from_state` (starea nu știe de pagină). Prețul necunoscut al
    ancorei (`ProductRef.price == 0.0`) rămâne necunoscut, nu zero."""
    page = _page_anchor_ref(ctx)
    return ShownItem(page.product_id, page.name, page.price or None) if page else None


def _zoom_screen(ctx: TurnContext) -> bool:
    """Ecranul e un detaliu intrat dintr-o listă (regula `references.zoomed_list`, starea v2)."""
    state = ctx.state_v2
    if not isinstance(state, ConversationStateV2):
        return False
    screen = [ShownItem(d.product_id, d.name, d.price) for d in ctx.state.displayed_products]
    earlier = [
        tuple(ShownItem(d.product_id, d.name, d.price) for d in s)
        for s in state.references.recent_sets
    ]
    return zoomed_list(screen, earlier) is not None


def _state_v2_sources(ctx: TurnContext) -> dict[str, Any]:
    """NX-331: seturile de mai devreme, setul parcat și focusul, din starea v2 (când e aprinsă).

    Fără stare v2 sursele rămân goale, exact ca înainte (I16). Id-urile lor sunt doar referințe:
    `plan_lookup` le revalidează pe catalog ca pe oricare altele (I1)."""
    state = ctx.state_v2
    if not isinstance(state, ConversationStateV2):
        return {}

    def items(refs: Iterable[Any]) -> tuple[ShownItem, ...]:
        return tuple(ShownItem(d.product_id, d.name, d.price) for d in refs)

    references = state.references
    return {
        "shown_earlier": tuple(items(s) for s in references.recent_sets),
        "parked": items(state.parked.shown) if state.parked else (),
        "focus": references.selected_product,
    }


async def _v2_shortcut(
    ctx: TurnContext, deps: PipelineDeps, query: str, gate: str
) -> ShortcutDecision | None:
    """NX-329 — referințele scurtăturii, rezolvate de resolverul v2 pe faptele recitite în tur.
    None = faptele n-au putut fi citite: apelantul cade pe calea NX-326 (P6, niciodată tăcere)."""
    trigger = _LINK_RE if gate == "link" else _COMPARE_RE
    refs = shortcut_references(
        query, trigger=trigger, locale=ctx.language, versus=gate == "compare"
    )
    resolved: list[ResolvedRef] = []
    if refs:
        sources = ReferenceSources(
            shown_now=tuple(
                ShownItem(p.product_id, p.name, p.price) for p in ctx.state.displayed_products
            ),
            page=page_source(ctx),
            **_state_v2_sources(ctx),
        )
        pack = getattr(ctx.business, "domain_pack", None)
        lookup = plan_lookup(refs, sources, pack=pack, locale=ctx.language)
        # Vocabularul judecă doar valori: un atribut, sau un nume pe care nu-l poartă niciun
        # produs de pe ecran. „linkul la al doilea" nu plătește încărcarea lui (~1 s cu cache rece).
        needs_vocab = bool(lookup.names) or any(r.kind == "attribute" for r in refs)
        try:
            vocab = await get_vocabulary(deps, ctx.business.id) if needs_vocab else None
            facts = await fetch_reference_facts(deps, ctx.business.id, lookup)
        except Exception:  # noqa: BLE001 — DB indisponibil: calea de dinainte, nu un tur picat
            log.warning("reference_v2_unavailable gate=%s", gate, exc_info=True)
            ctx.emit("reference_v2_unavailable", gate=gate)
            return None
        resolved = resolve_references(
            refs, sources, facts, vocab=vocab, pack=pack, locale=ctx.language
        )
        for r in resolved:
            ctx.emit(
                "reference_v2",
                gate=gate,
                kind=r.kind,
                source=r.source,
                outcome=r.outcome,
                reason=r.reason,
            )
    decision = decide_shortcut(gate, refs, resolved)
    source = (
        "none"
        if not refs
        else "unresolved"
        if decision.action == "model"
        else "ambiguous"
        if any(r.outcome == "ambiguous" for r in resolved)
        else "named"
    )
    ctx.emit(
        "shortcut_targets",
        gate=gate,
        source=source,
        n=len(decision.ids) or len(refs),
        outcome=decision.action,
        resolver="v2",
        reason=decision.reason,
    )
    return decision


def _link_targets(ctx: TurnContext, query: str, refs: list[ProductRef]) -> list[str] | None:
    """NX-326 (B1): produsele pe care cererea de link le NUMEȘTE, sau None = toate ancorele.

    Un cuvânt comun mai multor produse („The Fresh") servește candidații, nu tot ecranul: e un act
    read-only, deci răspunsul despre toți candidații bate o întrebare (designul kernelului, §D).
    Fără nicio țintă numită rămâne comportamentul de dinainte. Acolo se numără separat mesajele
    care aveau totuși cuvinte în plus (`unnamed_with_residue`): un nume care nu e pe ecran («linkul
    la Cerave») arată exact așa, iar rezolvarea lui în catalog e a resolverului v2 (pasul 2)."""
    if not getattr(get_settings(), "named_shortcut_targets_enabled", False):
        return None
    targets = named_targets(query, refs, locale=ctx.language)
    if targets.source == "named":
        _emit_shortcut_targets(ctx, "link", targets, outcome="served")
        return [refs[i].product_id for i in targets.indices]
    if targets.source == "ambiguous":
        _emit_shortcut_targets(ctx, "link", targets, outcome="served")
        return [refs[i].product_id for i in targets.candidates]
    residue = _shortcut_residue(query, _LINK_RE, ctx.language)
    _emit_shortcut_targets(
        ctx, "link", targets, outcome="unnamed_with_residue" if residue else "fallback"
    )
    return None


def is_show_more(ctx: TurnContext) -> bool:
    """Faza B: predicat de PAGINARE („mai arată-mi" pe o sesiune activă). Paginarea propriu-zisă
    (`continue_search_session`) rămâne în `stage.py`/GENERATE. `carries_new_constraints`:
    constrângeri NOI = RAFINARE (cade pe bucla LLM), nu paginare pură — poarta e critică aici,
    fiindcă ramura de paginare se consumă ÎNAINTEA creierului unic (`agent_stage`), deci un
    fals-pozitiv nu doar alege prost, ci scoate turul de sub model cu totul. NU pe «mai ieftin»
    (cheaper_intent)."""
    route = ctx.route
    if route is None or route.route != Route.SALES:
        return False
    # NX-236: acțiunea `show_more` e paginare DECLARATĂ, nu dedusă din text — mesajul unui buton e
    # gol prin construcție, deci regexul de mai jos n-ar avea ce prinde. Prospețimea sesiunii
    # (`fp`) e deja verificată în kernel; aici rămâne doar existența ei.
    if action_kind(ctx) == "show_more":
        return bool(get_settings().search_sessions_enabled and ctx.state.active_search)
    query = (ctx.message.body or "").strip()
    return (
        get_settings().search_sessions_enabled
        and bool(ctx.state.active_search)
        and show_more_phrase(query)
        and not carries_new_constraints(ctx, _MORE_RE)
    )
