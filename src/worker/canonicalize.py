"""NX-160 felia 4 — canonicalizer determinist (raw_key → canonical_key).

Al treilea pas din pipeline-ul de memorie (`capture broad → classify safety → CANONICALIZE →
inject safe`). Modelul extrage cheia LIBER (`raw_key`: `preferred_brand`, `budget_max_lei`,
`vehicle_make_model`); codul o mapează pe un slot CANONIC stabil pe care search/profile/analytics
se pot baza. PUR (fără DB/LLM) — testabil izolat.

Vocabularul canonic = **nucleu universal** (valabil pe orice comerț) + cheile din DomainPack-ul
businessului (`fact_type_whitelist` / `profile_whitelist` / `searchable_facets`) — P9: derivat din
config, nu hardcodat de vertical. Fără match sigur → `None` (rămâne raw candidate; codul nu-l
folosește, dar dacă e safe poate fi injectat cu `raw_key` formatat prezentabil de `facts_block`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.pack import DomainPack

# Nucleu universal de chei canonice — valabile pe ORICE business (magazin, service, salon,
# restaurant, servicii). Injectate în promptul extractorului (P9) ca modelul să aterizeze pe ele.
UNIVERSAL_CANONICAL: frozenset[str] = frozenset(
    {
        "budget_band",
        "fav_brands",
        "restriction",
        "size",
        "use_case",
        "recipient",
        "style_pref",
        "preferred_time",
    }
)

# Alias map universal: raw_key NORMALIZAT → cheie canonică. Sinonimele frecvente pe care le emite
# modelul liber. Mapările per-vertical vin din DomainPack (identity pe cheile lui) — vezi
# `resolve_canonical`. Extins aditiv; un sinonim nou = o linie aici, nu cod de vertical.
_UNIVERSAL_ALIASES: dict[str, str] = {
    # brand
    "preferred_brand": "fav_brands",
    "brand_preference": "fav_brands",
    "fav_brand": "fav_brands",
    "favourite_brands": "fav_brands",
    "favorite_brands": "fav_brands",
    # buget
    "budget": "budget_band",
    "budget_max": "budget_band",
    "budget_max_lei": "budget_band",
    "max_budget": "budget_band",
    "price_limit": "budget_band",
    "price_range": "budget_band",
    # restricții / preferințe de excludere
    "fragrance_free_preference": "restriction",
    "avoid_fragrance": "restriction",
    "diet_preference": "restriction",
    "dietary_restriction": "restriction",
    "allergen_free": "restriction",
    "avoid": "restriction",
    # timp / programare
    "appointment_time_preference": "preferred_time",
    "preferred_time_slot": "preferred_time",
    "time_preference": "preferred_time",
    "service_time_preference": "preferred_time",
    # stil
    "style_preference": "style_pref",
    "preferred_style": "style_pref",
    # scop / destinatar
    "purpose": "use_case",
    "occasion": "use_case",
    "gift_recipient": "recipient",
    "buying_for": "recipient",
    # auto (vertical frecvent) — canonice proprii, nu în nucleu
    "vehicle_make_model": "vehicle_model",
    "car_model": "vehicle_model",
    "part_needed": "part_category",
    "part": "part_category",
}


def _norm_key(key: str) -> str:
    """Normalizează o cheie: lower + strip + spații/cratime → underscore. Match O(1) robust
    peste variații de formatare emise de model (`Preferred Brand` → `preferred_brand`)."""
    return "_".join((key or "").strip().lower().replace("-", " ").split())


def canonical_keys_for(pack: DomainPack | None) -> list[str]:
    """Cheile canonice DISPONIBILE pentru un business = nucleu universal + cheile DomainPack.
    Injectate în promptul extractorului (P9) ca modelul să prefere vocabular canonic. Ordonate
    (nucleul întâi) + dedupe, deterministe pentru prompt caching byte-stabil."""
    keys: list[str] = list(UNIVERSAL_CANONICAL)
    if pack is not None:
        for extra in (pack.fact_type_whitelist, pack.profile_whitelist, pack.searchable_facets):
            keys.extend(extra)
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        nk = _norm_key(k)
        if nk and nk not in seen:
            seen.add(nk)
            out.append(nk)
    return sorted(out)


def resolve_canonical(raw_key: str, pack: DomainPack | None) -> str | None:
    """Rezolvă `raw_key` → cheie canonică, sau `None` dacă nu mapăm SIGUR. Ordine:
      1. raw_key e DEJA canonic (nucleu sau DomainPack) → identitate;
      2. alias universal cunoscut → ținta lui;
      3. altfel → None (rămâne raw candidate).
    Determinist, fără ghicit fuzzy (un match greșit ar polua căutarea/analytics)."""
    nk = _norm_key(raw_key)
    if not nk:
        return None
    if nk in UNIVERSAL_CANONICAL:
        return nk
    if pack is not None:
        for group in (pack.fact_type_whitelist, pack.profile_whitelist, pack.searchable_facets):
            if any(_norm_key(k) == nk for k in group):
                return nk
    return _UNIVERSAL_ALIASES.get(nk)


def memory_key(raw_key: str, canonical_key: str | None) -> str:
    """Cheia de deduplicare per (business, contact). `canonical:<k>` dacă avem canonical (două
    raw_key sinonime converg pe același rând), altfel `raw:<raw_key>` (fapte libere distincte nu se
    ciocnesc). Sursa unique-ului din 024 — un fact activ per memory_key/contact."""
    if canonical_key:
        return f"canonical:{_norm_key(canonical_key)}"
    return f"raw:{_norm_key(raw_key)}"
