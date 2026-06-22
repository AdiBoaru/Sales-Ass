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

from src.agent import prompt_builder
from src.agent.prompt_builder import PromptInputs
from src.agent.tool_definitions import tool_schemas
from src.config import get_settings
from src.db.queries.catalog import (
    get_products_by_ids,
    list_category_names,
    list_routing_aliases,
    search_cheaper_than,
)
from src.models import RetrievalResult, Route, RouteDecision, TurnContext
from src.tools import (  # noqa: F401 — importul înregistrează tool-urile
    catalog_tools,
    commerce_tools,
    faq_tools,
    handoff_tools,
    orders_tools,
)
from src.tools.base import enabled_tools, run_tool
from src.worker import compose
from src.worker.context import context_blocks, conversation_transcript
from src.worker.text_scrub import has_text_claim

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# NX-117: prinde valuta în SUFIX („89 lei", „89 de lei", „89 ron") ȘI în PREFIX („RON 89", „lei 89")
# → un preț real prefixat nu e tratat fals ca cifră bară, iar un preț prefixat negroundat e prins.
_PRICE_RE = re.compile(
    r"\b(?:lei|ron)\s*(\d{1,6}(?:[.,]\d{1,2})?)"  # prefix-valută
    r"|(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:de\s+)?(?:lei|ron)\b",  # sufix (+ „de lei")
    re.IGNORECASE,
)
_BUDGET_RE = re.compile(
    r"(?:sub|pana la|până la|maxim|maximum|buget|max)\s*(\d{1,5})|(\d{1,5})\s*(?:lei|ron)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+")

# P1 (ARCH-product-retrieval): follow-up de PREȚ pe un set deja afișat → re-căutare DETERMINISTĂ a
# produselor strict mai ieftine (search_cheaper_than), NU re-rank pe setul afișat (R3). Precizie
# mare RO/HU/EN (comparativ/superlativ de preț). Un miss cade grațios pe comportamentul vechi (R3).
_CHEAPER_RE = re.compile(
    r"\bmai\s+ieftin\w*|\bcea\s+mai\s+ieftin\w*|\bmai\s+accesibil\w*"
    r"|\bpre[țt]\s+mai\s+mic|\bmai\s+mic\s+la\s+pre[țt]|\bbuget\s+mai\s+mic"
    r"|\bprea\s+scump\w*|\bcam\s+scump\w*"
    r"|\bcheaper\b|\bcheapest\b|\bolcs[óo]bb\w*|\blegolcs[óo]bb\w*",
    re.IGNORECASE,
)

# Mesaj determinist când NU există nimic mai ieftin (niciodată tăcere/padding, P6). Per-locale.
_CHEAPEST_ALREADY: dict[str, str] = {
    "ro": "Momentan asta e cea mai ieftină opțiune pe care o am pentru tine. "
    "Vrei să-ți arăt altceva sau o altă categorie?",
    "en": "This is the cheapest option I have right now. "
    "Want me to show you something else or another category?",
    "hu": "Jelenleg ez a legolcsóbb lehetőség, amim van. Mutassak mást vagy egy másik kategóriát?",
}


def _cheapest_already_msg(language: str | None) -> str:
    return _CHEAPEST_ALREADY.get(language or "ro") or _CHEAPEST_ALREADY["ro"]


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

# Pool epuizat pe „mai arată-mi" → mesaj determinist per-locale (P6, fără tăcere; cacheable=False
# fiindcă e relativ la sesiunea ACESTUI contact — un cache hit l-ar servi altui context).
_NO_MORE_RESULTS: dict[str, str] = {
    "ro": "Astea sunt toate opțiunile pe care le am pe criteriile astea. "
    "Vrei să căutăm altceva sau să schimbăm filtrele?",
    "en": "That's everything I have for these criteria. "
    "Want to search for something else or adjust the filters?",
    "hu": "Ez minden, amim ezekre a feltételekre van. Keressünk mást vagy módosítsuk a szűrőket?",
}


def _no_more_msg(language: str | None) -> str:
    return _NO_MORE_RESULTS.get(language or "ro") or _NO_MORE_RESULTS["ro"]


