"""Vocabularul SERVABIL al catalogului — DESCOPERIT din date, niciodată numit în cod.

Motivul pentru care există modulul: până acum, cuvintele pe care sistemul le punea în `WHERE`
veneau din trei surse care trebuiau să coincidă, dar pe care nimic nu le lega — ce e în catalog,
ce anunță promptul, și ce traduce harta de sinonime. Un re-seed de catalog le desincroniza tăcut,
fiindcă **un filtru pe un token care nu există în niciun rând întoarce exact același rezultat ca
un filtru pe un token real fără potriviri: zero.** Zero era apoi citit ca un fapt despre catalog
(„n-avem"), când era de fapt un fapt despre interogare („am vorbit limbi diferite").

Invarianta pe care o impune modulul:

    **Nimic nu devine constrângere înainte să fie găsit în catalog, cu produse în spate.**

Și, la fel de important, regula care o face să reziste pe ORICE client:

    **Nicio dimensiune de vocabular nu e numită în cod.**

`load_vocabulary` nu știe ce e un „concern". Descoperă cheile din `products.attributes` așa cum
sunt ele în catalogul tenantului, cu valorile și frecvențele lor reale. Un magazin de cosmetice va
produce `concerns`/`finish`; unul de HVAC `agent_frigorific`/`clasa_energetica`; unul auto
`compatibil_cu`. Onboarding-ul unui client nou nu are pas de „configurare a vocabularului",
fiindcă nu există vocabular de configurat — există doar catalogul lui.

Verdictul rezolvării e TRI-STATE: `KNOWN` (aplică-l), `AMBIGUOUS` (constrânge pe uniune și/sau
întreabă), `UNKNOWN` (nu-l aplica NICIODATĂ ca filtru — raportează-l). `Resolution` refuză
structural să existe în starea `KNOWN` fără `count > 0` (`__post_init__`): dovada nu e un câmp
opțional pe care cineva uită să-l verifice, e condiția de construcție.

Overlay-ul de limbă per tenant („piele uscată" → „ten uscat") rămâne posibil pentru ORICE
dimensiune — e cunoaștere despre limbă, nu despre client — dar e VALIDAT la fiecare rezolvare:
o țintă care nu se află în vocabular nu produce un filtru, produce `UNKNOWN` cu motivul
`overlay_target_dead`. Exact acest caz a rulat nedetectat cinci săptămâni, fiindcă vechea
verificare compara cu HARTA, nu cu DATELE.

Totul e pur (fără I/O) în afară de `load_vocabulary`. Tenant-scoped peste tot (P7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from src.domain.normalize import normalize as _norm

if TYPE_CHECKING:  # pragma: no cover — doar pentru tipuri
    import asyncpg

__all__ = [
    "CATEGORY_DIMENSION",
    "CatalogVocabulary",
    "Resolution",
    "ResolutionStatus",
    "VocabEntry",
    "load_vocabulary",
    "resolve",
    "resolve_any",
]

# Numele REZERVAT al dimensiunii structurale. Categoriile nu vin din `attributes`, ci din tabelul
# `categories` (au arbore, deci au subarbore de numărat) — singura dimensiune despre care codul
# știe ceva, și doar pentru că e o relație, nu un vocabular.
CATEGORY_DIMENSION = "category"

# Câți candidați raportăm la `AMBIGUOUS`: lista e pentru o întrebare pusă unui om, iar o întrebare
# cu 30 de opțiuni nu e o întrebare.
_MAX_CANDIDATES = 8

# --- garduri de cardinalitate (generice, nu praguri ghicite pe un catalog anume) ---------------
# O cheie de `attributes` e VOCABULAR doar dacă valorile ei se REPETĂ între produse. Dacă aproape
# fiecare produs are altă valoare, cheia e un identificator sau text liber (SKU, descriere), nu
# ceva ce un client ar cere pe nume — iar transformarea ei în vocabular ar umple promptul cu zgomot
# și ar da rezoluții false. Pragurile sunt RELATIVE la mărimea catalogului, nu absolute.
_MIN_VALUE_SUPPORT = 2  # o valoare cu un singur produs nu e o categorie de cerere
_MAX_DISTINCT_RATIO = 0.5  # >50% valori distincte din produsele care au cheia ⇒ identificator
_MAX_VALUES_PER_DIMENSION = 200  # plafon dur, ca un catalog patologic să nu explodeze memoria
_MAX_VALUE_LEN = 60  # o „valoare" mai lungă de-atât e o propoziție, nu un termen de vocabular
# Un TERMEN de vocabular e scurt („dry", „acid hialuronic"); o PROZĂ generată e lungă („cine vrea
# un finish luminos", „Oferă hidratare și confort pielii."). Distincția contează pentru că proza
# conține cuvinte comune care produc potriviri false: o cheie cu descrieri care conțin „ten" ar
# fura orice cerere despre ten. Testul e pe MEDIA cuvintelor, nu pe o listă de chei interzise.
_MAX_MEAN_WORDS = 3.0
# O intrare de UN SINGUR cuvânt trebuie potrivită EXACT. Altfel „uscat" (o valoare de `hair_type`)
# ar fi subset al lui „ten uscat" și ar rezolva o cerere de îngrijire a tenului la produse de păr —
# măsurat pe catalogul real, nu ipotetic.
_MIN_WORDS_FOR_SUBSET = 2


class ResolutionStatus(str, Enum):
    """Verdictul rezolvării. Trei stări, nu două — „nu știu ce e cuvântul ăsta" și „știu, dar sunt
    mai multe variante" cer acțiuni diferite: prima lărgește, a doua întreabă."""

    KNOWN = "known"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class VocabEntry:
    """O intrare de vocabular: cheia REALĂ din date + eticheta afișabilă + DOVADA (câte produse
    servabile o susțin). `count` nu e statistică, e criteriu de existență: o intrare cu 0 produse
    nu intră niciodată în vocabular, deci nu poate fi oferită nici modelului, nici clientului."""

    key: str
    label: str
    count: int
    # Doar pentru categorii: calea materializată, ca rezolvarea să prefere nodul cel mai specific
    # (o frunză bate o rădăcină la aceeași potrivire textuală). Gol pentru dimensiunile din
    # `attributes`, care sunt plate.
    path: str = ""

    @property
    def depth(self) -> int:
        return self.path.count("/") if self.path else 0


