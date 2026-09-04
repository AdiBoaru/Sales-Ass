"""Termenii de CĂUTARE dintr-o frază de client — pur, fără I/O, fără LLM.

De ce există modulul, măsurat pe catalogul SOLE (2.758 produse, 2026-08-28):
`websearch_to_tsquery` leagă toate cuvintele cu **ȘI**, iar configurația `'simple'` nu elimină
niciun cuvânt. Deci „sampon pentru par gras" devine `'sampon' & 'pentru' & 'par' & 'gras'`, iar un
produs trebuie să conțină LITERAL și „pentru" ca să se potrivească. Rezultatul pe fraze reale de
client (media unui mesaj inbound e 28 de caractere, deci fraze, nu cuvinte-cheie): **11 din 16
interogări întorceau ZERO**, nu „rezultate slabe". „ceva pentru cearcane" → 0. „produse pentru
acnee" → 0. „ce imi recomanzi pentru riduri" → 0.

Configurația `'romanian'` NU rezolvă asta, verificat: lista ei de cuvinte goale e scrisă CU
diacritice, iar noi normalizăm cu `ro_unaccent` înainte de indexare (033), deci „pentru", „si",
„ceva" trec neatinse prin ea. De aceea lista trăiește aici, lângă normalizarea pe care o
presupune, nu în dicționarul motorului.

**Cuvintele goale sunt o optimizare, nu garanția.** Garanția că un client primește ceva e treapta
de relaxare din `search_products_lexical` (ȘI → SAU pe miss). Un termen scăpat din listă înseamnă
o interogare care coboară o treaptă, nu una care întoarce zero. Asta e și motivul pentru care
lista poate rămâne scurtă și conservatoare: conține DOAR cuvinte funcționale (prepoziții,
conjuncții, pronume, auxiliare, umplutură conversațională), niciodată un cuvânt care ar putea numi
un produs sau o nevoie.

Principiul 11 (D3): limba e o CHEIE, nu o constantă. Tabelul e indexat pe locale, iar o locale
necunoscută primește mulțimea goală — adică exact comportamentul de dinainte, nu o listă
românească aplicată peste un catalog maghiar.
"""

from __future__ import annotations

import re

# Aceeași normalizare ca `ro_unaccent` (033), replicată aici ca funcție PURĂ de Python: dacă cele
# două capete diferă, potrivirea nu se produce — vezi comentariul migrării. Include formele cu
# sedilă (ş/ţ), care apar în text tastat din surse vechi.
_RO_FOLD = str.maketrans("ăâîșțşţ", "aaistst")

# Cuvinte FUNCȚIONALE, per locale. Regula de includere e strictă: un cuvânt intră aici doar dacă
# nu poate numi niciodată un produs, un brand sau o nevoie. „crema", „ulei", „par" NU au ce căuta
# în listă, oricât de frecvente ar fi. Scrise ca text, nu ca set literal, ca lista să rămână
# citibilă pe grupuri gramaticale după trecerea formatterului.
_RO_STOPWORDS = """
    a al ale cu de din dintre in intr intre la pe pentru peste pana prin spre sub si sau dar ori ca
    un o una unui unei niste cel cea cei cele acest aceasta acesta aceste acestea
    eu tu el ea noi voi ei imi iti isi mi ti ma te se ne va le lui mea meu mei tau ta
    am ai are as ar au fi fie este sunt esti era fost vreau vrei vrea caut cauti doresc trebuie
    poate pot recomanzi recomanda spune arata da ajuta
    ce care cine cum cand unde cat cata cati cate ceva orice altceva nimic mai prea foarte doar
    numai tot toate toti buna salut multumesc rog
    produs produse produsul produsele articol articole varianta optiune optiuni recomandare
    recomandari sfat
"""

_STOPWORDS: dict[str, frozenset[str]] = {"ro": frozenset(_RO_STOPWORDS.split())}

# NX-266 — cuvintele de COMPARAȚIE, per locale. Stau aici, lângă cuvintele goale, din același
# motiv: sunt vocabular FUNCȚIONAL al limbii, nu al magazinului. „sub 100 lei" înseamnă același
# lucru într-un magazin de cosmetice și într-unul de anvelope, iar un tenant nou nu trebuie să le
# redeclare. Ce ÎNSEAMNĂ „100" (lei? ml?) e al pachetului de domeniu; ce înseamnă „sub" e al limbii.
#
# Formatul e o tabelă `op: frază, frază, …`, scrisă ca text ca să rămână citibilă pe grupuri după
# formatter. Frazele se potrivesc pe text NORMALIZAT (lower, fără diacritice), cea mai LUNGĂ prima:
# „cel putin" trebuie să bată „putin", altfel o negație parțială ar schimba operatorul.
#
# Lista e deliberat conservatoare. Un cuvânt de comparație scăpat înseamnă un operator implicit
# (cel declarat de fațetă în pachet), nu o constrângere inversată — degradarea merge spre „mai
# puțin precis", niciodată spre „exclude ce nu trebuie".
_RO_COMPARATORS = """
    lte: cel mult, nu mai mult de, nu mai scump de, mai putin de, mai ieftin de, pana in, pana la,
         maximum, maxim, sub
    gte: cel putin, nu mai putin de, mai mult de, incepand de la, incepand cu, minimum, minim,
         macar, peste
    eq:  exact, fix de, fix
"""