# NX-91: cifre «grele» FĂRĂ valută (preț/stoc/rating inventat). ≥2 cifre SAU cu zecimale → sărim
# numerele mici de proză („top 3", „pasul 2"). `(?<![\w./-])` / `(?![\w%])` → nu prinde procente
# (89% = NX-30), nici cifre lipite de litere/căi (id-uri, „p2", versiuni). Vs _allowed_numbers.
_BARE_NUM_RE = re.compile(r"(?<![\w./-])(\d{2,6}(?:[.,]\d{1,2})?|\d[.,]\d{1,2})(?![\w%])")
# Whitelist mic, documentat: 24/48h (ferestre), „100%" fără semn, 2026 (anul curent — schema_v2 e
# 2026). Conservator: la fals-pozitiv în live, extinzi setul SAU kill-switch, nu rescrii regula.
_SAFE_BARE: frozenset[float] = frozenset({24.0, 48.0, 100.0, 2026.0})

# System prompt-urile sunt GENERATE din DB per (business, locale) — vezi `prompt_builder`
# (NX-78, principiul 9). ZERO vertical hardcodat aici. `agent_stage` construiește `PromptInputs`
# o dată și pasează prompturile la run_tool_loop / _finalize / _finalize_rich.

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
    for m in _PRICE_RE.finditer(reply):
        tok = m.group(1) or m.group(2)  # prefix-valută (grup 1) sau sufix (grup 2)
        value = float(tok.replace(",", "."))
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


def _allowed_numbers(products: list[dict[str, Any]], grounded_prices: set[float]) -> set[float]:
    """Toate numerele pe care botul AVEA voie să le spună fără valută: prețuri (price/sale_price),
    stoc, rating — din produsele retrievate + variante — plus sumele grounded (total comandă)."""
    allowed: set[float] = set(grounded_prices)
    for p in products:
        for key in ("price", "sale_price", "stock", "stock_total", "rating"):
            v = p.get(key)
            if v is not None:
                allowed.add(round(float(v), 2))
        for var in p.get("variants") or []:
            for key in ("price", "sale_price", "stock"):
                v = var.get(key)
                if v is not None:
                    allowed.add(round(float(v), 2))
    return allowed


def _bad_bare_numbers(
    reply: str, products: list[dict[str, Any]], grounded_prices: set[float]
) -> list[float]:
    """Cifrele «grele» fără valută din reply care NU sunt grounded (nici preț cu valută deja
    validat, nici whitelist de proză, nici valoare din retrieval). Gol = ok. Kill-switch
    dezactivat → întotdeauna gol (fail-open). Toleranță 0.5 (ca _prices_ok)."""
    if not get_settings().validator_bare_numbers_enabled:
        return []
    # NX-117: _PRICE_RE are 2 grupuri (prefix/sufix-valută) → finditer + group, nu findall (tuple).
    priced = {
        float((m.group(1) or m.group(2)).replace(",", ".")) for m in _PRICE_RE.finditer(reply)
    }  # prețurile deja validate în _prices_ok
    allowed = _allowed_numbers(products, grounded_prices)
    bad: list[float] = []
    for token in _BARE_NUM_RE.findall(reply):
        value = float(token.replace(",", "."))
        if any(abs(value - p) <= 0.5 for p in priced):  # „89 lei" → numărul 89 e deja acoperit
            continue
        if value in _SAFE_BARE:
            continue
        if not any(abs(value - a) <= 0.5 for a in allowed):
            bad.append(value)
    return bad


def _bare_numbers_ok(
    reply: str, products: list[dict[str, Any]], grounded_prices: set[float]
) -> bool:
    return not _bad_bare_numbers(reply, products, grounded_prices)


def _claims_ok(reply: str) -> bool:
    """NX-117: pe calea de proză, claim-uri ne-numerice neverificabile (superlativ „best seller",
    claim de stoc/disponibilitate) → respins → retry/fallback. Gated FAIL-OPEN de flag."""
    if not get_settings().validator_claims_enabled:
        return True
    return not has_text_claim(reply)


def _valid(
    reply: str,
    products: list[dict[str, Any]],
    allowed_links: set[str] | None = None,
    allowed_prices: set[float] | None = None,
    *,
    check_bare: bool = True,
    check_claims: bool = True,
) -> bool:
    """Preț + link grounded (mereu) + cifre bare grounded (NX-91, doar SALES) + claim-uri de text
    neverificabile (NX-117, calea de proză). `check_bare=False` + `check_claims=False` pe ORDER:
    statusul comenzii are numere DB legitime (dată/AWB/cantitate) și fapte de livrare grounded care
    NU sunt claim-uri de marketing → ar da fals-pozitive; sumele rămân păzite de _prices_ok."""
    if not (
        _prices_ok(reply, products, allowed_prices) and _links_ok(reply, products, allowed_links)
    ):
        return False
    if check_bare and not _bare_numbers_ok(reply, products, allowed_prices or set()):
        return False
    if check_claims and not _claims_ok(reply):
        return False
    return True


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


