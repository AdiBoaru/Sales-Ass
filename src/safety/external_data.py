"""D14 — graniță fail-closed pentru text trimis către un serviciu extern.

Graniţa apără **IDENTITATEA**, nu vocabularul: email, telefon, IBAN, card/CNP, adresă, nume
declarat. Un text care conţine oricare dintre ele e nepublicabil, oriunde ar merge.

Ce NU apără, deliberat: **subiectele de sănătate**. Nu identifică pe nimeni, iar blocarea lor tăia
retrievalul exact pentru interogările de siguranţă — vezi `external_query_text`.

Numele declarat are DOUĂ tipare, pentru că are două forme cu proprietăţi diferite:
  • verb de prezentare neechivoc („mă numesc", „mă cheamă") → se poate detecta pe text lowercase;
  • „sunt X Y" → cere MAJUSCULE pe nume. Fără condiţia asta, tiparul prinde orice descriere de sine
    în română („sunt cu ten gras", „sunt în căutarea unui ser") — măsurat, 9 din 10 interogări
    reale de beauty pierdeau calea semantică, iar o gardă care se declanşează aproape mereu nu
    protejează nimic. Cu ea, „sunt Ion Popescu" e prins, iar descrierile trec.

Un singur loc unde sunt definite tiparele: înainte existau două regex-uri de PII în branch
(`src/evals/retrieval/validation.py` avea al doilea, deja divergent), adică două răspunsuri
posibile la aceeaşi întrebare.
"""

from __future__ import annotations

import re
import unicodedata

from src.domain.normalize import normalize

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:(?:\+|00)\d{1,3}(?:[ .-]?\d){8,11}|0(?:[ .-]?\d){9})(?!\d)")
_CARD_OR_CNP_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_ADDRESS_RE = re.compile(
    r"\b(?:strada|str\.?|bulevardul|bd\.?|bloc|scara|apartament|ap\.?|sector(?:ul)?|judet(?:ul)?)\b"
)
# Verb de PREZENTARE — neechivoc, funcţionează pe text lowercase.
_DECLARED_NAME_RE = re.compile(
    r"\b(?:ma numesc|ma cheama|numele meu (?:este|e))\s+[a-z]{2,}(?:\s+[a-z]{2,})?\b"
)

# „sunt X Y" cere MAJUSCULE, deci se evaluează pe textul cu litera mare păstrată (vezi
# `_strip_diacritics`). Motivul e măsurat în ambele direcţii:
#   • fără condiţia de majusculă, tiparul prinde orice descriere de sine în română — „sunt cu ten
#     gras", „sunt în căutarea unui ser", „sunt foarte mulţumită". 9 din 10 interogări reale de
#     beauty pierdeau calea semantică: o gardă care se declanşează aproape mereu nu protejează
#     nimic, doar degradează tot;
#   • fără tipar deloc, „sunt Ion Popescu" trece, iar numele ajunge la serviciul extern.
# Majuscula e semnalul pe care îl dau chiar oamenii când îşi scriu numele, şi e singurul care
# separă cele două cazuri fără o listă de cuvinte care n-are cum să fie completă.
# `[Ss]unt` — la început de frază verbul e scris cu majusculă („Sunt Maria Ionescu"); numele
# rămân obligatoriu capitalizate. Rămâne fals-pozitiv doar textul scris integral în Title Case,
# care e mult mai rar decât un nume scăpat.
_DECLARED_NAME_CASED_RE = re.compile(r"\b[Ss]unt\s+[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b")


def _strip_diacritics(text: str) -> str:
    """Ca `normalize`, dar PĂSTREAZĂ litera mare — `normalize` face lower, iar aici majuscula e
    exact informaţia de care avem nevoie."""
    nfkd = unicodedata.normalize("NFKD", text.strip())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


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
    """True dacă textul conține un IDENTIFICATOR de persoană.

    Două treceri, pentru că tiparele au nevoie de forme diferite ale aceluiași text: cele
    insensibile la caz rulează pe textul normalizat (altfel „Bulevardul" și „bulevardul" ar fi
    întrebări diferite), iar detecția numelui declarat cu „sunt" rulează pe forma care păstrează
    majuscula — singurul semnal care separă „sunt Ion Popescu" de „sunt cu ten gras"."""
    if any(pattern.search(normalize(text)) for pattern in PII_PATTERNS):
        return True
    return bool(_DECLARED_NAME_CASED_RE.search(_strip_diacritics(text)))


def external_query_text(normalized_query: str, *, detect_on: str | None = None) -> str | None:
    """Întoarce singurul text de query permis către embedding/reranking extern, sau `None`.

    **Ce se EXPORTĂ e mereu `normalized_query`.** `detect_on` (de regulă `raw_query`) e folosit
    NUMAI ca semnal de detecție și nu poate ieși pe nicio cale: funcția întoarce ori
    `normalized_query`, ori `None`. Distincția e necesară pentru că `normalized_query` vine deja
    lowercase din `query_spec`, iar majuscula e exact informația care separă un nume declarat de o
    descriere de sine. Fără el, un tipar bazat pe majuscule ar trece testele directe și n-ar
    detecta niciodată nimic în producție.

    La orice IDENTIFICATOR de persoană nu încercăm să păstrăm fragmente „aproape sigure": oprim
    exportul complet și apelantul cade pe retrieval intern.

    Limită cunoscută și acceptată: un nume scris fără majuscule („sunt ion popescu") trece. Nu
    există semnal determinist care să-l separe de o descriere de sine, iar o listă de cuvinte n-are
    cum să fie completă într-o limbă cu adjective nelimitate după „sunt".

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
    return None if contains_pii(detect_on or normalized_query) else text