_COMPARATORS: dict[str, tuple[tuple[str, str], ...]] = {}


def _parse_comparators(table: str) -> tuple[tuple[str, str], ...]:
    """Tabela text → perechi `(frază, op)`, ordonate descrescător după lungime.

    Ordinea NU e cosmetică: potrivirea se face pe prima frază care se potrivește, iar „cel putin"
    conține „putin". Fără sortare, operatorul depinde de ordinea în care cineva a scris lista."""
    pairs: list[tuple[str, str]] = []
    for chunk in table.split(";"):
        for op_block in re.finditer(r"(\w+)\s*:\s*([^:]*?)(?=\s+\w+\s*:|$)", chunk, re.S):
            op = op_block.group(1).strip()
            for phrase in op_block.group(2).split(","):
                cleaned = " ".join(phrase.split())
                if cleaned:
                    pairs.append((cleaned, op))
    return tuple(sorted(pairs, key=lambda p: (-len(p[0]), p[0])))


def comparators(locale: str | None) -> tuple[tuple[str, str], ...]:
    """Frazele de comparație ale unei locale, cele mai lungi întâi. Locale necunoscută → `()`,
    adică extracția cade pe operatorul implicit al fațetei (P11: nu aplicăm româna peste o limbă
    pe care n-o cunoaștem)."""
    if not locale:
        return ()
    key = locale.split("-")[0].lower()
    if key not in _COMPARATORS:
        table = {"ro": _RO_COMPARATORS}.get(key)
        _COMPARATORS[key] = _parse_comparators(table) if table else ()
    return _COMPARATORS[key]


def fold(text: str) -> str:
    """`lower` + fără diacritice RO — oglinda Python a lui `ro_unaccent(text)` din 033."""
    return text.lower().translate(_RO_FOLD)


def stopwords(locale: str | None) -> frozenset[str]:
    """Cuvintele goale ale unei locale. Locale necunoscută/absentă → mulțimea goală (P11: nu
    aplicăm româna peste o limbă pe care n-o cunoaștem)."""
    if not locale:
        return frozenset()
    # `ro-RO` și `ro` sunt aceeași limbă pentru scopul ăsta; cheia e prefixul.
    return _STOPWORDS.get(locale.split("-")[0].lower(), frozenset())


def content_terms(query: str, locale: str | None) -> list[str]:
    """Termenii PURTĂTORI DE SENS dintr-o frază, normalizați, în ordinea din text, fără duplicate.

    Tokenizarea păstrează doar litere și cifre (`spf 50`, `c` din „vitamina c" rămân), pe text deja
    normalizat — deci ce iese de aici se potrivește lexem cu lexem cu `products.search_tsv`.

    Nu există prag pe lungime: „c" din „vitamina c" și „50" din „spf 50" sunt informație, iar
    „a" din „a mea" cade fiindcă e în lista de cuvinte goale, nu fiindcă e scurt. Un prag ar fi
    tăiat exact termenii scurți care discriminează cel mai bine într-un catalog de cosmetice.

    **Niciodată gol pentru o interogare care are text.** Dacă filtrarea ar consuma tot („ce imi
    recomanzi"), întoarcem tokenii bruți: o căutare slabă e recuperabilă de treapta de relaxare, o
    căutare fără niciun termen nu e — ar deveni tăcere (P6).
    """
    tokens = [t for t in re.split(r"[^0-9a-z]+", fold(query)) if t]
    if not tokens:
        return []
    stop = stopwords(locale)
    kept = [t for t in tokens if t not in stop]
    # dedup păstrând ordinea: „fond de ten pentru ten gras" → ten o singură dată (un `&` repetat
    # nu schimbă potrivirea, dar umflă degeaba tsquery-ul și rangul).
    seen: set[str] = set()
    out = [t for t in kept if not (t in seen or seen.add(t))]
    return out or list(dict.fromkeys(tokens))


def strict_query(terms: list[str]) -> str:
    """Fraza pentru `websearch_to_tsquery` cu semantica ȘI (toți termenii trebuie să apară).

    Întoarcem TEXT, nu SQL: parametrizarea rămâne a apelantului, iar `websearch_to_tsquery` e
    exact funcția care ignoră sintaxa neașteptată în loc să crape pe ea."""
    return " ".join(terms)


def relaxed_query(terms: list[str]) -> str:
    """Fraza pentru semantica SAU — treapta de relaxare, când ȘI n-a găsit nimic.

    `websearch_to_tsquery` tratează „or" ca operator, deci `„a or b"` → `'a' | 'b'`. Termenii sunt
    deja normalizați la `[0-9a-z]`, deci niciunul nu poate fi literalmente „or" într-o cerere
    românească și nici nu poate introduce sintaxă."""
    return " or ".join(terms)
