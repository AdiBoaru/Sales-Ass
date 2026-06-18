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
from src.db.queries.catalog import get_products_by_ids
from src.models import RetrievalResult, Route, RouteDecision, TurnContext
from src.tools import (  # noqa: F401 — importul înregistrează tool-urile
    catalog_tools,
    commerce_tools,
    faq_tools,
    orders_tools,
)
from src.tools.base import enabled_tools, run_tool
from src.worker import compose
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
- search_products(query, price_max, category, brand, concerns, limit): caută pe nevoia clientului.
  Pasează `concerns` cu nevoile lui în cuvintele LUI (ex. „ten gras", „acnee"), `category` (slug)
  dacă primești „Categorie probabilă" potrivită, `brand` doar dacă l-a cerut explicit. Filtrarea
  pe nevoie dă recomandări relevante, nu doar potrivire de nume.
- get_product_details(product_id): preț, rating, ce laudă clienții (recenzii) pentru un produs.
- compare_products(product_ids): compară 2-3 produse.
- checkout_link(cart_items): creează linkul de cumpărare. Cheamă-l DOAR când clientul e gata să
  cumpere sau cere linkul/să comande; trimite-i URL-ul întors, nu inventa linkuri.
- check_order(order_ref): status + livrarea unei comenzi. Cheamă-l când clientul întreabă de o
  comandă („unde e comanda mea?", „status ORD-123"); raportează DOAR ce întoarce, nu inventa.
- faq_lookup(query): un fapt de business din baza de cunoștințe (livrare, retur, garanție, plată,
  facturare). Cheamă-l când clientul întreabă o regulă/politică în mijlocul vânzării; raportează
  DOAR ce întoarce, nu inventa reguli.

Reguli:
- Pentru o cerere de produs, cheamă ÎNTÂI search_products. Folosește get_product_details /
  compare_products când clientul vrea detalii sau o comparație. Maxim 3 apeluri de unelte.
