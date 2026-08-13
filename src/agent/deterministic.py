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

import re
from typing import TYPE_CHECKING

from src.agent.fallbacks import (
    _card_products,
    _compare_chips,
    _link_lead,
    _no_link_msg,
    _view_label,
)
from src.agent.reference_resolver import (
    ActionAnchor,
    ReferenceRequest,
    normalize_for_match,
    page_anchor_from_snapshot,
    resolve_from_displayed,
    resolve_product_reference,
    resolve_reference,
)
from src.config import get_settings
from src.conversation.state_reducer import StateUpdateProposal
from src.db.queries.catalog import get_products_by_ids, product_category_roots
from src.models import Offer, ProductRef, RetrievalResult, Route, TurnContext
from src.safety.policy import SafetyPolicy
from src.web.action_models import action_command, action_kind
from src.worker import compose
from src.worker.text_scrub import has_medical_claim

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

# P1 (ARCH-product-retrieval): follow-up de PREȚ pe un set deja afișat → re-căutare DETERMINISTĂ a
# produselor strict mai ieftine (search_cheaper_than), NU re-rank pe setul afișat (R3). Precizie
# mare RO/HU/EN (comparativ/superlativ de preț). Un miss cade grațios pe comportamentul vechi (R3).
# Partajat cu planner-ul (NX-144): gating-ul link/compare/show_more exclude «mai ieftin».
_CHEAPER_RE = re.compile(
    r"\bmai\s+ieftin\w*|\bcea\s+mai\s+ieftin\w*|\bmai\s+accesibil\w*"
    r"|\bpre[țt]\s+mai\s+mic|\bmai\s+mic\s+la\s+pre[țt]|\bbuget\s+mai\s+mic"
    r"|\bprea\s+scump\w*|\bcam\s+scump\w*"
    r"|\bcheaper\b|\bcheapest\b|\bolcs[óo]bb\w*|\blegolcs[óo]bb\w*",
    re.IGNORECASE,
)

# IZI-parity (Tier 1, G2): intenție de COMPARAȚIE pe un set deja afișat → tabel DETERMINIST (ca
# cheaper/show_more/link), fără să depindem de modelul care cheamă `compare_products`. RO/EN/HU,
# agnostic de vertical. ÎNALTĂ PRECIZIE deliberat (gate-ul n-are recurs la model pe fals-pozitiv):
# DOAR verbul „a compara" + „versus/vs" + verbul HU. `compar[aăie]\w*` prinde compara/compară/
# comparați/comparație ȘI EN compare/comparison/comparing, dar NU „compartiment" (compar+t). Frazele
# laxe („ce diferență e între ele") cad pe calea model-driven (modelul cheamă compare_products) — nu
# le prindem determinist ca să nu confundăm „diferența dintre garanție și retur" cu o comparație.
_COMPARE_RE = re.compile(
    r"\bcompar[aăie]\w*|\bversus\b|\bvs\.?\b|\b[öo]sszehasonl\w*|\bhasonl[íi]ts\w*",
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
    r"|\bshow\s+more\b|\bmore\s+(?:options|products|results)\b|\bother\s+(?:options|ones)\b"
    r"|\bt[öo]bbet\b",  # HU: többet (mai mult)
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
    r"|\bwhere\s+(?:can\s+i\s+|to\s+)?(?:buy|get|find)\b"
    r"|\bhol\s+(?:tudom\s+)?(?:veszem|vehetem|megvenni|megveszem)\b",
    re.IGNORECASE,
)

