"""Meniul de clarificare: ce are voie botul să OFERE clientului când îi cere o lămurire.

Problema, găsită pe trafic real (`conversation_traces`, 2026-09-16, tenant SOLE): la
„vreau un cablu usb" botul a întrebat politicos ce fel de cablu și a oferit patru variante —
«Pentru telefon, USB-C, 1-2 metri», «Pentru consola, cablu USB de date». SOLE vinde cosmetice.
Sugestiile triajului erau SINGURUL câmp al contractului nano fără nicio poartă: `category_key`
inventat se aruncă de mult, `concerns` se filtrează pe vocabularul pachetului, iar `suggestions`
plecau la client exact cum le scria modelul. Un chip nu e o părere, e o PROMISIUNE pe care
clientul o poate apăsa, iar apăsarea lui reintră în pipeline ca mesaj nou: ofereai un raft
inexistent și apoi trebuia să te dezici de el.

Reparația nu e un filtru peste ce scrie modelul, ci un MENIU ÎNCHIS pe care i-l dăm înainte.
Diferența nu e stilistică, e măsurată: pe catalogul SOLE, rezolvarea termen-cu-termen a chip-ului
«Pentru telefon, USB-C, 1-2 metri» întoarce `c` = KNOWN pe `key_ingredients` (de la „vitamina c")
și `1`/`2` = KNOWN pe `shade_code` (coduri de nuanță) — deci un filtru „măcar un termen se
rezolvă" ar fi PĂSTRAT tocmai chip-ul cu cablul, în timp ce ar fi aruncat «Am tenul uscat»
(„tenul" e flexionat, „uscat" singur nu e subset admis). Un vocabular bogat nu poate fi folosit
ca detector de minciuni, fiindcă are un cuvânt pentru aproape orice.

Contractul modulului, în trei invariante:

1. **Round-trip.** O frază intră în meniu DOAR dacă se rezolvă înapoi, prin `resolve_any`, pe o
   cheie cu produse (`Resolution` garantează `KNOWN ⇒ count > 0`). Rezolvarea e ACEEAȘI pe care o
   folosește căutarea, deci meniul nu poate promite ce căutarea nu poate servi. Măsurat pe SOLE:
   182 din 186 de fraze candidate trec, iar cele 4 respinse sunt exact «ten mixt»/«ten normal» —
   chei pe care pachetul le declară și catalogul nu le are (`overlay_target_dead`). Nicio listă
   scrisă de mână nu le-ar fi prins, fiindcă nimeni nu știa că lipsesc.
2. **Fail-OPEN pe vocabular indisponibil.** DB jos ⇒ vocabular gol ⇒ totul iese `UNKNOWN`. O
   poartă scrisă naiv ar transforma o clipeală de DB în „zero sugestii, mereu". Meniul gol nu
   filtrează nimic: sugestiile modelului trec ca azi. (Aceeași capcană ca la garda off-category,
   vezi `CLAUDE.md`, fix 2026-09-16 §3.)
3. **Fail-CLOSED pe chip.** Cu meniu prezent, un chip care nu conține NICIUNA dintre frazele
   oferite se aruncă. Dacă rămân prea puține, chips-urile devin chiar etichetele din meniu —
   seci, dar adevărate. Zero chips e o opțiune acceptabilă; un chip fals nu e.

`catalog_miss` e o afirmație mai tare și are propriul prag de dovadă: **niciun** cuvânt al
cererii nu seamănă cu limba catalogului. Regula e structurală (nicio listă de cuvinte românești,
P11) și a fost aleasă pe măsurătoare, nu pe intuiție — vezi `is_catalog_miss`.

## Cine mai poate emite un chip (`CHIP_PRODUCERS`)

Modulul ăsta a reparat UN producător, cel al triajului. Citind codul cu ochiul păreau să mai fie
patru; poarta mecanică scrisă în `tests/test_single_producer_guard.py` a găsit **doisprezece**.

Unsprezece dintre ei sunt ancorați — dar **prin construcție, nu structural**: textul chip-ului e ori
copy fix din cod, ori vine din date deja citite (fraze FAQ din DB, produse tocmai afișate, coloanele
unei comparații calculate). Adică se întâmplă să nu poată minți. „Se întâmplă" nu e un invariant:
al treisprezecelea producător nu are nimic care să-l oprească, iar cel de pe calea bogată
(`compose.assemble`) chiar nu e ancorat.

De aceea sunt ENUMERAȚI mai jos, fiecare cu SURSA din care i se poate verifica afirmația, iar poarta
verifică mecanic (AST) că nu apare unul nedeclarat și, simetric, că niciunul declarat n-a rămas în
urma codului. Registrul nu face chip-urile adevărate: face vizibil cine le produce.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from src.catalog.query_terms import content_terms, fold
from src.catalog.vocabulary import (
    CATEGORY_DIMENSION,
    CatalogVocabulary,
    ResolutionStatus,
    VocabEntry,
    facet_overlays,
    resolve_any,
)

__all__ = [
    "CHIP_PRODUCERS",
    "ClarifyMenu",
    "MenuOption",
    "build_menu",
    "catalog_evidence",
    "contains_run",
    "clear_clarify_menu_cache",
    "ground_suggestions",
    "is_catalog_miss",
    "menu_dimensions",
    "menu_for_turn",
    "words_of",
]

#: Fiecare loc din `src/` care poate emite un chip către client, cu sursa lui de ancorare.
#:
#: Cheia e `"<cale relativă din repo>::<nume funcție>"`. Poarta mecanică cere ca orice scriere de
#: `Reply.suggestions` (sau `suggestions=`/`chips=` la construcția unui `Reply`) să fie declarată
#: aici. Valoarea nu e o bifă: e sursa din care se poate verifica afirmația.
#:
#: Registrul NU face chip-urile adevărate. Face vizibil cine le produce — fiindcă azi patru dintre
#: cinci sunt ancorate ACCIDENTAL (se întâmplă să cheme date reale), iar un al cincilea producător
#: nu are nimic care să-l oprească.
CHIP_PRODUCERS: dict[str, str] = {
    # ── poartă EXPLICITĂ ────────────────────────────────────────────────────────────────────
    "src/worker/stages/triage.py::triage_stage": (
        "ANCORAT STRUCTURAL: sugestiile lui nano trec prin `ground_suggestions` contra meniului "
        "închis construit din catalogul real (NX-295). Singurul producător cu poartă explicită, "
        "fiindcă e singurul unde textul chip-ului e scris de un model."
    ),
    "src/agent/brain.py::_set_brain_reply": (
        "ANCORAT STRUCTURAL, prin MUTĂRI (NX-296): chip-ul nu e un text pe care îl validăm, ci o "
        "mutare cu dovadă calculată în tur (`conversation/chip_moves.py`), exprimată dintr-un "
        "șablon al tenantului. Modelul poate doar să REFORMULEZE o mutare oferită, iar poarta e "
        "per mutare: textul lui trebuie să păstreze ancora, altfel cade pe șablon. Un `move_id` "
        "inventat se numără și se ignoră, deci nu există cale prin care o sugestie scrisă de "
        "model să ajungă la client fără o mutare în spate. Cu felia stinsă rămâne comportamentul "
        "de dinainte (`_clarify_chips`: chip-urile SUNT frazele meniului închis)."
    ),
    # ── ancorate prin CONSTRUCȚIE: textul vine din date reale ───────────────────────────────
    "src/worker/stages/faq.py::faq_stage": (
        "Chips-urile SUNT întrebările FAQ candidate, citite din `faqs`. Nu pot numi ceva ce "
        "magazinul nu are, fiindcă textul lor e chiar rândul din DB."
    ),
    "src/agent/deterministic.py::_handle_review_intent": (
        "`_review_choice_chips(refs)` numește produse tocmai AFIȘATE (setul afișat + produsul "
        "paginii, NX-234). Ancora e o referință la ce s-a arătat, nu o promisiune nouă."
    ),
    "src/agent/deterministic.py::_handle_detail_intent": (
        "`_detail_choice_chips(refs)`, aceeași sursă ca la recenzii: produsele deja afișate."
    ),
    "src/agent/deterministic.py::serve_comparison": (
        "`_compare_chips(comparison.columns)` — axele unei comparații deja calculate din produse "
        "reale. Chip-ul numește o coloană care există, fiindcă tabelul e deja construit."
    ),
    "src/agent/finalize.py::render": (
        "`_compare_chips(comparison.columns)`, aceeași sursă ca `serve_comparison`."
    ),
    # ── ancorate prin CONSTRUCȚIE: copy localizat, fără nicio afirmație de catalog ───────────
    "src/agent/deterministic.py::serve_reviews": (
        "`_review_next_steps(language)` — copy FIX din cod («Vezi detaliile»), nu numește niciun "
        "produs sau raft, deci nu are ce promite greșit."
    ),
    "src/agent/deterministic.py::serve_details": (
        "Trei chip-uri de copy fix (`review_chip`/`link_chip`/`compare_chip`), despre produsul "
        "deja în context. Fără nume de catalog în text."
    ),
    "src/agent/finalize.py::_attach_no_result_alternatives": (
        "`_thin_path_chips(language)` — copy fix, pe drumul «n-am găsit». Tocmai aici un chip cu "
        "afirmație de catalog ar fi cel mai toxic: ai spune «n-am găsit» și ai oferi un raft."
    ),
    "src/agent/planner.py::resolve_cheaper_followup": ("`_thin_path_chips(language)`, copy fix."),
    "src/worker/stages/greeting.py::greeting_stage": (
        "Sugestiile de salut vin din categoriile DECLARATE de tenant (`settings.welcome."
        "categories`) plus nevoile din pachet; fără ele se cade pe generice care nu numesc niciun "
        "produs («Vreau o recomandare»). Nu e citire de catalog: salutul e fast path."
    ),
    # ── NEANCORAT ───────────────────────────────────────────────────────────────────────────
    "src/worker/compose.py::assemble": (
        "NEANCORAT — gaura cunoscută, singura din listă. Calea bogată ia `j['suggestions']` direct "
        "din modelul de vânzare și doar le normalizează (trim/dedupe/cap), fără nicio verificare "
        "că numesc ceva servabil. `ground_suggestions` NU e poarta potrivită aici: meniul închis e "
        "construit pentru CLARIFICARE, pe când astea sunt follow-up-uri despre produse deja "
        "afișate, iar aplicarea lui oarbă ar fi exact greșeala pe care NX-295 a MĂSURAT-O (poarta "
        "naivă păstra chip-ul fals și arunca pe cel adevărat). Ancorarea corectă cere întâi un "
        "corpus de chips bogate REALE pe care să se măsoare pragul — o poartă ghicită aici ar "
        "arunca follow-up-uri legitime, ceea ce e mai scump decât gaura."
    ),
}

# Câte opțiuni intră în meniul dat modelului. Meniul e un ajutor, nu un catalog: peste vreo zece
# intrări promptul crește fără ca alegerea să se îmbunătățească.
_MAX_OPTIONS = 10
# Cât poate contribui O SINGURĂ dimensiune. Fără plafon, dimensiunea cu cele mai multe valori
# umple meniul și întrebarea devine o alegere pe o singură axă (vezi `build_menu`).
_MAX_PER_DIMENSION = 4
# Câte AXE intră în meniu. Prea multe, și lista devine o înșiruire fără temă („Ingrijirea
# tenului, ten uscat, tonifiere, crema de fata, seara, mat, hidratare, acid hialuronic"), din
# care modelul n-are cum să compună o întrebare coerentă. Prea puține, și cad axe pe care un
# client chiar le-ar numi: la patru, pe catalogul SOLE, `concerns` («riduri», «acnee») rămânea pe
# dinafară în favoarea lui `routine_step`, deși e exact felul în care oamenii își descriu cererea.
_MAX_DIMENSIONS = 5
# Câte intrări se examinează per dimensiune. Intrările vin deja sortate după numărul de produse,
# iar round-trip-ul costă o rezolvare pe fiecare: fără plafon, o dimensiune cu 200 de valori
# (`key_ingredients`) ar plăti 200 de rezolvări ca să aleagă 4.
_MAX_CANDIDATE_SCAN = 24
# Peste atâtea valori, dimensiunea nu mai poate fi o ÎNTREBARE. `key_ingredients` are 200 pe
# catalogul SOLE: perfectă la căutare, inutilă la oferit — oricare patru ai alege, ai ales
# arbitrar dintr-o listă pe care clientul n-o vede.
_MAX_VALUES_FOR_QUESTION = 60
# Câte chips ies la client NU se decide aici. `limit` e parametru OBLIGATORIU al lui
# `ground_suggestions` tocmai ca să nu existe un al doilea adevăr: proprietarul e
# `settings.chip_slots`, iar un apelant care uită de el nu compilează, în loc să taie tăcut la o
# cifră scrisă în modulul ăsta.
# Sub atâtea chips supraviețuitoare nu merită să servim rămășițele modelului: cădem pe etichetele
# din meniu, care sunt adevărate prin construcție. Unul singur ar arăta ca o alegere, nu ca o listă.
_MIN_KEPT = 2

# Un termen mai scurt de-atât nu e dovadă că știm despre ce vorbește clientul: pe catalogul SOLE,
# `c` se rezolvă pe „vitamina c" și `1`/`2` pe coduri de nuanță. Pragul se aplică DOAR la
# judecarea cererii clientului (`catalog_evidence`), nu la căutare, unde termenii scurți chiar
# discriminează (`content_terms` explică de ce n-are prag).
_MIN_EVIDENCE_TERM_LEN = 3
# Câți termeni trebuie să aibă cererea ca „niciunul nu se rezolvă" să însemne ceva. Cu un singur
# termen („ieftin", „multumesc") absența dovezii e regula, nu excepția.
_MIN_TERMS_FOR_MISS = 2
# Cât de aproape trebuie să fie un cuvânt al clientului de limba catalogului ca să NU declarăm
# că n-avem ce cere. Prinde typo-urile („peiele" → „piele") și cuvintele parțiale („piele" e în
# «piele uscata», dar singur nu e subset admis de `resolve`). Pe traficul real, exact acest test
# a scos ultimele două fals-pozitive.
_NEAR_MATCH_CUTOFF = 0.82
_MIN_NEAR_WORD_LEN = 4


@dataclass(frozen=True, slots=True)
class MenuOption:
    """O opțiune oferibilă: fraza pe care o citește clientul + cheia reală din spate + dovada.

    `phrase` e ce ajunge în prompt și, la nevoie, chiar pe chip. `key`/`dimension` există ca
    meniul să poată fi scăzut din ce știm deja despre conversație, iar `count` ca ordinea să fie
    a catalogului, nu a norocului."""

    phrase: str
    dimension: str
    key: str
    count: int


@dataclass(frozen=True, slots=True)
class ClarifyMenu:
    """Ce poate oferi tenantul ACUM, pe conversația ASTA.

    `usable` (meniu ne-gol) e singura poartă pe care o consultă apelanții: un meniu gol înseamnă
    „n-am cu ce compara", deci nu filtrează nimic (invariantul 2 din docstring-ul modulului)."""

    options: tuple[MenuOption, ...] = ()
    catalog_miss: bool = False
    topic: str | None = None
    reason: str = "unavailable"
    # Fațetele deja acoperite de conversație, excluse din meniu. Numai pentru telemetrie.
    known_keys: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return bool(self.options)

    def phrases(self) -> tuple[str, ...]:
        return tuple(o.phrase for o in self.options)

    def prompt_block(self, heading: str) -> str:
        """Blocul care intră în promptul de clasificare. Gol când meniul e gol — un titlu fără
        listă ar invita modelul să completeze lista singur, adică fix defectul reparat aici."""
        if not self.options:
            return ""
        return f"{heading}: {', '.join(o.phrase for o in self.options)}\n"


