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
from src.observability import turn_latency
from src.worker.text_scrub import has_medical_claim, has_stock_claim, has_text_claim

# Suma, cu SAU fără separator de mii. Ordinea alternativelor contează: variantele grupate stau
# ÎNAINTEA celei simple, altfel „1.234,50" s-ar potrivi doar parțial („234,50") și un preț REAL ar
# fi citit ca o sumă inventată. Formatul românesc („1.234,50") e cel pe care îl scriem noi și cel
# pe care îl scrie modelul când răspunde firesc în română.
_AMOUNT = (
    r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?"  # 1.234 / 1.234,50 (grupare ro)
    r"|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"  # 1,234 / 1,234.50 (grupare en)
    r"|\d{1,6}(?:[.,]\d{1,2})?"  # 89 / 89,00 / 89.00
)

# NX-117: prinde valuta în SUFIX („89 lei", „89 de lei", „89 ron") ȘI în PREFIX („RON 89", „lei 89")
# → un preț real prefixat nu e tratat fals ca cifră bară, iar un preț prefixat negroundat e prins.
# „leu" (singular, ex. „1 leu") ALĂTURI de „lei" (plural) — altfel un preț halucinat de exact 1
# scapă structural de validator (nici preț cu valută, nici cifră bară pe o singură cifră).
_PRICE_RE = re.compile(
    rf"\b(?:lei|leu|ron)\s*({_AMOUNT})"  # prefix-valută
    rf"|({_AMOUNT})\s*(?:de\s+)?(?:lei|leu|ron)\b",  # sufix (+ „de lei")
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


def parse_amount(token: str) -> float:
    """Textul unei sume → valoare, indiferent de convenția de scriere („1.234,50" ro, „1,234.50"
    en, „89.00", „89,00", „1.500"). Un `replace(",", ".")` naiv citea „1.234,50" ca 1234.50 doar
    din noroc și „1.500" ca 1.5, adică un preț REAL de 1.500 lei devenea „inventat" (P2 respinge
    răspunsul, retry, fallback). Determinist, fără localizare: forma decide.

    Regula: cu AMBII separatori, ultimul e cel zecimal. Cu unul singur, e grupare de mii doar dacă
    apare de mai multe ori SAU e urmat de EXACT 3 cifre cu cel mult 3 înainte („1.500" = 1500);
    altfel e zecimal („1.5" = 1.5, „89,00" = 89.0). Nimeni nu scrie un preț cu 3 zecimale, deci
    ambiguitatea „1.500" nu e reală."""
    t = token.strip()
    if "." in t and "," in t:
        cut = max(t.rfind("."), t.rfind(","))
        return float(re.sub(r"[.,]", "", t[:cut]) + "." + t[cut + 1 :])
    for sep in (".", ","):
        if sep in t:
            parts = t.split(sep)
            if len(parts) > 2 or (len(parts[-1]) == 3 and len(parts[0]) <= 3):
                return float("".join(parts))  # grupare de mii
            return float(f"{parts[0]}.{parts[1]}")  # separator zecimal
    return float(t)


def _prices_ok(
    reply: str, products: list[dict[str, Any]], allowed_prices: set[float] | None = None
) -> bool:
    """Fiecare preț menționat în reply trebuie să fie real (toleranță 0.5 lei): preț de produs
    retrievat SAU o sumă grounded din DB (ex. total comandă/checkout, G7-3)."""
    allowed = _allowed_prices(products) + sorted(allowed_prices or set())
    for m in _PRICE_RE.finditer(reply):
        tok = m.group(1) or m.group(2)  # prefix-valută (grup 1) sau sufix (grup 2)
        value = parse_amount(tok)
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


def _collapse(text: Any) -> str:
    """Casefold + orice rulare de non-alfanumerice devine UN spațiu.

    Se aplică identic numelui și ferestrei din răspuns, altfel comparația ar depinde de
    punctuație: numele de nuanță al unui BB cream („… PA+++, 27, 50 ml") are virgule pe care
    răspunsul nu le reproduce neapărat, iar potrivirea ar rata exact cazurile pentru care
    există."""
    return " ".join(re.sub(r"\W+", " ", str(text or "").casefold()).split())


def _grounded_names(products: list[dict[str, Any]]) -> list[str]:
    """Faptele TEXT pe care botul are voie să le citeze: numele produselor retrievate + etichetele
    variantelor lor. Sunt fapte ca oricare altele — doar că nu sunt numerice."""
    names: list[str] = []
    for p in products:
        names.append(_collapse(p.get("name")))
        for var in p.get("variants") or []:
            names.append(_collapse(var.get("label")))
    return [n for n in names if n]


def _neighbour(reply: str, index: int, *, before: bool) -> str:
    """Cuvântul alipit numărului, în formă colapsată. Gol dacă nu există."""
    parts = _collapse(reply[:index] if before else reply[index:]).split()
    if not parts:
        return ""
    return parts[-1] if before else parts[0]


def _quotes_a_name(reply: str, match: re.Match[str], names: list[str]) -> bool:
    """Numărul e o bucată dintr-un NUME citat, nu o afirmație proprie?

    Se cere ca numărul ÎMPREUNĂ cu un cuvânt vecin din răspuns să apară ca atare într-un nume
    fondat: „peach 77" ⊂ „anua peach 77 niacin enriched cream", „50 ml" ⊂ „… 50 ml". Vecinul e
    esențial — fără el am valida numărul GOL, iar atunci „costă 77 lei" ar deveni legitim doar
    fiindcă există un produs numit „Peach 77". Aici nu se permite o VALOARE, se recunoaște un
    CITAT: același 77, în altă frază, rămâne respins.

    Sumele cu valută nu trec pe drumul ăsta: ele sunt judecate separat de `_prices_ok`, care nu
    se relaxează. Deci nici un nume otrăvit („Cremă 9.99 lei") nu poate legitima un preț.
    """
    token = _collapse(match.group(1))
    left = _neighbour(reply, match.start(1), before=True)
    right = _neighbour(reply, match.end(1), before=False)
    windows = [w for w in (f"{left} {token}" if left else "", f"{token} {right}" if right else "")]
    return any(window in name for window in windows if window for name in names)


def _bad_bare_numbers(
    reply: str, products: list[dict[str, Any]], grounded_prices: set[float]
) -> list[float]:
    """Cifrele «grele» fără valută din reply care NU sunt grounded (nici preț cu valută deja
    validat, nici whitelist de proză, nici valoare din retrieval, nici citat dintr-un nume
    fondat). Gol = ok. Kill-switch dezactivat → întotdeauna gol (fail-open). Toleranță 0.5.

    Citatul din nume există fiindcă regula folosește „număr izolat între spații" ca aproximare
    pentru „afirmație", iar pe un catalog real formatul face parte din identitatea produsului:
    măsurat pe SOLE, 2.218 din 2.758 de nume (80%) conțineau un număr izolat — gramaje, nu
    prețuri — deci botul nu putea rosti numele produsului pe care tocmai îl afișa pe card."""
    if not get_settings().validator_bare_numbers_enabled:
        return []
    # NX-117: _PRICE_RE are 2 grupuri (prefix/sufix-valută) → finditer + group, nu findall (tuple).
    priced = {
        parse_amount(m.group(1) or m.group(2)) for m in _PRICE_RE.finditer(reply)
    }  # prețurile deja validate în _prices_ok
    allowed = _allowed_numbers(products, grounded_prices)
    names = _grounded_names(products)
    bad: list[float] = []
    for match in _BARE_NUM_RE.finditer(reply):
        value = parse_amount(match.group(1))
        if any(abs(value - p) <= 0.5 for p in priced):  # „89 lei" → numărul 89 e deja acoperit
            continue
        if value in _SAFE_BARE:
            continue
        if any(abs(value - a) <= 0.5 for a in allowed):
            continue
        if names and _quotes_a_name(reply, match, names):
            continue
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
    with turn_latency.span("validation"):  # NX-241: faza de validare, măsurată acolo unde se face
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
