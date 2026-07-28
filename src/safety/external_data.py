"""D14 — graniță fail-closed pentru text trimis către un serviciu extern.

Tiparele sunt împărțite în DOUĂ familii, pentru că nu au aceeași natură și nu au aceiași
consumatori:

Graniţa apără **IDENTITATEA**, nu vocabularul: email, telefon, IBAN, card/CNP, adresă, nume
declarat. Un text care conţine oricare dintre ele e nepublicabil, oriunde ar merge.

Ce NU apără (și de ce, pentru că ambele au fost aici și au fost scoase deliberat în NX-209):
  • **subiectele de sănătate** — nu identifică pe nimeni, iar blocarea lor tăia retrievalul exact
    pentru interogările de siguranţă (vezi `external_query_text`);
  • **„sunt X Y" ca nume declarat** — în română `sunt` introduce aproape orice descriere de sine.
    Măsurat pe interogări reale de beauty: 9 din 10 pierdeau calea semantică. O gardă care se
    declanşează aproape mereu nu protejează nimic, doar degradează tot.

Un singur loc unde sunt definite tiparele: înainte existau două regex-uri de PII în branch
(`src/evals/retrieval/validation.py` avea al doilea, deja divergent), adică două răspunsuri
posibile la aceeaşi întrebare.
"""

from __future__ import annotations

import re

from src.domain.normalize import normalize

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:(?:\+|00)\d{1,3}(?:[ .-]?\d){8,11}|0(?:[ .-]?\d){9})(?!\d)")
_CARD_OR_CNP_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_ADDRESS_RE = re.compile(
    r"\b(?:strada|str\.?|bulevardul|bd\.?|bloc|scara|apartament|ap\.?|sector(?:ul)?|judet(?:ul)?)\b"
)
# Verb de PREZENTARE, nu „sunt". `sunt` a fost scos după ce a fost măsurat: în română introduce
# aproape orice descriere de sine, iar tiparul `sunt X Y` prindea „sunt cu ten gras", „sunt în
# căutarea unui ser", „sunt foarte mulțumită". 9 din 10 interogări reale de beauty pierdeau calea
# semantică — o gardă care se declanșează aproape mereu nu protejează nimic, doar degradează tot.
_DECLARED_NAME_RE = re.compile(
    r"\b(?:ma numesc|ma cheama|numele meu (?:este|e))\s+[a-z]{2,}(?:\s+[a-z]{2,})?\b"
)

#: Identifică o PERSOANĂ. Verificat de oricine exportă text în afara sistemului.
PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    _EMAIL_RE,
    _IBAN_RE,
    _PHONE_RE,
    _CARD_OR_CNP_RE,
    _ADDRESS_RE,
    _DECLARED_NAME_RE,
)


def contains_pii(text: str) -> bool:
    """True dacă textul conține un IDENTIFICATOR de persoană. Normalizează întâi (fără diacritice,
    lower) — altfel „Bulevardul" și „bulevardul" ar fi întrebări diferite."""
    return any(pattern.search(normalize(text)) for pattern in PII_PATTERNS)


def external_query_text(normalized_query: str) -> str | None:
    """Întoarce singurul text de query permis către embedding/reranking extern, sau `None`.

    `raw_query` nu e argument al funcției — structural, nu prin convenție. La orice IDENTIFICATOR
    de persoană nu încercăm să păstrăm fragmente „aproape sigure": oprim exportul complet și
    apelantul cade pe retrieval intern.

    **Subiectele de sănătate NU mai blochează** (decizie NX-209). Trei motive, în ordinea greutății:
      • „sunt însărcinată, ce cremă pot folosi" e exact interogarea pentru care există gate-ul de
        contraindicații (NX-173). A-i tăia retrievalul semantic însemna ca fix cazul de siguranță
        să primească cel mai slab răspuns — protecție care înrăutățește lucrul pe care îl apără;
      • nu identifică pe nimeni: „ten sensibil" sau „afecțiune a scalpului" descriu o nevoie, nu o
        persoană. Ce protejăm aici e identitatea, nu vocabularul;
      • textul ajunge oricum la ACELAȘI furnizor în stagiul agent, care primește conversația
        întreagă. O graniță care oprește 6 cuvinte pe o cale și lasă 6000 pe alta nu e o graniță.
    """
    text = normalize(normalized_query).strip()
    if not text:
        return None
    return None if contains_pii(text) else text