@dataclass(frozen=True, slots=True)
class CatalogVocabulary:
    """Ce poate servi ACUM tenantul, derivat din catalogul lui. Construit doar de
    `load_vocabulary`.

    `dimensions` e o hartă `nume → intrări`, în care numele vin DIN DATE (cheile lui `attributes`),
    plus `CATEGORY_DIMENSION`. Codul nu presupune nicăieri ce dimensiuni există.

    `business_id` e purtat pe obiect ca vocabularul unui tenant să nu poată fi folosit din greșeală
    la rezolvarea altuia (P7): un cache indexat greșit ar fi o scurgere între clienți, nu un bug de
    relevanță.
    """

    business_id: str
    dimensions: Mapping[str, tuple[VocabEntry, ...]] = field(default_factory=dict)

    @property
    def categories(self) -> tuple[VocabEntry, ...]:
        return self.dimensions.get(CATEGORY_DIMENSION, ())

    @property
    def facet_names(self) -> tuple[str, ...]:
        """Dimensiunile descoperite în `attributes`, fără cea structurală. Ordine deterministă."""
        return tuple(sorted(k for k in self.dimensions if k != CATEGORY_DIMENSION))

    def entries(self, dimension: str) -> tuple[VocabEntry, ...]:
        return self.dimensions.get(dimension, ())

    def servable_category_labels(self) -> tuple[str, ...]:
        """Etichetele TUTUROR categoriilor servabile — exact ce are voie să anunțe promptul.

        Regula, în forma ei minimă: **nu anunța ce nu poți servi.** Înainte, promptul lista tot
        tabelul `categories`, iar modelul alegea cuminte un raft gol pe care i-l arătasem noi.
        Se dau toate nivelurile, nu doar rădăcinile: un model care poate numi «Creme hidratante»
        nu mai trebuie să ghicească un părinte, iar o cerere de cremă nu mai poate ateriza pe măști.
        """
        return tuple(sorted({e.label for e in self.categories}))

    def is_empty(self) -> bool:
        return not any(self.dimensions.values())


