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
from typing import TYPE_CHECKING

from src.catalog.folding import fold_text
from src.catalog.product_type import split_name
from src.catalog.query_terms import any_locale_stopwords, stopwords

if TYPE_CHECKING:
    from collections.abc import Mapping

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


#: NX-301 — cantitatea de la coada numelui: CIFRE + unitate. Nu e o listă de cuvinte românești
#: (P11): unitățile sunt notație, nu limbă, iar tiparul cere obligatoriu o cifră în față. Formele
#: compuse reale din catalog („22 ml x 5 buc", „30 buc (310 gr)") intră prin coada permisivă.
_QUANTITY = re.compile(
    r"^\d+(?:[.,]\d+)?\s*(?:ml|l|g|gr|kg|mg|buc|bucati|bucăți|pcs|set)\b.*$",
    re.IGNORECASE,
)


def size_label(name: str | None) -> str | None:
    """Gramajul, dacă numele îl poartă la coadă („… - 125 ml" → `"125 ml"`), altfel `None`.

    Perechea obligatorie a lui `display_name`: capul scurt NU conține cantitatea pe NICIUNUL din
    cele 2.758 de produse ale primului catalog real, deci scurtarea titlului ar pierde-o integral.
    Recuperabilă determinist pe **47,5%** — de aceea răspunsul e `None`, nu o ghicire: cheia
    lipsește din card când n-o știm, exact ca restul câmpurilor opționale (nu inventăm `null`-uri).
    """
    full = (name or "").strip()
    if " - " not in full:
        return None
    tail = full.rsplit(" - ", 1)[-1].strip()
    return tail if _QUANTITY.match(tail) else None


def word_key(word: str) -> str:
    """Cheia de comparație a UNUI cuvânt: pliat (lower, fără diacritice), doar litere și cifre.
    „IT'S” → `its`, „EUBOS,” → `eubos`. Un cuvânt doar din punctuație dă cheia goală."""
    return "".join(re.findall(r"[0-9a-z]+", fold_text(word)))


def name_keys(name: str | None) -> tuple[str, ...]:
    """Cuvintele unui nume ca chei (`word_key`), câte una pe cuvânt separat de spații — deci
    lungimea se potrivește cu `name.split()`, pe care îl scurtează ancora chip-ului."""
    return tuple(word_key(w) for w in (name or "").split())


def unique_prefixes(
    names: Mapping[str, str], *, min_words: int = 1, locale: str | None = None
) -> dict[str, tuple[str, ...]]:
    """NX-318 — pentru fiecare id, cel mai scurt prefix de cuvinte (ca chei, `name_keys`) care nu
    e prefix al numelui NICIUNUI alt produs din set.

    Răspunsul la „câte cuvinte identifică un produs” depinde de ce ALTCEVA e pe ecran, nu de o
    constantă: „EUBOS” singur e unic lângă SOME BY MI, dar „HARUHARU WONDER” nu e lângă alt
    HARUHARU WONDER. Nicio listă de branduri, nicio limbă: regula e egalitatea de cuvinte în set.

    Un nume care e prefix complet al altuia (sau identic cu altul) primește numele întreg: nu
    există prefix unic mai scurt, iar a pretinde unul ar lega textul de produsul greșit. Un prefix
    de UN cuvânt care e cuvânt gol pe locale nu se folosește (se extinde la două). `locale=None`
    ⇒ cuvintele goale ale TUTUROR limbilor cunoscute (direcția sigură, nu „nicio gardă”). Nume gol
    sau lipsă ⇒ id-ul lipsește din rezultat. Determinist, independent de ordinea intrărilor.
    """
    keyed = {pid: name_keys(n) for pid, n in names.items() if n and name_keys(n)}
    empty = stopwords(locale) if locale else any_locale_stopwords()
    out: dict[str, tuple[str, ...]] = {}
    for pid, words in keyed.items():
        others = [w for other, w in keyed.items() if other != pid]
        chosen = words
        for cut in range(max(1, min_words), len(words) + 1):
            prefix = words[:cut]
            if cut == 1 and prefix[0] in empty:
                continue
            if not any(o[:cut] == prefix for o in others):
                chosen = prefix
                break
        out[pid] = chosen
    return out


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
