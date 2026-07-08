"""DomainPack — contractul de configurare per-(business, vertical) (NX-114).

Mută politica/taxonomia per-vertical din COD în DB+seed (principiul 9): un vertical nou =
config (`src/domain/defaults/<vertical>.json` + override în `businesses.settings["domain_pack"]`),
NU deploy. `DomainPack` e PUR de date — nicio mapare hardcodată aici; loader-ul
(`src/domain/loader.py`) îl construiește din JSON-seed + override per-tenant, normalizat o
singură dată la încărcare (lookup-uri O(1) downstream).

Acesta e SKELETON-ul + contractul. Consumatorii hardcodați (taxonomy.py, gates.py RISK_PATTERNS,
greeting.py, profile.py) NU sunt migrați încă — wiring-ul lor e follow-up per-feature (NX-124 etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FacetSpec:
    """O fațetă de DOMENIU surfacing-uită în comparație (Tier 2, IZI-parity). GENERIC: `key` =
    cheia din `products.attributes` (ex. „concerns", „finish", „material", „spf"); `labels` =
    eticheta rândului per-locale; `value_labels` = traduceri OPȚIONALE cod→locale pentru valori
    canonice (ex. „oily"→„ten gras"). Valoare fără traducere → afișată ca atare (atribut deja
    display-ready). Zero hardcodat de vertical — totul din DomainPack (defaults JSON + override)."""

    key: str
    labels: dict[str, str] = field(default_factory=dict)  # locale → eticheta rândului
    value_labels: dict[str, dict[str, str]] = field(default_factory=dict)  # cod → locale → text


@dataclass(frozen=True)
class DomainPack:
    """Config per-(business, vertical). Owner: `load_domain_pack` (atașat pe BusinessConfig).
    Toate câmpurile au default-uri agnostice de vertical (P6 — un pack incomplet nu crapă)."""

    vertical: str  # verticalul tenantului (ecommerce | beauty_salon | auto_service | other | ...)
    # termen liber NORMALIZAT → cheia canonică din products.attributes->'concerns' (ex. "oily").
    concern_map: dict[str, str] = field(default_factory=dict)
    # locale → {reason: [phrase_norm,...]} — termeni de risc/legal keyed pe locale (P11).
    risk_terms: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # locale → saluturi adiționale NORMALIZATE (peste baza din greeting.py) (P11).
    greetings: dict[str, list[str]] = field(default_factory=dict)
    # NX-121: locale → pattern-uri NORMALIZATE de prompt-injection (peste baza neutră din cod, P9).
    # Ecranul e DETECTARE/observabilitate; apărarea reală = validatorul de stagiul 8.
    injection_patterns: dict[str, list[str]] = field(default_factory=dict)
    # chei permise în contacts.profile (peste minimul agnostic). NICIODATĂ PII (telefon/email/nume).
    profile_whitelist: frozenset[str] = frozenset()
    # NX-148: tipuri permise de conversation_facts per vertical. Extractorul aruncă tipurile
    # din afara listei (plasă anti-halucinație de memorie). NICIODATĂ PII (P12).
    fact_type_whitelist: frozenset[str] = frozenset()
    # statusuri „finalizat" pt check_order (ex. delivered/closed) — neutre pe vertical.
    settled_order_statuses: tuple[str, ...] = ()
    currency: str = "RON"  # moneda afișată (din businesses.settings["currency"], fallback RON)
    # IZI: praguri pt badge-ul DERIVAT de card (top_rating/top_reviews/deal_discount_pct). Gol →
    # default-uri agnostice de vertical din `src/worker/badges.py`. Override per-tenant în settings.
    badge_rules: dict[str, float] = field(default_factory=dict)
    # ARCH-2026 P0: ponderile scorului de ranking blended (relevance/rating/availability/sale/
    # concern). Gol → default-uri agnostice de vertical din `fusion.py` (`RANK_WEIGHTS`). Override
    # (parțial) per-tenant în settings → merge peste default-uri (un vertical „deal-driven" poate
    # urca `sale`, unul „premium" poate urca `rating`). Ne-hardcodat (P9).
    rank_weights: dict[str, float] = field(default_factory=dict)
    # Tier 2 (IZI-parity): fațete de DOMENIU pentru tabelul de comparație (rânduri finish/acoperire/
    # potrivit-pentru/material/..., din `products.attributes`). Ordinea = ordinea de afișare. Gol →
    # tabelul are doar rândurile generice (preț/rating/avantaje/brand) ca azi. Auto-scalează: când
    # `attributes` crește, rândurile apar fără schimbare de cod. Per-vertical (defaults JSON).
    comparison_facets: tuple[FacetSpec, ...] = ()
    # Tier 2b p2: cheile din `attributes` (ARRAY) pe care le poate FILTRA search-ul de feature
    # („ceva cu niacinamidă" → key_ingredients). Match NORMALIZAT (lower + strip diacritice). Gol →
    # fără filtru de feature. Separat de concern_map (concerns are calea lor de mapare).
    searchable_facets: tuple[str, ...] = ()