# Follow-up de recenzii pe un set deja afișat. Rulează pe text normalizat fără diacritice, astfel
# încât aceeași intenție să funcționeze în RO/HU/EN și când clientul tastează fără diacritice.
_REVIEW_RE = re.compile(r"(?<![a-z0-9])(?:recenzi|parer|opini|review|velemen)[a-z]*", re.IGNORECASE)
_DETAIL_RE = re.compile(
    r"\b(?:spune|zi)-?mi\s+mai\s+multe\b"
    r"|\bmai\s+multe\s+detali\w*\b|\bdetali\w*\s+(?:despre|pentru)\b"
    r"|\b(?:vreau|as vrea)\s+detali\w*\b|\bmore\s+(?:details|about)\b"
    r"|\btell\s+me\s+more\b|\btovabbi\s+reszletek\b",
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


def _resolve_review_product(query: str, refs: list[ProductRef]) -> ProductRef | None:
    """Rezolvă deixis-ul fără LLM: produs unic, ordinal, nume complet sau tokeni unici.

    NX-234: logica trăiește acum în `src/agent/reference_resolver.py` (API comun, ca ancora
    paginii să folosească EXACT aceleași reguli). Semantica rămâne neschimbată."""
    resolution = resolve_from_displayed(query, refs)
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
    """Rezolvarea referinței, prin precedența UNICĂ din `reference_resolver`.

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
        return _resolve_review_product(query, refs)
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
            )
        )
    else:
        resolution = resolve_product_reference(
            query,
            refs,
            page=page_anchor_from_snapshot(ctx.snapshot),
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
        return refs[resolution.index]
    if resolution.product_id is None:
        return None
    if page is not None and resolution.product_id == page.product_id:
        return page
    # Rezolvat pe un id pe care NU-l putem hidrata aici (produs selectat într-un tur anterior, ieșit
    # din setul afișat): apelantul întreabă. Nu inventăm un `ProductRef` fără nume/preț canonice.
    return next((r for r in refs if r.product_id == resolution.product_id), None)


def _review_copy(language: str) -> dict[str, str]:
    if language == "en":
        return {
            "which": "Which product would you like reviews for?",
            "summary": "Review summary for {name}: {value}",
            "pros": "What customers liked: {value}.",
            "cons": "Worth considering: {value}.",
            "rating": "Rating: {rating}/5 from {count} reviews.",
            "empty": "I don't have enough review data for {name} yet.",
            "chip": "Reviews: {name}",
        }
    if language == "hu":
        return {
            "which": "Melyik termék véleményeit szeretnéd látni?",
            "summary": "A(z) {name} véleményeinek összefoglalója: {value}",
            "pros": "Amit a vásárlók kedveltek: {value}.",
            "cons": "Amit érdemes mérlegelni: {value}.",
            "rating": "Értékelés: {rating}/5, {count} vélemény alapján.",
            "empty": "Még nincs elég véleményadat a(z) {name} termékről.",
            "chip": "Vélemények: {name}",
        }
    return {
        "which": "Pentru care produs vrei să vezi recenziile?",
        "summary": "Rezumatul recenziilor pentru {name}: {value}",
        "pros": "Ce au apreciat clienții: {value}.",
        "cons": "De luat în calcul: {value}.",
        "rating": "Rating: {rating}/5 din {count} recenzii.",
        "empty": "Nu am încă suficiente date din recenzii pentru {name}.",
        "chip": "Recenzii: {name}",
    }


def _safe_review_text(value: object) -> str | None:
    text = str(value or "").strip().rstrip(".")
    return text if text and not has_medical_claim(text) else None


def _review_answer(product: dict, language: str) -> tuple[str, bool]:
    copy = _review_copy(language)
    name = str(product.get("name") or "produs")
    lines: list[str] = []
    summary = _safe_review_text(product.get("review_summary"))
    pros = [_safe_review_text(value) for value in (product.get("top_pros") or [])]
    cons = [_safe_review_text(value) for value in (product.get("top_cons") or [])]
    pros = [value for value in pros if value]
    cons = [value for value in cons if value]
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
    template = _review_copy(language)["chip"]
    return [
        template.format(name=f"{index}. {ref.name[:24].rstrip()}")[:40]
        for index, ref in enumerate(refs[:4], start=1)
    ]


def _review_next_steps(language: str) -> list[str]:
    if language == "en":
        return ["Tell me more", "View product", "Add to cart"]
    if language == "hu":
        return ["További részletek", "Termék megtekintése", "Kosárba"]
    return ["Spune-mi mai multe", "Vezi produsul", "Adaugă în coș"]


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
    selected = _resolve_anchor(ctx, query)
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
            "reviews": "What customers say",
            "empty_features": "I don't have additional specifications for this product yet.",
            "unavailable": "That product is no longer available, so I can't show reliable details.",
            "chip": "Details: {name}",
            "review_chip": "View reviews",
            "link_chip": "View product",
            "compare_chip": "Compare with another",
        }
    if language == "hu":
        return {
            "which": "Melyik termékről szeretnél további részleteket?",
            "why": "Miért ajánlom",
            "features": "Fő jellemzők",
            "reviews": "Amit a vásárlók mondanak",
            "empty_features": "Ehhez a termékhez még nincs további specifikációm.",
            "unavailable": (
                "Ez a termék már nem elérhető, ezért nem tudok megbízható részleteket mutatni."
            ),
            "chip": "Részletek: {name}",
            "review_chip": "Vélemények",
            "link_chip": "Termék megtekintése",
            "compare_chip": "Összehasonlítás",
        }
    return {
        "which": "Pentru care produs vrei mai multe detalii?",
        "why": "De ce ți-l recomand",
        "features": "Caracteristici principale",
        "reviews": "Ce spun clienții",
        "empty_features": "Nu am încă specificații suplimentare pentru acest produs.",
        "unavailable": "Produsul nu mai este disponibil, așa că nu îți pot arăta detalii sigure.",
        "chip": "Detalii: {name}",
        "review_chip": "Vezi recenzii",
        "link_chip": "Vezi produsul",
        "compare_chip": "Compară cu alt produs",
    }


def _detail_choice_chips(refs: list[ProductRef], language: str) -> list[str]:
    template = _detail_copy(language)["chip"]
    return [
        template.format(name=f"{i}. {ref.name[:22].rstrip()}")[:40]
        for i, ref in enumerate(refs[:4], 1)
    ]


def _detail_answer(product: dict, ctx: TurnContext) -> str:
    """Build a product deep-dive exclusively from catalog facets and review aggregates."""
    copy = _detail_copy(ctx.language)
    summary = " ".join(str(product.get("ai_summary") or "").split()).strip()
    if not summary or has_medical_claim(summary):
        summary = str(product.get("name") or "Produs")

    pack = getattr(ctx.business, "domain_pack", None)
    facet_text = compose.facet_summary(
        product, (pack.comparison_facets if pack else ()), ctx.language
    )
    facts = [part.strip() for part in facet_text.split(";") if part.strip()][:6]
    feature_lines = [f"✓ {fact}" for fact in facts]
    features = "\n".join(feature_lines) or copy["empty_features"]
    reviews, _ = _review_answer(product, ctx.language)
    return (
        f"{copy['why']}\n\n{summary}\n\n"
        f"{copy['features']}\n\n{features}\n\n"
        f"{copy['reviews']}\n\n{reviews}"
    )


async def serve_details(ctx: TurnContext, deps: PipelineDeps, product_id: str) -> None:
    """Detaliile unui produs DEJA rezolvat (vezi `serve_reviews` pentru de ce e extras)."""
    async with deps.db("detail_intent_product") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, [product_id], limit=1)
    products = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose="detail_intent")[0]
    if not products:
        ctx.set_reply(_detail_copy(ctx.language)["unavailable"], cacheable=False)
        ctx.emit("detail_intent", served=0, reason="unavailable")
        return

    product = products[0]
    copy = _detail_copy(ctx.language)
    ctx.retrieval = RetrievalResult(products=products, source="detail_intent")
    ctx.set_reply(
        _detail_answer(product, ctx), products=_card_products(products, n=1), cacheable=False
    )
    ctx.reply.suggestions = [copy["review_chip"], copy["link_chip"], copy["compare_chip"]]
    ctx.emit("detail_intent", served=1)


async def _handle_detail_intent(ctx: TurnContext, deps: PipelineDeps, query: str) -> None:
    refs = _anchor_refs(ctx)  # NX-234: setul afișat + produsul PAGINII (dacă există)
    selected = _resolve_anchor(ctx, query)
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


async def _handle_link_intent(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Servește o cerere de LINK pe produsele DEJA arătate, FĂRĂ bucla LLM (NX-131) — ca
    show_more/cheaper. State ține doar ref-uri (P8) → fetch `product_url` PROASPĂT din catalog
    (sursa de adevăr). Link real → Offer(open_url) + card(uri); `product_url` NULL (gaură de date
    demo) → mesaj ONEST, NU link inventat (PP-F4). Mereu setează un reply (P6, niciodată tăcere).

    NX-234: „unde-l cumpăr?" pe o pagină de produs, fără nimic afișat înainte, avea zero ancore —
    ancora paginii intră în set, cu URL-ul luat tot din catalog (`product_url`), niciodată din
    browser (un URL afirmat de host ar fi exact linkul pe care nu-l putem valida)."""
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
    rating/avantaje/minusuri/disponibilitate/brand) → tabel structurat DETERMINIST (build_comparison
    → zero proză LLM în celule). Implicit primele 2 (perechea = cazul dominant „compară primele
    două"); un număr explicit poate extinde tabelul la 3 sau 4. `get_products_by_ids` păstrează
    ORDINEA afișată (deixis ordinal
    corect). <2 valide → False → cade pe bucla LLM (caută/compară fresh). True = a servit turul."""
    n = 4 if _FOUR_RE.search(query) else (3 if _THREE_RE.search(query) else 2)
    ids = [p.product_id for p in ctx.state.displayed_products][:n]
    return await serve_comparison(ctx, deps, ids)


async def serve_comparison(ctx: TurnContext, deps: PipelineDeps, ids: list[str]) -> bool:
    """Tabelul de comparație pe ID-uri DEJA rezolvate (extras pentru NX-236, ca `serve_reviews`).

    Aceleași porți ca pe calea text: safety gate, coerență de categorie, `build_comparison`. O
    acțiune opacă poartă `product_refs` explicite, deci reordonarea listei afișate între emitere
    și click NU poate schimba ce se compară — exact invariantul din failure matrix."""
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
    comparison = compose.build_comparison(products, ctx.language, _comparison_facets(ctx))
    if comparison is None:
        return False
    ctx.retrieval = RetrievalResult(products=products, source="compare_intent")
    ctx.set_comparison_reply(
        comparison,
        text=compose.flatten_comparison(comparison, ctx.language),
        products=compose.comparison_cards(comparison),
        chips=_compare_chips(comparison.columns, ctx.language),
    )
    ctx.emit("agent_compared", n=len(comparison.columns), deterministic=True)
    return True


async def try_pre_intents(ctx: TurnContext, deps: PipelineDeps) -> bool:
    """Faza B: intenții deterministe PRE-loop (link + compare). True = tratat (early-exit din
    `stage.py`); False = lasă bucla LLM. Doar SALES; toate exclud «mai ieftin» (cheaper_intent) și
    o căutare nouă (`route.filters`)."""
    route = ctx.route
    if route is None or route.route != Route.SALES:
        return False
    query = (ctx.message.body or "").strip()
    if not query:
        return False

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
        and _resolve_review_product(query, displayed) is not None
    )
    review_intent = (
        getattr(get_settings(), "review_intent_enabled", True)
        and bool(anchorable)
        and (explicit_review or resolves_pending_review)
    )
    if review_intent:
        await _handle_review_intent(ctx, deps, query)
        return True

    explicit_detail = _DETAIL_RE.search(_norm_followup(query)) is not None
    resolves_pending_detail = (
        pending.get("field") == "product_for_details"
        and _resolve_review_product(query, displayed) is not None
    )
    detail_intent = (
        getattr(get_settings(), "detail_intent_enabled", True)
        and bool(anchorable)
        and (explicit_detail or resolves_pending_detail)
    )
    if detail_intent:
        await _handle_detail_intent(ctx, deps, query)
        return True

    # NX-131: cerere de LINK pe un produs deja arătat = intenție DETERMINISTĂ, NU re-recomandare.
    # Calea rich interzice modelului linkurile → cererea de link cădea în re-randarea bogată cu
    # coaching repetat. O servim direct din state → product_url proaspăt → Offer(open_url) + card.
    link_intent = (
        get_settings().link_intent_enabled
        and bool(anchorable)
        and not route.filters
        and _LINK_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )
    if link_intent:
        await _handle_link_intent(ctx, deps)
        return True

    # IZI-parity G2: COMPARAȚIE pe setul afișat → tabel structurat determinist, fără să depindem de
    # model. ≥2 afișate; fără filtre noi; NU «mai ieftin». <2 valide la fetch → handler False.
    compare_intent = (
        get_settings().compare_intent_enabled
        and len(ctx.state.displayed_products) >= 2
        and not route.filters
        and _COMPARE_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )
    if compare_intent and await _handle_compare_intent(ctx, deps, query):
        return True
    return False


def is_show_more(ctx: TurnContext) -> bool:
    """Faza B: predicat de PAGINARE („mai arată-mi" pe o sesiune activă). Paginarea propriu-zisă
    (`continue_search_session`) rămâne în `stage.py`/GENERATE. `not route.filters`: constrângeri NOI
    = RAFINARE (cade pe bucla LLM), nu paginare pură. NU pe «mai ieftin» (cheaper_intent)."""
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
        and not route.filters
        and _MORE_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )
