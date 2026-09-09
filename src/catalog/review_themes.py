"""NX-279 — ce spun recenziile despre un produs, ca FRECVENȚĂ verificabilă, nu ca rezumat de model.

Pur: fără DB, fără I/O, fără ceas, fără model. Jobul care îl folosește e
`scripts/derive_review_summaries.py`; consumatorii sunt deja scriși (`evidence_bundle`,
`finalize`, `deterministic._review_answer`, `context_resolver`) și tratează
`product_review_summaries` ca SURSĂ DE ADEVĂR. De aici vine regula care dictează forma modulului:
un „pro" inventat aici nu mai e prins de nimeni în aval — validatorul (stagiul 8) și
`grounding_guard` (NX-240) verifică afirmațiile FAȚĂ DE tabela de fapte, deci un rezumat greșit
se confirmă singur.

## Ce s-a măsurat pe corpusul real (SOLE, 183.003 recenzii, 2026-09-07)

* Distribuția e aproape degenerată: 5★ = 94,9%, 4★ = 5,1%, sub 4★ = 92 de recenzii în total.
  Iar recenziile de 4★ sunt, ca text, INDISTINCTIBILE de cele de 5★ („absoarbe rapid" 464,
  „nu lasă urme" 278). Deci „găsește minusurile" nu se poate deriva din rating: nuanțele sunt
  fraze cu polaritate proprie, în orice recenzie, și sunt RARE și SPECIFICE („furnicături" pe 41
  de produse, cu 29 de recenzii pe unul singur, un plumper de buze).
* Recenziile vorbesc masiv despre MAGAZIN, nu despre produs: „mostre gratuite" în 21.636 de
  recenzii pe 2.740 din 2.743 de produse, „curierul preferat" 4.231, „cashback" 2.852. Ceva ce
  apare pe 99,9% din produse nu spune nimic despre niciunul (același test de discriminare ca la
  NX-264). De aceea vocabularul e de teme de PRODUS, iar raportul jobului marchează fiecare temă
  cu rata de bază pe produse, ca o temă universală să nu poată trece neobservată.
* Lauda se scrie în negație: „nu irită" 6.334, „nu usucă" 5.272, „nu lasă urme" 5.275. Deci
  fraza unei teme de laudă E forma negată, iar o temă de nuanță poartă EXCLUDERI cu negația
  („se transferă" e nuanță doar dacă nu apare în „nu se transferă").
* Reacțiile adverse NU se pot număra determinist cu precizie utilă: „irit" apare în 16.601 de
  recenzii, 3.202 fără negator în fereastra de 4 cuvinte, iar exemplele sunt „de obicei balsamurile
  mă irită, acesta nu" și „după o reacție alergică, crema m-a salvat". Un număr cunoscut ca
  supraestimat de 5-10× nu e un semnal, e zgomot cu aer de cifră. Modulul nu produce așa ceva, iar
  în textul către client nu apare nimic despre reacții: ar fi afirmație medicală (P0) și ar cădea
  oricum la `has_medical_claim`.

## Contractul

* O TEMĂ = cheie canonică + fraze (potrivite cu matcherul NX-268: tokeni pe prefix, în ordine,
  toleranță la flexiune, excluderi cu poziție) + etichetă afișabilă PER LOCALE + `kind`
  (`praise` | `nuance`) + `promise` (`experience` | `claim`). Totul e DATĂ a tenantului
  (`domain_pack.review_themes`), nimic nu e în cod (P9, P11).
* Un „pro" = o temă de laudă menționată de cel puțin `MIN_THEME_REVIEWS` recenzii ȘI de cel puțin
  `MIN_PRAISE_SHARE` din recenziile produsului. Se numără RECENZII distincte, nu apariții.
  Lauda se ia doar din recenziile cu rating ≥ `PRAISE_MIN_RATING`: o recenzie de 2★ care spune
  „nu se absoarbe rapid" conține fraza „absoarbe rapid".
* Un „contra" = o temă de nuanță peste `MIN_NUANCE_SHARE`, din orice recenzie (polaritatea e a
  frazei, nu a ratingului). Pragul e mai jos fiindcă nuanțele sunt structural rare într-un corpus
  95% 5★, iar una care se repetă pe același produs e informativă chiar la 5%.
* O temă `claim` (o promisiune, ca „nu irită") se NUMĂRĂ, dar nu se AFIȘEAZĂ până când tenantul
  o ratifică explicit (`ratified: true`). Fail-closed: aceeași disciplină ca `enforce_ready` la
  NX-271. Rămâne vizibilă în evidence și în raport, ca reținerea să fie auditabilă.
* `summary` e ȘABLON localizat, fără nicio cifră: benzile de frecvență („cele mai multe recenzii" /
  „multe" / „unele") sunt derivate din cotă, deci verificabile, dar nu pun un număr în proză pe
  care `grounding_guard` să-l confrunte cu un `review_count` care poate diverge după sync.
* Un produs sub `MIN_REVIEWS_PER_PRODUCT` recenzii sau fără nicio temă selectată NU primește rând:
  absența e proiectată de `evidence_bundle` ca `unknown(missing_value)`, ceea ce e adevărat.
* Determinism: aceleași recenzii + același vocabular ⇒ aceiași bytes. Recenziile se ordonează după
  id înainte de orice, eșantioanele de dovadă sunt primele id-uri în ordinea aia.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from src.agent.voice import naturalize
from src.catalog.derivation import KeyMatcher, build_matchers, match_keys, tokens
from src.domain.normalize import normalize
from src.worker.text_scrub import has_medical_claim

# Prefixul regulii. Versiunea vocabularului se lipește de el (`rule_id(vocab)`), fiindcă două
# rulări cu vocabulare diferite sunt reguli diferite, iar rândul trebuie să spună care l-a produs.
RULE_PREFIX = "review_themes.v1"

# Sub pragul ăsta nu există „ce spun clienții", există două păreri. Măsurat: 24 de produse SOLE au
# sub 5 recenzii; rămân fără rând, iar consumatorii spun onest că n-au date.
MIN_REVIEWS_PER_PRODUCT = 5

# O temă are nevoie de cel puțin atâtea recenzii distincte pe produs. Două recenzii pot fi aceeași
# persoană sau aceeași zi; trei încep să fie un tipar.
MIN_THEME_REVIEWS = 3

# Cota minimă din recenziile produsului. Lauda e abundentă, deci pragul e mai sus; nuanța e rară
# prin construcție (vezi docstringul modulului), deci mai jos.
MIN_PRAISE_SHARE = 0.10
MIN_NUANCE_SHARE = 0.05

# Lauda se numără doar din recenziile care chiar laudă. Sub 4★, „absoarbe rapid" e cel mai probabil
# în „nu se absoarbe rapid". Nuanțele n-au filtrul ăsta: polaritatea lor e în frază.
PRAISE_MIN_RATING = 4

# Câte teme intră pe rând. Aliniate cu ce randează consumatorii (`_review_answer`: 3 și 2).
MAX_PROS = 3
MAX_CONS = 2

# Câte id-uri de recenzie se păstrează ca dovadă per temă în `evidence`. Suficient ca un auditor
# să deschidă trei rânduri și să vadă fraza; nu atât încât evidence-ul să devină o a doua tabelă.
SAMPLE_IDS = 3

# Benzile de frecvență ale rezumatului. Cota e pe TOATE recenziile considerate ale produsului.
MAJORITY_SHARE = 0.50
MANY_SHARE = 0.20

KINDS = frozenset({"praise", "nuance"})
PROMISES = frozenset({"experience", "claim"})

_DIGIT_RE = re.compile(r"\d")

# Șabloanele rezumatului, per locale. Eticheta unei teme trebuie să se lege după conectorul
# locale-i („spun că {listă}"), deci e o propoziție fără subiect: „se absoarbe rapid", „nu lasă
# urme". O locale fără șablon nu produce rezumat, deci nu produce rând (fail-closed, P11).
COPY: Mapping[str, Mapping[str, str]] = {
    "ro": {
        "majority": "Cele mai multe recenzii spun că {list}.",
        "many": "Multe recenzii spun că {list}.",
        "minority": "Unele recenzii spun că {list}.",
        "nuance": "Câteva recenzii menționează că {list}.",
        "sep": ", ",
        "last": " și ",
    },
    "en": {
        "majority": "Most reviews say it {list}.",
        "many": "Many reviews say it {list}.",
        # domain-leak: ok — „some" e cuvântul englezesc din copy, nu brandul din catalog
        "minority": "Some reviews say it {list}.",
        "nuance": "A few reviews mention that it {list}.",
        "sep": ", ",
        "last": " and ",
    },
}


# --- vocabularul: DATE ale tenantului, validate fail-closed ---------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewTheme:
    key: str
    kind: str  # praise | nuance
    promise: str  # experience | claim
    labels: tuple[tuple[str, str], ...]  # (locale, etichetă), ordonate
    phrases: tuple[str, ...]
    excludes: tuple[str, ...]
    ratified: bool
    reason: str

    def label(self, locale: str) -> str | None:
        return next((text for loc, text in self.labels if loc == locale), None)

    @property
    def renderable(self) -> bool:
        """O promisiune neratificată se numără, dar nu se afișează."""
        return self.promise == "experience" or self.ratified


@dataclass(frozen=True, slots=True)
class ThemeVocabulary:
    version: str
    themes: tuple[ReviewTheme, ...]
    rejected: tuple[tuple[str, str], ...]  # (cheie, motiv) — o intrare respinsă se vede în raport

    def get(self, key: str) -> ReviewTheme | None:
        return next((t for t in self.themes if t.key == key), None)

    def index(self, key: str) -> int:
        """Poziția temei în pachet = ordinea la egalitate de număr. Stabilă, a tenantului."""
        return next((i for i, t in enumerate(self.themes) if t.key == key), len(self.themes))


def rule_id(vocab: ThemeVocabulary) -> str:
    return f"{RULE_PREFIX}:{vocab.version}"


def label_problem(text: str) -> str | None:
    """De ce o etichetă NU poate ajunge la client. `None` = poate.

    Trei porți, toate la BUILD, ca rândul să nu depindă de plasa de la citire (`_safe_review_text`
    o aruncă oricum, dar atunci produsul rămâne fără pro fără să știe nimeni de ce):
    * claim medical (P0) — `has_medical_claim`, aceeași funcție ca validatorul;
    * cifră — un număr în proză e o afirmație pe care `grounding_guard` o confruntă cu faptele, iar
      eticheta nu are fapt în spate;
    * punctuație interzisă (principiul 13) — dacă `naturalize` ar schimba-o, nu e curată."""
    if not text or not text.strip():
        return "label_empty"
    if has_medical_claim(text):
        return "label_medical_claim"
    if _DIGIT_RE.search(text):
        return "label_digits"
    if naturalize(text) != text:
        return "label_punctuation"
    return None


def load_vocabulary(raw: Mapping[str, object] | None) -> ThemeVocabulary:
    """`domain_pack.review_themes` → vocabular validat. Fiecare intrare cade SINGURĂ (fail-closed
    per temă, ca `build_facets`); lipsa versiunii cade tot vocabularul, fiindcă fără versiune
    `rule_id` n-ar putea spune ce a produs rândul."""
    if not isinstance(raw, Mapping):
        return ThemeVocabulary(version="", themes=(), rejected=(("*", "missing"),))
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        return ThemeVocabulary(version="", themes=(), rejected=(("*", "missing_version"),))
    themes: list[ReviewTheme] = []
    rejected: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    seen_phrases: dict[str, str] = {}
    for entry in raw.get("themes") or ():
        if not isinstance(entry, Mapping):
            rejected.append(("?", "not_a_mapping"))
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            rejected.append(("?", "missing_key"))
            continue
        if key in seen_keys:
            rejected.append((key, "duplicate_key"))
            continue
        kind = entry.get("kind")
        if kind not in KINDS:
            rejected.append((key, "bad_kind"))
            continue
        promise = entry.get("promise", "experience")
        if promise not in PROMISES:
            rejected.append((key, "bad_promise"))
            continue
        phrases = tuple(
            normalize(p) for p in (entry.get("phrases") or ()) if isinstance(p, str) and p.strip()
        )
        phrases = tuple(p for p in phrases if tokens(p))
        if not phrases:
            rejected.append((key, "no_phrases"))
            continue
        collision = next((p for p in phrases if p in seen_phrases), None)
        if collision is not None:
            rejected.append((key, f"phrase_collision:{seen_phrases[collision]}"))
            continue
        raw_labels = entry.get("labels")
        if not isinstance(raw_labels, Mapping) or not raw_labels:
            rejected.append((key, "no_labels"))
            continue
        labels: list[tuple[str, str]] = []
        bad_label: str | None = None
        for loc, text in raw_labels.items():
            if not isinstance(loc, str) or not isinstance(text, str):
                bad_label = "label_type"
                break
            problem = label_problem(text.strip())
            if problem:
                bad_label = f"{problem}:{loc}"
                break
            labels.append((loc, text.strip()))
        if bad_label:
            rejected.append((key, bad_label))
            continue
        excludes = tuple(
            normalize(e) for e in (entry.get("excludes") or ()) if isinstance(e, str) and e.strip()
        )
        themes.append(
            ReviewTheme(
                key=key,
                kind=str(kind),
                promise=str(promise),
                labels=tuple(sorted(labels)),
                phrases=phrases,
                excludes=excludes,
                ratified=bool(entry.get("ratified", False)),
                reason=str(entry.get("reason") or ""),
            )
        )
        seen_keys.add(key)
        for p in phrases:
            seen_phrases[p] = key
    return ThemeVocabulary(version=version.strip(), themes=tuple(themes), rejected=tuple(rejected))


def build_theme_matchers(vocab: ThemeVocabulary) -> dict[str, KeyMatcher]:
    """Temele → matchere NX-268. Excluderile ANULEAZĂ o potrivire produsă în interiorul lor, cu
    poziție: „nu se transferă" anulează „se transferă" doar acolo, nu în toată recenzia."""
    concern_map = {phrase: theme.key for theme in vocab.themes for phrase in theme.phrases}
    excludes = {theme.key: list(theme.excludes) for theme in vocab.themes if theme.excludes}
    return build_matchers(concern_map, excludes=excludes, normalize=normalize)


# --- numărarea: recenzii distincte per temă, pe un produs -----------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewRow:
    id: str
    rating: int | None
    body: str


@dataclass(frozen=True, slots=True)
class ThemeCount:
    key: str
    reviews: int
    share: float  # din recenziile CONSIDERATE ale produsului
    sample: tuple[str, ...]  # primele id-uri, în ordinea id-ului


@dataclass(frozen=True, slots=True)
class ReviewDigest:
    product_id: str
    reviews_considered: int
    rated: int
    positive: int  # rating ≥ PRAISE_MIN_RATING
    themes: tuple[ThemeCount, ...]  # toate temele cu ≥1 recenzie, ordonate (-recenzii, poziție)

    @property
    def sentiment(self) -> Decimal | None:
        """Cota recenziilor pozitive între cele cu rating. Pe un corpus 95% 5★ va fi ~0,99 peste
        tot: o coloană sinceră, aproape inutilă. Nu se inventează altceva ca să varieze."""
        if not self.rated:
            return None
        return (Decimal(self.positive) / Decimal(self.rated)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


def digest(
    product_id: str,
    reviews: Iterable[ReviewRow],
    vocab: ThemeVocabulary,
    matchers: Mapping[str, KeyMatcher],
) -> ReviewDigest | None:
    """Recenziile unui produs → numărătoarea temelor. `None` sub pragul de recenzii.

    Se numără recenzii DISTINCTE per temă (o recenzie lungă nu bate zece scurte) și se ordonează
    după id înainte de orice, ca eșantionul de dovadă și ordinea la egalitate să fie
    deterministe."""
    rows = sorted((r for r in reviews if r.body and r.body.strip()), key=lambda r: r.id)
    if len(rows) < MIN_REVIEWS_PER_PRODUCT:
        return None
    counts: dict[str, list[str]] = {}
    rated = positive = 0
    for row in rows:
        if row.rating is not None:
            rated += 1
            if row.rating >= PRAISE_MIN_RATING:
                positive += 1
        words = tokens(normalize(row.body))
        if not words:
            continue
        for key in match_keys(words, matchers):
            theme = vocab.get(key)
            if theme is None:
                continue
            if theme.kind == "praise" and (row.rating is None or row.rating < PRAISE_MIN_RATING):
                continue
            counts.setdefault(key, []).append(row.id)
    total = len(rows)
    themes = [
        ThemeCount(
            key=key, reviews=len(ids), share=len(ids) / total, sample=tuple(ids[:SAMPLE_IDS])
        )
        for key, ids in counts.items()
    ]
    themes.sort(key=lambda t: (-t.reviews, vocab.index(t.key)))
    return ReviewDigest(
        product_id=product_id,
        reviews_considered=total,
        rated=rated,
        positive=positive,
        themes=tuple(themes),
    )


# --- compunerea: selecție + text localizat, cu porțile la build --------------------------------


@dataclass(frozen=True, slots=True)
class Composition:
    summary: str
    top_pros: tuple[str, ...]  # etichete, în locale
    top_cons: tuple[str, ...]
    pro_keys: tuple[str, ...]
    con_keys: tuple[str, ...]
    withheld: tuple[tuple[str, str], ...]  # (cheie, motiv) — ce s-a numărat dar nu s-a afișat


def _threshold_reason(theme: ReviewTheme, count: ThemeCount) -> str | None:
    if count.reviews < MIN_THEME_REVIEWS:
        return "below_min_reviews"
    floor = MIN_PRAISE_SHARE if theme.kind == "praise" else MIN_NUANCE_SHARE
    if count.share < floor:
        return "below_min_share"
    return None


def specificity(count: ThemeCount, base_rates: Mapping[str, float] | None) -> float:
    """Cât de mult spune tema despre ACEST produs, față de cât spune despre oricare.

    Măsurat pe prima rulare (SOLE): „are o textură ușoară" apare pe 88% din produse și „dă
    rezultate vizibile" pe 85%, iar ordonarea după număr brut le punea pe primele locuri aproape
    peste tot — 1.680 de teme SPECIFICE („nu lasă urme albe" la un SPF, „nu încarcă părul" la un
    balsam) ieșeau `over_cap` în spatele lor. Un pro adevărat pe orice produs nu ajută la niciunul.
    Deci ordinea e cota în produs împărțită la rata de bază a temei pe catalog (câte produse
    eligibile o au măcar o dată) — același test de lift ca la NX-264. Fără rate de bază (teste,
    un singur produs) cade pe cotă: determinist, doar mai puțin informat."""
    rate = (base_rates or {}).get(count.key)
    if not rate or rate <= 0:
        return count.share
    return count.share / rate


def select_themes(
    d: ReviewDigest,
    vocab: ThemeVocabulary,
    locale: str,
    base_rates: Mapping[str, float] | None = None,
) -> tuple[list[ThemeCount], list[ThemeCount], list[tuple[str, str]]]:
    """(pro-uri, contra, reținute). Porțile sunt pe fiecare temă; ordinea celor care trec e după
    specificitate (vezi `specificity`), apoi număr, apoi poziția în pachet. Motivele reținerii
    sunt vocabular închis, ca raportul să le poată agrega."""
    eligible_pros: list[ThemeCount] = []
    eligible_cons: list[ThemeCount] = []
    withheld: list[tuple[str, str]] = []
    for count in d.themes:
        theme = vocab.get(count.key)
        if theme is None:
            withheld.append((count.key, "unknown_theme"))
            continue
        reason = _threshold_reason(theme, count)
        if reason:
            withheld.append((count.key, reason))
            continue
        if not theme.renderable:
            withheld.append((count.key, "claim_unratified"))
            continue
        if theme.label(locale) is None:
            withheld.append((count.key, f"no_label:{locale}"))
            continue
        (eligible_pros if theme.kind == "praise" else eligible_cons).append(count)

    def _order(c: ThemeCount) -> tuple[float, int, int]:
        return (-specificity(c, base_rates), -c.reviews, vocab.index(c.key))

    pros = sorted(eligible_pros, key=_order)
    cons = sorted(eligible_cons, key=_order)
    for dropped in pros[MAX_PROS:] + cons[MAX_CONS:]:
        withheld.append((dropped.key, "over_cap"))
    return pros[:MAX_PROS], cons[:MAX_CONS], withheld


def _join(labels: Sequence[str], copy: Mapping[str, str]) -> str:
    if len(labels) == 1:
        return labels[0]
    return copy["sep"].join(labels[:-1]) + copy["last"] + labels[-1]


def band(share: float) -> str:
    if share >= MAJORITY_SHARE:
        return "majority"
    if share >= MANY_SHARE:
        return "many"
    return "minority"


def compose(
    d: ReviewDigest,
    vocab: ThemeVocabulary,
    locale: str,
    base_rates: Mapping[str, float] | None = None,
) -> Composition | None:
    """Digest → rândul afișabil, sau `None` dacă n-are ce spune (nicio temă selectată, locale
    fără șablon, sau rezumatul pică poarta medicală — ultima e imposibilă dacă etichetele au
    trecut, dar poarta e ieftină și rândul e adevăr)."""
    copy = COPY.get(locale)
    if copy is None:
        return None
    pros, cons, withheld = select_themes(d, vocab, locale, base_rates)
    if not pros and not cons:
        return None
    labels = {c.key: vocab.get(c.key).label(locale) or "" for c in pros + cons}
    sentences: list[str] = []
    for name in ("majority", "many", "minority"):
        group = [labels[c.key] for c in pros if band(c.share) == name]
        if group:
            sentences.append(copy[name].format(list=_join(group, copy)))
    if cons:
        sentences.append(copy["nuance"].format(list=_join([labels[c.key] for c in cons], copy)))
    summary = naturalize(" ".join(sentences)) or ""
    if has_medical_claim(summary) or _DIGIT_RE.search(summary):
        return None
    return Composition(
        summary=summary,
        top_pros=tuple(labels[c.key] for c in pros),
        top_cons=tuple(labels[c.key] for c in cons),
        pro_keys=tuple(c.key for c in pros),
        con_keys=tuple(c.key for c in cons),
        withheld=tuple(withheld),
    )


def evidence(
    d: ReviewDigest,
    comp: Composition,
    vocab: ThemeVocabulary,
    base_rates: Mapping[str, float] | None = None,
) -> dict[str, object]:
    """Ce intră în `product_review_summaries.evidence`: destul ca un auditor să refacă rândul din
    recenzii, cu chei ordonate ca doi build-uri identice să dea aceiași bytes. Rata de bază
    folosită la ordonare intră și ea: fără ea, ordinea pro-urilor n-ar fi reproductibilă."""
    rates = base_rates or {}
    return {
        "rule_id": rule_id(vocab),
        "vocabulary_version": vocab.version,
        "ordering": "specificity" if base_rates else "share",
        "reviews_considered": d.reviews_considered,
        "rated": d.rated,
        "positive": d.positive,
        "pros": list(comp.pro_keys),
        "cons": list(comp.con_keys),
        "themes": {
            t.key: {
                "reviews": t.reviews,
                "share": round(t.share, 4),
                "sample": list(t.sample),
                **({"base_rate": round(rates[t.key], 4)} if t.key in rates else {}),
            }
            for t in sorted(d.themes, key=lambda t: t.key)
        },
        "withheld": [list(w) for w in comp.withheld],
    }
