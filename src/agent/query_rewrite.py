"""NX-208 — Înțelegerea DETERMINISTĂ a interogării (query understanding, ZERO LLM).

Transformă textul colocvial al clientului într-un `RuntimeQuerySpec`: extrage constrângeri
canonice (preț, fără parfum, concern) și construiește un `search_text` EXPANDAT cu vocabular
canonic, ca lexical (FTS `ro_unaccent`) și semantic (embeddings) să prindă query-urile pe care
textul brut le rata. Nu înlocuiește raw-ul — îl completează (D6: cele 3 reprezentări în paralel).

Exemple (din cazurile compuse ratate la baseline-ul NX-203):
  „ceva să nu mă lucesc … pe căldură"      → + matifiant, mat, ten gras, rezistent
  „vreau ceva ca <X>, dar mai accesibil"   → referință + sort preț crescător (soft)
  „fără parfum pentru toată rutina de față" → fragrance_free + curățare, ser, cremă hidratantă

Sursa vocabularului = DomainPack (`concern_map` + `query_expansions`, config per-vertical, P9) —
nimic hardcodat pe beauty. Pattern-urile de LIMBĂ (preț „sub N", referință „ca X dar mai ieftin")
sunt RO generic, nu specifice unui vertical. Determinist → testabil, prompt-cache-friendly, P2.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.domain.normalize import normalize

if TYPE_CHECKING:
    from src.domain.pack import DomainPack

# Preț plafon: „sub 120", „buget 200", „maxim 150", „cel mult 100", „până în 80".
_PRICE_RE = re.compile(
    r"\b(?:sub|pana in|maxim|max|cel mult|buget(?:ul)?(?: de)?|in jur de|aprox|circa|pana la)"
    r"\s+(\d{2,5})\b"
)
# „mai ieftin / mai accesibil / mai convenabil …" → cerere de ALTERNATIVĂ mai ieftină (sort preț).
_CHEAPER_RE = re.compile(
    r"\bmai\s+(?:ieftin\w*|accesibil\w*|convenabil\w*|avantajos\w*|acceptabil\w*|rezonabil\w*)"
)
# Referință „ca X" / „similar cu X" / „gen X" — capturează descriptorul până la virgulă/„dar/însă".
_REF_RE = re.compile(
    r"\b(?:ca|similar cu|asemanator cu|gen|de genul|precum)\s+(.+?)"
    r"(?:\s*,|\s+dar\b|\s+insa\b|\s+doar\b|\s+numai\b|$)"
)
# Fără parfum / fără miros — fațetă POZITIVĂ de selecție (D9: absență confirmată = beneficiu).
_FRAGFREE_RE = re.compile(r"\bfara\s+(?:parfum|miros|fragrant\w*|arome)\b")


def _scan_text(norm: str) -> str:
    """Normalizat → doar litere/cifre separate de spații (pentru match pe cuvânt întreg)."""
    return " " + re.sub(r"[^a-z0-9]+", " ", norm).strip() + " "


def _word_hit(scan: str, phrase_norm: str) -> bool:
    """Frază NORMALIZATĂ prezentă ca secvență de cuvinte întregi în `scan` (evită false-positive
    de tip substring: „pete" în „competente")."""
    return f" {phrase_norm} " in scan


def _extract_price_max(norm: str) -> float | None:
    m = _PRICE_RE.search(norm)
    return float(m.group(1)) if m else None


def _extract_reference(norm: str) -> str | None:
    m = _REF_RE.search(norm)
    if not m:
        return None
    ref = m.group(1).strip()
    # Ignoră capturi degenerate (prea scurte / doar stopword-uri).
    return ref if len(ref) >= 3 else None


def _detect_intent(norm: str, reference: str | None) -> str:
    if "diferenta" in norm or "dintre" in norm or " sau " in norm:
        return "compare"
    if reference is not None:
        return "find_alternative"
    return "recommend"


def _compose(base: str, extra_terms: list[str]) -> str:
    """`base` + termeni de expandare care nu sunt deja prezenți (word-level), ordine stabilă."""
    scan = _scan_text(normalize(base))
    out = base
    for term in extra_terms:
        tn = normalize(term)
        if tn and not _word_hit(scan, tn):
            out = f"{out} {term}"
            scan = _scan_text(normalize(out))
    return out


def build_query_spec(
    raw_query: str,
    domain_pack: DomainPack | None,
    *,
    locale: str = "ro",
) -> RuntimeQuerySpec:
    """Text brut → `RuntimeQuerySpec` (raw + normalized + search_text expandat + constrângeri).

    DETERMINIST, fără LLM, fără scriere. DomainPack lipsă/gol → doar pattern-urile de limbă (preț,
    referință, fără-parfum); zero expandare de vocabular (degradare grațioasă, P6)."""
    norm = normalize(raw_query)
    scan = _scan_text(norm)
    constraints: list[Constraint] = []
    reference_terms: list[str] = []
    extra_terms: list[str] = []
    sort = "relevance"

    concern_map = domain_pack.concern_map if domain_pack else {}
    expansions = domain_pack.query_expansions if domain_pack else {}

    # 1. Preț plafon (hard) — buget/negație = inviolabil de model (D7).
    price_max = _extract_price_max(norm)
    if price_max is not None:
        constraints.append(
            Constraint(facet="price", op="lte", value=price_max, strength="hard", source="derived")
        )

    # 2. Referință + „mai ieftin" → sort preț crescător (soft) + referință de exclus downstream.
    reference = _extract_reference(norm)
    base = norm
    if _CHEAPER_RE.search(norm):
        sort = "price_asc"
        if reference:
            reference_terms.append(reference)
            base = reference  # focalizează căutarea pe descriptorul referinței (nu pe schelă)

    # 3. Fără parfum → fațetă pozitivă de selecție (hard, D9), textul rămâne.
    if _FRAGFREE_RE.search(norm):
        constraints.append(
            Constraint(
                facet="fragrance_free", op="eq", value=True, strength="hard", source="derived"
            )
        )

    # 4. Concern scan (soft) — mapare colocvial → cheie canonică din attributes->'concerns'.
    for phrase_norm, canonical in concern_map.items():
        if _word_hit(scan, phrase_norm):
            constraints.append(
                Constraint(
                    facet="concern",
                    op="contains",
                    value=canonical,
                    strength="soft",
                    source="derived",
                )
            )

    # 5. Expandare de vocabular → termeni canonici de căutare (hrănesc lexical + semantic).
    for phrase_norm, terms in expansions.items():
        if _word_hit(scan, phrase_norm):
            extra_terms.extend(terms)

    # Constrângeri concern deduplicate, ordine stabilă (determinist + telemetrie curată).
    seen_concern: set[str] = set()
    deduped: list[Constraint] = []
    for c in constraints:
        key = f"{c.facet}:{c.value}"
        if c.facet == "concern":
            if key in seen_concern:
                continue
            seen_concern.add(key)
        deduped.append(c)

    search_text = _compose(base, extra_terms)

    return RuntimeQuerySpec(
        raw_query=raw_query,
        normalized_query=norm,
        search_text=search_text,
        intent=_detect_intent(norm, reference),
        category=None,  # categoria hard vine din QuerySpec-ul turului/state, nu din ghicit aici
        constraints=tuple(deduped),
        reference_terms=tuple(reference_terms),
        sort=sort,
        locale=locale,
    )
