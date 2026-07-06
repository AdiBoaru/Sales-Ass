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
from typing import Any

from src.agent import prompt_builder
from src.agent.fallbacks import _deterministic_reply, _products_brief
from src.agent.validator import _allowed_prices, _valid
from src.config import get_settings
from src.models import TurnContext
from src.worker import compose

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
) -> str:
    """Validează textul final (preț + link). Invalid → 1 retry (recompune din produse cu
    prețuri permise) → fallback determinist. Invariantul: zero prețuri/linkuri inventate.
    `reco_system` = system-ul de recompunere generat din DB (NX-78). `allowed_links`/
    `allowed_prices` = linkuri/sume grounded de bot (checkout_link/check_order)."""
    if text and _valid(text, products, allowed_links, allowed_prices):
        return text

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
        return reply2

    log.warning("agent: validator a eșuat → fallback determinist")
    return _deterministic_reply(products)


async def _finalize_grounded(
    llm,
    text: str,
    facts: str,
    language: str,
    allowed_links: set[str],
    allowed_prices: set[float],
) -> str:
    """Cale fără produse, dar cu date grounded (status comandă): validează textul; invalid →
    1 retry order-shaped (din `facts` + sume permise) → fallback SIGUR (non-tăcere, fără numere,
    NU forma de produs `_deterministic_reply`)."""
    # NX-117: ORDER → fără claims-check (faptele de livrare/stoc din check_order sunt grounded).
    if text and _valid(
        text, [], allowed_links, allowed_prices, check_bare=False, check_claims=False
    ):
        return text

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
        return reply2

    log.warning("agent: validator status comandă a eșuat → fallback sigur")
    return "Ți-am verificat comanda 🙂 Îți confirm imediat detaliile exacte — revin la tine."


def _no_result_msg(is_order: bool) -> str:
    if is_order:
        return "N-am găsit nicio comandă pe contul tău. Îmi dai numărul comenzii?"
    return (
        "Momentan n-am găsit produse potrivite. Îmi spui mai exact ce cauți (tip de produs, buget)?"
    )


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