async def _finalize_rich(
    llm, rich_system: str, query: str, products: list[dict[str, Any]], ctx, history: str
):
    """Compune recomandarea STRUCTURATĂ (model iZi). Modelul emite intro + referințe
    product_id/pro_index/fit_clause + pick + education + chip_intents (enum închis); codul
    (compose) hidratează faptele. `rich_system` = system generat din DB (NX-78). Întoarce
    `RichReply` sau None (→ fallback pe proză)."""
    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    user = (
        f"Limba clientului: {ctx.language}\n{history_block}"
        f"Nevoia clientului: {query}\n\nProduse disponibile (alege dintre acestea):\n"
        f"{_rich_bundle(products)}"
    )
    try:
        j = await llm.complete_schema(rich_system, user, _RICH_SCHEMA)
    except Exception as e:  # noqa: BLE001 — apel structurat eșuat → fallback pe proză
        log.warning("agent: finalize structured eșuat (%s)", type(e).__name__)
        return None
    return compose.assemble(ctx, j, products)


async def _load_prompt_inputs(deps: PipelineDeps, ctx: TurnContext) -> PromptInputs:
    """Citește categoriile + aliasele aprobate (scoped pe business) și compune `PromptInputs`
    (NX-78). Determinist (query-uri `order by`) → prefix de cache stabil. Ridicarea unei
    excepții de DB se propagă în `try`-ul din `agent_stage` (→ echo fallback, P6)."""
    categories = await list_category_names(deps.conn, ctx.business.id)
    aliases = await list_routing_aliases(deps.conn, ctx.business.id)
    return PromptInputs.build(
        ctx.business.name, ctx.business.vertical, ctx.language, categories, aliases
    )


