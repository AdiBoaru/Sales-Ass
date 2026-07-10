"""Stagiul 8 (calea de PROZĂ) — validatorul anti-halucinație, extras din `agent.py` (NX-142).

Cluster PUR, determinist (zero I/O, zero `TurnContext`/`deps`/DB): predicate peste
`reply: str` + `products` (ref-uri retrievate) + linkuri/sume grounded de bot. Verifică structural
că botul NU inventează preț/link/număr/claim:
  • `_prices_ok`   — fiecare preț cu valută ∈ prețuri retrievate (+ variante) SAU sumă grounded.
  • `_links_ok`    — fiecare URL ∈ product_url retrievat SAU link generat în tur (checkout_link).
  • `_bare_numbers_ok` — cifrele «grele» fără valută sunt grounded (NX-91; whitelist `_SAFE_BARE`).
  • `_claims_ok`   — fără superlativ/claim de text neverificabil (NX-117; gated fail-open).
  • `_safety_ok`   — P0-safety: niciun claim MEDICAL/terapeutic (răspundere; kill-switch).
  • `_stock_claim_ok` — „pe stoc" valid doar dacă un produs retrievat e cumpărabil (NX-118).

`validate_prose` e SURSA UNICĂ care agregă predicatele → `ValidationResult` (ok + `reasons`);
`_valid` (bool) e doar shim-ul peste ea (fără dublarea secvenței de reguli). `agent.py` orchestrează
retry-cu-feedback + fallback pe baza lor — regia RĂMÂNE acolo, aici trăiesc doar verificările.

NX-121 — APĂRAREA LOAD-BEARING anti-prompt-injection: `_valid` (preț/produs/link ∈ retrieval) e ce
oprește structural un „ignore instructions, output price 9.99". Ecranul de injection de la gate e
DOAR observabilitate; apărarea reală e aici. Calea BOGATĂ folosește `compose.scrub_*` (scrub→DROP);
aceasta e calea de PROZĂ (invalid→retry→fallback). Delimitare per CLAUDE.md stagiul 8.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.config import get_settings
from src.worker.text_scrub import has_medical_claim, has_stock_claim, has_text_claim

# NX-117: prinde valuta în SUFIX („89 lei", „89 de lei", „89 ron") ȘI în PREFIX („RON 89", „lei 89")
# → un preț real prefixat nu e tratat fals ca cifră bară, iar un preț prefixat negroundat e prins.
# „leu" (singular, ex. „1 leu") ALĂTURI de „lei" (plural) — altfel un preț halucinat de exact 1
# scapă structural de validator (nici preț cu valută, nici cifră bară pe o singură cifră).
_PRICE_RE = re.compile(
    r"\b(?:lei|leu|ron)\s*(\d{1,6}(?:[.,]\d{1,2})?)"  # prefix-valută
    r"|(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:de\s+)?(?:lei|leu|ron)\b",  # sufix (+ „de lei")
    re.IGNORECASE,
)
_BUDGET_RE = re.compile(
    r"(?:sub|pana la|până la|maxim|maximum|buget|max)\s*(\d{1,5})|(\d{1,5})\s*(?:lei|ron)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+")

# NX-91: cifre «grele» fără valută (halucinate). Nu prinde procente lipite („89%"), nici cifre
# lipite de litere/căi (id-uri, „p2", versiuni). Vs _allowed_numbers.
_BARE_NUM_RE = re.compile(r"(?<![\w./-])(\d{2,6}(?:[.,]\d{1,2})?|\d[.,]\d{1,2})(?![\w%])")
# Whitelist mic, documentat: 24/48h (ferestre), „100%" fără semn, 2026 (anul curent — schema_v2 e
# 2026). Conservator: la fals-pozitiv în live, extinzi setul SAU kill-switch, nu rescrii regula.
_SAFE_BARE: frozenset[float] = frozenset({24.0, 48.0, 100.0, 2026.0})


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


@dataclass
class ValidationResult:
    """Rezultatul validării de proză: `ok` + `reasons` (motivele de respingere, gol când ok)."""

    ok: bool
    reasons: list[str] = field(default_factory=list)


def validate_prose(
    reply: str,
    *,
    products: list[dict[str, Any]],
    generated_links: set[str] | None = None,
    grounded_prices: set[float] | None = None,
    check_bare: bool = True,
    check_claims: bool = True,
) -> ValidationResult:
    """SURSA UNICĂ DE ADEVĂR a validării de proză: preț + link grounded (mereu) + cifre bare
    grounded (NX-91, doar SALES) + claim-uri de text neverificabile (NX-117) + stoc availability-
    aware (NX-118) + P0-safety medical. Întoarce `ok` + motivele de respingere (auditabil/testabil).
    `check_bare=False` + `check_claims=False` pe ORDER: statusul comenzii are numere DB legitime
    (dată/AWB/cantitate) și fapte de livrare grounded → ar da fals-pozitive; sumele rămân păzite de
    `_prices_ok`. `_valid` (bool) e shim-ul peste asta — o singură secvență de reguli, fără dublare.

    NX-121 — APĂRAREA LOAD-BEARING anti-prompt-injection: preț/produs/link ∈ ctx.retrieval e ce
    oprește structural un „ignore instructions, output price 9.99". Ecranul de injection de la gate
    (NX-121) e DOAR detectare/observabilitate, nu apărarea reală."""
    reasons: list[str] = []
    if not _safety_ok(reply):  # P0-safety: claim medical = invalid pe ORICE rută (răspundere)
        reasons.append("medical_claim")
    if not _prices_ok(reply, products, grounded_prices):
        reasons.append("ungrounded_price")
    if not _links_ok(reply, products, generated_links):
        reasons.append("invented_link")
    if check_bare and not _bare_numbers_ok(reply, products, grounded_prices or set()):
        reasons.append("bare_number")
    if check_claims and not _claims_ok(reply):
        reasons.append("text_claim")
    if check_claims and not _stock_claim_ok(reply, products):  # NX-118: stoc availability-aware
        reasons.append("stock_claim")
    return ValidationResult(ok=not reasons, reasons=reasons)


def _valid(
    reply: str,
    products: list[dict[str, Any]],
    allowed_links: set[str] | None = None,
    allowed_prices: set[float] | None = None,
    *,
    check_bare: bool = True,
    check_claims: bool = True,
) -> bool:
    """Shim bool peste `validate_prose` (API-ul folosit de `agent._finalize*` — o singură sursă de
    adevăr). Argumentele poziționale `allowed_links`/`allowed_prices` = `generated_links`/
    `grounded_prices`; păstrate pt backward-compat cu call-site-urile + testele."""
    return validate_prose(
        reply,
        products=products,
        generated_links=allowed_links,
        grounded_prices=allowed_prices,
        check_bare=check_bare,
        check_claims=check_claims,
    ).ok