@dataclass(frozen=True, slots=True)
class Resolution:
    """Rezultatul traducerii unui termen liber într-o cheie reală de catalog.

    Contractul care omoară clasa de bug: **`KNOWN` nu poate exista fără `count > 0`.** Nu e o
    convenție pe care apelantul trebuie s-o verifice, e o condiție de construcție — un token mort
    nu are cum să iasă de aici arătând ca unul valid.
    """

    status: ResolutionStatus
    term: str
    dimension: str
    key: str | None = None
    count: int = 0
    candidates: tuple[VocabEntry, ...] = ()
    # Cum s-a potrivit — pentru observabilitate: `overlay` masiv înseamnă că vocabularul natural al
    # clienților nu seamănă cu al catalogului, adică o problemă de CONȚINUT, nu de cod.
    matched_by: str = "none"
    # De ce nu s-a rezolvat: `not_in_vocabulary` (nimeni nu-l cunoaște) vs `overlay_target_dead`
    # (harta îl traduce, dar în ceva ce nu există) — al doilea e drift de configurare, se repară
    # altfel decât un gol de vocabular.
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status is ResolutionStatus.KNOWN:
            if not self.key:
                raise ValueError("Resolution KNOWN fără cheie")
            if self.count <= 0:
                raise ValueError(
                    f"Resolution KNOWN fără dovadă: {self.key!r} are {self.count} produse. "
                    "Un token fără produse în spate nu e o potrivire, e un filtru care golește."
                )
        if self.status is ResolutionStatus.AMBIGUOUS and not self.candidates:
            raise ValueError("Resolution AMBIGUOUS fără candidați")

    @property
    def constraint_keys(self) -> tuple[str, ...]:
        """Cheile care au voie să intre într-un `WHERE`.

        `KNOWN` → una. `UNKNOWN` → NICIUNA (aici murea sistemul înainte: aplica un token pe care
        nu-l cunoștea nimeni și primea zero).

        `AMBIGUOUS` → **toate candidatele**, și asta e alegerea de design care contează. „Cremă"
        rămâne o cerere de cremă chiar dacă nu știm care fel: constrângerea pe uniunea familiilor
        de creme exclude măștile, în timp ce renunțarea la constrângere le-ar lăsa să câștige pe
        text (o mască ce scrie «Textură cremă» în descriere se potrivește la fel de bine). Uniunea
        NU poate fi goală — fiecare candidat are produse în spate prin construcție — deci
        constrângerea rămâne sigură. Îngustarea mai departe e treaba unei întrebări, nu a unui
        filtru care ghicește.
        """
        if self.status is ResolutionStatus.KNOWN and self.key:
            return (self.key,)
        if self.status is ResolutionStatus.AMBIGUOUS:
            return tuple(c.key for c in self.candidates)
        return ()

    @property
    def evidence(self) -> int:
        """Câte produse susțin rezoluția (suma candidaților, la `AMBIGUOUS`). Zero la `UNKNOWN`."""
        if self.status is ResolutionStatus.AMBIGUOUS:
            return sum(c.count for c in self.candidates)
        return self.count


# --- descoperirea vocabularului din catalog -----------------------------------

# Produsele „servabile" = exact ce poate ajunge în fața clientului. Dacă definiția se schimbă, se
# schimbă AICI, într-un loc — altfel vocabularul ar promite altceva decât întoarce căutarea, adică
# fix desincronizarea pe care modulul o previne.
_SERVABLE = "p.status = 'active'"

# Categoriile: singura dimensiune cu arbore, deci singura care se numără pe SUBARBORE (o cerere pe
# «Îngrijirea tenului» trebuie să vadă produsele din «Creme hidratante»).
_CATEGORY_SQL = f"""
select c.slug,
       c.name,
       c.path,
       (select count(*)
          from products p
         where p.business_id = c.business_id
           and {_SERVABLE}
           and exists (select 1
                         from categories sub
                        where sub.business_id = c.business_id
                          and (sub.id = c.id or sub.path like c.path || '/%')
                          and (sub.id = p.primary_category_id
                               or exists (select 1
                                            from product_category_map m
                                           where m.product_id = p.id
                                             and m.category_id = sub.id)))) as n
  from categories c
 where c.business_id = $1
 order by c.path
"""