def menu_dimensions(vocab: CatalogVocabulary, pack: object) -> tuple[str, ...]:
    """Dimensiunile din care se poate compune o ofertă, în ordinea în care se oferă.

    Nu orice dimensiune descoperită în `attributes` e ofertabilă: pe SOLE, `shade_group` are
    valori de tip `254d9a22fa84`, `volume_raw` are „50 ml", `sku` are coduri. Sunt vocabular
    corect (căutarea le folosește), dar nimeni nu răspunde „254d9a22fa84" la întrebarea ce caută.
    Filtrul e structural și derivat din pachet, nu o listă de chei: oferim categoria plus
    fațetele pe care TENANTUL le-a declarat ca fiind ale lui (`DomainPack.facets`). O fațetă
    nedeclarată rămâne căutabilă, dar nu ofertabilă — asimetria e intenționată, fiindcă a oferi
    e o promisiune, iar a găsi nu.

    Ordinea folosește tot declarații existente ale pachetului, nu preferințe scrise aici, și a
    fost REFĂCUTĂ pe date: prima variantă ordona doar după `binding` (NX-257) plus numărul de
    valori, iar pe catalogul SOLE asta scotea `concerns` («riduri», «acnee») din meniu în
    favoarea lui `routine_time` («zi si noapte»), fiindcă routine_time are trei valori și e mai
    „îngust". Numărul de valori nu spune cât de des NUMEȘTE cineva dimensiunea.

    Regula, în ordinea priorității:
      1. `searchable_facets` — tenantul a declarat explicit pe ce cred ei că vor căuta clienții.
         E cea mai apropiată declarație de „cu ce cuvinte vine omul".
      2. `binding = partitioning` — cumpărătorul are exact una dintre valori (tip de ten, tip de
         produs), deci răspunsul chiar îngustează; o fațetă `additive` e un obiectiv, unde „și" e
         la fel de valid ca „sau".
      3. mai puține valori întâi — o întrebare cu trei opțiuni e o întrebare.

    Iar o dimensiune cu prea multe valori nu e o întrebare deloc: `key_ingredients` (200 de
    valori pe SOLE) e excelentă la căutare și inutilă la oferit, așa că iese din listă."""
    declared = {
        getattr(f, "key", ""): getattr(f, "binding", "additive")
        for f in (getattr(pack, "facets", ()) or ())
    }
    searchable = {str(k) for k in (getattr(pack, "searchable_facets", ()) or ())}
    rest = sorted(
        (
            n
            for n in vocab.facet_names
            if n in declared and len(vocab.entries(n)) <= _MAX_VALUES_FOR_QUESTION
        ),
        key=lambda n: (
            n not in searchable,
            declared[n] != "partitioning",
            len(vocab.entries(n)),
            n,
        ),
    )
    return (CATEGORY_DIMENSION, *rest)


