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
    # statusuri „finalizat" pt check_order (ex. delivered/closed) — neutre pe vertical.
    settled_order_statuses: tuple[str, ...] = ()
    currency: str = "RON"  # moneda afișată (din businesses.settings["currency"], fallback RON)
    # IZI: praguri pt badge-ul DERIVAT de card (top_rating/top_reviews/deal_discount_pct). Gol →
    # default-uri agnostice de vertical din `src/worker/badges.py`. Override per-tenant în settings.
    badge_rules: dict[str, float] = field(default_factory=dict)
