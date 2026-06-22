"""Taxonomie concern→cheie de filtru (NX-72, NX-124) — clasificator DETERMINIST, ZERO LLM (P2).

Normalizează termenul liber al clientului („ten gras", „piele sensibilă") la cheia REALĂ din
`products.attributes->'concerns'` (ex. „oily"). Fără ea, modelul ar trimite `concerns=["ten gras"]`
iar operatorul jsonb `?|` n-ar prinde nimic (în DB e „oily").

NX-124: maparea vine acum din **DomainPack** (`concern_map`, config DB per-(business,vertical) —
principiul 9), NU hardcodat pe beauty. Orice vertical cu `concern_map` seedat (HVAC „zgomotos"→
„low_noise", auto etc.) mapează corect, fără deploy. `_BEAUTY_RAW`/`_BEAUTY` rămân DOAR ca referință
a seed-ului demo (`src/domain/defaults/beauty_salon.json`) pentru parity-guard-ul din
test_domain_pack — NU mai sunt sursa de mapare la runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# `_norm` = helper partajat (lower + strip diacritice + trim) — aceeași normalizare ca DomainPack.
from src.domain.normalize import normalize as _norm

if TYPE_CHECKING:
    from src.domain.pack import DomainPack

# Referință seed beauty (= conținutul `concern_map` din defaults/beauty_salon.json). NU sursa de
# mapare la runtime (aceea e DomainPack din DB); păstrat pentru parity-guard seed↔cod
# (test_domain_pack) și ca documentare a mapării demo. Cheile se normalizează la încărcare.
_BEAUTY_RAW: dict[str, str] = {
    "ten gras": "oily",
    "piele grasă": "oily",
    "oily": "oily",
    "ten uscat": "dry",
    "piele uscată": "dry",
    "dry": "dry",
    "ten sensibil": "sensitive",
    "piele sensibilă": "sensitive",
    "sensitive": "sensitive",
    "ten mixt": "combination",
    "piele mixtă": "combination",
    "combination": "combination",
    "acnee": "acne",
    "cosuri": "acne",
    "coșuri": "acne",
    "acne": "acne",
    "riduri": "anti_aging",
    "anti-îmbătrânire": "anti_aging",
    "antiîmbătrânire": "anti_aging",
    "anti-aging": "anti_aging",
    "pete": "hyperpigmentation",
    "pete pigmentare": "hyperpigmentation",
    "hyperpigmentation": "hyperpigmentation",
    "hidratare": "hydration",
    "deshidratat": "hydration",
    "hydration": "hydration",
}

_BEAUTY: dict[str, str] = {_norm(k): v for k, v in _BEAUTY_RAW.items()}


def map_concerns(domain_pack: DomainPack | None, raw: list[str] | None) -> list[str]:
    """Termeni liberi → chei canonice din `attributes->'concerns'`, prin `domain_pack.concern_map`
    (config DB per-vertical, NX-124).

    Necunoscutele se IGNORĂ (nu inventăm un filtru fals care ar goli rezultatul — mai bine zero
    filtru decât unul greșit, P6 indirect). DomainPack lipsă / `concern_map` gol → `[]` (fără crash,
    fără filtru). Întoarce chei unice, ordine stabilă (determinist → testabil + cache-friendly).
    """
    if not raw:
        return []
    table = domain_pack.concern_map if domain_pack else {}
    if not table:
        return []
    out = [table[_norm(c)] for c in raw if _norm(c) in table]
    return sorted(dict.fromkeys(out))