# Dimensiunile din `attributes`: DESCOPERITE. Nicio cheie nu e numită aici — se expandează orice
# cheie a cărei valoare e text sau listă de text. Numericele/booleenele nu sunt vocabular (nu ceri
# „produse cu 49.99"), ele sunt intervale — alt mecanism, altă poveste.
_ATTRIBUTE_SQL = f"""
select kv.key as dimension,
       e.elem  as value,
       count(*) as n
  from products p
 cross join lateral jsonb_each(coalesce(p.attributes, '{{}}'::jsonb)) kv
 cross join lateral (
        select jsonb_array_elements_text(kv.value) as elem
         where jsonb_typeof(kv.value) = 'array'
        union all
        select kv.value #>> '{{}}' as elem
         where jsonb_typeof(kv.value) = 'string'
       ) e
 where p.business_id = $1
   and {_SERVABLE}
   and e.elem is not null
   and length(e.elem) between 1 and {_MAX_VALUE_LEN}
 group by 1, 2
 order by 1, 3 desc, 2
"""


def _keep_dimension(values: list[tuple[str, int]]) -> bool:
    """E cheia asta VOCABULAR, sau un identificator/o proză deghizată?

    Două teste, ambele structurale — nicio listă de chei permise, deci funcționează pe un catalog
    de pompe de căldură la fel ca pe unul de cosmetice:

    1. **Se repetă valorile?** Dacă aproape fiecare produs are altă valoare, cheia e un SKU sau un
       text liber, iar rezolvarea pe ea ar produce potriviri false.
    2. **Sunt valorile TERMENI, nu propoziții?** O cheie ale cărei valori sună a frază („cine vrea
       un finish luminos") conține cuvinte comune care fură cereri: măsurat pe catalogul demo,
       cheia `best_for` capta orice întrebare care conținea cuvântul „ten".
    """
    if not values:
        return False
    total = sum(n for _, n in values)
    if total <= 0 or (len(values) / total) > _MAX_DISTINCT_RATIO:
        return False
    mean_words = sum(len(v.split()) for v, _ in values) / len(values)
    return mean_words <= _MAX_MEAN_WORDS


async def load_vocabulary(conn: asyncpg.Connection, business_id: str) -> CatalogVocabulary:
    """Descoperă vocabularul servabil al tenantului din catalogul lui. Două interogări,
    tenant-scoped.

    Nimic din ce urmează nu presupune un vertical: categoriile vin din arbore, restul dimensiunilor
    din cheile reale ale lui `attributes`. Intrările fără produse sunt eliminate AICI, o dată — nu
    la fiecare consumator, unde cineva ar uita.

    `conn` trebuie să fie deja tenant-scoped. Ordinea e deterministă (`order by` + sortări locale),
    ca prefixul de prompt construit din vocabular să rămână stabil pentru prompt caching.
    """
    cat_rows = await conn.fetch(_CATEGORY_SQL, business_id)
    attr_rows = await conn.fetch(_ATTRIBUTE_SQL, business_id)

    dimensions: dict[str, tuple[VocabEntry, ...]] = {}

    categories = tuple(
        VocabEntry(
            key=r["slug"],
            label=r["name"] or r["slug"],
            count=int(r["n"]),
            path=r["path"] or "",
        )
        for r in cat_rows
        if int(r["n"]) > 0
    )
    if categories:
        dimensions[CATEGORY_DIMENSION] = categories

    raw: dict[str, list[tuple[str, int]]] = {}
    for r in attr_rows:
        n = int(r["n"])
        if n < _MIN_VALUE_SUPPORT:
            continue
        raw.setdefault(str(r["dimension"]), []).append((str(r["value"]), n))

    for name, values in raw.items():
        if name == CATEGORY_DIMENSION or not _keep_dimension(values):
            continue
        top = sorted(values, key=lambda kv: (-kv[1], kv[0]))[:_MAX_VALUES_PER_DIMENSION]
        dimensions[name] = tuple(VocabEntry(key=v, label=v, count=n) for v, n in top)

    return CatalogVocabulary(business_id=business_id, dimensions=dimensions)


