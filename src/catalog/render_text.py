"""Text de catalog pentru vederile modelului: nume scurt și tăiere la graniță de propoziție.

Două funcții PURE, fără vocabular de domeniu, folosite de vederile din `src/tools/catalog_tools.py`.

De ce există: pe primul catalog real, `products.name` are ~190 de caractere (nume + frază de
marketing), iar vederile îl repetau de câte ori aveau nevoie de o etichetă — o comparație de două
produse ajungea la 1.400 de caractere din care ~800 erau același nume de patru ori. Iar textele
tăiate la un număr fix de caractere rupeau propoziția la jumătate („…mai luminoasa dupa doar").
Măsurat în docs/DB-QUERY-PROBE-2026-09-08.md.
"""

from __future__ import annotations

import re

from src.catalog.product_type import split_name

#: Sub lungimea asta capul numelui nu mai identifică nimic („Ser", „Cremă") — păstrăm întregul.
_MIN_DISPLAY_LEN = 8

# Sfârșit de propoziție SAU de element de listă: punct/semn urmat de spațiu, ori marcatorul de
# listă al fișelor autorate. Căutat DE LA COADĂ, în interiorul plafonului.
_BOUNDARY = re.compile(r"[.!?;](?=\s)|\s[•·]\s")


def display_name(name: str | None) -> str:
    """Numele SCURT al produsului: capul dinaintea primului ` - ` și al primei virgule
    (aceeași regulă ca `_DISTINCTIVE_NAME` din SQL, folosită de fast path). Un cap prea scurt ca
    să identifice ceva întoarce numele întreg — mai bine lung decât ambiguu."""
    full = (name or "").strip()
    head, _tail = split_name(full)
    head = head.split(",", 1)[0].strip()
    return head if len(head) >= _MIN_DISPLAY_LEN else full


def cut_at_sentence(text: str | None, max_chars: int) -> str:
    """Normalizează spațiile și taie la cel mult `max_chars`, la ultima graniță de propoziție sau
    de element de listă din interiorul plafonului. Dacă nicio graniță nu cade în a doua jumătate a
    plafonului, taie la ultimul spațiu și marchează cu `…` — o propoziție ruptă e mai rea decât
    una lipsă, dar un text gol e mai rău decât amândouă."""
    body = " ".join((text or "").split())
    if len(body) <= max_chars:
        return body
    window = body[:max_chars]
    last = None
    for m in _BOUNDARY.finditer(window):
        last = m
    if last is not None and last.end() >= max_chars // 2:
        return window[: last.start() + (1 if window[last.start()] != " " else 0)].rstrip()
    cut = window.rfind(" ")
    if cut < max_chars // 2:
        cut = max_chars
    return window[:cut].rstrip(" ,;:") + "…"
