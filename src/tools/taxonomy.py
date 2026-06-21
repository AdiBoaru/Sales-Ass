"""Taxonomie concern→cheie de filtru (NX-72) — clasificator DETERMINIST, ZERO LLM (P2).

Normalizează termenul liber al clientului („ten gras", „piele sensibilă") la cheia REALĂ
din `products.attributes->'concerns'` (ex. „oily", „sensitive"). Fără ea, modelul ar trimite
`concerns=["ten gras"]` iar operatorul jsonb `?|` n-ar prinde nimic (în DB e „oily").

Decizie (CLAUDE.md, principiul 9, nota): maparea stă STATIC în cod pentru `beauty` (singurul
vertical live). Un tabel `taxonomy` în DB se adaugă ADITIV doar când apar verticale multiple
(editabil din dashboard) — vezi Out of Scope în tasks/NX-72.md.
"""

from __future__ import annotations

# NX-114: `_norm` extras în helper-ul partajat src/domain/normalize.py (comportament IDENTIC —
# lower + strip diacritice + trim). Re-exportat ca alias pentru compat (DomainPack folosește
# aceeași normalizare pentru concern_map).
from src.domain.normalize import normalize as _norm

# Termeni liberi (RO + EN) → cheia canonică din attributes->'concerns'. Cheile sunt
# normalizate la încărcare, deci pot fi scrise natural (cu diacritice) aici.
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

_BY_VERTICAL: dict[str, dict[str, str]] = {"beauty": _BEAUTY}


def map_concerns(vertical: str, raw: list[str] | None) -> list[str]:
    """Termeni liberi → chei canonice din `attributes->'concerns'`.

    Necunoscutele se IGNORĂ (nu inventăm un filtru fals care ar goli rezultatul — mai bine
    zero filtru decât unul greșit, P6 indirect). Vertical necunoscut → tabel gol → `[]`.
    Întoarce chei unice, ordine stabilă (determinist → testabil + prompt-cache-friendly).
    """
    if not raw:
        return []
    table = _BY_VERTICAL.get(vertical, {})
    out = [table[_norm(c)] for c in raw if _norm(c) in table]
    return sorted(dict.fromkeys(out))