# --- rezolvarea unui termen liber --------------------------------------------


def _index(entries: tuple[VocabEntry, ...]) -> dict[str, list[VocabEntry]]:
    """Index normalizat cheie/etichetă → intrări. O etichetă poate apărea de două ori (taxonomii
    duplicate), de aceea valoarea e listă: două potriviri egale înseamnă AMBIGUU, nu «prima»."""
    idx: dict[str, list[VocabEntry]] = {}
    for e in entries:
        for token in {_norm(e.key), _norm(e.label)}:
            if token:
                idx.setdefault(token, []).append(e)
    return idx


def _words(entry: VocabEntry) -> set[str]:
    """Cuvintele unei intrări, din cheie ȘI etichetă (slug-urile despart cu `-`, numele cu spații).
    Fără liste de stopwords: potrivirea cere cuvinte întregi, în ambele sensuri."""
    return {w for w in _norm(entry.label).split() if w} | {
        w for w in _norm(entry.key).replace("-", " ").replace("_", " ").split() if w
    }


def _best(entries: list[VocabEntry]) -> list[VocabEntry]:
    """Dintre potriviri egale textual, preferă nodul cel mai SPECIFIC (adâncime mare), iar la
    egalitate pe cel cu mai multe produse. Determinist: tie-break final pe cheie."""
    return sorted(entries, key=lambda e: (-e.depth, -e.count, e.key))


def resolve(
    vocab: CatalogVocabulary,
    term: str,
    dimension: str,
    *,
    overlay: Mapping[str, str] | None = None,
) -> Resolution:
    """Termen liber → cheie REALĂ din dimensiunea cerută, cu verdict tri-state și dovadă.

    Trepte, de la sigur la permisiv, oprire la prima care produce ceva: cheie/etichetă exactă
    (normalizat) → overlay de limbă VALIDAT contra vocabularului → cuvinte întregi (subset în
    oricare sens). Nu există treaptă „aproximativ": o potrivire slabă devenită filtru dur e exact
    greșeala pe care modulul o previne. (Treapta semantică prin embeddings se adaugă aici, fără să
    schimbe contractul — un candidat în plus, aceleași verdicte.)

    `overlay` e opțional și aparține TENANTULUI (mapare de limbă, ex. „piele uscată" → „ten
    uscat"). Nu e crezut pe cuvânt: ținta lui trebuie să existe în vocabular, altfel iese
    `UNKNOWN(overlay_target_dead)` — nu un filtru care golește.

    Pur, determinist, fără I/O.
    """
    norm = _norm(term or "")
    if not norm or not any(ch.isalnum() for ch in norm):
        return Resolution(
            status=ResolutionStatus.UNKNOWN, term=norm, dimension=dimension, reason="empty_term"
        )

    entries = vocab.entries(dimension)
    if not entries:
        # Dimensiune inexistentă la acest tenant = n-avem cu ce compara. NU e „termen greșit", și
        # mai ales nu e un filtru: ar goli orice căutare.
        return Resolution(
            status=ResolutionStatus.UNKNOWN,
            term=norm,
            dimension=dimension,
            reason="unknown_dimension",
        )

    idx = _index(entries)

    if hits := idx.get(norm):
        return _from_hits(_best(hits), norm, dimension, "exact")

    if overlay:
        target = overlay.get(norm)
        if target is not None:
            if hits := idx.get(_norm(target)):
                return _from_hits(_best(hits), norm, dimension, "overlay")
            # Harta traduce într-un cuvânt pe care catalogul nu-l are. Cinci săptămâni de tăcere
            # au început exact aici — de-asta e verdict raportabil, nu o cădere pe ramura „nimic".
            return Resolution(
                status=ResolutionStatus.UNKNOWN,
                term=norm,
                dimension=dimension,
                matched_by="overlay",
                reason="overlay_target_dead",
            )

    # Potrivire pe cuvinte întregi, în ambele sensuri — dar o intrare de UN cuvânt nu are voie să
    # fie „inclusă" într-o cerere mai lungă: ar însemna că un singur cuvânt comun decide dimensiunea
    # întregii cereri (vezi `_MIN_WORDS_FOR_SUBSET`). Un cuvânt se potrivește exact, sau deloc.
    words = {w for w in norm.split() if w}
    subset = []
    for e in entries:
        e_words = _words(e)
        if not words or not e_words:
            continue
        covers_query = words <= e_words
        covered_by_query = e_words <= words and len(e_words) >= _MIN_WORDS_FOR_SUBSET
        if covers_query or covered_by_query:
            subset.append(e)
    if subset:
        return _from_hits(_best(subset), norm, dimension, "tokens")

    return Resolution(
        status=ResolutionStatus.UNKNOWN,
        term=norm,
        dimension=dimension,
        reason="not_in_vocabulary",
    )


