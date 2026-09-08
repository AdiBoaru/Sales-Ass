"""Ingredientele-cheie ale unui produs, extrase DETERMINIST din secțiunea de fișă dedicată.

Contextul: fațeta `key_ingredients` e declarată `searchable` în pachetul de domeniu, deci
`_feature_clause` filtrează pe `attributes->'key_ingredients'` — dar pe primul catalog real cheia
nu exista în `attributes`, iar `product_ingredients.is_key` era `false` pe toate cele 93.887 de
rânduri. Consecința măsurată (docs/DB-QUERY-PROBE-2026-09-08.md): „ser cu niacinamidă" filtra pe
gol, scara relaxa și modelul primea seruri NEfiltrate pe ingredient, cu o notă care spunea că s-au
relaxat „criterii secundare". Conținutul exista însă: `product_sections.kind='key_ingredients'`,
o listă de „Nume - descriere", un element pe linie.

Parserul e PUR și agnostic de limbă/vertical: nu știe ce e un ingredient, știe doar forma listei.
Regulile, în ordinea în care se aplică pe fiecare linie (după eliminarea liniilor goale și a celor
din `drop`, adică butoanele de UI copiate odată cu textul):

1. `Nume - descriere` pe aceeași linie → un element nou, cu numele dinaintea separatorului.
2. O linie care ÎNCEPE cu `-` e descrierea elementului curent (numele a venit pe linia
   precedentă, rupt de un link).
3. O linie fără separator care începe cu MAJUSCULĂ e un element nou (listele fără descrieri).
4. O linie cu minusculă, cât timp elementul curent NU are încă descriere, e o CONTINUARE a
   numelui (același caz: link în interiorul numelui — „Derivati de" / „acid hialuronic").
5. Orice altceva (minusculă, după descriere) e continuarea descrierii → ignorat.

Ce s-a încercat și NU merge: potrivirea cu tabelul `ingredients` (INCI, în engleză —
„Niacinamide" nu e „Niacinamida"); tăierea la primul spațiu (pierde „acid hialuronic").
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.domain.normalize import normalize

#: Un nume de ingredient rezonabil: nici un fragment, nici o propoziție.
MIN_CHARS = 3
MAX_CHARS = 60

_SEP = " - "
_PERCENT = re.compile(r"\s*\d+(?:[.,]\d+)?\s*%\s*")
_PAREN = re.compile(r"\s*\([^)]*\)")


#: Un nume care se termină într-un cuvânt de legătură (scurt și cu minuscule) nu e terminat.
#: Regula e pe FORMĂ, nu pe listă de cuvinte: „de", „din", „cu" sunt scurte și mici în orice
#: fișă, iar „Vitamina C" nu e prins, fiindcă „C" e majusculă.
_LINK_WORD_MAX = 3


def _unfinished(name: str) -> bool:
    last = name.rsplit(" ", 1)[-1]
    return last.islower() and len(last) <= _LINK_WORD_MAX


def _clean(name: str) -> str:
    """Numele afișabil: fără procente, fără paranteze, fără punctuație de coadă."""
    s = _PERCENT.sub(" ", name)
    s = _PAREN.sub("", s)
    return " ".join(s.split()).strip(" .,:;-–")


def parse_key_ingredients(body: str | None, *, drop: Iterable[str] = ()) -> list[str]:
    """Numele afișabile din corpul secțiunii, în ordinea din text, fără duplicate (după
    normalizare). Listă goală când nu e nimic de extras."""
    dropped = {normalize(d) for d in drop}
    names: list[str] = []
    current: str | None = None
    has_desc = False

    def flush() -> None:
        nonlocal current, has_desc
        if current:
            cleaned = _clean(current)
            if MIN_CHARS <= len(cleaned) <= MAX_CHARS:
                names.append(cleaned)
        current, has_desc = None, False

    for raw in (body or "").splitlines():
        line = " ".join(raw.split())
        if not line or normalize(line) in dropped:
            continue
        if _SEP in line:
            flush()
            current = line.split(_SEP, 1)[0]
            has_desc = True
        elif line.startswith("-"):
            has_desc = True
        elif current is not None and not has_desc and _unfinished(current):
            # „Extract de" / „Centella Asiatica": numele s-a rupt la un link exact după un cuvânt
            # de legătură, iar partea legată începe cu majusculă. Un nume terminat într-un cuvânt
            # scurt și mic nu e un nume terminat.
            current = f"{current} {line}"
        elif line[0].isupper():
            flush()
            current = line
        elif current is not None and not has_desc:
            current = f"{current} {line}"
        # altfel: continuarea unei descrieri → ignorat
    flush()

    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        key = normalize(n)
        if key and key not in seen:
            seen.add(key)
            out.append(n)
    return out


def canonical_values(names: Iterable[str]) -> list[str]:
    """Valorile scrise în `attributes.key_ingredients`: forma NORMALIZATĂ (minuscule, fără
    diacritice), fiindcă `_feature_clause` compară exact cu `normalize(termen)`. Forma afișabilă
    rămâne în secțiune; atributul e pentru FILTRU."""
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        key = normalize(_clean(n))
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out
