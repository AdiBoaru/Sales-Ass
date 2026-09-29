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
românească aplicată peste un catalog în altă limbă.
"""

from __future__ import annotations

import re
from itertools import combinations

from src.catalog.folding import fold as _fold

# Plierea de diacritice are UN singur producător (`src/catalog/folding.py`) — vezi docstring-ul de
# acolo pentru de ce. `fold` se re-exportă aici fiindcă ăsta e numele public prin care o importă
# restul codului; nu se REDEFINEȘTE.

# Cuvinte FUNCȚIONALE, per locale. Regula de includere e strictă: un cuvânt intră aici doar dacă
# nu poate numi niciodată un produs, un brand sau o nevoie. „crema", „ulei", „par" NU au ce căuta
# în listă, oricât de frecvente ar fi. Scrise ca text, nu ca set literal, ca lista să rămână
# citibilă pe grupuri gramaticale după trecerea formatterului.
#
# NX-298: grupul al treilea de verbe/conjuncții e DERIVAT, nu ghicit — din cele 98 de mesaje reale
# ale tenantului pilot, confruntând fiecare termen supraviețuitor cu catalogul: „sa" apare în 20 de
# mesaje și prinde 2.084 din 2.758 de produse (76% — deci nu desparte nimic), „pai"/„stiu"/„zici"/
# „cumpar" prind ZERO. `e` și `s` au fost măsurate la fel și NU au intrat, deliberat: un prag pe
# lungime ar tăia „vitamina E" și „spf 50", exact termenii scurți care discriminează cel mai bine.
# Lista rămâne igienă de RANKING, nu poartă: recall-ul e apărat mecanic, de treapta terminală în
# care textul coboară din poartă în ordonator și de completarea paginii din filtrul de subiect
# (`filters_only`, `db/queries/catalog.py`). Un cuvânt scăpat aici înseamnă o ordine puțin mai
# proastă, niciodată un răspuns mai scurt.
_RO_STOPWORDS = """
    a al ale cu de din dintre in intr intre la pe pentru peste pana prin spre sub si sau dar ori ca
    decat
    un o una unui unei niste cel cea cei cele acest aceasta acesta aceste acestea
    eu tu el ea noi voi ei imi iti isi mi ti ma te se ne va le lui mea meu mei tau ta
    am ai are as ar au fi fie este sunt esti era fost vreau vrei vrea caut cauti doresc trebuie
    poate pot recomanzi recomanda spune arata da ajuta
    sa nu pai stiu zici cumpar cumpara iau vad pune scap scapa scape scapi despre multe asta
    nevoie nevoi
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


#: Re-export: `fold` trăiește în `src/catalog/folding.py`, unicul producător al plierii. Importat
#: aici ca să rămână numele public prin care îl consumă `deterministic`, `clarify_menu`,
#: `domain.constraints` și calea lexicală.
fold = _fold


def stopwords(locale: str | None) -> frozenset[str]:
    """Cuvintele goale ale unei locale. Locale necunoscută/absentă → mulțimea goală (P11: nu
    aplicăm româna peste o limbă pe care n-o cunoaștem)."""
    if not locale:
        return frozenset()
    # `ro-RO` și `ro` sunt aceeași limbă pentru scopul ăsta; cheia e prefixul.
    return _STOPWORDS.get(locale.split("-")[0].lower(), frozenset())


def any_locale_stopwords() -> frozenset[str]:
    """Reuniunea cuvintelor goale ale TUTUROR limbilor cunoscute. Pentru apelanții care nu știu
    limba turului și trebuie să greșească în direcția sigură: a refuza un cuvânt gol al altei limbi
    drept identificator costă un cuvânt în plus, a-l accepta leagă textul de produsul greșit."""
    return frozenset().union(*_STOPWORDS.values())


def _tokens(text: str) -> list[str]:
    """Tokenii unei fraze: text pliat, doar litere și cifre. UN SINGUR producător.

    Există ca funcție fiindcă ambele capete ale unei potriviri trebuie tăiate la fel. Precedentul
    e măsurat: în `vocabulary.py`, intrările se despărțeau pe „-" iar termenul cerut nu, deci
    «ingrijirea-parului» ieșea `UNKNOWN` acolo unde «ingrijirea parului» se rezolva, iar turul
    rula fără filtru de raft. O a doua tokenizare scrisă de mână aici ar reface exact defectul.
    """
    return [t for t in re.split(r"[^0-9a-z]+", fold(text)) if t]


#: Numele public al tokenizatorului, pentru consumatorii din afara modulului (NX-329: resolverul
#: de referințe taie numele cerut și numele produselor cu ACEEAȘI funcție).
tokens = _tokens


