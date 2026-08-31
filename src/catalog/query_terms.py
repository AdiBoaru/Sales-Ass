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
