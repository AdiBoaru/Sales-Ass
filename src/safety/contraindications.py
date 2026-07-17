"""NX-173 (P0) — contraindicații DETERMINISTE: un produs marcat incompatibil cu contextul declarat
de client (sarcină / alăptare) nu ajunge în retrieval, reply, carduri, `displayed_products` sau în
pool-ul sesiunii de căutare.

De ce există separat de [reason_codes.not_recommended_gate](../tools/reason_codes.py): acela exclude
DOAR pe baza `attributes.not_recommended_for` ȘI doar dacă valoarea coincide cu un **concern
cerut**. Pe catalogul real 0/654 produse au câmpul populat, iar `pregnancy` nu e concern canonic →
gate-ul e inert exact pe scenariul P0. Aici gate-ul e union de DOUĂ semnale deterministe:

  1. `not_recommended_for` `level='hard'` + provenance (calea NX-170, când datele există);
  2. regula de INGREDIENT din registrul curat `db/seed/safety_rules.json` peste
     `key_ingredients` / INCI / numele produsului — calea care ține pe date incomplete.

INVARIANTE:
  - ZERO LLM. Nici detecția contextului, nici decizia de excludere nu trec prin model. O inferență
    de model nu devine niciodată contraindicație (P2 — LLM doar la triaj + agent).
  - Registrul e DATE cu provenance (`source`/`source_ref`/`verified_at`), revizuite de om.
  - NU producem sfat medical: `advice_ro` declină recomandarea și trimite la medic/farmacist.
  - NU declarăm sigur ce a rămas — necunoscutul rămâne necunoscut (`unverifiable`), cu notă.
  - Fail-safe: la orice ambiguitate excludem (a nu recomanda e ieftin; a recomanda greșit, nu).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_RULES_PATH = Path(__file__).resolve().parents[2] / "db" / "seed" / "safety_rules.json"

# Câmpurile de produs scanate de regula de ingredient. `name` contează: pe catalogul real numele
# poartă ingredientul („Auralis Retinol Ser de noapte") chiar când `key_ingredients` lipsește (178
# produse fără el) → semnal REAL, nu inferență.
_INGREDIENT_FIELDS = ("key_ingredients", "ingredients", "ingredients_db")


def _norm(s: Any) -> str:
    """lower + fără diacritice (paritate cu `reason_codes._norm` și cu normalizarea SQL)."""
    d = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


@dataclass(frozen=True)
class SafetyContext:
    id: str
    label_ro: str
    patterns: tuple[str, ...]
    advice_ro: str


@dataclass(frozen=True)
class SafetyRule:
    id: str
    contexts: frozenset[str]
    level: str
    prefixes: tuple[str, ...]
    reason_ro: str
    source: str
    source_ref: str
    verified_at: str


@dataclass(frozen=True)
class Block:
    """Un produs respins + DE CE (grounded: regula + provenance). Fără PII."""

    product_id: str
    context_id: str
    rule_id: str
    reason_ro: str
    matched: str


@dataclass(frozen=True)
class Registry:
    contexts: tuple[SafetyContext, ...]
    rules: tuple[SafetyRule, ...]

    def context(self, cid: str) -> SafetyContext | None:
        return next((c for c in self.contexts if c.id == cid), None)


def _parse(raw: dict[str, Any]) -> Registry:
    contexts = tuple(
        SafetyContext(
            id=str(c["id"]),
            label_ro=str(c.get("label_ro") or c["id"]),
            patterns=tuple(_norm(p) for p in c.get("context_patterns") or []),
            advice_ro=str(c.get("advice_ro") or ""),
        )
        for c in raw.get("contexts") or []
    )
    rules = tuple(
        SafetyRule(
            id=str(r["id"]),
            contexts=frozenset(str(x) for x in r.get("contexts") or []),
            level=str(r.get("level") or "hard"),
            prefixes=tuple(_norm(p) for p in r.get("match_ingredient_prefixes") or []),
            reason_ro=str(r.get("reason_ro") or ""),
            source=str(r.get("source") or ""),
            source_ref=str(r.get("source_ref") or ""),
            verified_at=str(r.get("verified_at") or ""),
        )
        for r in raw.get("rules") or []
    )
    return Registry(contexts=contexts, rules=rules)


@lru_cache(maxsize=1)
def load_registry() -> Registry:
    """Registrul curat, citit o dată. Fișier lipsă/corupt → registru GOL + log de eroare: gate-ul
    devine inert, dar pipeline-ul nu cade (P6). Absența datelor nu are voie să rupă turul."""
    try:
        return _parse(json.loads(_RULES_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        log.exception(
            "safety: registru de contraindicații necitibil (%s) — gate INERT", _RULES_PATH
        )
        return Registry(contexts=(), rules=())


@lru_cache(maxsize=512)
def _pattern_re(pattern: str) -> re.Pattern[str]:
    """Prefix pe graniță de cuvânt: `retinol` prinde „retinol"/„retinoloid"; `insarcinat` prinde
    „însărcinată"/„însărcinat". NU prinde „bakuchiol" pe `retinol` (nu e prefix de cuvânt)."""
    return re.compile(r"\b" + re.escape(pattern), re.IGNORECASE)


