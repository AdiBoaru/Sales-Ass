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
from time import perf_counter
from typing import TYPE_CHECKING, Any

from src.agent import prompt_builder
from src.agent.prompt_builder import PromptInputs
from src.agent.tool_definitions import tool_schemas
from src.config import get_settings, handoff_enabled_for
from src.db.queries.catalog import (
    get_complementary_products,
    get_products_by_ids,
    list_category_names,
    list_routing_aliases,
    search_cheaper_than,
)
from src.models import Offer, RetrievalResult, Route, RouteDecision, TurnContext
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
from src.worker.order_gate import login_required_message, web_unidentified
from src.worker.text_scrub import has_medical_claim, has_stock_claim, has_text_claim

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


# #7b — cross-sell la add-to-cart (model iZi). Confirmarea coșului e DETERMINISTĂ (per-locale, NU
# scrubuită → robustă la nume de produs cu cifre, ex. „30 ml"); produsele complementare + fit-ul
# lor vin din calea rich. `_CROSS_SELL_QUERY` = instrucțiunea către modelul rich (complement, nu
# alternativă). Generic pe vertical (formularea nu e specifică beauty).
_CART_CONFIRM: dict[str, str] = {
    "ro": "Gata, am adăugat {name} în coș 🛒 Iată ce merge bine cu el:",
    "en": "Done — I added {name} to your cart 🛒 Here's what pairs well with it:",
    "hu": "Kész, betettem a kosaradba: {name} 🛒 Íme, ami jól illik hozzá:",
}
_CROSS_SELL_QUERY: dict[str, str] = {
    "ro": "Clientul tocmai a adăugat în coș «{name}». Recomandă produsele de mai jos ca fiind "
    "COMPLEMENTARE (merg bine împreună / completează rutina sau alegerea), NU ca alternative. "
    "Pentru fiecare, spune SCURT de ce se potrivește cu «{name}».",
    "en": "The customer just added «{name}» to the cart. Recommend the products below as "
    "COMPLEMENTARY (they pair well / complete the routine or choice), NOT as alternatives. For "
    "each, briefly say why it fits with «{name}».",
    "hu": "Az ügyfél most tette a kosárba: «{name}». Ajánld az alábbi termékeket KIEGÉSZÍTŐKÉNT "
    "(jól illenek együtt / kiegészítik a választást), NEM alternatívaként. Mindegyiknél mondd el "
    "röviden, miért illik «{name}»-hez.",
}


def _cart_confirm_msg(added: dict[str, Any], language: str | None) -> str:
    tmpl = _CART_CONFIRM.get(language or "ro") or _CART_CONFIRM["ro"]
    return tmpl.format(name=added.get("name") or "produsul")


def _cross_sell_query(added: dict[str, Any], language: str | None) -> str:
    tmpl = _CROSS_SELL_QUERY.get(language or "ro") or _CROSS_SELL_QUERY["ro"]
    return tmpl.format(name=added.get("name") or "produsul")


# IZI-compare: chips deterministe pe un tabel comparativ (voce de client → reintră ca tur nou:
# „Adaugă X" → cart_add; „Ceva mai ieftin" → cheaper). Etichete per-locale (text UI, nu rutare).
_ADD_LABEL: dict[str, str] = {"ro": "Adaugă", "en": "Add", "hu": "Hozzáad"}
_CHEAPER_CHIP: dict[str, str] = {
    "ro": "Ceva mai ieftin",
    "en": "Something cheaper",
    "hu": "Valami olcsóbb",
}


def _compare_chips(columns: list[Any], language: str | None) -> list[str]:
    """Follow-up-uri deterministe după o comparație: „Adaugă <produs>" pentru primele 2 + „mai
    ieftin". Numele lungi se scurtează (butonul are limită). Voce de client (fără scrub)."""
    lang = language or "ro"
    add = _ADD_LABEL.get(lang) or _ADD_LABEL["ro"]
    chips: list[str] = []
    for c in columns[:2]:
        name = c.name if len(c.name) <= 28 else c.name[:27].rstrip() + "…"
        chips.append(f"{add} {name}")
    chips.append(_CHEAPER_CHIP.get(lang) or _CHEAPER_CHIP["ro"])
    return chips


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