def _filters_hint(filters: dict[str, Any]) -> str:
    """NX-116: constrângerile structurate din triaj (`RouteDecision.filters`) ca HINT determinist
    pentru primul `search_products` — agentul nu le reparsează din proză. Args rămân ale modelului
    (P3); hint-ul doar îl seedează cu ce a extras nano."""
    if not filters:
        return ""
    parts: list[str] = []
    if filters.get("budget_max") is not None:
        parts.append(f"buget max {filters['budget_max']:g}")
    if filters.get("concerns"):
        parts.append("nevoi: " + ", ".join(filters["concerns"]))
    if filters.get("suitable_for"):
        parts.append(f"pentru: {filters['suitable_for']}")
    if filters.get("brand"):
        parts.append(f"brand: {filters['brand']}")
    if not parts:
        return ""
    return "Constrângeri detectate (folosește-le în search_products): " + "; ".join(parts) + "\n"


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

    # NX-119b: „mai arată-mi" pe o sesiune activă = paginare DETERMINISTĂ (fără bucla LLM, cost $0
    # de inferență). NU pe ORDER/„mai ieftin" (cheaper_intent). Sesiune absentă → flux normal.
    # `not route.filters`: dacă triajul a extras constrângeri NOI (buget/concerns/brand/categorie),
    # mesajul e o RAFINARE („mai multe sub 50", „mai multă hidratare"), NU paginare pură → cade pe
    # bucla LLM (filters_hint → search_products → fp nou → sesiune nouă pe filtrele rafinate). Fără
    # asta, refinarea ar servi pagina sesiunii VECHI și ar pierde tăcut constrângerile (review).
    sess = ctx.state.active_search
    show_more = (
        not is_order
        and get_settings().search_sessions_enabled
        and bool(sess)
        and not route.filters
        and _MORE_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )

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
        if result.state_patch:  # NX-79: cart_add → mutație de state (persistată de processor)
            ctx.state_patch.update(result.state_patch)
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
    filters_hint = _filters_hint(route.filters)  # NX-116: seed structurat din triaj (P3 respectat)
    user = (
        f"Limba clientului: {ctx.language}\n{cat_hint}{filters_hint}{context_block}{history_block}"
        f"Mesaj client: {query}"
    )

    try:
        inp = await _load_prompt_inputs(deps, ctx)  # prompt generat din DB (NX-78, P9)
        if show_more:
            # Pagina următoare din pool-ul sesiunii — determinist, fără inferență LLM (NX-119b).
            page = await catalog_tools.continue_search_session(ctx, deps, sess, 6)
            if not page.products:
                # Pool epuizat → mesaj determinist per-locale, fără tăcere (P6, cacheable=False).
                ctx.emit("show_more", served=0)
                ctx.set_reply(_no_more_msg(ctx.language), cacheable=False)
                return
            retrieved = page.products
            ctx.emit("show_more", served=len(retrieved))
            final = ""  # fără text de model — compose-ul rich phrasează din produse mai jos
        else:
            final = await deps.llm.run_tool_loop(
                prompt_builder.build_agent_system(inp), user, tools, execute
            )
    except Exception as e:  # noqa: BLE001 — bucla eșuată → lasă echo fallback
        log.warning("agent: tool loop eșuat (%s)", type(e).__name__)
        return

    products = _dedupe(retrieved)
    # P1 (ARCH-product-retrieval): follow-up „mai ieftin" pe un set deja afișat → re-căutare
    # DETERMINISTĂ a produselor strict mai ieftine decât cel mai ieftin afișat, în aceeași categorie
    # (search_cheaper_than) — NU re-rank pe setul afișat (bug-ul „cea mai ieftină 80.99 când există
    # 18.99"). Arată DOAR ce e mai ieftin (1 dacă e 1, zero padding); nimic mai ieftin → mesaj
    # determinist (niciodată tăcere/padding, P6). Sare peste R3 pentru această intenție.
    cheaper_intent = (
        not is_order
        and not show_more  # „mai arată-mi" deja paginat determinist mai sus
        and get_settings().cheaper_intent_enabled
        and bool(ctx.state.displayed_products)
        and _CHEAPER_RE.search(query) is not None
    )
    if cheaper_intent:
        baseline = min(p.price for p in ctx.state.displayed_products)
        ref_ids = [p.product_id for p in ctx.state.displayed_products]
        cheaper = await search_cheaper_than(deps.conn, ctx.business.id, ref_ids, baseline, limit=6)
        ctx.emit("cheaper_followup", baseline=round(baseline, 2), found=len(cheaper))
        if cheaper:
            products = _dedupe(cheaper)
        else:
            # Nimic mai ieftin → mesaj sigur (NU cacheabil: e relativ la setul afișat al ACESTUI
            # client; un cache hit l-ar servi altui context — clasa de cache-poisoning știută).
            ctx.set_reply(_cheapest_already_msg(ctx.language), cacheable=False)
            return
    # R3: follow-up pe produse DEJA arătate („care e cea mai bună?") la care modelul n-a rechemat
    # un tool → re-hidratează produsele afișate (după id, din state) ca set de retrieval, ca să
    # răspundem GROUNDED pe ele în loc de „n-am găsit". Doar SALES, NU pe intenția de preț (aia o
    # tratează cheaper_intent mai sus), doar cu id-uri în state, și DOAR când textul singur ar pica
    # (gol sau preț negroundat). Rămâne plasa de grounding pentru follow-up-urile neclasificate.
    if (
        not products
        and not is_order
        and not cheaper_intent
        and not show_more
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
            rich = await _finalize_rich(
                deps.llm, prompt_builder.build_rich_system(inp), query, products, ctx, history
            )
            if rich is not None and rich.items:
                ctx.set_rich_reply(
                    rich,
                    text=compose.flatten(rich),
                    products=compose.card_products(rich.items),
                )
                ctx.emit("agent_recommended", n=len(rich.items), rich=True)
                return
        # NX-91: dacă textul brut al modelului are cifre bare negroundate, semnalează (P12: doar
        # contorul, NU corpul). _finalize declanșează retry-ul/fallback-ul pe baza lui _valid.
        bare = _bad_bare_numbers(final, products, grounded_prices) if final else []
        if bare:
            ctx.emit("validator_rejected", kind="bare_number", n=len(bare))
        # NX-117: claim ne-numeric neverificabil pe proză → semnalează (P12: doar contorul).
        if final and not _claims_ok(final):
            ctx.emit("validator_rejected", kind="claim")
        reply = await _finalize(
            deps.llm,
            prompt_builder.build_reco_system(inp),
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
            # ORDER: fără bare-check (numere DB legitime: dată/AWB/cantitate) — vezi _valid.
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
