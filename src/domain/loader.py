"""Loader DomainPack (NX-114) — PUR, fără I/O DB.

`load_domain_pack(business)` citește default-ul JSON per-vertical (cache-uit la boot),
face deep-merge cu `business.settings["domain_pack"]` (override per-tenant câștigă),
normalizează toate cheile/frazele și întoarce un `DomainPack` frozen. Primește un
`BusinessConfig` deja încărcat (tenant-scoped) → zero atingere de DB aici (P7).

Fail-safe (P6): kill-switch OFF / JSON lipsă / override de tip greșit → pack gol sau None,
NICIODATĂ crash. Consumatorii hardcodați (taxonomy/gates/greeting/profile) cad pe constantele
lor de cod până la migrarea per-feature (NX-124 etc.).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import get_settings
from src.domain.normalize import normalize
from src.domain.pack import DomainPack

if TYPE_CHECKING:
    from src.models import BusinessConfig

log = logging.getLogger(__name__)

_DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"

# businesses.vertical (live) → fișierul de default canonic. Necunoscut / lipsă → "other".
_VERTICAL_TO_DEFAULT = {
    "ecommerce": "ecommerce",
    "beauty": "beauty_salon",
    "beauty_salon": "beauty_salon",
    "salon": "beauty_salon",
    "auto": "auto_service",
    "auto_service": "auto_service",
}


@lru_cache(maxsize=16)
def _load_default_json(name: str) -> dict[str, Any]:
    """Citește src/domain/defaults/<name>.json o singură dată (cache-uit pe boot). Fișier
    lipsă/corupt → {} (fail-safe). Întoarce dict NEMODIFICAT — apelanții nu îl mutează."""
    path = _DEFAULTS_DIR / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("DomainPack default %s ilizibil (%s) — pack gol", path.name, e)
        return {}


def _default_name(vertical: str) -> str:
    name = _VERTICAL_TO_DEFAULT.get((vertical or "").strip().lower(), "other")
    # dacă verticalul mapat n-are fișier, cădem pe "other".
    if not (_DEFAULTS_DIR / f"{name}.json").exists():
        return "other"
    return name


def _deep_merge(base: dict, over: dict) -> dict:
    """Merge recursiv: dict×dict se contopesc; restul (liste/scalari) — override câștigă."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _norm_concern_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {normalize(k): v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _norm_risk_terms(raw: Any) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    if not isinstance(raw, dict):
        return out
    for locale, reasons in raw.items():
        if not isinstance(reasons, dict):
            continue
        out[locale] = {
            reason: [normalize(p) for p in phrases if isinstance(p, str)]
            for reason, phrases in reasons.items()
            if isinstance(phrases, list)
        }
    return out


def _norm_greetings(raw: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for locale, greets in raw.items():
        if isinstance(greets, list):
            out[locale] = [normalize(g) for g in greets if isinstance(g, str)]
    return out


def load_domain_pack(business: BusinessConfig) -> DomainPack | None:
    """Construiește DomainPack-ul tenantului. None dacă kill-switch-ul e OFF (fail-safe:
    consumatorii cad pe constantele de cod). Owner unic = loader-ul (apelat din load_business)."""
    if not get_settings().domain_pack_enabled:
        return None
    vertical = business.vertical or "other"
    merged = dict(_load_default_json(_default_name(vertical)))
    settings = business.settings or {}
    override = settings.get("domain_pack")
    if isinstance(override, dict):
        merged = _deep_merge(merged, override)
    currency = settings.get("currency") or merged.get("currency") or "RON"
    return DomainPack(
        vertical=vertical,
        concern_map=_norm_concern_map(merged.get("concern_map")),
        risk_terms=_norm_risk_terms(merged.get("risk_terms")),
        greetings=_norm_greetings(merged.get("greetings")),
        profile_whitelist=frozenset(
            k for k in (merged.get("profile_whitelist") or []) if isinstance(k, str)
        ),
        settled_order_statuses=tuple(
            s for s in (merged.get("settled_order_statuses") or []) if isinstance(s, str)
        ),
        currency=str(currency),
    )