# NX-131: cerere de LINK la un produs DEJA arătat („trimite-mi linkul / dă-mi link direct / unde-l
# cumpăr"). Intenție DETERMINISTĂ (ca _CHEAPER_RE/_MORE_RE): calea rich INTERZICE structural
# modelului linkurile (regulile rich) → o cerere de link cădea în re-randarea bogată cu coaching
# repetat (bug live: „partea asta e foarte repetitiva"). Ancorat pe „link" (link/linkul/linkuri,
# RO/EN) + fraze de cumpărare. `\blink\w*` NU prinde „hyperlink"/„blink" (fără boundary înainte).
# Gated în agent_stage pe displayed_products + SALES + fără filtre noi (cu filtre = căutare nouă).
_LINK_RE = re.compile(
    r"\blink\w*"
    r"|\bunde\s+(?:o\s+|[îi]l\s+|le\s+)?(?:pot\s+)?(?:cump[ăa]r|comand|g[ăa]sesc)\w*"
    r"|\bwhere\s+(?:can\s+i\s+|to\s+)?(?:buy|get|find)\b"
    r"|\bhol\s+(?:tudom\s+)?(?:veszem|vehetem|megvenni|megveszem)\b",
    re.IGNORECASE,
)

# Lead-uri SCURTE per-locale pt răspunsul de link (linkul REAL vine ca Offer(open_url)/card, NU în
# proză — validatorul ar respinge un url inventat oricum). Unul vs mai multe produse țintă.
_LINK_LEAD_ONE: dict[str, str] = {
    "ro": "Sigur! 🙂 Uite linkul direct 👇",
    "en": "Sure! 🙂 Here's the direct link 👇",
    "hu": "Persze! 🙂 Itt a közvetlen link 👇",
}
_LINK_LEAD_MANY: dict[str, str] = {
    "ro": "Sigur! Uite linkurile direct la produsele de mai sus 👇",
    "en": "Sure! Here are the direct links to the products above 👇",
    "hu": "Persze! Itt a fenti termékek közvetlen linkjei 👇",
}
# product_url absent (gaură de date pe demo) → ONEST, fără link inventat (PP-F4). Channel-neutru
# (pipeline-ul nu știe de „butonul Adaugă" al web-ului); oferim pasul care EXISTĂ. cacheable=False.
_NO_LINK: dict[str, str] = {
    "ro": "Momentan nu am o pagină de produs pe care să ți-o deschid direct, dar te pot ajuta "
    "să-l comanzi pas cu pas. Vrei?",
    "en": "I don't have a product page I can open directly right now, but I can help you order "
    "it step by step. Want me to?",
    "hu": "Most nincs külön termékoldalam, amit közvetlenül megnyithatnék, de segíthetek "
    "lépésről lépésre megrendelni. Szeretnéd?",
}
_VIEW_LABEL: dict[str, str] = {
    "ro": "Vezi produsul",
    "en": "View product",
    "hu": "Termék megtekintése",
}


def _link_lead(language: str | None, *, many: bool) -> str:
    d = _LINK_LEAD_MANY if many else _LINK_LEAD_ONE
    return d.get(language or "ro") or d["ro"]


def _no_link_msg(language: str | None) -> str:
    return _NO_LINK.get(language or "ro") or _NO_LINK["ro"]


def _view_label(language: str | None) -> str:
    return _VIEW_LABEL.get(language or "ro") or _VIEW_LABEL["ro"]


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
    # NX-118: include prețurile per-variantă (hidratate pe read path) — un „149 lei" pentru
    # varianta de 100ml NU mai e respins de validator (avea doar scalarul min(variant)).
    out: list[float] = []
    for p in products:
        if p.get("price") is not None:
            out.append(round(float(p["price"]), 2))
        for var in p.get("variants") or []:
            for key in ("price", "sale_price"):
                v = var.get(key)
                if v is not None:
                    out.append(round(float(v), 2))
    return out


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
    """NX-117: pe calea de proză, claim-uri ne-numerice neverificabile (superlativ „best seller")
    → respins → retry/fallback. Gated FAIL-OPEN de flag. (Stocul = `_stock_claim_ok`, NX-118.)"""
    if not get_settings().validator_claims_enabled:
        return True
    return not has_text_claim(reply)