- Pentru produsele DEJA arătate (vezi „Produse arătate recent" din context), folosește id-ul
  lor din [] în get_product_details / compare_products / checkout_link — NU re-căuta. La un
  follow-up de tip „care e cea mai bună?" / „trimite-mi linkul la prima", ia id-ul de acolo.
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

# Recomandarea STRUCTURATĂ (model iZi, NX-richreply): modelul emite DOAR cuvinte + referințe
# product_id/pro_index; codul (compose) pune prețuri/rating/linkuri din retrieval. Așa, clasa
# „preț inventat" dispare prin construcție, iar motivul fiecărui card e ancorat pe un avantaj REAL.
_FINAL_SCHEMA_SYSTEM = """Ești consultant de vânzări într-un magazin de beauty online din România.
Primești nevoia clientului și o listă de produse REALE (id, preț, rating, avantaje din recenzii).
Compui o recomandare structurată. Răspunzi DOAR cu JSON conform schemei.

REGULI DURE:
- NU scrii prețuri, linkuri, ratinguri, procente, număr de recenzii, termene de livrare sau ORICE
  cifră. Codul le pune din date. Tu scrii DOAR cuvinte. SINGURA excepție: în `intro` poți relua
  bugetul EXACT pe care l-a scris CLIENTUL (ex. „sub 80 lei") — e cifra LUI, nu un preț de produs.
- `intro` = o frază scurtă care REIA nevoia clientului în cuvintele LUI (ex. dacă a zis „mâini
  uscate" → „Pentru mâini uscate..."; dacă a zis „sub 80 lei" → poți păstra „sub 80 lei").
  NU generic — legat de ce a cerut.
- Pentru fiecare produs ales: `product_id` = un id EXACT din listă; `pro_index` = indicele unui
  avantaj REAL din lista lui (nu inventa avantaje); `fit_clause` = o clauză SCURTĂ care leagă
  produsul de NEVOIA exactă a clientului (ex. „pentru mâini foarte uscate") — doar nevoia lui.
- Recomandă 3-5 produse din listă, în limba clientului.
- `pick` = un singur produs (cel mai potrivit) + justificare în cuvinte (fără cifre,
  fără „cel mai bun").
- `education` = 1-2 propoziții despre ce contează la nevoia clientului (fără cifre).
- `suggestions` = 2-4 mesaje SCURTE de follow-up pe care CLIENTUL le-ar putea trimite mai departe,
  în limba lui, CONCRETE și legate de ce a cerut + de produsele arătate (ex. „Una mai ieftină",
  „Ceva fără parfum", „Compară primele două"). Sunt mesaje din partea CLIENTULUI (pot conține și un
  buget cu cifre), NU afirmațiile tale. Evită generice de tip „Spune-mi mai multe".
- Folosește DOAR produsele din listă. Un id inventat e ignorat de sistem."""

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


def _budget(text: str) -> float | None:
    m = _BUDGET_RE.search(text)
    if not m:
        return None
    val = m.group(1) or m.group(2)
    return float(val) if val else None


def _allowed_prices(products: list[dict[str, Any]]) -> list[float]:
    return [round(float(p["price"]), 2) for p in products if p.get("price") is not None]


def _prices_ok(
    reply: str, products: list[dict[str, Any]], allowed_prices: set[float] | None = None
) -> bool:
    """Fiecare preț menționat în reply trebuie să fie real (toleranță 0.5 lei): preț de produs
    retrievat SAU o sumă grounded din DB (ex. total comandă/checkout, G7-3)."""
    allowed = _allowed_prices(products) + sorted(allowed_prices or set())
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
    reply: str,
    products: list[dict[str, Any]],
    allowed_links: set[str] | None = None,
    allowed_prices: set[float] | None = None,
) -> bool:
    return _prices_ok(reply, products, allowed_prices) and _links_ok(reply, products, allowed_links)


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
    allowed_prices: set[float] | None = None,
) -> str:
    """Validează textul final (preț + link). Invalid → 1 retry (recompune din produse cu
    prețuri permise) → fallback determinist. Invariantul: zero prețuri/linkuri inventate.
    `allowed_links`/`allowed_prices` = linkuri/sume grounded de bot (checkout_link/check_order)."""
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
        reply2 = await llm.complete(_RECO_SYSTEM, user)
    except Exception as e:  # noqa: BLE001 — retry eșuat → fallback determinist
        log.warning("agent: retry compunere eșuat (%s)", type(e).__name__)
        reply2 = ""
    if reply2 and _valid(reply2, products, allowed_links, allowed_prices):
        return reply2

    log.warning("agent: validator a eșuat → fallback determinist")
    return _deterministic_reply(products)


# Prompt de recompunere pt răspunsuri FĂRĂ produse cu date grounded (ex. status comandă) —
# NU produse: validatorul a respins textul (sumă inventată). Forma e de SUPORT, nu de vânzare.
_ORDER_RECO_SYSTEM = """Ești un asistent de suport pentru un magazin online din România.
Raportezi statusul comenzii clientului, concis și prietenos, în limba lui. Folosește DOAR datele
și sumele din informațiile primite — NU inventa numere, AWB, date de livrare sau linkuri."""


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
    if text and _valid(text, [], allowed_links, allowed_prices):
        return text

    allowed = ", ".join(f"{p:.2f} lei" for p in sorted(allowed_prices)) or "(fără sume)"
    user = (
        f"Limba clientului: {language}\nDate comandă:\n{facts}\n\n"
        f"FOLOSEȘTE EXACT doar aceste sume: {allowed}. Niciun alt număr, AWB sau link inventat."
    )
    try:
        reply2 = await llm.complete(_ORDER_RECO_SYSTEM, user)
    except Exception as e:  # noqa: BLE001 — retry eșuat → fallback sigur
        log.warning("agent: retry status comandă eșuat (%s)", type(e).__name__)
        reply2 = ""
    if reply2 and _valid(reply2, [], allowed_links, allowed_prices):
        return reply2

    log.warning("agent: validator status comandă a eșuat → fallback sigur")
    return "Ți-am verificat comanda 🙂 Îți confirm imediat detaliile exacte — revin la tine."