def catalog_evidence(
    text: str,
    vocab: CatalogVocabulary,
    *,
    locale: str,
    overlays: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(termeni judecați, termeni cu dovadă în catalog)` pentru mesajul clientului.

    „Judecați" exclude termenii prea scurți și pe cei pur numerici: ei se rezolvă pe dimensiuni
    tehnice (nuanțe, coduri) și ar produce dovadă acolo unde nu există înțelegere. Restul trece
    prin ACEEAȘI rezolvare ca o căutare."""
    judged: list[str] = []
    hits: list[str] = []
    for term in content_terms(text, locale):
        if len(term) < _MIN_EVIDENCE_TERM_LEN or not any(c.isalpha() for c in term):
            continue
        judged.append(term)
        r = resolve_any(vocab, term, overlays=overlays)
        if r.status is not ResolutionStatus.UNKNOWN and r.evidence > 0:
            hits.append(term)
    return tuple(judged), tuple(hits)


def _catalog_words(vocab: CatalogVocabulary, pack: object) -> frozenset[str]:
    """Cuvintele din care e făcută limba catalogului (chei, etichete, aliasuri de pachet).

    Se folosesc doar pentru testul de APROPIERE, nu pentru rezolvare: un cuvânt apropiat nu devine
    filtru, doar oprește o afirmație tare („n-avem așa ceva")."""
    from src.catalog.vocabulary import _tokens  # noqa: PLC0415 — același tokenizator, un loc

    words: set[str] = set()
    for entries in vocab.dimensions.values():
        for e in entries:
            words |= _tokens(e.key) | _tokens(e.label)
    for alias in getattr(pack, "concern_map", None) or {}:
        words |= _tokens(alias)
    for facet in getattr(pack, "facets", ()) or ():
        for alias in getattr(facet, "aliases", None) or {}:
            words |= _tokens(alias)
    return frozenset(w for w in words if len(w) >= _MIN_NEAR_WORD_LEN and w.isalpha())


def is_catalog_miss(
    text: str,
    vocab: CatalogVocabulary,
    pack: object,
    *,
    locale: str,
    has_displayed_products: bool,
    overlays: Mapping[str, Mapping[str, str]] | None = None,
) -> bool:
    """„Magazinul chiar n-are ce cere clientul" — afirmația care dă dreptul să i-o spui.

    Trei condiții, toate structurale. Fiecare a fost ADĂUGATĂ pentru că măsurătoarea pe traficul
    real al tenantului (25 de ture, `conversation_traces`) a arătat că fără ea regula minte:

      • **≥2 termeni de conținut, niciunul rezolvabil.** Singură, regula asta declanșa pe 8 ture
        din 25 și era corectă pe 2 — 25% precizie. „Trimite-mi linkul la produs" ar fi primit
        „nu vindem așa ceva".
      • **Conversația n-a arătat încă produse.** Un mesaj care urmează unor carduri se referă
        aproape sigur la ele („Compară-l cu un produs similar", „care e diferenta dintre al 4lea
        si al 5lea"), iar cuvintele lui n-au de ce să fie în catalog. Structural, nu lexical:
        nicio listă de pronume, deci ține pe orice limbă. Precizia urcă la 50%.
      • **Niciun cuvânt nu e APROAPE de limba catalogului.** Prinde typo-ul („vreau o rutina
        pentru peiele sucata") și cuvântul parțial („pai am zis piele"). Precizia urcă la 100%:
        2 declanșări, ambele pe cererea de cablu USB, zero false.

    Prima condiție a fost întâi încercată cu „e primul tur al conversației" în locul celei de-a
    doua; măsurătoarea a respins-o, fiindcă ambele ture despre cablu erau al doilea și al treilea
    mesaj (primul fusese un salut). Se păstrează aici ca să nu fie reintrodusă.

    Eșantionul e mic (25 de ture, 2 pozitive) și regula e deliberat ASIMETRICĂ: un fals pozitiv
    costă o frază stânjenitoare, un fals negativ costă exact comportamentul de azi. `clarify_miss`
    se numără în analytics, deci pragul se poate strânge pe date, nu pe păreri.
    """
    if vocab.is_empty() or has_displayed_products:
        return False
    judged, hits = catalog_evidence(text, vocab, locale=locale, overlays=overlays)
    if hits or len(judged) < _MIN_TERMS_FOR_MISS:
        return False
    words = _catalog_words(vocab, pack)
    if not words:
        return False
    return not any(
        difflib.get_close_matches(t, words, n=1, cutoff=_NEAR_MATCH_CUTOFF) for t in judged
    )


def _subtree_slugs(vocab: CatalogVocabulary, topic: str | None) -> frozenset[str]:
    """Categoriile din subarborele raftului `topic` (inclusiv el). Gol dacă n-avem raft."""
    if not topic:
        return frozenset()
    return frozenset(
        e.key
        for e in vocab.categories
        if e.path and (e.path == topic or e.path.startswith(f"{topic}/"))
    )


def _phrase_candidates(
    vocab: CatalogVocabulary,
    pack: object,
    dimension: str,
    *,
    locale: str,
    allowed_keys: Iterable[str] | None = None,
) -> list[tuple[str, VocabEntry]]:
    """`(frază oferibilă, intrare)` pentru o dimensiune, în limba clientului acolo unde există.

    Puntea peste cheile tehnice e harta de limbă a tenantului, citită INVERS: `concern_map`
    traduce „ten uscat" → `dry`, deci `dry` se poate OFERI ca „ten uscat". Dintre aliasuri se ia
    cel mai scurt — pe catalogul SOLE, `anti_aging` are șapte, iar cel scurt e și cel canonic
    („riduri" înaintea lui „linii de expresie").

    Fără alias, cheia se oferă ca atare DOAR dacă arată a cuvânt, nu a identificator
    (`_looks_technical`). Pragul e pe FORMĂ, nu pe limbă, și de-asta lasă să treacă o valoare ca
    `velvet`: n-avem cum deosebi structural un cuvânt englezesc de unul românesc, iar a cere alias
    pentru tot ar tăia și `crema de fata` sau `sampon` — chei derivate din numele reale ale
    produselor, deci deja în limba catalogului. Compromisul e acceptat conștient: cel mai rău caz
    e un cuvânt de jargon pe un chip, nu un produs pe care magazinul nu-l are."""
    entries = vocab.entries(dimension)
    if allowed_keys is not None:
        allowed = set(allowed_keys)
        entries = tuple(e for e in entries if e.key in allowed)
    if not entries:
        return []
    # Ordinea DOVEZII, aici, nu la sfârșit: apelantul taie lista la primele câteva, iar
    # categoriile vin din vocabular sortate pe `path` (alfabetic). Fără rândul ăsta, meniul de
    # rafturi al lui SOLE ieșea «Barbati, Copii, Corp, Dermato cosmetice» — cele mai MICI patru
    # rafturi, în loc de «Ten, Machiaj, Par, Protectie solara». Măsurat, nu presupus.
    entries = tuple(sorted(entries, key=lambda e: (-e.count, e.key)))

    if dimension == CATEGORY_DIMENSION:
        return [(e.label, e) for e in entries]

    # cheie → aliasuri din pachet (harta de nevoi + aliasurile DECLARATE ale fațetei)
    by_key: dict[str, list[str]] = {}
    for alias, key in (getattr(pack, "concern_map", None) or {}).items():
        by_key.setdefault(str(key), []).append(str(alias))
    subject = _facet_subject_words(pack, dimension, locale)
    for facet in getattr(pack, "facets", ()) or ():
        if getattr(facet, "key", None) != dimension:
            continue
        for alias, key in (getattr(facet, "aliases", None) or {}).items():
            by_key.setdefault(str(key), []).append(str(alias))

    out: list[tuple[str, VocabEntry]] = []
    for e in entries:
        aliases = sorted(set(by_key.get(e.key, ())), key=lambda a: (_alias_rank(a, subject), a))
        if aliases:
            out.append((aliases[0], e))
        elif not _looks_technical(e.key):
            # Cheia e deja o frază lizibilă în limba catalogului („crema de fata", „sampon").
            out.append((e.key, e))
    return out


def _facet_subject_words(pack: object, dimension: str, locale: str) -> frozenset[str]:
    """Cuvintele din eticheta fațetei — SUBIECTUL despre care vorbește ea.

    Există pentru o singură decizie: care alias al unei chei se arată clientului. Pe catalogul
    SOLE, `oily` are opt aliasuri, iar cel mai scurt e «luciu» — un simptom scos din context, care
    pe un chip nu mai înseamnă nimic. Eticheta declarată a fațetei («Tip de ten») spune despre ce
    e vorba, deci aliasul care conține „ten" («ten gras») e cel care se înțelege singur.

    Fără etichetă, sau când niciun alias n-o atinge, rămâne regula scurtă — corectă acolo unde
    fațeta n-are subiect propriu: `concerns` se numește «Potrivit pentru», iar «riduri» n-are de
    ce să conțină „potrivit"."""
    from src.catalog.vocabulary import _tokens  # noqa: PLC0415 — același tokenizator, un loc

    for facet in getattr(pack, "facets", ()) or ():
        if getattr(facet, "key", None) != dimension:
            continue
        labels = getattr(facet, "labels", None) or {}
        label = labels.get(locale) or labels.get((locale or "").split("-")[0]) or ""
        return frozenset(w for w in _tokens(str(label)) if len(w) >= 3)
    return frozenset()


def _alias_rank(alias: str, subject: frozenset[str]) -> tuple[int, int]:
    """`(nu atinge subiectul, lungime)` — vezi `_facet_subject_words`."""
    from src.catalog.vocabulary import _tokens  # noqa: PLC0415

    return (0 if subject & _tokens(alias) else 1, len(alias))


def _looks_technical(key: str) -> bool:
    """Cheie care arată a identificator, nu a cuvânt pe care l-ar rosti un client.

    Testul e pe ALFABET, nu pe o listă de chei: orice caracter care nu e literă, spațiu sau
    cratimă trage cheia în zona identificatorilor. Măsurat pe catalogul SOLE, exact asta separă
    `crema de fata` și `sampon` (oferibile) de `fata:tratament`, `anti_aging`, `254d9a22fa84`,
    `50 ml` și `AQ-102` — toate vocabular corect pentru CĂUTARE, niciunul un răspuns pe care l-ar
    da un om la întrebarea ce caută."""
    if len(key) < 3:
        return True
    return not all(ch.isalpha() or ch in " -" for ch in key)


def build_menu(
    vocab: CatalogVocabulary,
    pack: object,
    *,
    locale: str,
    topic: str | None = None,
    known_keys: Sequence[str] = (),
    catalog_miss: bool = False,
    scoped_keys: Mapping[str, Sequence[str]] | None = None,
) -> ClarifyMenu:
    """Meniul oferibil, compus din catalog și scăzut din ce știe deja conversația.

    `topic` (raftul discutat) restrânge categoriile la subarbore, iar `scoped_keys` — cheile de
    fațetă care chiar EXISTĂ sub acel raft, măsurate în DB de apelant — restrânge fațetele. Fără
    `scoped_keys` nu se oferă fațete pe un raft anume: „par uscat" la o discuție despre creme e
    tot o promisiune falsă, doar mai subtilă decât cablul USB.

    `known_keys` sunt cheile pe care clientul ni le-a spus deja (nevoi active, constrângeri): a
    le re-oferi înseamnă a-l pune să repete, adică exact ce face poarta de clarificare NX-235 la
    nivel de întrebare, aici la nivel de opțiune.

    Round-trip-ul se verifică AICI, pe fiecare frază, cu aceeași rezolvare pe care o face
    căutarea. O frază care nu se întoarce pe o cheie cu produse nu intră în meniu, oricât de bine
    ar arăta în pachet."""
    if vocab.is_empty():
        return ClarifyMenu(catalog_miss=False, reason="vocabulary_unavailable")

    overlays = facet_overlays(pack, vocab.facet_names)
    known = {str(k).strip().lower() for k in known_keys if str(k).strip()}
    subtree = _subtree_slugs(vocab, topic)

    # Un bazin per dimensiune, apoi alegere pe rând din fiecare. Umplerea „prima dimensiune,
    # până se termină locurile" ar fi dat, pe catalogul SOLE, un meniu din zece ingrediente
    # („acid hialuronic", „niacinamida", ...): adevărat, dar inutilizabil ca întrebare. Un meniu
    # bun acoperă AXE diferite, nu valori multe pe aceeași axă.
    pools: list[list[MenuOption]] = []
    seen_phrases: set[str] = set()
    for dimension in menu_dimensions(vocab, pack):
        if len(pools) >= _MAX_DIMENSIONS:
            break
        if dimension == CATEGORY_DIMENSION:
            allowed: set[str] | None = subtree or None
            if catalog_miss or not subtree:
                # Nimic ancorat: oferim RAFTURILE, nu frunzele. Un client care n-a nimerit
                # magazinul are nevoie să vadă din ce e făcut, nu a 45-a subcategorie.
                allowed = {e.key for e in vocab.categories if not e.depth}
        elif catalog_miss:
            continue  # nu întrebăm de tip de ten pe cineva care a cerut un cablu
        elif scoped_keys is not None:
            allowed = set(scoped_keys.get(dimension, ()))
            if not allowed:
                continue
        else:
            continue  # fațete neancorate pe raft: vezi docstring

        pool: list[MenuOption] = []
        candidates = _phrase_candidates(vocab, pack, dimension, locale=locale, allowed_keys=allowed)
        for phrase, entry in candidates[:_MAX_CANDIDATE_SCAN]:
            if len(pool) >= _MAX_PER_DIMENSION:
                break
            norm = fold(phrase).strip()
            if not norm or norm in seen_phrases or entry.key.lower() in known:
                continue
            # ROUND-TRIP: fraza trebuie să se întoarcă pe o cheie cu produse.
            back = resolve_any(vocab, phrase, overlays=overlays)
            if back.status is ResolutionStatus.UNKNOWN or back.evidence <= 0:
                continue
            seen_phrases.add(norm)
            pool.append(
                MenuOption(phrase=phrase, dimension=dimension, key=entry.key, count=entry.count)
            )
        pool.sort(key=lambda o: (-o.count, o.phrase))
        if pool:
            pools.append(pool)

    picked: list[MenuOption] = []
    for rank in range(_MAX_PER_DIMENSION):
        for pool in pools:
            if rank < len(pool) and len(picked) < _MAX_OPTIONS:
                picked.append(pool[rank])
    picked.sort(key=lambda o: (o.dimension != CATEGORY_DIMENSION, -o.count, o.phrase))
    reason = "shelves" if (catalog_miss or not topic) else "topic"
    return ClarifyMenu(
        options=tuple(picked[:_MAX_OPTIONS]),
        catalog_miss=catalog_miss,
        topic=topic,
        reason=reason if picked else "empty",
        known_keys=tuple(sorted(known)),
    )


def words_of(text: str) -> list[str]:
    """Cuvintele unui text, pliate (fără diacritice) și fără punctuație. Ordinea se PĂSTREAZĂ:
    potrivirea cere cuvinte consecutive, nu o mulțime."""
    return [w for w in re.split(r"[^0-9a-z]+", fold(text)) if w]


def contains_run(haystack: list[str], needle: list[str]) -> bool:
    """Apare `needle` ca secvență consecutivă de cuvinte în `haystack`?"""
    n = len(needle)
    if not n or n > len(haystack):
        return False
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def ground_suggestions(
    suggestions: Sequence[str], menu: ClarifyMenu, *, limit: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(chips păstrate, chips aruncate)`.

    Cu meniu gol nu se filtrează nimic — nu avem cu ce judeca, iar o poartă care se închide când
    nu știe ar transforma o clipeală de DB în tăcere (P6). Cu meniu prezent, un chip trece doar
    dacă NUMEȘTE o opțiune oferită: modelul poate îmbrăca fraza natural, cu diacritice („Caut o
    cremă de față pentru riduri"), atâta timp cât o păstrează întreagă. Dacă o strică, pierde
    chip-ul — asimetria e voită: un chip pierdut e o opțiune mai puțin, un chip fals e o
    promisiune pe care magazinul n-o poate onora.

    Potrivirea e pe CUVINTE ÎNTREGI, consecutive, nu pe subșir, și diferența a fost găsită de un
    test: opțiunea «Ten» (trei litere) e subșir în „am **ten**ul uscat de la frig", deci poarta
    trecea un chip pe care nu-l acoperea nimic. Un magazin cu rafturi scurte («Par», «Ten»,
    «Corp») ar fi avut o poartă care spune „da" aproape întotdeauna — adică nicio poartă.

    Când rămân prea puține, lista se COMPLETEAZĂ cu etichete din meniu — vezi mai jos de ce
    completarea nu e înlocuire."""
    if not menu.usable:
        cleaned = tuple(dict.fromkeys(s.strip() for s in suggestions if s and s.strip()))
        return cleaned[:limit], ()

    needles = [words_of(p) for p in menu.phrases()]
    kept: list[str] = []
    dropped: list[str] = []
    for raw in suggestions:
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = " ".join(raw.split()).strip()
        haystack = words_of(text)
        if any(n and contains_run(haystack, n) for n in needles):
            if text not in kept:
                kept.append(text)
        else:
            dropped.append(text)

    if len(kept) < _MIN_KEPT:
        # Se COMPLETEAZĂ, nu se înlocuiește. Varianta care înlocuia a fost scrisă întâi și a picat
        # pe date reale: dintr-un set de patru sugestii, una singură era grounded
        # („Am tenul uscat si caut o crema de fata"), iar căderea o arunca ca să pună în loc patru
        # etichete seci. Pierdeam exact sugestia pe care poarta o declarase bună.
        covered = {fold(k) for k in kept}
        for phrase in menu.phrases():
            if len(kept) >= limit:
                break
            if fold(phrase) not in covered:
                kept.append(phrase)
                covered.add(fold(phrase))
    return tuple(kept[:limit]), tuple(dropped)


# --- asamblarea pentru un tur (singurul loc cu I/O) -------------------------------------------

# Fațetele unui raft se schimbă doar la sync de catalog, dar interogarea costă ~250 ms măsurat pe
# catalogul SOLE (2.758 de produse, sub RLS, deci fără index pe `attributes`). Fără cache, prețul
# s-ar plăti la FIECARE tur, inclusiv pe cele care nu vor ajunge niciodată la o clarificare —
# meniul se construiește înaintea apelului fiindcă intră în prompt, deci nu se poate amâna.
# Politica e a cache-ului de vocabular, deliberat identică: TTL scurt, plafon, evicție a celui mai
# vechi. Cheia include tenantul ȘI raftul (P7: un raft al unui client nu poate fi servit altuia).
_SCOPED_TTL_S = 300.0
_SCOPED_MAX_ENTRIES = 64
_scoped_cache: dict[tuple[str, tuple[str, ...]], tuple[float, dict[str, list[str]]]] = {}


def clear_clarify_menu_cache() -> None:
    """Pentru teste și pentru joburile care tocmai au rescris catalogul."""
    _scoped_cache.clear()


def _scoped_cache_get(business_id: str, slugs: tuple[str, ...]) -> dict[str, list[str]] | None:
    import time  # noqa: PLC0415 — modul altfel PUR

    hit = _scoped_cache.get((business_id, slugs))
    if hit is None or (time.monotonic() - hit[0]) >= _SCOPED_TTL_S:
        return None
    return hit[1]


def _scoped_cache_put(
    business_id: str, slugs: tuple[str, ...], value: dict[str, list[str]]
) -> None:
    import time  # noqa: PLC0415

    key = (business_id, slugs)
    if len(_scoped_cache) >= _SCOPED_MAX_ENTRIES and key not in _scoped_cache:
        _scoped_cache.pop(min(_scoped_cache, key=lambda k: _scoped_cache[k][0]), None)
    _scoped_cache[key] = (time.monotonic(), value)


async def menu_for_turn(ctx: object, deps: object) -> ClarifyMenu:
    """Meniul turului curent: vocabular (din cache) + fațetele raftului (o interogare) + starea.

    Best-effort prin construcție. Orice eșec întoarce un meniu GOL, iar un meniu gol nu filtrează
    nimic (invariantul 2): o pană de DB nu are voie să devină „botul nu mai sugerează nimic".

    Stă aici, și nu în stagiu, fiindcă ambele căi care pun întrebări — triajul de azi și MainBrain
    de mâine — au nevoie de ACELAȘI meniu. Două construcții separate ar diverge, iar atunci
    poarta ar judeca alt catalog decât cel din prompt."""
    import logging  # noqa: PLC0415 — modul altfel PUR; logging doar pe calea cu I/O

    from src.catalog.vocabulary import topic_root_of  # noqa: PLC0415
    from src.catalog.vocabulary_cache import get_vocabulary  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415
    from src.conversation.state_v2 import active_needs  # noqa: PLC0415
    from src.db.queries.catalog import facet_keys_in_scope  # noqa: PLC0415

    log = logging.getLogger(__name__)
    if not get_settings().clarify_menu_enabled:
        return ClarifyMenu(reason="disabled")

    message = getattr(getattr(ctx, "message", None), "body", "") or ""
    business = getattr(ctx, "business", None)
    pack = getattr(business, "domain_pack", None)
    locale = getattr(ctx, "language", None) or "ro"
    state = getattr(ctx, "state", None)

    try:
        vocab = await get_vocabulary(deps, getattr(business, "id", ""))
    except Exception:  # noqa: BLE001 — vezi docstring: gol, nu excepție
        log.warning("clarify_menu: vocabular indisponibil", exc_info=True)
        return ClarifyMenu(reason="vocabulary_unavailable")
    if vocab.is_empty():
        return ClarifyMenu(reason="vocabulary_unavailable")

    overlays = facet_overlays(pack, vocab.facet_names)
    displayed = bool(getattr(state, "displayed_products", None))
    miss = is_catalog_miss(
        message,
        vocab,
        pack,
        locale=locale,
        has_displayed_products=displayed,
        overlays=overlays,
    )

    # Raftul discutat: categoria din stare (NX-133), tradusă în rădăcina ei. Un `catalog_miss` nu
    # moștenește raft — cererea a plecat din altă parte, iar a o îngusta pe raftul vechi ar
    # ascunde exact faptul că n-avem ce cere.
    topic: str | None = None
    if not miss:
        constraints = getattr(state, "search_constraints", None) or {}
        if isinstance(constraints, dict):
            topic = topic_root_of(vocab, constraints.get("category_key"))

    scoped: dict[str, list[str]] | None = None
    if not miss:
        dims = tuple(d for d in menu_dimensions(vocab, pack) if d != CATEGORY_DIMENSION)
        slugs = tuple(sorted(_subtree_slugs(vocab, topic)))
        cached = _scoped_cache_get(getattr(business, "id", ""), slugs)
        if cached is not None:
            scoped = cached
        else:
            try:
                async with deps.db("clarify_menu_facets") as conn:  # type: ignore[attr-defined]
                    scoped = await facet_keys_in_scope(
                        conn,
                        getattr(business, "id", ""),
                        dimensions=dims,
                        category_slugs=slugs,
                    )
                _scoped_cache_put(getattr(business, "id", ""), slugs, scoped)
            except Exception:  # noqa: BLE001 — fără fațete ancorate rămân categoriile, tot
                # adevărate: o categorie servabilă e servabilă indiferent de fațete.
                log.warning("clarify_menu: fațetele raftului indisponibile", exc_info=True)
                scoped = None

    known = [n.key for n in active_needs(ctx)]
    constraints = getattr(state, "constraints", None)
    if isinstance(constraints, dict):
        known.extend(str(k) for k in constraints)
    return build_menu(
        vocab,
        pack,
        locale=locale,
        topic=topic,
        known_keys=known,
        catalog_miss=miss,
        scoped_keys=scoped,
    )
