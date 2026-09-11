"""NX-203 — familii candidate de retrieval, extrase din frazele REALE de căutare ale tenantului.

De ce există scriptul ăsta, și de ce nu reia corpusul vechi: corpusul de la 2026-07 e etichetat pe
`catalog_version='demo-2026-07-22'`, pe `business_id`-ul demo, cu UUID-uri de produs dintr-un
proiect Supabase abandonat din 2026-08-28. Cardul cere explicit ca etichetele să EXPIRE la
schimbarea catalogului; aici s-a schimbat baza de date întreagă. Deci nu reluăm 64 de familii, ci
pornim de la zero pe catalogul real.

Ce câștigăm din asta: SOLE aduce ceva ce demo-ul n-avea. `product_sections.kind =
'recommendation_trigger'` conține ~5 fraze de căutare per produs, scrise ca de client (diacritice
inconsecvente, colocvial, cu greșeli) — 12.665 în total. Sunt materie primă pentru intenții REALE,
nu parafraze inventate, exact ce cere `provenance` din card.

UNITATEA e familia, nu fraza (contractul schimbat 2026-07-31): `run_benchmark` face media ÎN
familie și macro peste familii, deci rezoluția metricii crește cu numărul de contracte de adevăr
distincte, nu cu numărul de formulări. Două fraze care trebuie să întoarcă ACELAȘI set cu ACELEAȘI
constrângeri sunt o singură familie, oricâte diacritice le despart.

Cheia de familie se derivă DETERMINIST, din catalog, nu din intuiție:

    familie = (tip canonic de produs)  ×  (valorile de fațetă rostite în frază)

Tipul vine din `src/catalog/product_type.py` — aceleași două reguli GRAMATICALE care au produs cele
54 de chei ale catalogului, aplicate pe frază în loc de pe nume. Fațetele vin din vocabularul
DESCOPERIT al tenantului (`load_vocabulary`), nu dintr-o listă scrisă de mână: pe alt vertical
scriptul găsește alte dimensiuni, fără nicio schimbare de cod.

Produsul din a cărui fișă vine fraza se REȚINE, dar NU ca etichetă de relevanță: e o afirmație a
comerciantului, nu o judecată verificată. Valoarea lui e la construcția pool-ului de candidați
(pasul următor), unde acoperă exact cazul pe care pooling-ul clasic îl ratează — produsele
relevante pe care retrievalul NOSTRU nu le găsește. Un pool construit doar din ce întoarce motorul
măsoară re-rankingul, nu recall-ul.

Nimic din ce scrie aici nu e `human_verified`. Fișierul rezultat e input pentru etichetare.

    python scripts/nx203_extract_families.py --business <uuid>              # raport, nu scrie
    python scripts/nx203_extract_families.py --business <uuid> --sample 20
    python scripts/nx203_extract_families.py --business <uuid> --write      # scrie draftul
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from src.catalog.product_type import fold  # noqa: E402
from src.catalog.query_terms import content_terms  # noqa: E402
from src.catalog.vocabulary import load_vocabulary  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

OUT_DIR = ROOT / "tests" / "golden" / "nx203"
REPORT_DIR = ROOT / "reports"

#: Linia de frază din corpul secțiunii: `• „...”`. Ghilimelele sunt cele românești, din import.
_PHRASE_RE = re.compile(r"[•\-\*]?\s*[„\"]([^”\"]{3,120})[”\"]")

#: Un token de mărime („50 ml", „100g") e semnalul cel mai curat că fraza e o căutare de PRODUS
#: ANUME, nu de raft. Împreună cu un brand din catalog, clasifică fraza ca `exact`.
_SIZE_RE = re.compile(r"\b\d{1,4}\s?(ml|l|g|gr|kg|buc|bucati|capsule|comprimate)\b")

#: Negațiile care schimbă contractul de adevăr al cererii („fără parfum" INTERZICE, nu preferă).
#: Sunt cuvinte funcționale de limbă, nu de cosmetice — se mută cu `locale`, nu cu verticalul.
_NEGATION_RE = re.compile(r"\b(fara|fär|nu\s+(vreau|contine|are|usuca)|non|free)\b")

#: Cuvintele care ÎNCHID domeniul unei negații, fiindcă deschid o afirmație nouă: în „fara parabeni
#: cu acid hialuronic", acidul e cerut, nu interzis. Fără regula asta, negația s-ar întinde până la
#: capătul frazei și ar interzice exact ce cere clientul. „si"/„sau" NU închid: în „fara parabeni si
#: sulfati" enumerarea continuă negația, iar oprirea acolo ar lăsa al doilea termen ca o CERINȚĂ.
_NEGATION_STOP = frozenset({"cu", "pentru", "care", "plus", "dar"})

#: Câte cuvinte de conținut poate acoperi o negație. „fara parabeni, sulfati si siliconi" are trei;
#: peste patru, aproape sigur am depășit clauza și interzicem lucruri nespuse.
_NEGATION_SPAN = 4

#: Conectorii dintr-o enumerare negată. Nu sunt termeni și nu consumă din domeniu.
_NEGATION_CONNECTORS = frozenset({"si", "sau", "ori", "de", "la", "in"})

#: Marcatorii înșiși. Nu sunt termeni de conținut: negația trăiește în prefixul `!` de pe cuvântul
#: negat, nu în cuvântul „fara".
_NEGATION_MARKERS = frozenset({"fara", "non", "free", "nu"})

#: Sub atâtea produse în spate, o valoare de fațetă e prea rară ca să ancoreze o familie: am
#: eticheta un caz pe care catalogul nu-l poate servi nici în cel mai bun scenariu.
_MIN_FACET_SUPPORT = 3

#: O familie susținută de o singură frază e o formulare, nu un contract de adevăr. Pragul ține
#: coada lungă în afara setului de etichetat, unde ar consuma timp uman fără să crească rezoluția.
_MIN_FAMILY_PHRASES = 2

#: Cea mai lungă valoare de vocabular (sau brand) pe care o căutăm în frază, în cuvinte. Peste
#: patru, n-gramele explodează fără să prindă nimic: valorile reale sunt „piele uscata",
#: „acid hialuronic", nu propoziții — `_keep_dimension` le-a eliminat deja pe cele care sunt.
_MAX_FACET_WORDS = 4


#: Dimensiunea care poartă TIPUL de produs. E o fațetă ca oricare alta în `attributes`
#: (scrisă de `derive_product_type.py` pe 2.088 de produse), dar are un rol special aici: fără
#: ea, familia n-are margini. „Orice conține acid hialuronic" nu e un contract de adevăr — 311
#: produse îl satisfac, iar un top-6 din 311 nu poate fi nici corect, nici greșit.
_TYPE_DIMENSION = "product_type"

#: Sub atâta suprapunere lexicală între formulările unei familii, gruparea e suspectă: fațetele
#: tenantului n-au fost destul de fine ca să deosebească două cereri diferite. Familia nu se aruncă
#: (golul e informație) și nici nu se exclude — pleacă la om MARCATĂ.
#:
#: Pragul e ales pentru RECALL, nu pentru precizie, fiindcă cele două greșeli nu costă la fel: o
#: fuziune ratată intră în corpus ca dată bună și strică măsurătoarea tăcut, în timp ce un semnal
#: fals costă o privire, iar decizia rămâne oricum a omului. Măsurat pe cele 307 familii ne-exacte,
#: 0,35 marchează 13%; în zona 0,29-0,33 stau amestecate cazuri reale („ruj lucios" vs „ruj mat")
#: și parafraze adevărate („balsam ... păr gros" vs „balsam ... pentru par"), deci un prag mai jos
#: le-ar pierde pe primele.
_MIN_MERGE_OVERLAP = 0.35

#: Un termen rezidual e o AXĂ de nevoie doar dacă CATALOGUL îl cunoaște — apare într-un nume de
#: produs sau într-o valoare de atribut. Prima variantă filtra pe frecvența în interogări (df ≥ 12)
#: și se autosabota: termenii care deosebesc cel mai bine („Guaiazulene", „Chaga") sunt tocmai cei
#: rari, deci cădeau, iar toate cererile de ingredient rar se fuzionau în „cremă de față" — trei
#: contracte diferite sub o singură familie. Ancorarea în catalog e și mai onestă: dacă termenul nu
#: există în marfă, familia n-ar avea cum să fie satisfăcută, deci n-are ce căuta în benchmark.
_MIN_RESIDUAL_DF = 2

#: Plafonul rămâne, dar acum are un singur rol: taie cuvintele prezente aproape peste tot
#: („hidratant"), care nu deosebesc nimic nici măcar când catalogul le cunoaște.
_MAX_RESIDUAL_DF = 1200

#: Câte reziduuri intră în cheia unei familii. Peste două, fiecare frază devine propria familie și
#: n-am mai grupat nimic — am doar redenumit corpusul.
_MAX_RESIDUALS_PER_FAMILY = 2

#: Marcajul unui termen rostit sub negație. Intră în cheia familiei, deci „cu volum" și „fara
#: volum" sunt contracte de adevăr diferite — ceea ce chiar sunt.
NEGATED_PREFIX = "!"


def _norm_words(text: str) -> str:
    """Forma canonică pentru potrivire: pliat, cu punctuația colapsată în spații.

    Trebuie să fie EXACT transformarea din `_ngrams`, altfel un brand ca „DR. KONOPKA'S" ajunge în
    index ca `dr. konopka's` și nu se potrivește niciodată cu n-grama `dr konopka s`. Cele
    două capete ale unei potriviri se normalizează cu aceeași funcție — aceeași lecție ca la
    `ro_unaccent` și migrarea 046.
    """
    return " ".join(w for w in re.split(r"[^a-z0-9]+", fold(text)) if w)


@dataclass
class Phrase:
    """O frază de căutare, așa cum a scris-o comerciantul, plus ce am putut deriva din ea."""

    text: str
    folded: str
    product_id: str
    product_name: str
    product_type: str | None = None
    facets: tuple[tuple[str, str], ...] = ()
    residuals: tuple[str, ...] = ()
    residual_pool: tuple[str, ...] = ()
    forbidden: tuple[tuple[str, str], ...] = ()
    klass: str = "colloquial"

    @property
    def family_key(self) -> str:
        """Contractul de adevăr al frazei, ca șir stabil.

        `exact` primește o familie PER PRODUS: „DR. KONOPKA'S deodorant roll-on 50 ml" nu e o cerere
        de raft, iar a o fuziona cu alte deodorante ar transforma o potrivire exactă într-o
        recomandare — alt contract, altă măsurătoare.
        """
        if self.klass == "exact":
            return f"exact:{self.product_id}"
        facets = ",".join(f"{d}={v}" for d, v in sorted(self.facets) if d != _TYPE_DIMENSION)
        # Reziduurile intră în cheie fiindcă altfel „șampon pentru păr uscat" și „șampon pentru
        # scalp sensibil" sunt aceeași familie: catalogul n-are nicio fațetă pentru păr, deci
        # ambele se reduc la `product_type=sampon`. Un top-6 măsurat pe familia aia n-ar putea fi
        # nici corect,
        # nici greșit. Termenul nerezolvat e tot ce avem ca să deosebim două nevoi — și, în plus,
        # lista lui e chiar inventarul de goluri de vocabular al catalogului.
        residuals = ",".join(sorted(self.residuals))
        forbidden = ",".join(f"!{d}={v}" for d, v in sorted(self.forbidden))
        return f"{self.product_type}|{facets}|{residuals}|{forbidden}"


@dataclass
class Family:
    """Un contract de adevăr distinct: ce ar trebui să întoarcă motorul, indiferent de formulare."""

    key: str
    product_type: str | None
    facets: tuple[tuple[str, str], ...]
    klass: str
    residuals: tuple[str, ...] = ()
    forbidden: tuple[tuple[str, str], ...] = ()
    phrases: list[str] = field(default_factory=list)
    merchant_products: set[str] = field(default_factory=set)
    merge_overlap: float = 1.0
    needs_split: bool = False

    def as_draft(self, catalog_version: str, business_id: str) -> dict:
        return {
            "needs_split": self.needs_split,
            "merge_overlap": self.merge_overlap,
            "family_id": _family_id(self.key),
            "family_key": self.key,
            "business_id": business_id,
            "catalog_version": catalog_version,
            "locale": "ro",
            "provenance": "merchant_content",
            "query_class": self.klass,
            "product_type": self.product_type,
            # Cuvintele pe care clientul le rostește și catalogul nu le poate reprezenta ca fațetă.
            # Ele DEOSEBESC familia, dar nu pot deveni `hard_constraints`: n-au cheie pe care un
            # `WHERE` s-o interogheze. Lista lor, agregată, e inventarul de goluri de vocabular.
            "unresolved_terms": list(self.residuals),
            "hard_constraints": [
                {"facet": d, "op": "contains", "value": v} for d, v in sorted(self.facets)
            ],
            # Cerute EXPLICIT să lipsească. Separate de `hard_constraints` fiindca metricile le
            # trateaza diferit: o cerinta neindeplinita scade relevanta, o interdictie incalcata
            # e o VIOLARE, care se numara aparte (§2 din card).
            "forbidden_constraints": [
                {"facet": d, "op": "not_contains", "value": v} for d, v in sorted(self.forbidden)
            ],
            # Formulările familiei. Prima e reprezentantul pe care îl vede omul la etichetare;
            # restul rămân ca indicator de robusteţe la formă (nu sunt familii separate).
            "queries": sorted(self.phrases, key=lambda p: (len(p), p)),
            # AFIRMAȚIA comerciantului, nu o judecată. Intră în pool-ul de candidați, niciodată
            # direct în `judgments` — altfel am eticheta catalogul cu propriul lui marketing.
            "merchant_asserted_products": sorted(self.merchant_products),
            "human_verified": False,
            "judgments": [],
            "forbidden_products": [],
        }


def _family_id(key: str) -> str:
    """Id scurt și STABIL pentru o cheie de familie: aceeași cheie dă același id între rulări, deci
    o re-extragere nu rescrie manifestul și nu invalidează etichete deja puse."""
    return "f-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


def parse_phrases(body: str) -> list[str]:
    """Frazele dintr-un corp de secțiune `recommendation_trigger`.

    Formatul importat e stabil (un antet, apoi `• „frază”` pe linie), dar parsarea nu se bazează pe
    antet: dacă mâine dispare, frazele tot ies. O frază fără ghilimele NU se ia — ar aduce antetul
    însuși în corpus, iar „Acest produs apare frecvent în recomandări" ar deveni o interogare.
    """
    return [m.group(1).strip() for m in _PHRASE_RE.finditer(body or "") if m.group(1).strip()]


def _facet_index(vocab, brands: set[str]) -> dict[str, list[tuple[str, str]]]:
    """`token de căutare → [(dimensiune, valoare)]`, din vocabularul DESCOPERIT al tenantului.

    Cheia e valoarea pliată (fără diacritice, minuscule), fiindcă frazele reale sunt scrise în
    ambele feluri în aceeași fișă („piele uscată" și „piele uscata"). Fără pliere, jumătate din
    corpus n-ar rezolva nicio fațetă.

    Categoriile sunt EXCLUSE deliberat: ele descriu raftul, iar tipul de produs e deja axa
    principală a familiei. A le include ar fuziona „ser" cu „cremă" sub `ten-ingrijirea-tenului`,
    adică exact defectul pe care fațeta de tip a fost construită să-l repare.
    """
    index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for dim in vocab.facet_names:
        for entry in vocab.entries(dim):
            if entry.count < _MIN_FACET_SUPPORT:
                continue
            token = _norm_words(entry.key)
            # Valorile de un singur caracter sau pur numerice fură fraze întregi („50" din „50 ml").
            if len(token) < 3 or token.isdigit() or token in brands:
                continue
            if len(token.split()) > _MAX_FACET_WORDS:
                continue
            index[token].append((dim, entry.key))
    return index


def negated_words(folded: str) -> set[str]:
    """Cuvintele aflate sub domeniul unei negații.

    Defectul real care a cerut funcția: „balsam pentru volum păr fin" și „balsam pentru par fin fara
    volum" ajungeau în ACEEAȘI familie. Sunt cereri opuse. `volum` era rezidual în amândouă, iar
    negația era doar o etichetă de clasă, nu parte din contractul de adevăr — deci un motor care
    întoarce balsamuri de volum ar fi „corect" pe amândouă.

    Domeniul începe la marcatorul de negație și se închide la primul cuvânt care deschide o
    afirmație nouă (`_NEGATION_STOP`) sau după `_NEGATION_SPAN` cuvinte de conținut. Regula e de
    limbă, nu de vertical: se mută cu `locale`, ca listele de cuvinte goale (P11).
    """
    words = [w for w in re.split(r"[^a-z0-9]+", folded) if w]
    out: set[str] = set()
    i = 0
    while i < len(words):
        if not _NEGATION_RE.fullmatch(words[i]) and words[i] not in _NEGATION_MARKERS:
            i += 1
            continue
        taken = 0
        j = i + 1
        while j < len(words) and taken < _NEGATION_SPAN:
            if words[j] in _NEGATION_STOP:
                break
            # Conectorii nu se neagă și nu consumă din domeniu: „fara parabeni si sulfati si
            # siliconi" are TREI termeni negați, iar numărându-i pe „si" al treilea ar rămâne afară.
            if words[j] not in _NEGATION_CONNECTORS:
                out.add(words[j])
                taken += 1
            j += 1
        i = j if j > i else i + 1
    return out


def _ngrams(folded: str, max_words: int) -> set[str]:
    """N-gramele de cuvinte ale frazei, până la `max_words`.

    Potrivirea se face prin căutare de n-grame în dicționar, nu prin câte un regex per valoare de
    vocabular: cu ~500 de valori și 12.665 de fraze, varianta cu regex face milioane de scanări și
    ține minute. Aici costul e liniar în lungimea frazei, iar rezultatul e identic — n-grama e
    delimitată de cuvinte prin construcție, deci „ten" nu se mai potrivește în „intensiv".
    """
    words = [w for w in re.split(r"[^a-z0-9]+", folded) if w]
    out: set[str] = set()
    for n in range(1, max_words + 1):
        for i in range(len(words) - n + 1):
            out.add(" ".join(words[i : i + n]))
    return out


def classify_phrase(
    phrase: Phrase, facet_index: dict[str, list[tuple[str, str]]], brands: set[str]
) -> None:
    """Umple `product_type`, `facets` și `klass` pe loc. Pur: nicio interogare, nicio inferență."""
    folded = phrase.folded
    grams = _ngrams(folded, _MAX_FACET_WORDS)

    negated = negated_words(folded)

    hits: list[tuple[str, str]] = []
    forbidden: list[tuple[str, str]] = []
    consumed: set[str] = set()
    for gram in grams:
        pairs = facet_index.get(gram)
        if pairs:
            # O valoare rostită sub negație e o INTERDICȚIE, nu o cerință. Pusă în
            # `hard_constraints`, ar cere exact ce clientul a exclus — iar benchmarkul ar număra
            # drept „încălcare" tocmai răspunsul corect.
            if set(gram.split()) & negated:
                forbidden.extend(pairs)
            else:
                hits.extend(pairs)
            consumed.update(gram.split())
        if gram in brands:
            consumed.update(gram.split())
    # O dimensiune contribuie cu o singură valoare: dacă fraza atinge două valori ale aceleiași
    # fațete, cererea e ambiguă, iar a le pune pe amândouă în `hard_constraints` cu ȘI ar construi o
    # familie pe care catalogul n-o poate satisface niciodată.
    by_dim: dict[str, str] = {}
    ambiguous: set[str] = set()
    for dim, value in hits:
        if dim in by_dim and by_dim[dim] != value:
            ambiguous.add(dim)
        by_dim.setdefault(dim, value)
    for dim in ambiguous:
        by_dim.pop(dim, None)
    phrase.facets = tuple(sorted(by_dim.items()))
    # Tipul NU se mai derivă cu `raw_type` din capul frazei. Prima variantă făcea asta și a ieșit
    # `None` pe 12.193 din 12.665 de fraze: `build_vocabulary` construiește harta din NUMELE de
    # catalog, care încep cu brandul („AMLA Șampon…"), deci tipul brut al unui nume nu e cuvântul cu
    # care începe cererea unui client. Fațeta scrisă pe produs (`derive_product_type.py`) e exact
    # puntea care lipsea — și e ACEEAȘI cale pe care o folosește retrievalul, deci familia se
    # exprimă în vocabularul în care se va măsura.
    phrase.product_type = by_dim.get(_TYPE_DIMENSION)

    # Ce a rămas din cerere după ce vocabularul tenantului și-a luat partea. Astea sunt cuvintele pe
    # care clientul le folosește și catalogul nu le poate reprezenta.
    # Reziduul negat poartă prefixul `!`, ca „fara volum" să nu ajungă în aceeași familie cu
    # „pentru volum". Prefixul intră în cheia familiei prin `residuals`, deci separarea e
    # structurală, nu o etichetă pusă alături.
    phrase.residual_pool = tuple(
        (NEGATED_PREFIX + w if w in negated else w)
        for w in content_terms(phrase.text, "ro")
        if w not in consumed
        and len(w) > 2
        and not w.isdigit()
        # Marcatorul de negație nu e un termen. Lăsat înăuntru, „fara" ajungea în cheia familiei
        # lângă `!transfer` și o despărțea de formulări identice care spun „nu are transfer".
        and w not in _NEGATION_MARKERS
        and w not in _NEGATION_CONNECTORS
    )
    phrase.forbidden = tuple(sorted(set(forbidden)))

    refinements = [d for d in by_dim if d != _TYPE_DIMENSION]
    has_brand = bool(grams & brands)
    if has_brand and _SIZE_RE.search(folded):
        phrase.klass = "exact"
    elif _NEGATION_RE.search(folded):
        phrase.klass = "negative"
    elif len(refinements) >= 2:
        phrase.klass = "compound"
    elif has_brand:
        phrase.klass = "brand"
    else:
        phrase.klass = "colloquial"


async def _load(business_id: str):
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        vocab = await load_vocabulary(conn, business_id)
        brand_rows = await conn.fetch(
            "select name from brands where business_id = $1 and name is not null", business_id
        )
        product_rows = await conn.fetch(
            """select p.id::text as id, p.name
                 from products p
                where p.business_id = $1 and p.status = 'active'""",
            business_id,
        )
        section_rows = await conn.fetch(
            """select s.product_id::text as product_id, s.body
                 from product_sections s
                where s.business_id = $1 and s.kind = 'recommendation_trigger'""",
            business_id,
        )
        attr_rows = await conn.fetch(
            """select distinct e.elem as v
                 from products p
                 cross join lateral jsonb_each(coalesce(p.attributes, '{}'::jsonb)) kv
                 cross join lateral (
                        select jsonb_array_elements_text(kv.value) as elem
                         where jsonb_typeof(kv.value) = 'array'
                        union all
                        select kv.value #>> '{}' as elem
                         where jsonb_typeof(kv.value) = 'string'
                       ) e
                where p.business_id = $1
                  and e.elem is not null
                  and length(e.elem) between 2 and 60""",
            business_id,
        )
    await close_pool()
    attr_values = [r["v"] for r in attr_rows if r["v"]]
    return vocab, brand_rows, product_rows, section_rows, attr_values


def _catalog_version(product_rows) -> str:
    """Amprenta catalogului pe care se etichetează.

    Peste ID-URI, nu peste prețuri: o judecată de relevanță spune „produsul ăsta răspunde cererii",
    iar asta nu se schimbă când se schimbă prețul. Se schimbă când apar sau dispar produse — și
    atunci etichetele CHIAR expiră, fiindcă setul relevant e altul.
    """
    digest = hashlib.sha256()
    for pid in sorted(r["id"] for r in product_rows):
        digest.update(pid.encode("ascii"))
    return f"sole-{digest.hexdigest()[:12]}"


def build_families(
    vocab, brand_rows, product_rows, section_rows, attr_values
) -> tuple[list[Family], Counter]:
    names = {r["id"]: (r["name"] or "") for r in product_rows}
    brands = {
        b
        for r in brand_rows
        if r["name"]
        and len(b := _norm_words(r["name"])) >= 3
        and len(b.split()) <= _MAX_FACET_WORDS
    }
    facet_index = _facet_index(vocab, brands)
    # Vocabularul COMPLET al catalogului, nu cel plafonat de `load_vocabulary`. Acolo intră doar
    # valorile cu suport suficient și doar primele N per dimensiune — potrivit pentru a construi un
    # `WHERE`, prea sărac pentru a decide dacă un cuvânt rostit de client are corespondent în marfă.
    catalog_terms: set[str] = set()
    for name in names.values():
        catalog_terms.update(w for w in _norm_words(name).split() if len(w) > 2)
    for values in attr_values:
        catalog_terms.update(w for w in _norm_words(values).split() if len(w) > 2)

    stats: Counter = Counter()

    # === FAZA 1 — parsare + rezolvare, fără grupare ==========================================
    # Gruparea are nevoie de frecvența reziduurilor pe TOT corpusul: un cuvânt care apare o dată e
    # zgomot (un adjectiv de marketing), unul care apare în jumătate din fraze nu deosebește nimic.
    # Ambele praguri se pot decide doar după ce s-a văzut întregul, deci trecerea e dublă.
    parsed: list[Phrase] = []
    for row in section_rows:
        pid = row["product_id"]
        for text in parse_phrases(row["body"]):
            stats["phrases"] += 1
            ph = Phrase(
                text=text, folded=fold(text), product_id=pid, product_name=names.get(pid, "")
            )
            classify_phrase(ph, facet_index, brands)
            stats[f"class:{ph.klass}"] += 1
            parsed.append(ph)

    df: Counter = Counter()
    for ph in parsed:
        df.update(set(ph.residual_pool))
    salient = {t for t, n in df.items() if is_salient(t, n, catalog_terms)}
    stats["residual_vocab"] = len(salient)
    stats["residual_not_in_catalog"] = sum(
        1
        for t, n in df.items()
        if n >= _MIN_RESIDUAL_DF and t.removeprefix(NEGATED_PREFIX) not in catalog_terms
    )

    # === FAZA 2 — grupare pe contractul de adevăr ============================================
    families: dict[str, Family] = {}
    for ph in parsed:
        # Cele mai SPECIFICE reziduuri (df mic) — „cearcane" deosebește mai mult decât „hidratant".
        pool = sorted({t for t in ph.residual_pool if t in salient}, key=lambda t: (df[t], t))
        ph.residuals = tuple(sorted(pool[:_MAX_RESIDUALS_PER_FAMILY]))
        key = ph.family_key
        fam = families.get(key)
        if fam is None:
            fam = Family(
                key=key,
                product_type=ph.product_type,
                facets=ph.facets,
                residuals=ph.residuals,
                forbidden=ph.forbidden,
                klass=ph.klass,
            )
            families[key] = fam
        if ph.text not in fam.phrases:
            fam.phrases.append(ph.text)
        fam.merchant_products.add(ph.product_id)

    stats["families_raw"] = len(families)
    kept: list[Family] = []
    for fam in families.values():
        if fam.klass == "exact":
            # O cerere de produs ANUME are, firesc, o singură formulare în fișa acelui produs.
            # Pragul de ≥2 e făcut pentru familii de RAFT, unde o singură frază înseamnă
            # „formulare", nu „contract". Aplicat aici, ștergea tăcut toată clasa `exact` — 0
            # familii în prima
            # rulare, deși frazele existau.
            kept.append(fam)
            continue
        if fam.product_type is None:
            stats["dropped_no_type"] += 1
            continue
        if len(fam.phrases) < _MIN_FAMILY_PHRASES:
            stats["dropped_single_phrase"] += 1
            continue
        kept.append(fam)
    for fam in kept:
        # Cuvintele care NU deosebesc nimic în interiorul familiei: tipul de produs și valorile de
        # fațetă sunt, prin definiție, comune tuturor formulărilor ei. Măsurate împreună cu restul,
        # ridicau artificial suprapunerea — „șampon cu cica" și „șampon cu acid salicilic" ieșeau
        # „asemănătoare" fiindcă amândouă conțin „sampon".
        shared = set((fam.product_type or "").split())
        for _dim, value in fam.facets:
            shared.update(_norm_words(value).split())
        fam.merge_overlap = _phrase_overlap(fam.phrases, shared)
        if fam.klass != "exact" and fam.merge_overlap < _MIN_MERGE_OVERLAP:
            fam.needs_split = True
            stats["needs_split"] += 1
    stats["families_kept"] = len(kept)
    kept.sort(key=lambda f: (-len(f.phrases), f.key))
    return kept, stats


def is_salient(term: str, doc_freq: int, catalog_terms: set[str]) -> bool:
    """E `term` o axă de nevoie, sau zgomot?

    `NEGATED_PREFIX` se scoate ÎNAINTE de confruntarea cu catalogul. Fără asta, fixul de negație
    era inert și n-avea niciun semn: `!volum` nu apare în niciun nume de produs, deci fiecare
    reziduu negat pica testul de apartenență la catalog și dispărea din cheie — adică exact
    reziduurile care despart cererile opuse. Frecvența se numără însă pe forma PREFIXATĂ, fiindcă
    „cu volum" și „fara volum" chiar sunt termeni diferiți.
    """
    bare = term.removeprefix(NEGATED_PREFIX)
    return _MIN_RESIDUAL_DF <= doc_freq <= _MAX_RESIDUAL_DF and bare in catalog_terms


def _phrase_overlap(phrases: list[str], shared: set[str]) -> float:
    """Cât de mult seamănă între ele formulările unei familii, pe cuvintele care DEOSEBESC.

    `shared` = cuvintele comune prin construcție (tipul + valorile de fațetă). Prima variantă le
    includea și, măsurat pe catalogul real, detecta doar 3 familii din 779 — inutilizabil: două
    cereri de șampon complet diferite împărtășesc „sampon", deci păreau apropiate. Excluse, rămâne
    exact partea în care cele două cereri chiar diferă.

    Nu e o măsură de calitate, e un DETECTOR de fuziune prea largă. Exemplu real din prima rulare:
    „șampon pentru păr uscat", „șampon delicat scalp sensibil" și „șampon cu biotină" au căzut în
    aceeași familie fiindcă vocabularul tenantului n-are nicio fațetă pentru păr — catalogul n-are
    conceptul de scalp, iar `concerns` e gol pe toată categoria. Familia rezultată e prea largă ca
    un top-6 să însemne ceva. Golul e informație despre catalog și merită văzut, nu ascuns: familia
    pleacă la om marcată, nu se aruncă și nici nu se etichetează ca și cum ar fi fină.
    """
    sets = [{w for w in _norm_words(p).split() if len(w) > 2 and w not in shared} for p in phrases]
    sets = [s for s in sets if s]
    if len(sets) < 2:
        return 1.0
    scores = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            if union:
                scores.append(len(sets[i] & sets[j]) / len(union))
    return round(sum(scores) / len(scores), 3) if scores else 1.0


def _report(families: list[Family], stats: Counter, catalog_version: str, sample: int) -> str:
    by_class = Counter(f.klass for f in families)
    by_type = Counter(f.product_type for f in families if f.product_type)
    lines = [
        "# NX-203 — familii candidate pe catalogul SOLE",
        "",
        f"catalog_version: `{catalog_version}`",
        "",
        f"- fraze extrase: **{stats['phrases']}**",
        f"- familii brute: **{stats['families_raw']}**",
        f"- familii păstrate: **{stats['families_kept']}**",
        f"- respinse, fără tip de produs (contract fără margini): **{stats['dropped_no_type']}**",
        f"- respinse, o singură formulare: **{stats['dropped_single_phrase']}**",
        f"- marcate `needs_split` (fuziune suspectă): **{stats['needs_split']}**",
        "",
        "## Familii per clasă de interogare",
        "",
        "| clasă | familii | fraze |",
        "| --- | ---: | ---: |",
    ]
    for klass, n in by_class.most_common():
        lines.append(f"| {klass} | {n} | {stats.get(f'class:{klass}', 0)} |")
    lines += [
        "",
        f"## Acoperire pe tipuri de produs: {len(by_type)} tipuri distincte",
        "",
        "| tip | familii |",
        "| --- | ---: |",
    ]
    for ptype, n in by_type.most_common(25):
        lines.append(f"| {ptype} | {n} |")
    lines += ["", f"## Eșantion de {sample} familii", ""]
    for fam in families[:sample]:
        cons = ", ".join(f"{d}={v}" for d, v in fam.facets) or "—"
        lines.append(f"**{_family_id(fam.key)}** · `{fam.klass}` · tip `{fam.product_type}`")
        lines.append(f"  constrângeri: {cons}")
        for q in fam.phrases[:3]:
            lines.append(f"  - „{q}”")
        lines.append(f"  produse afirmate de comerciant: {len(fam.merchant_products)}")
        lines.append("")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--business", required=True, help="business_id (uuid) al tenantului")
    ap.add_argument("--sample", type=int, default=12, help="câte familii se arată în raport")
    ap.add_argument("--write", action="store_true", help="scrie draftul (implicit: doar raport)")
    args = ap.parse_args()

    vocab, brand_rows, product_rows, section_rows, attr_values = await _load(args.business)
    if not section_rows:
        print("Nicio secțiune `recommendation_trigger` pentru tenantul ăsta — nimic de extras.")
        return 1

    catalog_version = _catalog_version(product_rows)
    families, stats = build_families(vocab, brand_rows, product_rows, section_rows, attr_values)

    report = _report(families, stats, catalog_version, args.sample)
    print(report)

    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        draft = {
            "business_id": args.business,
            "catalog_version": catalog_version,
            "source": "product_sections.kind=recommendation_trigger",
            "human_verified": False,
            "families": [f.as_draft(catalog_version, args.business) for f in families],
        }
        out = OUT_DIR / "families_draft.json"
        out.write_text(json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
        rep = REPORT_DIR / "nx203-families.md"
        rep.write_text(report, encoding="utf-8")
        print(f"\nScris: {out.relative_to(ROOT)} ({len(families)} familii)")
        print(f"Scris: {rep.relative_to(ROOT)}")
    else:
        print("\n(dry-run — nimic scris; adaugă --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
