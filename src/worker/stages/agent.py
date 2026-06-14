"""Stagiul 7 — Agent (GPT-5.4-mini). Recomandă produse pentru rutele de vânzare.

V1 = retrieve-then-generate (RAG): embed mesajul → search HIBRID (semantic+filtre) →
mini compune recomandarea, grounded STRICT pe produsele retrievate. Include un
validator de prețuri inline (stagiul 8): orice preț din răspuns care nu e din
catalog → 1 retry cu feedback → fallback determinist (listă cu prețuri reale).
ZERO prețuri inventate (principiul 8).

Rulează DOAR pentru route='sales' (order/handoff → follow-up / echo). Degradare
grațioasă: fără LLM sau eroare → no-op (echo fallback). Tool-calling complet
(compară/detalii, max 3 tool calls) = refinement ulterior.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from src.db.queries.catalog import search_products_semantic
from src.models import RetrievalResult, Route, RouteDecision, TurnContext
from src.worker.context import conversation_transcript, search_query

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

_BUDGET_RE = re.compile(
    r"(?:sub|pana la|până la|maxim|maximum|buget|max)\s*(\d{1,5})|(\d{1,5})\s*(?:lei|ron)",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:lei|ron)", re.IGNORECASE)

_SYSTEM = """Ești consultant de vânzări într-un magazin de beauty online din România.
Primești întrebarea clientului și o listă de produse din catalog (cu prețuri REALE).
Recomanzi 2-3 produse potrivite, în limba clientului, prietenos și concis.

Reguli STRICTE:
- Folosește DOAR produsele și prețurile din listă. NU inventa produse, prețuri,
  ingrediente sau caracteristici.
- Pentru fiecare recomandare: numele, prețul EXACT din listă (în lei), o frază de
  ce se potrivește.
- Maxim 3 produse. Termină cu o întrebare scurtă (buget / tip de ten) sau ofertă
  de a trimite link.
- Dacă întrebarea e un follow-up (ex. „mai ieftin", „și pentru păr?"), ține cont
  de conversația de mai sus (ce a cerut clientul deja).
- Menționează ratingul (★) la fiecare produs recomandat, și ce apreciază clienții
  când e relevant. Folosește DOAR ce e în date (nu inventa rating sau recenzii).
- Text simplu pentru chat, fără markdown greu."""


def _budget(text: str) -> float | None:
    m = _BUDGET_RE.search(text)
    if not m:
        return None
    val = m.group(1) or m.group(2)
    return float(val) if val else None


def _allowed_prices(products: list[dict[str, Any]]) -> list[float]:
    return [round(float(p["price"]), 2) for p in products if p.get("price") is not None]


def _prices_ok(reply: str, products: list[dict[str, Any]]) -> bool:
    """Fiecare preț menționat în reply trebuie să fie unul real (toleranță 0.5 lei)."""
    allowed = _allowed_prices(products)
    for token in _PRICE_RE.findall(reply):
        value = float(token.replace(",", "."))
        if not any(abs(value - a) <= 0.5 for a in allowed):
            return False
    return True


def _products_brief(products: list[dict[str, Any]]) -> str:
    lines = []
    for p in products:
        summary = (p.get("ai_summary") or "")[:140]
        extra = ""
        if p.get("rating"):
            extra += f" | {float(p['rating']):.1f}★"
        if p.get("review_pro"):
            extra += f" | clienții laudă: {p['review_pro']}"
        lines.append(
            f"- {p['name']} | brand: {p.get('brand') or '-'} | "
            f"preț: {float(p['price']):.2f} lei{extra} | {summary}"
        )
    return "\n".join(lines)


def _deterministic_reply(products: list[dict[str, Any]]) -> str:
    lines = ["Îți recomand:"]
    for p in products[:3]:
        lines.append(f"• {p['name']} — {float(p['price']):.2f} lei")
    lines.append("Vrei detalii sau linkul la vreunul?")
    return "\n".join(lines)


def _card_products(products: list[dict[str, Any]], n: int = 3) -> list[dict[str, Any]]:
    """Câmpuri compacte pentru cardurile de produs (W1): name, price, url, image."""
    return [
        {
            "name": p["name"],
            "price": float(p["price"]),
            "url": p.get("url"),
            "image": p.get("image"),
        }
        for p in products[:n]
    ]


async def _recommend(
    llm, query: str, products: list[dict[str, Any]], language: str, *, history: str = ""
) -> str:
    """Compune recomandarea + validează prețurile (retry 1, apoi fallback determinist)."""
    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    user = (
        f"Limba clientului: {language}\n{history_block}"
        f"Întrebare: {query}\nProduse:\n{_products_brief(products)}"
    )
    reply = await llm.complete(_SYSTEM, user)
    if reply and _prices_ok(reply, products):
        return reply

    # 1 retry cu feedback strict pe prețuri
    allowed = ", ".join(f"{p:.2f} lei" for p in _allowed_prices(products))
    reply2 = await llm.complete(
        _SYSTEM, user + f"\n\nFOLOSEȘTE EXACT doar aceste prețuri: {allowed}. Niciun alt preț."
    )
    if reply2 and _prices_ok(reply2, products):
        return reply2

    log.warning("agent: validator preț a eșuat de 2 ori → fallback determinist")
    return _deterministic_reply(products)


async def agent_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Pentru route='sales': caută semantic + recomandă (grounded, prețuri validate)."""
    if deps.llm is None:
        return
    route: RouteDecision | None = ctx.route
    if route is None or route.route != Route.SALES:
        return
    query = (ctx.message.body or "").strip()
    if not query:
        return

    # Search context-aware: follow-up-urile scurte („mai ieftin") caută în contextul
    # ultimelor mesaje ale clientului, nu izolat.
    try:
        query_vec = (await deps.llm.embed([search_query(ctx.history, query)]))[0]
    except Exception as e:  # noqa: BLE001 — embed eșuat → lasă echo fallback
        log.warning("agent: embed query eșuat (%s)", type(e).__name__)
        return

    price_max = _budget(query)
    # NU filtrăm pe category_key: triaj-ul (nano) îl ghicește des greșit și taie
    # rezultate bune. Ranking-ul semantic prinde deja intenția de categorie. Păstrăm
    # doar filtrul de preț (cu fallback dacă bugetul taie tot).
    products = await search_products_semantic(
        deps.conn, ctx.business.id, query_vec, price_max=price_max, limit=6
    )
    if not products and price_max is not None:
        products = await search_products_semantic(deps.conn, ctx.business.id, query_vec, limit=6)
    if not products:
        ctx.set_reply(
            "Momentan n-am găsit produse potrivite. Îmi spui mai exact ce cauți "
            "(tip de produs, buget)?"
        )
        return

    ctx.retrieval = RetrievalResult(products=products, source="semantic")
    try:
        reply = await _recommend(
            deps.llm, query, products, ctx.language, history=conversation_transcript(ctx.history)
        )
    except Exception as e:  # noqa: BLE001 — compunere eșuată → fallback determinist
        log.warning("agent: compunere eșuată (%s) → fallback", type(e).__name__)
        reply = _deterministic_reply(products)

    ctx.set_reply(reply, products=_card_products(products))
    ctx.emit("agent_recommended", n=len(products), price_max=price_max)
