"""Stagiul 7 — Agent (GPT-5.4-mini) cu TOOL-CALLING (G7). Recomandă produse pe rutele
de vânzare, chemând unelte deterministe în catalog (max 3/tur, cap dur în adaptor).

Agentul DECIDE ce tool cheamă (search_products / get_product_details / compare_products);
uneltele sunt cod determinist scoped pe `business_id`. Bucla de function-calling stă în
adaptor (`llm.run_tool_loop`); aici dăm callback-ul `execute` care rulează uneltele și
acumulează produsele retrievate. Validator inline (stagiul 8): preț + link din reply ∈
retrieval → 1 retry → fallback determinist. ZERO halucinații structural.

Degradare grațioasă: fără LLM / eroare de buclă → no-op (echo fallback). Vezi
docs/agent-tools-architecture.md.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from src.agent.tool_definitions import tool_schemas
from src.models import RetrievalResult, Route, RouteDecision, TurnContext
from src.tools import (  # noqa: F401 — importul înregistrează tool-urile
    catalog_tools,
    commerce_tools,
)
from src.tools.base import enabled_tools, run_tool
from src.worker.context import context_blocks, conversation_transcript

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:lei|ron)", re.IGNORECASE)
_BUDGET_RE = re.compile(
    r"(?:sub|pana la|până la|maxim|maximum|buget|max)\s*(\d{1,5})|(\d{1,5})\s*(?:lei|ron)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+")

# Prompt pentru bucla de tool-calling: agentul alege uneltele.
_TOOL_SYSTEM = """Ești consultant de vânzări într-un magazin de beauty online din România.
Ai unelte ca să răspunzi GROUNDED pe catalogul real:
- search_products(query, price_max, limit): caută produse pe nevoia clientului.
- get_product_details(product_id): preț, rating, ce laudă clienții (recenzii) pentru un produs.
- compare_products(product_ids): compară 2-3 produse.
- checkout_link(cart_items): creează linkul de cumpărare. Cheamă-l DOAR când clientul e gata să
  cumpere sau cere linkul/să comande; trimite-i URL-ul întors, nu inventa linkuri.

Reguli:
- Pentru o cerere de produs, cheamă ÎNTÂI search_products. Folosește get_product_details /
  compare_products când clientul vrea detalii sau o comparație. Maxim 3 apeluri de unelte.
- Recomandă 2-3 produse, în limba clientului, prietenos și concis. Pentru fiecare: numele,
  prețul EXACT (lei) și ratingul (★) din rezultate, apoi de ce se potrivește pe nevoie.
- NU inventa produse, prețuri, ingrediente sau linkuri. Folosește DOAR ce întorc uneltele.
- Termină cu o întrebare scurtă (buget / tip de ten) sau oferta de a trimite link. Text
  simplu pentru chat, fără markdown greu."""

# Prompt pentru retry/recompunere (din produse, fără unelte) — validatorul a respins textul.
_RECO_SYSTEM = """Ești consultant de vânzări într-un magazin de beauty online din România.
Primești întrebarea clientului și o listă de produse din catalog (cu prețuri REALE).
Recomanzi 2-3 produse potrivite, în limba clientului, prietenos și concis. Pentru fiecare:
numele, prețul EXACT (lei) și ratingul (★) din listă, apoi de ce se potrivește. Folosește
DOAR produsele, prețurile și linkurile din listă — NU inventa nimic. Maxim 3 produse."""


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


def _links_ok(
    reply: str, products: list[dict[str, Any]], allowed_links: set[str] | None = None
) -> bool:
    """Fiecare URL din reply trebuie să fie un product_url retrievat SAU un link generat de bot
    în acest tur (checkout_link, F2) — niciodată inventat."""
    allowed = {p.get("url") for p in products if p.get("url")} | (allowed_links or set())
    for raw in _URL_RE.findall(reply):
        url = raw.rstrip(".,;:!?)\"'")
        if url not in allowed:
            return False
    return True


def _valid(
    reply: str, products: list[dict[str, Any]], allowed_links: set[str] | None = None
) -> bool:
    return _prices_ok(reply, products) and _links_ok(reply, products, allowed_links)


def _products_brief(products: list[dict[str, Any]]) -> str:
    lines = []
    for p in products:
        summary = (p.get("ai_summary") or "")[:140]
        extra = ""
        if p.get("rating"):
            extra += f" | {float(p['rating']):.1f}★"
        if p.get("review_summary") or p.get("review_pro"):
            laud = p.get("review_pro") or (p.get("top_pros") or [""])[0]
            if laud:
                extra += f" | clienții laudă: {laud}"
        lines.append(
            f"- {p['name']} | brand: {p.get('brand') or '-'} | "
            f"preț: {float(p['price']):.2f} lei{extra} | url: {p.get('url') or '-'} | {summary}"
        )
    return "\n".join(lines)


def _deterministic_reply(products: list[dict[str, Any]]) -> str:
    lines = ["Îți recomand:"]
    for p in products[:3]:
        lines.append(f"• {p['name']} — {float(p['price']):.2f} lei")
    lines.append("Vrei detalii sau linkul la vreunul?")
    return "\n".join(lines)


def _card_products(products: list[dict[str, Any]], n: int = 3) -> list[dict[str, Any]]:
    """Câmpuri compacte pentru cardurile de produs (W1 + carusel R2)."""
    return [
        {
            "product_id": p["id"],
            "name": p["name"],
            "price": float(p["price"]),
            "url": p.get("url"),
            "image": p.get("image"),
        }
        for p in products[:n]
    ]


def _dedupe(products: list[dict[str, Any]], cap: int = 6) -> list[dict[str, Any]]:
    """Produse unice (după id), ordine păstrată, max `cap` (principiul: ≤6 produse)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in products:
        pid = p.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        out.append(p)
        if len(out) >= cap:
            break
    return out