def _hit(haystack: str, prefixes: tuple[str, ...]) -> str | None:
    for p in prefixes:
        if p and _pattern_re(p).search(haystack):
            return p
    return None


def detect_contexts(text: str | None) -> frozenset[str]:
    """Contextele de siguranță declarate într-un text. Determinist, fără LLM.

    Fail-safe by design: nu facem analiză de negație („nu sunt însărcinată" declanșează) și nu
    dezambiguizăm „sarcină" = *task*. Ambele supra-declanșează → excludem retinoizi dintr-o
    căutare de beauty. Costul e o recomandare pierdută; alternativa e un produs contraindicat
    afișat. (Vezi Riscuri în tasks/NX-173.md.)"""
    hay = _norm(text)
    if not hay:
        return frozenset()
    return frozenset(c.id for c in load_registry().contexts if _hit(hay, c.patterns))


def contexts_for_turn(ctx: Any) -> frozenset[str]:
    """Contextele active în tur: mesajul CURENT + istoricul bugetat (doar mesajele CLIENTULUI —
    ce a scris botul nu declară nimic despre client). Acoperă multi-turul „sunt însărcinată" (t1)
    → „arată-mi un ser antirid" (t2) fără state nou.

    Limită cunoscută: `history` e plafonat la 8 (P4) → o declarație mai veche se pierde.
    Persistarea în profil = follow-up (NX-173 Riscuri)."""
    found: set[str] = set(detect_contexts(getattr(getattr(ctx, "message", None), "body", None)))
    for m in getattr(ctx, "history", None) or []:
        if getattr(m, "direction", None) == "inbound":
            found |= detect_contexts(getattr(m, "body", None))
    return frozenset(found)


def _attrs(product: dict[str, Any]) -> dict[str, Any]:
    a = product.get("attributes")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except (ValueError, TypeError):
            a = {}
    return a if isinstance(a, dict) else {}


def _ingredient_text(product: dict[str, Any]) -> str:
    """Textul scanat de regula de ingredient: numele + toate listele de ingrediente cunoscute."""
    a = _attrs(product)
    parts: list[str] = [_norm(product.get("name"))]
    for f in _INGREDIENT_FIELDS:
        v = product.get(f) if product.get(f) is not None else a.get(f)
        if isinstance(v, str):
            parts.append(_norm(v))
        elif isinstance(v, list):
            parts.extend(_norm(x) for x in v)
    return " | ".join(p for p in parts if p)


def _declared_block(
    product: dict[str, Any], contexts: frozenset[str], reg: Registry
) -> tuple[str, str] | None:
    """Calea NX-170 (date): `not_recommended_for` `hard` + provenance pe un context ACTIV.
    Spre deosebire de `reason_codes.not_recommended_gate`, NU cere ca valoarea să fie un
    *concern cerut* — contextul de siguranță nu e un filtru de căutare."""
    for nrf in _attrs(product).get("not_recommended_for") or []:
        if not isinstance(nrf, dict):
            continue
        val = _norm(nrf.get("value"))
        if not val or val not in {_norm(c) for c in contexts}:
            continue
        if nrf.get("level") == "hard" and nrf.get("source") and nrf.get("verified_at"):
            cid = next((c for c in contexts if _norm(c) == val), val)
            reason = str(nrf.get("reason") or "").strip() or (
                f"marcat de furnizor ca nerecomandat pentru {nrf.get('value')}"
            )
            return cid, reason
    return None


