"""Normalizare de text PARTAJATĂ pentru lookup-uri deterministe (NX-114).

`normalize(s)` = lower + strip diacritice (ă→a, ș→s, î→i) + trim. Așa „Ten Grăs" /
„TEN GRAS" / „ten gras" colapsează la aceeași cheie. E baza pe care o folosesc
DomainPack (concern_map / risk_terms / greetings) și `taxonomy._norm` (alias identic).

NOTĂ: `gates._norm` (fără strip) și `greeting._norm` (strip + doar litere/spații) au
variante MAI STRICTE, intenționat diferite — NU le unificăm aici (le-am schimba
comportamentul). Acesta e doar numitorul comun pentru chei de config.
"""

from __future__ import annotations

import unicodedata


def normalize(s: str) -> str:
    """lower + NFKD strip-diacritice + trim. Determinist, testabil, prompt-cache-friendly."""
    nfkd = unicodedata.normalize("NFKD", s.strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))