async def _finalize(
    llm,
    query: str,
    text: str,
    products: list[dict[str, Any]],
    language: str,
    history: str,
    allowed_links: set[str] | None = None,
) -> str:
    """Validează textul final (preț + link). Invalid → 1 retry (recompune din produse cu
    prețuri permise) → fallback determinist. Invariantul: zero prețuri/linkuri inventate.
    `allowed_links` = linkuri generate de bot (checkout_link) acceptate pe lângă product_url-uri."""
    if text and _valid(text, products, allowed_links):
        return text

    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    allowed = ", ".join(f"{p:.2f} lei" for p in _allowed_prices(products))
    user = (
        f"Limba clientului: {language}\n{history_block}"
        f"Întrebare: {query}\nProduse:\n{_products_brief(products)}\n\n"
        f"FOLOSEȘTE EXACT doar aceste prețuri: {allowed}. Niciun alt preț, niciun link inventat."
    )
    try:
        reply2 = await llm.complete(_RECO_SYSTEM, user)
    except Exception as e:  # noqa: BLE001 — retry eșuat → fallback determinist
        log.warning("agent: retry compunere eșuat (%s)", type(e).__name__)
        reply2 = ""
    if reply2 and _valid(reply2, products, allowed_links):
        return reply2

    log.warning("agent: validator a eșuat → fallback determinist")
    return _deterministic_reply(products)


async def agent_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Pentru route='sales': bucla de tool-calling → recomandare grounded (validată)."""
    if deps.llm is None:
        return
    route: RouteDecision | None = ctx.route
    if route is None or route.route != Route.SALES:
        return
    query = (ctx.message.body or "").strip()
    if not query:
        return

    tools = tool_schemas(enabled_tools(ctx.business))
    retrieved: list[dict[str, Any]] = []
    generated_links: set[str] = set()  # linkuri create de bot (checkout_link) → validator

    async def execute(name: str, args: dict[str, Any]) -> str:
        """Callback al buclei: rulează tool-ul, acumulează produsele + linkurile generate,
        întoarce vederea compactă modelului. `business_id` se ia din `ctx` (nu din `args`)."""
        result = await run_tool(ctx, deps, name, args)
        retrieved.extend(result.products)
        generated_links.update(result.links)
        ctx.emit("tool_call", name=name, ok=result.ok)
        return result.llm_view or (result.error or "(fără rezultat)")

    history = conversation_transcript(ctx.history)
    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    context = context_blocks(ctx)
    context_block = f"{context}\n\n" if context else ""
    user = f"Limba clientului: {ctx.language}\n{context_block}{history_block}Mesaj client: {query}"

    try:
        final = await deps.llm.run_tool_loop(_TOOL_SYSTEM, user, tools, execute)
    except Exception as e:  # noqa: BLE001 — bucla eșuată → lasă echo fallback
        log.warning("agent: tool loop eșuat (%s)", type(e).__name__)
        return

    products = _dedupe(retrieved)
    ctx.retrieval = RetrievalResult(products=products, source="tools")

    if products:
        reply = await _finalize(
            deps.llm, query, final, products, ctx.language, history, generated_links
        )
        ctx.set_reply(reply, products=_card_products(products))
        ctx.emit("agent_recommended", n=len(products))
    elif final:
        # model a răspuns fără produse (clarificare / conversațional) → servim textul
        ctx.set_reply(final)
    else:
        ctx.set_reply(
            "Momentan n-am găsit produse potrivite. Îmi spui mai exact ce cauți "
            "(tip de produs, buget)?"
        )