def check_product(product: dict[str, Any], contexts: frozenset[str]) -> Block | None:
    """`Block` dacă produsul e contraindicat pentru vreun context activ, altfel None. Pur."""
    if not contexts:
        return None
    reg = load_registry()
    pid = str(product.get("id") or product.get("product_id") or "")
    declared = _declared_block(product, contexts, reg)
    if declared:
        cid, reason = declared
        return Block(
            product_id=pid,
            context_id=cid,
            rule_id="not_recommended_for",
            reason_ro=reason,
            matched="declared",
        )
    hay = _ingredient_text(product)
    for rule in reg.rules:
        if rule.level != "hard" or not (rule.contexts & contexts):
            continue
        m = _hit(hay, rule.prefixes)
        if m:
            cid = next(iter(sorted(rule.contexts & contexts)))
            return Block(
                product_id=pid,
                context_id=cid,
                rule_id=rule.id,
                reason_ro=rule.reason_ro,
                matched=m,
            )
    return None


def filter_products(
    products: list[dict[str, Any]], contexts: frozenset[str]
) -> tuple[list[dict[str, Any]], list[Block]]:
    """`(păstrate, blocate)`. Ordinea păstratelor rămâne (ranking-ul nu se rescrie)."""
    if not contexts or not products:
        return products, []
    kept: list[dict[str, Any]] = []
    blocked: list[Block] = []
    for p in products:
        b = check_product(p, contexts)
        if b is None:
            kept.append(p)
        else:
            blocked.append(b)
    return kept, blocked


def has_verifiable_ingredients(product: dict[str, Any]) -> bool:
    """Avem din ce judeca produsul? Fals ⇒ nu-l putem declara compatibil (nu-l blocăm, dar nici nu
    lăsăm botul să afirme că e potrivit)."""
    a = _attrs(product)
    return any((product.get(f) or a.get(f)) for f in _INGREDIENT_FIELDS)


def safety_note(
    contexts: frozenset[str], products: list[dict[str, Any]], blocked: list[Block]
) -> str | None:
    """Nota DETERMINISTĂ pusă în `llm_view` când un context de siguranță e activ.

    Face trei lucruri, toate necesare ca P0 să nu depindă de bunăvoința modelului:
      1. spune ce s-a exclus și de ce (grounded pe registru — agentul poate fi sincer, P6);
      2. INTERZICE claim-ul de siguranță pe ce a rămas (nu declarăm sigur ce n-am verificat);
      3. cere trimiterea la medic/farmacist — declinare, NU sfat medical inventat.
    """
    if not contexts:
        return None
    reg = load_registry()
    known = [c for c in (reg.context(cid) for cid in sorted(contexts)) if c is not None]
    if not known:
        return None
    labels = ", ".join(c.label_ro for c in known)
    lines = [f"(CONTEXT DE SIGURANȚĂ declarat de client: {labels}.)"]
    if blocked:
        why = "; ".join(sorted({b.reason_ro for b in blocked if b.reason_ro}))
        lines.append(
            f"(am EXCLUS determinist {len(blocked)} produs(e) din listă: {why}. "
            f"Poți spune sincer că le-am lăsat deoparte, dar NU le numi și NU le descrie.)"
        )
    unverifiable = [p for p in products if not has_verifiable_ingredients(p)]
    if unverifiable:
        lines.append(
            "(pentru unele produse rămase nu avem lista de ingrediente în catalog → NU afirma că "
            "sunt potrivite/sigure în acest context.)"
        )
    lines.append(
        "(REGULI DURE: nu da sfat medical, nu afirma că un produs e sigur sau periculos, nu "
        "diagnostica. Spune că decizia o ia medicul/farmacistul. "
        + " ".join(c.advice_ro for c in known if c.advice_ro)
        + ")"
    )
    return "\n".join(lines)