def _no_result_msg(is_order: bool) -> str:
    if is_order:
        return "N-am găsit nicio comandă pe contul tău. Îmi dai numărul comenzii?"
    return (
        "Momentan n-am găsit produse potrivite. Îmi spui mai exact ce cauți (tip de produs, buget)?"
    )


def _rich_bundle(products: list[dict[str, Any]]) -> str:
    """Lista de produse pentru apelul structurat: id + preț + rating + avantaje INDEXATE
    (pentru `pro_index`). Modelul VEDE prețul (ca să ordoneze/aleagă) dar NU-l emite."""
    lines = []
    for p in products:
        raw = p.get("top_pros") or ([p["review_pro"]] if p.get("review_pro") else [])
        pros = [s.strip() for s in raw if isinstance(s, str) and s.strip()][:3]
        pros_str = "; ".join(f"{i}) {pr}" for i, pr in enumerate(pros)) or "(fără avantaje listate)"
        rating = f"{float(p['rating']):.1f}★" if p.get("rating") else "-"
        lines.append(
            f"[{p['id']}] {p['name']} | preț {float(p['price']):.2f} lei | "
            f"rating {rating} | avantaje: {pros_str}"
        )
    return "\n".join(lines)


async def _finalize_rich(llm, query: str, products: list[dict[str, Any]], ctx, history: str):
    """Compune recomandarea STRUCTURATĂ (model iZi). Modelul emite intro + referințe
    product_id/pro_index/fit_clause + pick + education + chip_intents (enum închis); codul
    (compose) hidratează faptele. Întoarce `RichReply` sau None (→ fallback pe proză)."""
    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    user = (
        f"Limba clientului: {ctx.language}\n{history_block}"
        f"Nevoia clientului: {query}\n\nProduse disponibile (alege dintre acestea):\n"
        f"{_rich_bundle(products)}"
    )
    try:
        j = await llm.complete_schema(_FINAL_SCHEMA_SYSTEM, user, _RICH_SCHEMA)
    except Exception as e:  # noqa: BLE001 — apel structurat eșuat → fallback pe proză
        log.warning("agent: finalize structured eșuat (%s)", type(e).__name__)
        return None
    return compose.assemble(ctx, j, products)


