"""Helpere PII-safe pentru captura de cerere (NX-163, Demand Capture).

Determinist, fără LLM, fără inferență (P2). Extrag DOAR referințe (`product_id`) din
structurile pe care pipeline-ul le are deja la sursă — niciodată text brut de user / PII
(P12). Atributele normalizate (`category_key`, `brand`) se pasează direct din args-ul
tool-ului la `ctx.emit`; aici trăiește doar logica de extras + capat id-uri, ca să nu se
repete în cei 4 emițători (product_search, unmet_query, agent_recommended, cart_updated,
checkout_link_created).

NU normalizează atribute noi (v1): `price_band`/`concerns` intră doar dacă tool-ul le dă
deja normalizate — vezi tasks/NX-163.md, Scope v1.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

# Cap dur pe listele de id-uri scrise în `analytics_events.properties`: payload mic (ref-uri, P8),
# agregare ieftină în rapoartele de cerere (NX-164). Top-N e suficient pentru „ce s-a cerut".
DEMAND_IDS_CAP = 8


def clean_ids(ids: Iterable[Any], *, cap: int = DEMAND_IDS_CAP) -> list[str]:
    """Normalizează o listă de id-uri deja extrase → `str`, fără `None`, capată la `cap`.
    Nu inventează: intrările `None`/goale se sar. Ordinea de intrare = ordinea de ieșire."""
    out: list[str] = []
    for i in ids:
        if i is None:
            continue
        s = str(i)
        if not s:
            continue
        out.append(s)
        if len(out) >= cap:
            break
    return out


def product_ids_from_dicts(
    products: Sequence[dict[str, Any]], *, cap: int = DEMAND_IDS_CAP
) -> list[str]:
    """Id-urile dintr-o listă de dict-uri de produs (`product_id` sau, fallback, `id`).
    Doar ref-uri (P8), capate. Sare peste intrările fără id — nu fabrică valori."""
    return clean_ids((p.get("product_id") or p.get("id") for p in products), cap=cap)