# NX-251 → NX-329: cuvintele care fac parte din FORMULA unei scurtături, dincolo de ce prinde
# regexul declanșator și de cuvintele goale. Regula de includere e ACEEAȘI ca a cuvintelor goale:
# un cuvânt intră doar dacă nu poate numi NICIODATĂ un produs, un brand sau o nevoie. Verbe de
# arătare/trimitere, adverbe de manieră și numeralele SCRISE cu litere (o cantitate de selecție:
# „primele două"), niciodată o CIFRĂ, fiindcă o cifră poate fi un preț, un volum sau un SPF.
# Mutat aici din `agent/deterministic.py`: extractorul de referințe al scurtăturilor îl consumă și
# el, iar tabelele de limbă au un singur loc (P11).
_FORMULA_FILLERS: dict[str, frozenset[str]] = {
    "ro": frozenset(
        """
        arata arati aratati trimite trimiteti da dati vezi vreau as putea poti
        direct rapid repede acum imediat te va rog rogu hai
        prima primul primele primii ultima ultimul ultimele ambele astea alea acelea astealalte
        doua doi trei patru cinci
        """.split()
    )
}


def formula_fillers(locale: str | None) -> frozenset[str]:
    """Fillerii formulei unei scurtături. Locale necunoscută → mulțimea goală (P11)."""
    if not locale:
        return frozenset()
    return _FORMULA_FILLERS.get(locale.split("-")[0].lower(), frozenset())


# NX-329 — markerii de REFERINȚĂ ai extractorului scurtăturilor: fraze care spun CE FEL de
# referință a scris clientul, nu spre ce produs arată (asta o decide resolverul, pe date). Sunt
# vocabular funcțional al limbii („celălalt", „de mai devreme"), la fel ca comparatorii; „ieftin" și
# „scump" numesc direcția PREȚULUI, care e o dimensiune universală, nu a unui vertical.
# Deixisul e doar la SINGULAR: „astea" / „acestea" arată spre tot setul afișat, adică exact ce
# face scurtătura fără nicio referință.
_RO_REFERENCE_MARKERS = """
    extreme_min: cel mai ieftin, cea mai ieftina, cel mai accesibil, cea mai accesibila
    extreme_max: cel mai scump, cea mai scumpa
    the_other: celalalt, cealalta, celalat, celalt
    earlier: de mai devreme, de dinainte, de adineauri, de data trecuta, de mai inainte
    deictic: acesta, aceasta, acest, acestui, acestei, asta
"""
_REFERENCE_MARKERS: dict[str, tuple[tuple[str, str], ...]] = {}

# NX-329 — conectorii dintre DOUĂ referințe. Două grupuri, fiindcă nu leagă la fel pe orice
# poartă: `list` desparte ținte pe orice scurtătură („linkul la X și Y"), `versus` doar pe
# comparație („compară X cu Y"); la link „cu" e de obicei parte din nume („crema cu acid").
_RO_CONNECTORS = """
    list: si, sau
    versus: cu, vs, versus, fata de
"""
_CONNECTORS: dict[str, tuple[tuple[str, str], ...]] = {}


# NX-329 PR B — sufixele de FLEXIUNE ale unui substantiv: articolul hotărât și pluralul. Clientul
# (și chip-ul nostru de comparație) scrie «serul Anua», «tonerul ZEROID», iar numele de pe card
# poartă „Ser", „Toner". Egalitatea strictă de cuvinte le despărțea: sonda NX-329 a găsit cinci
# chip-uri de comparație emise de noi care plecau la model pe ecrane unde ambele produse erau
# numite. NU e o potrivire pe prefix (ca `needs._prefix_match`): un prefix nelimitat ar lega „pro"
# din «Omnia Pro» de „produse". Doar tulpina + unul dintre sufixele de mai jos, cu tulpina de cel
# puțin 3 litere. Pe text pliat (fără diacritice).
_INFLECTION_SUFFIXES: dict[str, tuple[str, ...]] = {
    "ro": (
        "ul",
        "ului",
        "le",
        "lui",
        "lor",
        "ilor",
        "a",
        "ua",
        "ei",
        "i",
        "ii",
        "e",
        "ele",
        "uri",
        "urile",
        "urilor",
    ),
    "en": ("s", "es"),
}


def inflection_suffixes(locale: str | None) -> tuple[str, ...]:
    """Sufixele de flexiune ale locale-i. Locale necunoscută → `()`: doar egalitatea (P11)."""
    if not locale:
        return ()
    return _INFLECTION_SUFFIXES.get(locale.split("-")[0].lower(), ())


def _table(
    cache: dict[str, tuple[tuple[str, str], ...]], tables: dict[str, str], locale: str | None
) -> tuple[tuple[str, str], ...]:
    """O tabelă `op: frază, …` per locale, parsată o dată (același format ca comparatorii)."""
    if not locale:
        return ()
    key = locale.split("-")[0].lower()
    if key not in cache:
        table = tables.get(key)
        cache[key] = _parse_comparators(table) if table else ()
    return cache[key]