async def agent_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Bucla de tool-calling cu toolset PER RUTĂ: `sales` → recomandare grounded; `order` →
    status comandă (G7-3). Ambele validate; alte rute → no-op (lasă fallback/echo)."""
    if deps.llm is None:
        return
    route: RouteDecision | None = ctx.route
    if route is None or route.route not in (Route.SALES, Route.ORDER):
        return
    query = (ctx.message.body or "").strip()
    if not query:
        return
    is_order = route.route == Route.ORDER

    tools = tool_schemas(enabled_tools(ctx.business, route.route.value))
    retrieved: list[dict[str, Any]] = []
    generated_links: set[str] = set()  # linkuri create de bot (checkout_link) → validator
    grounded_prices: set[float] = set()  # sume din DB (total comandă/checkout) → validator
    order_views: list[str] = []  # vederi grounded de comandă, pt fallback-ul de status

    async def execute(name: str, args: dict[str, Any]) -> str:
        """Callback al buclei: rulează tool-ul, acumulează produse + linkuri + sume grounded,
        întoarce vederea compactă modelului. `business_id` se ia din `ctx` (nu din `args`)."""
        result = await run_tool(ctx, deps, name, args)
        retrieved.extend(result.products)
        generated_links.update(result.links)
        grounded_prices.update(result.prices)
        if name == "check_order" and result.ok and result.llm_view:
            order_views.append(result.llm_view)
        ctx.emit("tool_call", name=name, ok=result.ok)
        return result.llm_view or (result.error or "(fără rezultat)")

    history = conversation_transcript(ctx.history)
    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    context = context_blocks(ctx)
    context_block = f"{context}\n\n" if context else ""
    # `category_key` derivat + validat în triaj → HINT pentru agent (NX-72). NU-l forțăm în tool
    # args din cod (P3: args sunt ale modelului); modelul decide dacă se potrivește cererii.
    cat_hint = f"Categorie probabilă: {route.category_key}\n" if route.category_key else ""
    user = (
        f"Limba clientului: {ctx.language}\n{cat_hint}{context_block}{history_block}"
        f"Mesaj client: {query}"
    )

    try:
        final = await deps.llm.run_tool_loop(_TOOL_SYSTEM, user, tools, execute)
    except Exception as e:  # noqa: BLE001 — bucla eșuată → lasă echo fallback
        log.warning("agent: tool loop eșuat (%s)", type(e).__name__)
        return

    products = _dedupe(retrieved)
    # R3: follow-up pe produse DEJA arătate („care e cea mai bună?") la care modelul n-a rechemat
    # un tool → re-hidratează produsele afișate (după id, din state) ca set de retrieval, ca să
    # răspundem GROUNDED pe ele în loc de „n-am găsit". Doar SALES, doar cu id-uri în state, și DOAR
    # când textul singur ar pica (gol sau preț negroundat) — un răspuns prose valid (mulțumesc/
    # clarificare) rămâne neatins. Cuplat cu id-urile expuse în state_block, comparația merge sigur.
    if (
        not products
        and not is_order
        and ctx.state.displayed_products
        and not (final and _valid(final, [], generated_links, grounded_prices))
    ):
        ids = [p.product_id for p in ctx.state.displayed_products]
        products = await get_products_by_ids(deps.conn, ctx.business.id, ids, limit=6)
    ctx.retrieval = RetrievalResult(products=products, source="tools")

    if products:
        # Calea BOGATĂ (model iZi): recomandare structurată → compose. Doar pe SALES.
        # Orice eșec (apel structurat, zero items după membership) → fallback pe proză.
        if not is_order:
            rich = await _finalize_rich(deps.llm, query, products, ctx, history)
            if rich is not None and rich.items:
                ctx.set_rich_reply(
                    rich,
                    text=compose.flatten(rich),
                    products=compose.card_products(rich.items),
                )
                ctx.emit("agent_recommended", n=len(rich.items), rich=True)
                return
        reply = await _finalize(
            deps.llm,
            query,
            final,
            products,
            ctx.language,
            history,
            generated_links,
            grounded_prices,
        )
        ctx.set_reply(reply, products=_card_products(products))
        ctx.emit("agent_recommended", n=len(products))
    elif final:
        # Fără produse, dar avem text: îl VALIDĂM (nu servire oarbă). Forma de recuperare diferă
        # pe rută — nu trecem o întrebare de vânzare prin fallback-ul de status comandă.
        if is_order:
            reply = await _finalize_grounded(
                deps.llm,
                final,
                "\n".join(order_views),
                ctx.language,
                generated_links,
                grounded_prices,
            )
            ctx.set_reply(reply)
        elif _valid(final, [], generated_links, grounded_prices):
            # SALES: text fără produse și fără sumă inventată (clarificare) → servim
            ctx.set_reply(final)
        else:
            # SALES: preț negroundat fără produse care să-l susțină → mesaj sigur de vânzare.
            # NU cacheabil: altfel „n-am găsit" otrăvește semantic_cache și se re-servește la
            # fiecare query similar, sărind agentul (bug găsit live: hit_count=9 pe demo).
            ctx.set_reply(_no_result_msg(is_order=False), cacheable=False)
    else:
        ctx.set_reply(_no_result_msg(is_order), cacheable=False)
