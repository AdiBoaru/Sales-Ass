"""Plierea diacriticelor — UN singur producător pentru cele două capete ale potrivirii.

## De ce există fișierul ăsta

Potrivirea lexicală are două capete: textul INDEXAT (`products.search_tsv`, coloană generată peste
`ro_unaccent(...)`, migrarea 033) și TERMENUL cerut, normalizat în Python. Dacă cele două capete
normalizează diferit, potrivirea pur și simplu nu se produce — și nu se produce TĂCUT: un termen
care nu se rezolvă nu ajunge niciodată într-un `WHERE`, deci filtrul nu rulează, iar răspunsul e
plauzibil și greșit. Nimic din aval nu prinde asta: validatorul (stagiul 8) și `grounding_guard`
(NX-240) sunt porți de ADEVĂR, nu de POTRIVIRE.

Defectul ăsta s-a manifestat deja o dată (2026-09-16, #376): tokenizatorul vocabularului despărțea
intrările pe `-`, dar termenul cerut nu, deci «ingrijirea-parului» ieșea `UNKNOWN` iar filtrul de
raft nu rula. S-a reparat atunci UN tokenizator. Auditul de după a găsit însă că aceeași plierea de
diacritice era REIMPLEMENTATĂ în patru locuri, trei dintre ele cu docstring care promitea
„aceeași normalizare ca `ro_unaccent`" fără ca nimic să verifice promisiunea. Măsurat pe funcții:
divergeau pe orice diacritic care nu e românesc (`L'Oréal`, `Rêve`, `Müller`).

Cauza nu era tokenizatorul, era forma: **un adevăr al sistemului fusese RESCRIS în loc să fie
CITIT**. O rescriere e corectă în ziua în care o scrii și divergează tăcut după. De aceea adevărul
are acum un singur producător, iar `tests/test_single_producer_guard.py` refuză mecanic al doilea.

## DOUĂ adevăruri, nu unul

Poarta mecanică a găsit 19 plieri în `src/`, nu cele 4 pe care le găsise cititul cu ochiul. Dar ele
nu sunt toate același lucru, iar a le unifica pe toate ar fi greșeala simetrică — la fel de
generală și la fel de greșită (lecția NX-295: un mecanism care pare general poate fi greșit tocmai
prin generalitate).

**`fold` — cheia de potrivire cu CATALOGUL.** Pliază exact cele șapte caractere românești, fiindcă
asta face `ro_unaccent` în SQL, iar celălalt capăt al potrivirii e o coloană generată și indexată.
Python-ul se aliniază la SQL, nu invers. Orice text care ajunge confruntat cu `search_tsv` trece
pe aici.

**`fold_text` — comparația internă de TEXT.** Pliază ORICE diacritic (NFKD), fiindcă aici nu există
un al doilea capăt stocat în DB: comparăm textul clientului cu vocabulare din cod, chei de cache,
nume afișate, termeni de siguranță. Aici a plia mai mult e strict mai bine — un diacritic
nepliat ar rata o potrivire pe care o vrem.

Distincția nu e stilistică: pe `L'Oréal`, `fold` dă `l'oréal` și `fold_text` dă `l'oreal`. Cine
alege greșit produce exact defectul de la care a pornit fișierul ăsta, doar în cealaltă direcție.
Regula de ales, într-o propoziție: **dacă rezultatul ajunge într-un `WHERE` peste catalog, e
`fold`; dacă e comparat cu ceva din cod, e `fold_text`.**

## Ce NU e aici

Un slug ASCII (`sole_source.slugify`) e un al treilea adevăr — identificator de URL, nu cheie de
comparație. Plierile care nu sunt niciuna dintre cele două se declară în `DISTINCT_FOLDINGS`, cu
motiv; poarta le cere DECLARATE, nu interzise.
"""

from __future__ import annotations

import unicodedata

#: Oglinda EXACTĂ a lui `ro_unaccent(txt)` din migrarea 033:
#: `translate(lower(txt), 'ăâîșțşţ', 'aaistst')`.
#:
#: Deliberat NU e `unicodedata.normalize("NFKD", ...)` + strip combining. NFKD pliază ORICE
#: diacritic (`é → e`), pe când `ro_unaccent` pliază exact șapte caractere românești. Pe un catalog
#: cu nume internaționale cele două dau rezultate diferite, iar capătul care contează — coloana
#: generată din DB — e `ro_unaccent`. Python-ul se aliniază la SQL, nu invers: `search_tsv` e
#: stocată și indexată, deci ea e definiția.
#:
#: Include formele cu sedilă (`ş`/`ţ`, U+015F/U+0163), care apar în text tastat din surse vechi —
#: distincte în Unicode de formele corecte cu virgulă (`ș`/`ț`, U+0219/U+021B).
_RO_FOLD = str.maketrans("ăâîșțşţ", "aaistst")


def fold(text: str) -> str:
    """`lower` + pliere de diacritice RO. Oglinda lui `ro_unaccent(text)` din 033.

    Singurul producător al acestui adevăr. Orice alt loc din `src/` care are nevoie de cheia de
    potrivire îl IMPORTĂ; nu îl rescrie. Echivalența cu SQL-ul e pinuită în
    `tests/test_query_terms.py::test_fold_oglindeste_ro_unaccent`, iar unicitatea producătorului în
    `tests/test_single_producer_guard.py`.
    """
    return text.lower().translate(_RO_FOLD)


def strip_diacritics(text: str) -> str:
    """Scoate ORICE semn diacritic, fără să schimbe registrul literelor.

    Primitiva pe care o compun `fold_text` și apelanții care au nevoie de pliere fără `lower`
    (scanarea de siguranță a memoriei, unde majusculele poartă informație).
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fold_text(text: str) -> str:
    """`lower` + fără NICIUN diacritic. Cheia de comparație a textului cu vocabulare din COD.

    Al doilea producător. NU e `fold`: aici nu există un capăt stocat în DB cu care ar putea
    diverge, deci plierea are voie — și e mai bine — să fie completă. Vezi docstring-ul modulului
    pentru regula de ales între cele două.
    """
    return strip_diacritics(text.lower())


#: Plieri de diacritice care nu sunt niciunul dintre cei doi producători, deci au voie să difere.
#:
#: Cheia e `"<cale relativă din repo>::<nume funcție>"`. Poarta mecanică cere ca fiecare pliere
#: găsită în `src/` să fie ori delegare către `fold`, ori o intrare aici — și, simetric, ca fiecare
#: intrare de aici să corespundă unei funcții care chiar pliază. O excepție rămasă în urmă e la fel
#: de periculoasă ca una nedeclarată: amândouă spun că sistemul e într-o stare în care nu e.
#:
#: Lista se COMPLETEAZĂ cu motiv, nu se extinde ca să treacă poarta. Dacă motivul e „așa e mai
#: simplu", adevărul e același și locul e `fold`.
DISTINCT_FOLDINGS: dict[str, str] = {
    "src/catalog/sole_source.py::slugify": (
        "Slug ASCII pentru URL și SKU, nu cheie de potrivire. Rezultatul nu se confruntă niciodată "
        "cu `search_tsv`, deci nu există al doilea capăt cu care ar putea diverge. Aici plierea "
        "TREBUIE să fie mai agresivă decât `ro_unaccent`: un nume cu `é` are nevoie de slug ASCII, "
        "iar `ro_unaccent` l-ar lăsa neatins și ar produce un slug non-ASCII."
    ),
}
