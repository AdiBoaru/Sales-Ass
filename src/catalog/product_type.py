"""Tipul de produs, extras DETERMINIST din numele de catalog. Pur: fără DB, fără I/O, fără LLM.

## De ce există

Categoria e prea grosieră ca să separe „vreau o cremă" de „vreau un ser": pe catalogul SOLE,
`ten-ingrijirea-tenului` are 933 de produse și le conține pe amândouă. Iar fără o fațetă de TIP,
căutarea lexicală decide singură, pe text — și textul minte. Măsurat pe catalogul SOLE
(2.758 produse, 2026-09-07), interogarea „protectie solara spf" întorcea pe primul loc un ser cu
retinol, „ser" întorcea o cremă de ochi, iar „gel de curatare" un gel de duș. Cauza e în
`search_tsv`: numele de produs are 191 de caractere în medie și **2.287 din 2.758 conțin fraza
„care contribuie"**, deci umplutura de marketing stă în greutatea `A` (migrarea 046 presupunea că
`name` e identitatea produsului — pe primul catalog real, `name` e nume PLUS descriere).

Semnalul de tip există însă deja în date, îngropat. Numele are forma:

    <NUME REAL> - <TIP> formulat cu <ingrediente>, care contribuie la <beneficii>

Coada de după primul ` - ` începe cu tipul. 2.647 din 2.758 de nume au coada asta.

## Cele două reguli, ambele STRUCTURALE

Regulile de mai jos sunt gramaticale, nu de domeniu: nu conțin niciun cuvânt de cosmetice, deci
țin și pe un magazin de anvelope. Vocabularul REZULTAT e al catalogului, nu al codului (P9).

**R1 — fuziunea.** Un calificativ introdus de PREPOZIȚIE numește ținta și schimbă clasa obiectului:
„balsam **de buze**" nu e același lucru cu „balsam **de par**". Un calificativ alipit fără
prepoziție e format sau textură și NU schimbă clasa: „fond de ten **cushion**", „ruj **lichid**",
„masca de fata **tip servetel**". Deci un tip se pliază pe altul doar dacă acesta din urmă e prefix
de cuvinte ȘI restul nu conține prepoziție.

Direcția erorii e aleasă deliberat. O cheie prea îngustă golește filtrul, iar golirea are deja
treaptă de relaxare în `search_products_lexical` (P6). O cheie prea largă amestecă TĂCUT, și nimic
din aval nu prinde asta — validatorul (stagiul 8) și `grounding_guard` (NX-240) sunt porți de
ADEVĂR, nu de potrivire. Deci la dubiu nu fuzionăm.

**R2 — coordonarea.** Un tip de produs nu coordonează. „hidratare si luminozitate" e o listă de
beneficii scăpată din numele produsului, nu un obiect; „crema de fata" e un obiect. Regula costă
tipurile cu adevărat duale („crema de fata si corp", 5 produse) și elimină 33 de produse etichetate
cu fraze de marketing.

**Ce s-a încercat și NU merge**, ca să nu se reintroducă:

* *fuziune pe subset de cuvinte* (în loc de prefix): pliază „balsam de buze"(21), „balsam de
  par"(2) și „balsam de curatare"(28) peste „balsam"(31) → o cheie de 90 care amestecă balsamul de
  buze cu cel de păr. Exact eroarea pe care fațeta trebuie s-o excludă;
* *prag pe branduri DISTINCTE* (ipoteza: o frază de marketing e ticul unui singur brand): picată pe
  date. „hidratare si luminozitate" apare la 10 branduri — stilul de denumire e al importatorului,
  nu al brandului — iar tipuri reale ca „pasta de dinti" sau „creion contur buze" au unul singur.
  Pragul respingea produse reale și lăsa fraza de marketing să treacă;
* *respingere când un cuvânt al tipului e o nevoie declarată* (`concern_map`): respinge „luciu de
  buze" (66 de produse), fiindcă „luciu" e și nevoie (`oily`) și obiect (gloss). Un cuvânt poate
  numi și o nevoie și un obiect; dezambiguizează structura, nu cuvântul.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping

#: Suport minim ca un tip să intre în vocabular. Sub el, valoarea rămâne nescrisă (`None`) — un
#: tip cu două produse nu e o fațetă, e o coincidență, iar `load_vocabulary` l-ar tăia oricum.
MIN_SUPPORT = 5

#: Unde se termină tipul și începe umplutura de marketing. Toate sunt forme VERBALE care introduc
#: compoziția sau beneficiul („formulat cu…", „care contribuie la…"), nu substantive de produs.
#: `hidratare` a fost aici la prima încercare și tăia „Hidratare Profunda si Calmare" la zero,
#: producând cioburi de frază — un beneficiu nu e un delimitator, e conținut.
_STOP = re.compile(
    r"\b(formulat|formulata|formulate|formulati|care\s+contribuie|ce\s+contribuie|"
    r"imbogatit|imbogatita|conceput|conceputa|creat|creata|destinat|destinata)\b"
)

#: Prepoziții — introduc ținta, deci schimbă clasa obiectului (R1).
_PREPOSITIONS = frozenset({"de", "pentru", "din", "cu", "la", "in", "pe", "sub"})

#: Conjuncții coordonatoare — un obiect nu coordonează (R2).
_COORDINATORS = frozenset({"si", "sau", "ori"})

#: Cuvinte care nu pot sta nici la începutul, nici la sfârșitul unui tip: o frază care începe sau
#: se termină cu ele e TĂIATĂ, nu un obiect („luminozitate si", „de fata").
_GLUE = _PREPOSITIONS | _COORDINATORS | frozenset({"tip", "a", "al", "ale", "un", "o"})

#: Cel mai lung tip plauzibil. Peste el nu mai e un tip, e o propoziție.
_MAX_WORDS = 5
_MAX_CHARS = 40


def fold(text: str) -> str:
    """Aceeași normalizare ca `ro_unaccent` (033): minuscule, fără diacritice. Dacă cele două
    capete diferă, potrivirea nu se produce — vezi comentariul migrării 046."""
    d = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def split_name(name: str) -> tuple[str, str | None]:
    """`(numele real, coada descriptivă)` — despărțite la primul ` - `.

    Numele real e ce ar trebui să fie în greutatea `A` a vectorului de căutare: pe catalogul SOLE
    are 40 de caractere în medie, față de 191 cât are numele întreg.
    """
    head, sep, tail = (name or "").partition(" - ")
    return head.strip(), (tail.strip() if sep else None)


def raw_type(name: str) -> str | None:
    """Tipul BRUT (normalizat, încă necanonicalizat) sau `None` dacă numele nu-l poartă."""
    _, tail = split_name(name)
    if not tail:
        return None
    folded = fold(tail)
    if m := _STOP.search(folded):
        folded = folded[: m.start()]
    # Prima virgulă închide tipul: după ea vin volumul și nuanța („, 50 ml", „, MV02 Hurt").
    candidate = folded.split(",")[0].strip(" .-")
    candidate = re.sub(r"[^a-z0-9 ]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if not candidate or len(candidate) > _MAX_CHARS:
        return None
    words = candidate.split()
    if not 1 <= len(words) <= _MAX_WORDS:
        return None
    if words[0] in _GLUE or words[-1] in _GLUE:  # R2: frază tăiată, nu obiect
        return None
    if any(w in _COORDINATORS for w in words):  # R2: obiectele nu coordonează
        return None
    return candidate


def _folds_onto(specific: str, general: str) -> bool:
    """R1: `specific` se pliază pe `general`? Cere prefix de CUVINTE + niciun calificativ
    prepozițional în rest."""
    sw, gw = specific.split(), general.split()
    if len(gw) >= len(sw) or sw[: len(gw)] != gw:
        return False
    return not any(w in _PREPOSITIONS for w in sw[len(gw) :])


def build_vocabulary(
    names: Iterable[str], *, min_support: int = MIN_SUPPORT
) -> tuple[dict[str, str], dict[str, int]]:
    """`(tip brut → tip canonic, tip canonic → număr de produse)`.

    Determinist și idempotent: aceleași nume dau același vocabular, indiferent de ordine.
    Ancorele (țintele posibile de fuziune) se aleg ÎNAINTE de fuziune, din tipurile brute care trec
    pragul — altfel rezultatul ar depinde de ordinea în care se pliază.
    """
    counts: dict[str, int] = defaultdict(int)
    for name in names:
        if t := raw_type(name):
            counts[t] += 1

    anchors = sorted(t for t, n in counts.items() if n >= min_support)

    def target(t: str) -> str:
        # Cea mai SPECIFICĂ ancoră pe care se pliază (cele mai multe cuvinte); tie-break pe nume,
        # ca rezultatul să nu depindă de ordinea de iterare.
        best = [a for a in anchors if _folds_onto(t, a)]
        return max(best, key=lambda a: (len(a.split()), a)) if best else t

    mapping: dict[str, str] = {}
    for t in sorted(counts):
        cur = t
        for _ in range(4):  # punct fix; 4 e cu mult peste adâncimea observată (2)
            nxt = target(cur)
            if nxt == cur:
                break
            cur = nxt
        mapping[t] = cur

    canonical: dict[str, int] = defaultdict(int)
    for t, n in counts.items():
        canonical[mapping[t]] += n

    kept = {t: n for t, n in canonical.items() if n >= min_support}
    return {t: c for t, c in mapping.items() if c in kept}, dict(kept)


def classify(name: str, mapping: Mapping[str, str]) -> str | None:
    """Tipul canonic al unui produs, dat vocabularul. `None` = necunoscut, și `None` NU se scrie:
    un atribut absent înseamnă „nu știm" (`missing_value: unknown`), iar UNKNOWN nu devine niciodată
    o valoare — aceeași regulă ca la stocul de variantă (migrarea 047)."""
    t = raw_type(name)
    return mapping.get(t) if t else None
