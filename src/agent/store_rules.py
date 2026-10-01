"""NX-369 — o regulă a magazinului parafrazată se servește în cuvintele MAGAZINULUI.

Turul c14 din 2026-10-01 („daca nu-mi place o crema o pot returna?"): modelul a chemat
`faq_lookup` și a răspuns corect („Da, poți solicita returul în 30 de zile… pe
www.service-return.com…"), dar parafrazat. Validatorul de proză acceptă din regulile magazinului
doar propoziția citată ÎNTREAGĂ (NX-346, `strip_quoted`), deci a respins „30" și „zile", iar
clientul a primit „Momentan n-am găsit produse potrivite". Pe c7 („cat costa livrarea?") același
eșec a reîncărcat cardurile de dinainte sub răspuns.

Aici fiecare propoziție a prozei respinse se leagă de regula pe care o redă, pe cuvintele de
conținut ale locale-i (`query_terms.content_terms`, aceeași tokenizare ca la căutare), iar
răspunsul devine textul ACELOR reguli, întreg, în ordinea în care modelul le-a folosit. Nicio
afirmație a modelului nu ajunge la client: doar alegerea regulilor e a lui, textul e al
magazinului. PUR.

Prag (măsurat pe c14 și c7): o propoziție se leagă doar dacă cel puțin jumătate din cuvintele ei
de conținut sunt în regulă, iar regula câștigă cu cel puțin 0,1 față de următoarea. La egalitate
rămâne regula deja aleasă; o egalitate exactă (FAQ-uri care spun același lucru) ia prima regulă.
Propozițiile cu sub trei cuvinte de conținut („Vrei și altceva?") nu leagă nimic.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from src.catalog.query_terms import content_terms

MIN_SHARE = 0.5
MIN_MARGIN = 0.1
MIN_TERMS = 3
#: Rădăcina unui cuvânt: primele caractere, ca flexiunea („returul"/„returna", „cutia"/„cutie") să
#: nu rupă potrivirea. Doar pentru potrivire, niciodată pentru text servit.
_STEM = 5
_SENTENCE_SPLIT = re.compile(r"(?<![0-9].)(?<=[.!?])\s+")


def _stems(text: str, locale: str | None) -> set[str]:
    return {t[:_STEM] for t in content_terms(text, locale)}


def quoted_rules(text: str | None, sources: Sequence[str], locale: str | None) -> list[str]:
    """Regulile magazinului (din `sources`, textul exact arătat modelului) pe care `text` le
    parafrazează, în ordinea primei folosiri. Gol = nicio propoziție nu se leagă clar."""
    if not text or not sources:
        return []
    rules = [_stems(s, locale) for s in sources]
    chosen: list[int] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        words = _stems(sentence, locale)
        if len(words) < MIN_TERMS:
            continue
        scored = sorted(
            ((len(words & r) / len(words), i) for i, r in enumerate(rules)),
            key=lambda x: (-x[0], x[1]),
        )
        best, idx = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best < MIN_SHARE:
            continue
        if best - second < MIN_MARGIN:
            close = [i for s, i in scored if best - s < MIN_MARGIN]
            already = [i for i in close if i in chosen]
            if already:
                idx = already[0]  # aceeași regulă, folosită deja de o propoziție anterioară
            elif best != second:
                continue  # două reguli diferite, aproape la egalitate: ambiguu, nu leagă nimic
            # egalitate EXACTĂ: reguli care spun același lucru; rămâne prima (`idx`)
        if idx not in chosen:
            chosen.append(idx)
    return [sources[i] for i in chosen]