def reference_markers(locale: str | None) -> tuple[tuple[str, str], ...]:
    """`(frază, fel)` pentru markerii de referință ai locale-i, cele mai lungi întâi. Felurile:
    `extreme_min`, `extreme_max`, `the_other`, `earlier`, `deictic`. Locale necunoscută → `()`."""
    return _table(_REFERENCE_MARKERS, {"ro": _RO_REFERENCE_MARKERS}, locale)


def connectors(locale: str | None) -> tuple[tuple[str, str], ...]:
    """`(frază, grup)` pentru conectorii dintre referințe (`list` / `versus`), cei mai lungi
    întâi. Locale necunoscută → `()`: textul rămâne o singură referință, iar resolverul decide."""
    return _table(_CONNECTORS, {"ro": _RO_CONNECTORS}, locale)


# NX-330 — markerii de NEGAȚIE ai validatorului de proveniență (contractul kernelului, „Provenance
# and strength", pasul 3): un `avoid` fără negație în citatul clientului coboară la `implicit`,
# iar un `eq` cu negație lângă valoare e `polarity_conflict`. DOAR cuvinte funcționale, pe text
# pliat: un verb („evit") e cuvânt de conținut și nu intră, iar o formulare fără marker rămâne
# `implicit`, adică soft, direcția sigură. „nu" din comparatorii de mai sus („nu mai mult de") nu
# e negație: se scade întâi comparatorul, apoi se caută negația.
_NEGATION_MARKERS: dict[str, frozenset[str]] = {
    "ro": frozenset({"nu", "fara", "niciun", "nicio", "nici"}),
}

# NX-330 — comparatorii RELATIVI („mai ieftin decât ăsta"): direcția e a limbii, numărul îl
# calculează codul din prețul RECITIT al produsului referit (contractul, rândul „Value of a relative
# change"). „ieftin"/„scump" numesc direcția prețului, o dimensiune universală, ca markerii de
# referință NX-329 („cel mai ieftin"). Tabelul de comparatori de mai sus are doar „mai ieftin de".
_RO_RELATIVE_COMPARATORS = """
    lte: mai ieftin, mai ieftina, mai ieftine, mai ieftini, mai accesibil, mai accesibila
    gte: mai scump, mai scumpa, mai scumpe, mai scumpi
"""
_RELATIVE_COMPARATORS: dict[str, tuple[tuple[str, str], ...]] = {}


def negation_markers(locale: str | None) -> frozenset[str]:
    """Cuvintele de negație ale locale-i. Locale necunoscută → mulțimea goală: orice `avoid`
    coboară la `implicit` (soft), deci nicio negație ghicită nu exclude produse (P11)."""
    if not locale:
        return frozenset()
    return _NEGATION_MARKERS.get(locale.split("-")[0].lower(), frozenset())


def relative_comparators(locale: str | None) -> tuple[tuple[str, str], ...]:
    """`(frază, op)` pentru comparatorii relativi (`lte` / `gte`), cei mai lungi întâi."""
    return _table(_RELATIVE_COMPARATORS, {"ro": _RO_RELATIVE_COMPARATORS}, locale)


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
    tokens = _tokens(query)
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
    românească și nici nu poate introduce sintaxă.

    De la trei termeni în sus, relaxarea cere PERECHI: `(a ȘI b) SAU (a ȘI c) SAU (b ȘI c)`.
    Măsurat pe catalogul SOLE: cu SAU pe termeni singulari, „sampoon anti matreata" (strict: 0)
    urca pe locul 2 un aparat anti-îmbătrânire epuizat, fiindcă „anti" e un prefix care apare în
    greutatea maximă a sute de nume și `ts_rank_cd` nu răsplătește suficient numărul de termeni
    potriviți. O pereche cere ca DOUĂ dintre cuvintele clientului să fie pe același produs — „nu
    am tot, dar am ce contează" —, iar SAU-ul pe termeni singulari rămâne treapta următoare
    (`relaxed_query_any`), ca relaxarea să nu producă zerouri noi (P6). Spațiul leagă cu ȘI în
    sintaxa websearch, iar `&` are prioritate peste `|`, deci perechile nu au nevoie de
    paranteze."""
    if len(terms) >= 3:
        return " or ".join(f"{a} {b}" for a, b in combinations(terms, 2))
    return " or ".join(terms)


def relaxed_query_any(terms: list[str]) -> str:
    """SAU pe termeni singulari — ultima treaptă de text înaintea plasei de typo. E vechea
    `relaxed_query`, păstrată ca treaptă separată: mai permisivă decât perechile, dar tot o
    potrivire lexem cu lexem, deci mai precisă decât `word_similarity` pe nume."""
    return " or ".join(terms)
