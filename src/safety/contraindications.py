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


class RegistryError(RuntimeError):
    """Registrul e invalid. NU se înghite: un registru stricat = protecție absentă (vezi
    `load_registry` — fail-CLOSED)."""


@dataclass(frozen=True)
class SafetyContext:
    id: str
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class SafetyRule:
    id: str
    contexts: frozenset[str]
    level: str
    prefixes: tuple[str, ...]
    source: str
    source_ref: str
    verified_at: str
    reviewed_by: str  # IMPUS: o regulă ne-revizuită de om nu se încarcă (vezi `_parse`)


@dataclass(frozen=True)
class Block:
    """Un produs respins + DE CE, ca REF-URI (rule_id/context_id), nu ca text. Copy-ul de client se
    randează din chei în `messages.py` — stratul de decizie nu poartă limbă. Fără PII."""

    product_id: str
    context_id: str
    rule_id: str
    matched: str


@dataclass(frozen=True)
class Registry:
    contexts: tuple[SafetyContext, ...]
    rules: tuple[SafetyRule, ...]

    def context(self, cid: str) -> SafetyContext | None:
        return next((c for c in self.contexts if c.id == cid), None)


# Aprobările umane acceptate: orice ALTCEVA decât un marcaj de „încă nerevizuit". O regulă cu
# `PENDING_HUMAN_REVIEW` NU se încarcă → nu poate fi aplicată tăcut în producție (review Codex P1).
_UNREVIEWED = {"", "pending_human_review", "todo", "tbd", "none", "null"}
_VALID_LEVELS = {"hard", "soft"}


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RegistryError(msg)


def _parse(raw: dict[str, Any]) -> Registry:
    """Validare STRICTĂ. Orice abatere ridică `RegistryError` — nu „sare regula" tăcut: un registru
    parțial încărcat e mai periculos decât unul respins (crezi că ai protecție și n-o ai)."""
    _require(isinstance(raw, dict), "registrul nu e un obiect JSON")
    contexts: list[SafetyContext] = []
    seen_ctx: set[str] = set()
    for c in raw.get("contexts") or []:
        _require(isinstance(c, dict) and bool(c.get("id")), "context fără id")
        cid = str(c["id"])
        _require(cid not in seen_ctx, f"context duplicat: {cid}")
        seen_ctx.add(cid)
        pats = tuple(_norm(p) for p in c.get("context_patterns") or [] if str(p).strip())
        _require(bool(pats), f"context fără `context_patterns`: {cid}")
        contexts.append(SafetyContext(id=cid, patterns=pats))
    _require(bool(contexts), "registru fără contexte")

    rules: list[SafetyRule] = []
    seen_rule: set[str] = set()
    for r in raw.get("rules") or []:
        _require(isinstance(r, dict) and bool(r.get("id")), "regulă fără id")
        rid = str(r["id"])
        _require(rid not in seen_rule, f"regulă duplicată: {rid}")
        seen_rule.add(rid)
        rctx = frozenset(str(x) for x in r.get("contexts") or [])
        _require(bool(rctx), f"regula {rid}: fără contexte")
        unknown = rctx - seen_ctx
        _require(not unknown, f"regula {rid}: contexte inexistente {sorted(unknown)}")
        level = str(r.get("level") or "")
        _require(level in _VALID_LEVELS, f"regula {rid}: level invalid {level!r}")
        prefixes = tuple(
            _norm(p) for p in r.get("match_ingredient_prefixes") or [] if str(p).strip()
        )
        _require(bool(prefixes), f"regula {rid}: fără matcheri")
        # Provenance COMPLET pe orice regulă `hard` (contract v3 / NX-168d R8): o excludere dură
        # fără sursă verificabilă e exact „inferența devenită contraindicație" pe care o interzicem.
        for f in ("source", "source_ref", "verified_at"):
            _require(bool(str(r.get(f) or "").strip()), f"regula {rid}: `{f}` lipsă (provenance)")
        reviewed = str(r.get("reviewed_by") or "").strip()
        _require(
            reviewed.lower() not in _UNREVIEWED,
            f"regula {rid}: `reviewed_by`={reviewed!r} — regulă NEREVIZUITĂ de om, nu se încarcă",
        )
        rules.append(
            SafetyRule(
                id=rid,
                contexts=rctx,
                level=level,
                prefixes=prefixes,
                source=str(r["source"]),
                source_ref=str(r["source_ref"]),
                verified_at=str(r["verified_at"]),
                reviewed_by=reviewed,
            )
        )
    _require(bool(rules), "registru fără reguli")
    return Registry(contexts=tuple(contexts), rules=tuple(rules))


@lru_cache(maxsize=1)
def load_registry() -> Registry:
    """Registrul curat, citit + VALIDAT o dată.

    FAIL-CLOSED (review Codex P0): fișier lipsă/corupt/invalid → ridică `RegistryError`. Varianta
    veche întorcea registru gol „ca să nu cadă pipeline-ul" — dar asta înseamnă exact **catalog
    nefiltrat servit ca și cum ar fi în siguranță**, adică modul de eșec pe care îl reparăm. Pentru
    un gate P0, absența protecției trebuie să fie ZGOMOTOASĂ, nu tăcută.

    Cine cheamă decide degradarea (`SafetyPolicy.for_turn` → `unavailable` → blochează tot
    catalogul pe un tur cu context de siguranță; turul FĂRĂ context nu e afectat)."""
    try:
        raw = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    except OSError as e:
        raise RegistryError(f"registru necitibil ({_RULES_PATH}): {e}") from e
    except ValueError as e:
        raise RegistryError(f"registru JSON invalid ({_RULES_PATH}): {e}") from e
    return _parse(raw)


def registry_healthy() -> tuple[bool, str]:
    """`(ok, motiv)` — poartă de BOOT (verificată la pornirea workerului/serverului) și sondă de
    test. Nu ridică: cine cheamă alege ce face cu un registru stricat."""
    try:
        reg = load_registry()
    except RegistryError as e:
        return False, str(e)
    return True, f"{len(reg.rules)} regul(i), {len(reg.contexts)} context(e)"


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


def detect_contexts_in_turn(ctx: Any) -> frozenset[str]:
    """Contextele DECLARATE în acest tur: mesajul curent + istoricul bugetat (doar mesajele
    CLIENTULUI — ce a scris botul nu declară nimic despre client).

    Sursa de adevăr pentru contextul ACTIV nu e asta, ci `state.safety` (persistat) — vezi
    `SafetyPolicy.for_turn`. Istoricul (8 mesaje, P4) e prea scurt ca invariant de producție:
    o declarație de la turul 9 dispare. Aici doar DETECTĂM ce s-a declarat acum."""
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


def _declared_block(product: dict[str, Any], contexts: frozenset[str]) -> str | None:
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
            return cid
    return None


def check_product(product: dict[str, Any], contexts: frozenset[str]) -> Block | None:
    """`Block` dacă produsul e contraindicat pentru vreun context activ, altfel None. Pur."""
    if not contexts:
        return None
    reg = load_registry()
    pid = str(product.get("id") or product.get("product_id") or "")
    declared = _declared_block(product, contexts)
    if declared:
        return Block(
            product_id=pid,
            context_id=declared,
            rule_id="not_recommended_for",
            matched="declared",
        )
    hay = _ingredient_text(product)
    for rule in reg.rules:
        if rule.level != "hard" or not (rule.contexts & contexts):
            continue
        m = _hit(hay, rule.prefixes)
        if m:
            cid = next(iter(sorted(rule.contexts & contexts)))
            return Block(product_id=pid, context_id=cid, rule_id=rule.id, matched=m)
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