def resolve_any(
    vocab: CatalogVocabulary,
    term: str,
    *,
    overlays: Mapping[str, Mapping[str, str]] | None = None,
) -> Resolution:
    """Ca `resolve`, dar CAUTĂ DIMENSIUNEA. Apelantul nu trebuie să știe dinainte că „ten uscat" e
    o nevoie și „Creme hidratante" o categorie — catalogul știe, iar asta e tot ce contează.

    E cealaltă jumătate a regulii «nimic numit în cod»: fără ea, cineva ar trebui să eticheteze
    fiecare termen cu dimensiunea lui, adică exact munca manuală per client pe care o eliminăm.
    Câștigă rezoluția cu cea mai bună calitate (`KNOWN` > `AMBIGUOUS`), iar la egalitate cea cu mai
    multă dovadă; tie-break determinist pe numele dimensiunii.
    """
    order = {ResolutionStatus.KNOWN: 0, ResolutionStatus.AMBIGUOUS: 1, ResolutionStatus.UNKNOWN: 2}
    # Când NIMIC nu se rezolvă, motivul cel mai informativ trebuie să supraviețuiască: un
    # `overlay_target_dead` e alarma de drift de configurare, iar dacă l-am înlocui cu un
    # `not_in_vocabulary` alfabetic-întâmplător am reconstrui exact orbirea pe care o reparăm.
    reason_rank = {"overlay_target_dead": 0, "not_in_vocabulary": 1}

    def _key(r: Resolution) -> tuple[int, int, int, str]:
        return (order[r.status], reason_rank.get(r.reason, 2), -r.evidence, r.dimension)

    best: Resolution | None = None
    for name in (CATEGORY_DIMENSION, *vocab.facet_names):
        r = resolve(vocab, term, name, overlay=(overlays or {}).get(name))
        if best is None or _key(r) < _key(best):
            best = r
    return best or Resolution(
        status=ResolutionStatus.UNKNOWN, term=_norm(term or ""), dimension="", reason="no_dimension"
    )


def _from_hits(hits: list[VocabEntry], term: str, dimension: str, matched_by: str) -> Resolution:
    """Una sau mai multe potriviri → `KNOWN` / `AMBIGUOUS`.

    O singură potrivire e un răspuns. Mai multe potriviri **la același nivel de specificitate** sunt
    o întrebare, nu o alegere pe care are voie s-o facă sistemul în locul clientului. Dacă una e
    strict mai specifică decât restul (frunză vs rădăcină), aia câștigă — nu e ambiguitate reală.
    """
    top = hits[0]
    if [e for e in hits[1:] if e.depth == top.depth]:
        return Resolution(
            status=ResolutionStatus.AMBIGUOUS,
            term=term,
            dimension=dimension,
            candidates=tuple(hits[:_MAX_CANDIDATES]),
            matched_by=matched_by,
        )
    return Resolution(
        status=ResolutionStatus.KNOWN,
        term=term,
        dimension=dimension,
        key=top.key,
        count=top.count,
        matched_by=matched_by,
    )
