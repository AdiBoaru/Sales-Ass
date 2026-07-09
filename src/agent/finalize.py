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
from typing import TYPE_CHECKING, Any

from src.agent import prompt_builder
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
from src.config import get_settings
from src.models import Offer, TurnContext
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
) -> tuple[str, ValidationResult]:
    """Validează textul final (preț + link). Invalid → 1 retry (recompune din produse cu
    prețuri permise) → fallback determinist. Invariantul: zero prețuri/linkuri inventate.
    `reco_system` = system-ul de recompunere generat din DB (NX-78). `allowed_links`/
    `allowed_prices` = linkuri/sume grounded de bot (checkout_link/check_order). Întoarce textul
    servit ÎMPREUNĂ cu `ValidationResult` (NX-146 felia 2 fix — corelat în `agent_prompt` pt
    Turn Replay): pe fallback determinist, `reasons` arată DE CE a picat proza LLM-ului.

    Poarta de trecere/eșec rămâne `_valid` (shim monkeypatch-uit de proba anti-teatru NX-121 —
    `test_golden.test_injection_case_fails_without_its_guard`); `validate_prose` se cheamă
    SEPARAT doar pe calea de eșec, ca să raporteze motivele fără să schimbe gating-ul testat."""
    if text and _valid(text, products, allowed_links, allowed_prices):
        return text, ValidationResult(ok=True, reasons=[])

    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    prices = _allowed_prices(products) + sorted(allowed_prices or set())
    allowed = ", ".join(f"{p:.2f} lei" for p in prices)
    user = (
        f"Limba clientului: {language}\n{history_block}"
        f"Întrebare: {query}\nProduse:\n{_products_brief(products)}\n\n"
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

    allowed = ", ".join(f"{p:.2f} lei" for p in sorted(allowed_prices)) or "(fără sume)"
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
        "Ți-am verificat comanda 🙂 Îți confirm imediat detaliile exacte — revin la tine.",
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
            f"[{p['id']}] {p['name']} | preț {float(p['price']):.2f} lei | "
            f"rating {rating} | avantaje: {pros_str}{cons_str}{desc_str}{fac_str}"
        )
    return "\n".join(lines)


async def _finalize_rich(
    llm,
    rich_system: str,
    query: str,
    products: list[dict[str, Any]],
    ctx,
    history: str,
    notes: str = "",
):
    """Compune recomandarea STRUCTURATĂ (model iZi). Modelul emite intro + referințe
    product_id/pro_index/fit_clause + pick + education + chip_intents (enum închis); codul
    (compose) hidratează faptele. `rich_system` = system generat din DB (NX-78). `notes` =
    context per-tur din bucla de tool-uri (NX-137: ex. checkout eșuat → fără chips de coș).
    Întoarce `RichReply` sau None (→ fallback pe proză)."""
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
    user = (
        f"Limba clientului: {ctx.language}\n{notes_block}{history_block}"
        f"Nevoia clientului: {query}\n{axes_block}\nProduse disponibile (alege dintre acestea):\n"
        f"{_rich_bundle(products, _rich_facets(ctx), ctx.language)}"
    )
    try:
        j = await llm.complete_schema(rich_system, user, _RICH_SCHEMA)
    except Exception as e:  # noqa: BLE001 — apel structurat eșuat → fallback pe proză
        log.warning("agent: finalize structured eșuat (%s)", type(e).__name__)
        return None
    return compose.assemble(ctx, j, products)


def _attach_checkout_offer(ctx: TurnContext, url: str | None) -> None:
    """NX-137: linkul de checkout creat în ACEST tur ajunge GARANTAT la client, pe orice cale de
    compunere. Root cause (găsit live pe sim): pe calea RICH (web) modelul are INTERZIS structural
    să scrie linkuri (regulile rich) → linkul era creat în DB (`checkout_link_created`) și apoi
    murea tăcut — reply fără URL. Offer e neutru de canal (NX-114): marginile bogate randează
    buton/CTA; floor-ul din `set_offer` lipește URL-ul la text DOAR dacă nu e deja acolo (proza
    de WhatsApp îl poate conține deja — fără dublare)."""
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
    # Tabel structurat DETERMINIST din setul comparat (ordinea cerută păstrată) — fapte din
    # retrieval, lead determinist (cel mai ieftin / cel mai bine cotat), ZERO proză LLM în celule →
    # zero halucinație. Web randează tabelul; canalele text primesc floor-ul aplatizat. Precede
    # calea rich de recomandare (altfel ar re-RECOMANDA în loc să compare — bug-ul „Compară primele
    # două" care doar re-lista produsele). Sare peste rich/proză pentru acest tur.
    if plan.compared and not is_order:
        comparison = compose.build_comparison(plan.compared, ctx.language, _comparison_facets(ctx))
        if comparison is not None:
            ctx.set_comparison_reply(
                comparison,
                text=compose.flatten_comparison(comparison, ctx.language),
                products=compose.comparison_cards(comparison),
                chips=_compare_chips(comparison.columns, ctx.language),
            )
            ctx.emit("agent_compared", n=len(comparison.columns))
            return None

    if products:
        # Calea BOGATĂ (model iZi): recomandare structurată → compose. Doar pe SALES.
        # Orice eșec (apel structurat, zero items după membership) → fallback pe proză.
        if not is_order:
            rich = await _finalize_rich(
                deps.llm,
                prompt_builder.build_rich_system(plan.inp),
                plan.query,
                products,
                ctx,
                plan.history,
                notes=plan.commerce_note,
            )
            if rich is not None and rich.items:
                ctx.set_rich_reply(
                    rich,
                    text=compose.flatten(rich, ctx.language),
                    products=compose.card_products(rich.items),
                )
                # NX-137: regulile rich INTERZIC linkuri în proza modelului → fără atașarea asta,
                # linkul de checkout creat în acest tur nu ajungea NICIODATĂ la client pe web.
                _attach_checkout_offer(ctx, plan.checkout_url)
                ctx.emit("agent_recommended", n=len(rich.items), rich=True)
                return None
            # NX-122: downgrade tăcut rich → proză, acum vizibil. `rich is None` = apelul
            # structurat a eșuat/excepție; `rich.items == []` = toate produsele au picat la
            # grounding-ul de apartenență. Pur observabilitate (downgrade-ul exista deja, P6).
            reason = (
                "all-items-dropped-by-membership" if rich is not None else "structured-call-failed"
            )
            ctx.emit("rich_downgraded", reason=reason)
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
        )
        ctx.set_reply(reply, products=_card_products(products))
        # NX-137: pe proză modelul POATE scrie linkul (validat prin generated_links), dar dacă
        # l-a omis, Offer-ul îl garantează (floor-ul din set_offer nu dublează un URL deja în text).
        _attach_checkout_offer(ctx, plan.checkout_url)
        ctx.emit("agent_recommended", n=len(products))
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
