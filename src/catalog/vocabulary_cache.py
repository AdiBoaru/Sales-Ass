"""Cache mărginit pentru vocabularul catalogului (`src/catalog/vocabulary.py`).

`load_vocabulary` numără produsele pe subarbore și scanează `attributes` — măsurat ~1s pe un
catalog de 300 de produse. Pe drumul fierbinte al unui tur asta e inacceptabil, iar rezultatul se
schimbă doar când se schimbă catalogul, adică rar și în afara conversației.

Politica e deliberat plictisitoare: TTL scurt + plafon de tenanți + evicție a celui mai vechi. Fără
invalidare „deșteaptă" pe evenimente de sync — o invalidare care ratează un caz ar reintroduce
exact desincronizarea pe care vocabularul o previne, doar cu un ceas în plus. Un TTL care expiră
sigur e mai ușor de argumentat decât o invalidare care e corectă „de obicei".

Cheia e `business_id` (P7): vocabularul unui tenant nu poate fi servit altuia.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from src.catalog.vocabulary import CatalogVocabulary, load_vocabulary

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

__all__ = ["clear_vocabulary_cache", "get_vocabulary"]

# Catalogul se schimbă la sync (rar). Cinci minute țin drumul fierbinte rapid și mărginesc
# fereastra în care un produs nou-adăugat încă nu e „adresabil" prin vocabular.
_TTL_S = 300.0
# Un worker servește puțini tenanți simultan; plafonul e o plasă contra creșterii nemărginite, nu o
# strategie de performanță.
_MAX_TENANTS = 64

_cache: dict[str, tuple[float, CatalogVocabulary]] = {}


def clear_vocabulary_cache() -> None:
    """Golește cache-ul. Pentru teste și pentru joburile care tocmai au rescris catalogul."""
    _cache.clear()


async def get_vocabulary(deps: Any, business_id: str) -> CatalogVocabulary:
    """Vocabularul servabil al tenantului, din cache sau proaspăt.

    Două tururi concurente pot încărca amândouă la prima cerere; e acceptabil — încărcarea e
    idempotentă și fără efecte secundare, iar un lock ar ține o conexiune ocupată exact în momentul
    în care nu trebuie (NX-231: checkout scurt, nimic extern înăuntru).
    """
    now = time.monotonic()
    hit = _cache.get(business_id)
    if hit is not None and (now - hit[0]) < _TTL_S:
        return hit[1]

    try:
        async with deps.db("load_vocabulary") as conn:
            vocab = await load_vocabulary(conn, business_id)
    except Exception:  # noqa: BLE001 — DB indisponibil/înlocuit: degradăm, nu picăm turul (P6)
        # Vocabular gol ⇒ rezolvarea întoarce `UNKNOWN` pentru tot ⇒ NICIUN filtru dur. E singura
        # degradare corectă: dacă nu putem verifica un cuvânt, nu avem voie să constrângem pe el.
        # Alternativa (aplicăm tokenul neverificat) e exact defectul pe care modulul îl elimină —
        # un filtru care golește tăcut e mai rău decât o căutare largă.
        logger.warning("vocabulary_load_failed business=%s — căutare fără filtre dure", business_id)
        return CatalogVocabulary(business_id=business_id)

    if len(_cache) >= _MAX_TENANTS and business_id not in _cache:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[business_id] = (now, vocab)
    return vocab
