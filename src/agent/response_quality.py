"""NX-159 felia 1 — telemetrie de CALITATE a formei răspunsului (observator GLOBAL post-reply).

Corecția arhitecturală (vs. „check_completeness(ctx, plan)"): măsurarea NU poate atârna de
`ResponsePlan` — acela există DOAR pe calea agent/sales. Ar rata exact căile care produc răspunsuri
proaste: `simple`/nano („Da."), cache, FAQ, no-results, welcome, clarify. Deci telemetria pleacă din
RUNNER, singurul punct prin care trec TOATE căile terminale (`pipeline_early_exit`).

Module PUR: primește `TurnContext` (reply/route/retrieval deja setate) + numele stagiului de
early-exit, întoarce dict-uri de properties pentru `ctx.emit`. Zero I/O, zero LLM, zero scriere.
P12: `response_shape` = DOAR forma (lungimi, booleeni, rută, stagiu) — ZERO text de reply, ZERO PII.
`completeness_gap` = doar `intent` + CHEILE lipsă (nu conținut). Runner-ul le emite (P10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.models import Route

if TYPE_CHECKING:
    from src.models import TurnContext

# Sub acest prag (caractere, text strip-uit) un răspuns e „scurt" — semnalul clasic „Da." / „Ok.".
SHORT_REPLY_CHARS = 20


def _has_suggestions(r) -> bool:
    """Reply-ul are chips de follow-up? Calea NON-rich le pune în `Reply.suggestions` (clarify/
    comparație/thin-path); calea RICH le pune în `Reply.rich.chips` (`set_rich_reply` NU populează
    `suggestions`). Le tratăm pe AMÂNDOUĂ — altfel telemetria raportează fals „fără suggestions"
    exact pe rich (bug prins la review: web randează `reply.rich.chips`)."""
    return bool(r.suggestions or (r.rich is not None and r.rich.chips))


def reply_shape(ctx: TurnContext, stage: str) -> dict[str, Any]:
    """Forma răspunsului servit, derivată 100% determinist din `ctx.reply` — pt `response_shape`.
    P12: DOAR metadate de formă; niciun fragment de text, niciun câmp cu PII. `ctx.halt` (tăcere
    intenționată) n-are reply → caller-ul NU cheamă asta pe halt."""
    r = ctx.reply
    text = (r.text or "") if r is not None else ""
    n = len(text.strip())
    route = ctx.route.route.value if ctx.route and ctx.route.route else None
    return {
        "chars": n,
        "under_20": n < SHORT_REPLY_CHARS,
        "has_question": "?" in text,
        "has_products": bool(r and r.products),
        "has_suggestions": bool(r and _has_suggestions(r)),
        "is_rich": bool(r and r.rich is not None),
        "is_comparison": bool(r and r.comparison is not None),
        "has_offer": bool(r and r.offer is not None),
        "is_clarify": bool(r and r.pending_question is not None),
        "route": route,
        "stage": stage,
        "from_cache": bool(getattr(ctx, "from_cache", False)),
    }


def _has_next_step(r) -> bool:
    """„Următor pas" = reply-ul lasă clientului o cale de continuare: întrebare, chips de sugestie
    (inclusiv `rich.chips`), ofertă/CTA sau slot de clarificare. Absența TUTUROR = fundătură."""
    text = r.text or ""
    return bool(
        "?" in text or _has_suggestions(r) or r.offer is not None or r.pending_question is not None
    )


def completeness_gaps(ctx: TurnContext) -> list[str]:
    """CHEILE de completitudine lipsă, derivate determinist din `ctx` — pentru `completeness_gap`.
    DOAR unde are sens (sales/order/clarify); niciun LLM, nicio re-validare de grounding (aia e la
    validator, NX-142). Lista goală → caller-ul NU emite event. Chei posibile:
      • `next_step`   — sales cu produse, dar fără nicio cale de continuare
      • `alternative` — sales fără produse (no-result), fără alternativă oferită
      • `question`    — clarify fără „?" (o clarificare care nu întreabă nimic)
      • `asked_field` — order fără date de comandă și fără să ceară nr comandă / login
    """
    r = ctx.reply
    if r is None:
        return []
    route = ctx.route.route if ctx.route else None
    gaps: list[str] = []

    if route == Route.SALES:
        has_products = bool(r.products) or bool(r.rich is not None)
        if has_products or bool(r.comparison is not None):
            if not _has_next_step(r):
                gaps.append("next_step")
        elif not _has_next_step(r):
            # sales fără produse = no-result / clarificare de vânzare → trebuie o cale înainte.
            gaps.append("alternative")
    elif route == Route.CLARIFY:
        if "?" not in (r.text or "") and not r.suggestions:
            gaps.append("question")
    elif route == Route.ORDER:
        # order „complet" = fie a raportat date (produse/ofertă), fie cere explicit câmpul lipsă.
        reported = bool(r.products) or bool(r.offer is not None)
        if not reported and not _has_next_step(r):
            gaps.append("asked_field")

    return gaps


def answer_shape_report(ctx: TurnContext) -> dict[str, Any] | None:
    """NX-299 — ce FORMĂ cerea turul și ce a ieșit. `None` = turul n-are carduri, deci n-are formă.

    Există fiindcă eșecul de formă e azi invizibil. Turul `42744330` arată în telemetrie ca un
    succes curat (`validator_ok`, `is_rich: true`, `has_products: true`, `has_suggestions: true`),
    deși a servit două carduri dintr-un singur brand, fără frază de încadrare, la o cerere cu 518
    candidați în catalog. `response_shape` numără booleeni, nu sloturi, deci nu putea spune
    diferența dintre un răspuns bogat și unul care doar are câmpurile pline.

    Un tur de recomandare care servește un singur tip, fără încadrare și fără încheiere, e o
    degradare și trebuie să aibă nume și cod, ca `unmet_query` sau `rich_downgraded`. Fără
    măsurătoarea asta nu putem ști dacă reparația a ținut, iar peste o lună regresia se strecoară
    tăcut. Vocabularul e ÎNCHIS (`SLOTS`, `REASONS`), deci eticheta e mărginită (cardinalitate).

    Pur, ca restul modulului: derivat din `ctx.reply` + `ctx.retrieval`, zero I/O.
    """
    from src.agent import answer_shape as shape_mod
    from src.worker import compose

    r = ctx.reply
    rich = r.rich if r is not None else None
    if rich is None or not rich.items:
        return None

    retrieval = getattr(ctx, "retrieval", None)
    served = list(getattr(retrieval, "products", None) or [])
    shown_ids = {it.product_id for it in rich.items if it.product_id}
    # Tipurile se citesc de pe produsele CHIAR AFIȘATE, nu de pe tot pool-ul retrievat: pool-ul
    # poate fi divers iar pagina monotonă, și exact aia e degradarea pe care o căutăm.
    shown = [p for p in served if str(p.get("id") or p.get("product_id") or "") in shown_ids]
    types = shape_mod.distinct_types(shown or served[: len(rich.items)])

    pack = getattr(ctx.business, "domain_pack", None)
    facets = tuple(getattr(pack, "comparison_facets", ()) or ()) if pack else ()
    axes = compose.decision_axes(shown, facets, ctx.language) if shown else []

    shape = shape_mod.shape_for(n_items=len(rich.items), product_types=types, n_axes=len(axes))
    filled = {
        shape_mod.SLOT_FRAMING: bool(rich.intro),
        shape_mod.SLOT_FIT_LINE: all(bool(it.reason) for it in rich.items),
        shape_mod.SLOT_CLOSING: bool(rich.education),
    }
    missing = shape_mod.missing_slots(shape, filled)
    return {
        "n_items": len(rich.items),
        "n_types": len(types),
        "n_axes": len(axes),
        "required": list(shape.required),
        "missing": list(missing),
        # DE CE lipsește fiecare: „n-am cerut-o" și „am cerut-o și n-a ieșit" sunt întrebări
        # diferite, iar numai a doua e un defect.
        "reasons": {s: shape.reason_for(s) for s in missing},
        "complete": not missing,
    }