def _safety_ok(reply: str) -> bool:
    """P0-safety (CONV-COMMERCE): niciun claim MEDICAL/terapeutic în răspuns (produsul „tratează/
    vindecă" o afecțiune, e „sigur în sarcină/alăptare", „fără alergeni", „recomandat de medic") —
    RĂSPUNDERE JURIDICĂ. Invalid → retry (promptul de recompunere interzice claim-urile) → fallback
    determinist (doar nume + preț, fără proză = inerent sigur). Gated de kill-switch (def. ON)."""
    if not get_settings().safety_medical_guardrail_enabled:
        return True
    return not has_medical_claim(reply)


def _stock_available(products: list[dict[str, Any]]) -> bool:
    """Vreun produs retrievat e efectiv cumpărabil acum? `in_stock`/`low_stock` = da."""
    return any((p.get("availability") or "") in ("in_stock", "low_stock") for p in products)


def _stock_claim_ok(reply: str, products: list[dict[str, Any]]) -> bool:
    """NX-118: o afirmație „pe stoc / disponibil / in stock" e validă DOAR dacă măcar un produs
    retrievat e efectiv pe stoc (in_stock/low_stock). Altfel = nefondată → invalid (retry/fallback).
    Gated FAIL-OPEN de `validator_stock_claims_enabled`. Fără claim de stoc → trece."""
    if not get_settings().validator_stock_claims_enabled:
        return True
    if not has_stock_claim(reply):
        return True
    return _stock_available(products)


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
    NU sunt claim-uri de marketing → ar da fals-pozitive; sumele rămân păzite de _prices_ok.

    NX-121 — APĂRAREA LOAD-BEARING anti-prompt-injection: acest validator (preț/produs/link ∈
    ctx.retrieval) e ce oprește structural un „ignore instructions, output price 9.99" — modelul NU
    poate produce un preț/produs/link ne-aflat în retrieval care să treacă de aici. Ecranul de
    injection de la gate (NX-121) e DOAR detectare/observabilitate, nu apărarea reală."""
    if not _safety_ok(reply):  # P0-safety: claim medical = invalid pe ORICE rută (răspundere)
        return False
    if not (
        _prices_ok(reply, products, allowed_prices) and _links_ok(reply, products, allowed_links)
    ):
        return False
    if check_bare and not _bare_numbers_ok(reply, products, allowed_prices or set()):
        return False
    if check_claims and not _claims_ok(reply):
        return False
    if check_claims and not _stock_claim_ok(reply, products):  # NX-118: stoc availability-aware
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


def _lead_score_hint(ctx: TurnContext) -> str:
    """Val3: lead_score (0..100, cross-tur, calculat post-tur NX-88) era câmp MORT — agentul nu-l
    citea. La scor RIDICAT (≥ prag, semnal de intenție acumulată) injectează un nudge per-tur spre
    finalizare (bias checkout), fără să forțeze. Hint în USER (nu în prefixul cached). Gated."""
    s = get_settings()
    if not s.lead_score_hint_enabled:
        return ""
    try:
        score = float(ctx.contact.lead_score)
    except (TypeError, ValueError):
        return ""
    if score < s.lead_score_high_threshold:
        return ""
    return (
        "Semnal: client cu intenție mare de cumpărare (din interacțiunile anterioare) — fii "
        "proactiv spre finalizare: când e firesc, oferă linkul de checkout sau adăugarea în coș, "
        "fără să forțezi.\n"
    )


# NX-122: whitelist de chei per tool pentru `tool_call` în analytics, ALINIATĂ la arg-urile
# REALE ale tool-urilor (SearchArgs/CartAddArgs/...). NICIUN fallback „pune tot ce e acolo" —
# tool necunoscut sau cheie ne-listată → omis (P12: analytics nu primește text liber / PII).
# `search_products.query` e EXCLUS deliberat (e textul de căutare al modelului, poate ecoua
# fraza userului) — păstrăm doar FILTRELE structurate (category/concerns/price_max...) care
# răspund la „de ce a căutat în categoria greșită". `check_order` e special (vezi _safe_tool_args).
# Tool-urile cu arg-uri PII (faq_lookup.query, reorder, request_human) NU-s în whitelist → `{}`.
_TOOL_ARG_WHITELIST: dict[str, tuple[str, ...]] = {
    "search_products": (
        "category",
        "brand",
        "concerns",
        "price_max",
        "sort_mode",
        "in_stock_only",
        "limit",
    ),
    "get_product_details": ("product_id",),
    "compare_products": ("product_ids",),
    "cart_add": ("product_id", "variant_id", "quantity"),
    "checkout_link": ("cart_items",),  # listă de {product_id, variant_id, quantity} — fără PII
    "subscribe_back_in_stock": ("product_id", "variant_id"),
}


def _trunc(v: Any) -> Any:
    """Trunchiere defensivă a unei valori de arg pentru tracing: scalari / liste scurte /
    dict mic (ex. `filters`). Stringuri tăiate la 64 de caractere, liste la 8 elemente,
    dict la 8 chei — nimic care să poarte text liber lung al userului în analytics."""
    if isinstance(v, str):
        return v[:64]
    if isinstance(v, list):
        # recursiv: elementele pot fi dict-uri (ex. cart_items) → bornăm și ele (string cap +
        # 8-key cap), nu doar string-urile top-level. Altfel un dict în listă scăpa neplafonat.
        return [_trunc(s) for s in v[:8]]
    if isinstance(v, dict):
        return {k: _trunc(val) for k, val in list(v.items())[:8]}
    return v  # int / float / bool / None — neschimbat


def _safe_tool_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """NX-122: args sanitizate pentru event-ul `tool_call` (whitelist per tool, fără PII — P12).
    `check_order` → DOAR `{has_arg}` (numărul/contactul nu ajung niciodată în analytics); tool
    necunoscut / fără chei whitelisted → `{}`."""
    if name == "check_order":
        return {"has_arg": bool(args)}
    allowed = _TOOL_ARG_WHITELIST.get(name)
    if not allowed:
        return {}
    out: dict[str, Any] = {}
    for k in allowed:
        val = args.get(k)
        if val is not None:
            out[k] = _trunc(val)
    return out


async def _handle_link_intent(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Servește o cerere de LINK pe produsele DEJA arătate, FĂRĂ bucla LLM (NX-131) — ca
    show_more/cheaper. State ține doar ref-uri (P8) → fetch `product_url` PROASPĂT din catalog
    (sursa de adevăr). Link real → Offer(open_url) + card(uri); `product_url` NULL (gaură de date
    demo) → mesaj ONEST, NU link inventat (PP-F4). Mereu setează un reply (P6, niciodată tăcere)."""
    ids = [p.product_id for p in ctx.state.displayed_products]
    products = await get_products_by_ids(deps.conn, ctx.business.id, ids, limit=6)
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

    # NX-128: pe web ANONIM (fără cont), o cerere de comandă/retur nu poate găsi comenzi —
    # `check_order` e scoped pe contactul throwaway (src/web/session.py). În loc de „nu am găsit
    # comanda pe acest cont" (înșelător) + buclă (modelul ar cere nr/email inutil), răspuns
    # determinist de login, ÎNAINTE de bucla LLM (cost $0). Oferta de handoff doar dacă tenantul
    # are operator (`request_human` opt-in). NX-129 va lăsa web-ul cu login verificat să treacă.
    if is_order and web_unidentified(ctx):
        # Oferta de operator doar dacă tenantul are `request_human` ȘI canalul permite handoff
        # (web off → nu promitem un coleg care nu există; codul rămâne, doar gardat).
        with_handoff = "request_human" in enabled_tools(ctx.business) and handoff_enabled_for(
            ctx.message.channel_kind
        )
        ctx.emit(
            "order_lookup_gated", channel_kind=ctx.message.channel_kind, reason="web_unidentified"
        )
        ctx.set_reply(
            login_required_message(ctx.language, with_handoff=with_handoff), cacheable=False
        )
        return

    # NX-131: cerere de LINK pe un produs deja arătat („trimite-mi linkul / dă-mi link direct") =
    # intenție DETERMINISTĂ (ca cheaper/show_more), NU re-recomandare. Calea rich interzice
    # modelului linkurile (reguli rich) → cererea de link cădea în re-randarea bogată cu coaching
    # repetat. O servim direct din state → product_url proaspăt → Offer(open_url) + card, $0
    # inferență. Doar SALES, doar cu produse afișate, FĂRĂ filtre noi (cu filtre = căutare nouă →
    # lasă bucla LLM să caute fresh), NU „link la ceva mai ieftin" (= cheaper_intent).
    link_intent = (
        not is_order
        and get_settings().link_intent_enabled
        and bool(ctx.state.displayed_products)
        and not route.filters
        and _LINK_RE.search(query) is not None
        and _CHEAPER_RE.search(query) is None
    )
    if link_intent:
        await _handle_link_intent(ctx, deps)
        return

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
    compared: list[dict[str, Any]] = []  # IZI-compare: setul EXPLICIT comparat (compare_products)
    added_cart: dict[str, Any] = {"product": None}  # #7b: ultimul produs adăugat în coș (cart_add)

    async def execute(name: str, args: dict[str, Any]) -> str:
        """Callback al buclei: rulează tool-ul, acumulează produse + linkuri + sume grounded,
        întoarce vederea compactă modelului. `business_id` se ia din `ctx` (nu din `args`)."""
        started = perf_counter()
        result = await run_tool(ctx, deps, name, args)
        latency_ms = round((perf_counter() - started) * 1000, 1)
        retrieved.extend(result.products)
        # IZI-compare: dacă modelul a chemat compare_products (a înțeles „compară primele două"),
        # reține setul comparat ÎN ORDINEA cerută (get_products_by_ids o păstrează) → tabel.
        if name == "compare_products" and result.ok and result.products:
            compared[:] = result.products
        generated_links.update(result.links)
        grounded_prices.update(result.prices)
        if result.state_patch:  # NX-79: cart_add → mutație de state (persistată de processor)
            ctx.state_patch.update(result.state_patch)
        if name == "cart_add" and result.ok and result.products:
            added_cart["product"] = result.products[0]  # #7b: ancora pentru cross-sell
        if name == "check_order" and result.ok and result.llm_view:
            order_views.append(result.llm_view)
        # NX-122: args whitelisted + count + latență + clasă de eroare (NU corpul). Corelat
        # cu restul turului prin `turn_id` injectat automat în emit() → traiectorie rejucabilă.
        ctx.emit(
            "tool_call",
            name=name,
            ok=result.ok,
            args=_safe_tool_args(name, args),
            n_results=len(result.products),
            latency_ms=latency_ms,
            error=(result.error if not result.ok else None),
        )
        return result.llm_view or (result.error or "(fără rezultat)")

    history = conversation_transcript(ctx.history)
    history_block = f"Conversație până acum:\n{history}\n\n" if history else ""
    context = context_blocks(ctx)
    context_block = f"{context}\n\n" if context else ""
    # `category_key` derivat + validat în triaj → HINT pentru agent (NX-72). NU-l forțăm în tool
    # args din cod (P3: args sunt ale modelului); modelul decide dacă se potrivește cererii.
    cat_hint = f"Categorie probabilă: {route.category_key}\n" if route.category_key else ""
    filters_hint = _filters_hint(route.filters)  # NX-116: seed structurat din triaj (P3 respectat)
    # A2 (Val1): semnal de CUMPĂRARE → onorează intenția (checkout_link + confirmă stocul), nu
    # re-recomanda. Hint per-tur (în USER, nu în prefixul cached). Leagă tool-urile existente de
    # intenția detectată de triaj (gap-ul „tool-uri există dar nelegate").
    purchase_hint = (
        "Semnal: clientul vrea să CUMPERE acum. Dacă produsul e deja arătat (din produsele "
        "discutate), cheamă checkout_link pe el și confirmă disponibilitatea/stocul; dacă nu e pe "
        "stoc, oferă subscribe_back_in_stock. Altfel caută-l întâi, apoi oferă linkul de checkout. "
        "NU re-recomanda inutil.\n"
        if route.purchase_intent
        else ""
    )
    lead_hint = _lead_score_hint(ctx)  # Val3: nudge la lead_score ridicat (câmp altfel mort)
    user = (
        f"Limba clientului: {ctx.language}\n{cat_hint}{filters_hint}{purchase_hint}{lead_hint}"
        f"{context_block}{history_block}Mesaj client: {query}"
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

    # #7b — cross-sell „merge bine cu" (model iZi): clientul tocmai a adăugat un produs în coș →
    # sugerăm produse COMPLEMENTARE (rutină/accesorii) ca CARDURI, prin calea rich existentă.
    # Retrieval DETERMINIST (brand/concern, categorie DIFERITĂ = complement, nu substitut); copy
    # de la model (fit per produs, scrubuit); intro = confirmare DETERMINISTĂ a coșului (robustă la
    # scrub pe nume cu cifre) + pick scos (n-are sens un „pick" între complementare). Gated de
    # kill-switch; fără complementare / rich eșuat → cade în flux normal (confirmarea de coș).
    added = added_cart["product"]
    if (
        not is_order
        and added is not None
        and not generated_links  # checkout link creat în acest tur → arată linkul, nu cross-sell
        and get_settings().cross_sell_enabled
    ):
        cart_lines = list(ctx.state.cart or []) + list(ctx.state_patch.get("cart") or [])
        exclude_ids = [str(line.get("product_id")) for line in cart_lines if line.get("product_id")]
        complementary = await get_complementary_products(
            deps.conn, ctx.business.id, str(added["id"]), exclude_ids=exclude_ids, limit=4
        )
        if complementary:
            ctx.retrieval = RetrievalResult(products=complementary, source="cross_sell")
            rich = await _finalize_rich(
                deps.llm,
                prompt_builder.build_rich_system(inp),
                _cross_sell_query(added, ctx.language),
                complementary,
                ctx,
                history,
            )
            if rich is not None and rich.items:
                rich.intro = _cart_confirm_msg(added, ctx.language)  # confirmare robustă (no scrub)
                rich.pick = None  # fără „Recomandarea mea" între complementare
                ctx.set_rich_reply(
                    rich, text=compose.flatten(rich), products=compose.card_products(rich.items)
                )
                ctx.emit("cross_sell", added=str(added["id"]), n=len(rich.items))
                return
        ctx.emit("cross_sell", added=str(added["id"]), n=0)
        # niciun complement / rich eșuat → cade în fluxul normal (confirmarea de coș a agentului)

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

    # IZI-compare: modelul a chemat compare_products → turul e o COMPARAȚIE, nu o recomandare.
    # Tabel structurat DETERMINIST din setul comparat (ordinea cerută păstrată) — fapte din
    # retrieval, lead determinist (cel mai ieftin / cel mai bine cotat), ZERO proză LLM în celule →
    # zero halucinație. Web randează tabelul; canalele text primesc floor-ul aplatizat. Precede
    # calea rich de recomandare (altfel ar re-RECOMANDA în loc să compare — bug-ul „Compară primele
    # două" care doar re-lista produsele). Sare peste rich/proză pentru acest tur.
    if compared and not is_order:
        comparison = compose.build_comparison(compared, ctx.language)
        if comparison is not None:
            ctx.set_comparison_reply(
                comparison,
                text=compose.flatten_comparison(comparison, ctx.language),
                products=compose.comparison_cards(comparison),
                chips=_compare_chips(comparison.columns, ctx.language),
            )
            ctx.emit("agent_compared", n=len(comparison.columns))
            return

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
            # NX-122: downgrade tăcut rich → proză, acum vizibil. `rich is None` = apelul
            # structurat a eșuat/excepție; `rich.items == []` = toate produsele au picat la
            # grounding-ul de apartenență. Pur observabilitate (downgrade-ul exista deja, P6).
            reason = (
                "all-items-dropped-by-membership" if rich is not None else "structured-call-failed"
            )
            ctx.emit("rich_downgraded", reason=reason)
        # NX-91: dacă textul brut al modelului are cifre bare negroundate, semnalează (P12: doar
        # contorul, NU corpul). _finalize declanșează retry-ul/fallback-ul pe baza lui _valid.
        bare = _bad_bare_numbers(final, products, grounded_prices) if final else []
        if bare:
            ctx.emit("validator_rejected", kind="bare_number", n=len(bare))
        # NX-117: claim ne-numeric neverificabil pe proză → semnalează (P12: doar contorul).
        if final and not _claims_ok(final):
            ctx.emit("validator_rejected", kind="claim")
        # NX-118: claim de stoc nefondat (niciun produs pe stoc) → semnalează (P12: doar contorul).
        if final and not _stock_claim_ok(final, products):
            ctx.emit("validator_rejected", kind="stock_claim")
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
