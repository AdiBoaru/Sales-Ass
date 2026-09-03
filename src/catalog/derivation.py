"""NX-268 — cum se recunoaște o nevoie într-un text de produs. Pur: fără DB, fără I/O, fără ceas.

Modulul ăsta există separat de jobul care îl folosește (`scripts/derive_product_attributes.py`)
pentru un motiv practic: derivarea scrie în catalog, iar tot ce e în aval — validatorul stagiului 8,
`grounding_guard` (NX-240) — verifică adevărul FAȚĂ DE tabela de fapte. Un atribut greșit aici nu
mai e prins de nimeni și iese la client ca afirmație. Deci regula de potrivire trebuie să fie
testabilă fără bază de date.

## Ce s-a măsurat, și de ce mecanismul arată așa

Cardul pornea de la o cifră: „ten gras" apare la 93 de produse ca frază exactă și la 1.120 dacă
cauți stemurile `gras` / `sebum` / `luci`. De 12 ori mai mult semnal, deci treci pe stemuri.

Măsurat pe catalogul SOLE (2.758 produse, secțiunile pozitive), **cifra nu se confirmă ca
îmbunătățire de calitate.** Un stem dintr-un singur cuvânt se comportă ca o fațetă: ori
discriminează, ori descrie raftul. Aplicând testul de discriminare pe care îl folosește deja
`facet_discovery.py` (NX-264) — lift-ul față de rata de bază a cheii — rezultatul e:

* `gras` **pică** testul (lift sub 3): prinde „acizi grași", „grăsime", „grasă" din orice
  compoziție. Exact stemul din care venea cifra de 12×;
* `pielii`, `fara`, `care`, `aspect`, `stralucire` trec pragul de frecvență și pică lift-ul —
  adică ar fi adus volum, nu semnal. `barrier` ar fi urcat de 4,8× exclusiv prin cuvântul „pielii";
* stemurile care trec toate cele trei porți sunt variante MORFOLOGICE ale unui concept dintr-un
  singur cuvânt: `cearc`, `matre`, `scalp`, `luciu`, `pungi`, `dilat`, `solar`, `vopsi`.

Deci generalizarea corectă nu e „renunță la frază", ci **păstrează fraza și tolerează flexiunea**:
tokenii frazei se potrivesc pe PREFIX, în ordine, cu cel mult `MAX_GAP` cuvinte între ei. „ten gras"
prinde „tenul gras", „ten foarte gras", „tenurile grase" — dar NU „acizi grași", fiindcă „ten" nu
apare. Măsurat: **7.726 → 9.500 potriviri (1,23×), fără nicio pierdere** față de fraza exactă.

Stemurile dintr-un singur cuvânt rămân posibile, dar ca DATE: `concern_stems` din pachet, ratificate
o dată per tenant după ce trec porțile de mai sus. Codul nu știe niciunul.

## Excluderile

O excludere e o frază care ANULEAZĂ o potrivire când o conține. „acizi grasi" pentru `oily` e
exemplul canonic: dacă potrivirea s-a produs în interiorul unei fraze excluse, nu se numără. Sunt în
pachet, per cheie, cu motivul lor — nu în cod (P9), și nu ca renunțare la mecanism.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# Sub lungimea asta un token nu se potrivește pe prefix, ci pe egalitate: un token de două litere
# e prefixul a mii de cuvinte, iar o fațetă construită pe asta minte fără să dea eroare.
MIN_PREFIX_TOKEN = 3

# Cât de lungă poate fi COADA pe care o adaugă flexiunea. Asta e poarta care face prefixul sigur,
# și nu lungimea minimă a tokenului: „ten" trebuie să prindă „tenul" (+2), „tenuri" (+3) și
# „tenului" (+4) — formele în care textul chiar scrie —, dar nu „tendinta" (+5) sau „tensiune"
# (+5), care sunt alte cuvinte. Fără plafonul ăsta, orice token scurt ar fi trebuit scos din joc,
# adică exact cuvintele care discriminează cel mai bine într-un catalog de cosmetice.
MAX_INFLECTION_TAIL = 4

# Câte cuvinte pot sta între tokenii unei fraze. Doi acoperă intensificatorii și articolele
# („ten foarte gras", „bariera de protecție a pielii"); mai mult și fraza încetează să mai fie o
# frază — „ten" într-o propoziție și „gras" în următoarea nu spun împreună nimic.
MAX_GAP = 2

_WORD_RE = re.compile(r"[a-z0-9]+")


def tokens(text_normalized: str) -> list[str]:
    """Cuvintele unui text DEJA normalizat (lower, fără diacritice), în ordine.

    Normalizarea nu se face aici deliberat: apelantul o face o dată per text, iar o funcție care
    normalizează la fiecare apel ar face derivarea pe 2.758 de produse × 20 de chei să o refacă de
    zeci de mii de ori."""
    return _WORD_RE.findall(text_normalized)


def _matches(token: str, word: str) -> bool:
    """Cuvântul e o formă flexionară a tokenului? Prefix + coadă mărginită, în ambele porți."""
    if word == token:
        return True
    if len(token) < MIN_PREFIX_TOKEN:
        return False
    return word.startswith(token) and len(word) - len(token) <= MAX_INFLECTION_TAIL


def phrase_span(words: Sequence[str], phrase_tokens: Sequence[str]) -> tuple[int, int] | None:
    """Unde apare fraza în text, tolerând flexiunea → `(start, end)` inclusiv; `None` = nu apare.

    Tokenii trebuie să apară ÎN ORDINE, fiecare ca prefix al unui cuvânt, cu cel mult `MAX_GAP`
    cuvinte între vecini. Se încearcă TOATE aparițiile primului token, nu doar prima: fără
    backtracking, un „acnee" într-o propoziție anterioară ar consuma potrivirea și fraza reală de
    mai jos ar fi ratată (măsurat — pierdea produse pe care fraza exactă le găsea).

    Întoarce intervalul, nu un boolean, fiindcă excluderile au nevoie de POZIȚIE: „acizi grași"
    anulează o potrivire doar dacă potrivirea s-a produs înăuntrul ei."""
    return next(iter(phrase_spans(words, phrase_tokens)), None)


def phrase_spans(words: Sequence[str], phrase_tokens: Sequence[str]) -> list[tuple[int, int]]:
    """TOATE aparițiile frazei, în ordine. Există separat de `phrase_span` din cauza excluderilor:
    o frază poate apărea și înăuntrul unei excluderi, și în afara ei („nu e pentru ten gras. dar
    merge pe ten gras combinat"). Dacă am ști doar prima apariție, o singură mențiune negativă ar
    anula toate celelalte — adică excluderea ar acționa pe produs, nu pe potrivire."""
    if not phrase_tokens:
        return []
    first = phrase_tokens[0]
    out: list[tuple[int, int]] = []
    for start in (i for i, w in enumerate(words) if _matches(first, w)):
        pos = start
        for token in phrase_tokens[1:]:
            nxt = next(
                (
                    j
                    for j in range(pos + 1, min(len(words), pos + 2 + MAX_GAP))
                    if _matches(token, words[j])
                ),
                None,
            )
            if nxt is None:
                break
            pos = nxt
        else:
            out.append((start, pos))
    return out


@dataclass(frozen=True, slots=True)
class KeyMatcher:
    """Tot ce recunoaște o cheie canonică, venit EXCLUSIV din pachetul tenantului.

    `phrases` = frazele din `concern_map` (tokenizate o dată); `stems` = cuvinte-rădăcină ratificate
    separat (`concern_stems`), pentru conceptele dintr-un singur cuvânt; `excludes` = fraze care
    ANULEAZĂ o potrivire produsă în interiorul lor."""

    key: str
    phrases: tuple[tuple[str, ...], ...] = ()
    stems: tuple[str, ...] = ()
    excludes: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class KeyHit:
    """O cheie susținută de un text, cu dovada. `via_stem` separă ce a adus fraza de ce a adus
    stemul — fără separare, o fațetă care crește n-ar spune din ce a crescut."""

    key: str
    evidence: tuple[str, ...]
    from_phrase: bool = False
    from_stem: bool = False
    vetoed: int = 0  # potriviri anulate de o excludere (numărate, nu ascunse)

    @property
    def stem_only(self) -> bool:
        """Cheia a intrat DOAR prin stem. Ăsta e setul care trebuie auditat: fraza e mecanismul
        conservator, stemul e cel care lărgește."""
        return self.from_stem and not self.from_phrase


def build_matchers(
    concern_map: Mapping[str, str],
    *,
    stems: Mapping[str, Sequence[str]] | None = None,
    excludes: Mapping[str, Sequence[str]] | None = None,
    normalize=lambda s: s,
) -> dict[str, KeyMatcher]:
    """`concern_map` (frază → cheie) + stemuri/excluderi opționale → matchere per cheie.

    Tot ce intră aici e DATĂ a tenantului. O cheie fără nicio frază și fără stem nu produce
    matcher: nu se derivă nimic pentru ea, iar absența se vede în raport."""
    phrases: dict[str, list[tuple[str, ...]]] = {}
    for phrase, key in concern_map.items():
        toks = tuple(tokens(normalize(phrase)))
        if toks:
            phrases.setdefault(key, []).append(toks)
    out: dict[str, KeyMatcher] = {}
    keys = set(phrases) | set(stems or {})
    for key in sorted(keys):
        key_stems = tuple(
            normalize(s) for s in (stems or {}).get(key, ()) if isinstance(s, str) and s.strip()
        )
        key_excludes = tuple(
            tuple(tokens(normalize(e)))
            for e in (excludes or {}).get(key, ())
            if isinstance(e, str) and e.strip()
        )
        matcher = KeyMatcher(
            key=key,
            phrases=tuple(phrases.get(key, ())),
            stems=key_stems,
            excludes=tuple(e for e in key_excludes if e),
        )
        if matcher.phrases or matcher.stems:
            out[key] = matcher
    return out


def _veto_spans(words: Sequence[str], excludes: Iterable[Sequence[str]]) -> list[tuple[int, int]]:
    spans = []
    for exclude in excludes:
        span = phrase_span(words, exclude)
        if span is not None:
            spans.append(span)
    return spans


def match_keys(words: Sequence[str], matchers: Mapping[str, KeyMatcher]) -> dict[str, KeyHit]:
    """Cheile susținute de un text, cu dovada și cu excluderile aplicate.

    Ordinea contează: mai întâi se caută potrivirea, apoi se verifică dacă a căzut în interiorul
    unei fraze excluse. Invers (excludere pe text întreg) ar arunca produsul întreg pentru o singură
    mențiune nefericită — „conține acizi grași" ar șterge o cremă chiar dedicată tenului gras."""
    hits: dict[str, KeyHit] = {}
    for key, matcher in matchers.items():
        vetoes = _veto_spans(words, matcher.excludes)
        evidence: list[str] = []
        from_phrase = from_stem = False
        vetoed = 0

        def _accept(spans: list[tuple[int, int]], label: str) -> bool:
            """Acceptă dacă MĂCAR O apariție cade în afara oricărei excluderi. Cele anulate se
            numără — o excludere care lucrează în tăcere nu se poate audita."""
            nonlocal vetoed
            free = [s for s in spans if not any(v[0] <= s[0] and s[1] <= v[1] for v in vetoes)]
            vetoed += len(spans) - len(free)
            if not free:
                return False
            evidence.append(label)
            return True

        for phrase_tokens in matcher.phrases:
            if _accept(phrase_spans(words, phrase_tokens), " ".join(phrase_tokens)):
                from_phrase = True
        for stem in matcher.stems:
            if _accept(phrase_spans(words, (stem,)), stem):
                from_stem = True
        if evidence:
            hits[key] = KeyHit(
                key=key,
                evidence=tuple(sorted(set(evidence))[:3]),
                from_phrase=from_phrase,
                from_stem=from_stem,
                vetoed=vetoed,
            )
    return hits


# --- ingrediente: o secțiune de proză nu e o fațetă --------------------------------------------

# Separatorul dintre numele ingredientului și explicația lui, în liniile secțiunii. Măsurat: liniile
# sunt propoziții întregi („ulei de macadamia - ulei bogat în acizi grași mononesaturați…"), ceea ce
# producea 10.392 de valori distincte pe 99,1% acoperire. Excelent ca text de căutare, inutilizabil
# ca fațetă.
_INGREDIENT_SPLIT = re.compile(r"\s+[-–—]\s+")

# Un nume de ingredient are câteva cuvinte, nu o propoziție. Peste plafon, capul liniei nu e un
# nume, e o frază — și intră la text, nu la fațetă.
MAX_INGREDIENT_WORDS = 5


def ingredient_head(line: str, *, function_words: frozenset[str] = frozenset()) -> str | None:
    """Capul unei linii de ingrediente = numele canonic candidat; `None` dacă linia nu are unul.

    Nu încearcă să înțeleagă compoziția: taie la primul separator și acceptă rezultatul doar dacă
    arată ca un nume. Restul liniei rămâne TEXT (intră în documentul de căutare, nu în fațetă) —
    „ca text de căutare, excelent; ca fațetă, inutilizabil".

    `function_words` (cuvintele goale ale locale-i, `catalog.query_terms.stopwords`) taie capetele
    TRUNCHIATE: măsurat pe catalogul SOLE, „extract de" ajunsese a cincea cea mai frecventă valoare
    canonică, cu 321 de produse. Nu e un ingredient, e o frază tăiată în două de un separator pus
    altundeva decât ne așteptam. Testul e al limbii, nu al verticalului: un nume nu se termină în
    prepoziție. Fără listă (locale necunoscută) testul nu rulează — nu inventăm una."""
    head = _INGREDIENT_SPLIT.split(line.strip(), maxsplit=1)[0].strip(" .,:;")
    if not head:
        return None
    words = head.lower().split()
    if not words or len(words) > MAX_INGREDIENT_WORDS:
        return None
    if function_words and words[-1] in function_words:
        return None
    return " ".join(words)


@dataclass
class IngredientVocabulary:
    """Vocabularul canonic de ingrediente: cele mai frecvente `limit` capete de linie din catalog.

    Plafonul e cerința cardului (sub 300 de valori distincte) și e o proprietate a FAȚETEI, nu a
    produsului: o fațetă cu zece mii de valori nu e o fațetă, e text. Ce nu intră în vocabular nu se
    aruncă — rămâne în text, unde era și util."""

    limit: int = 250
    counts: dict[str, int] = field(default_factory=dict)

    def observe(self, head: str) -> None:
        self.counts[head] = self.counts.get(head, 0) + 1

    def canonical(self) -> frozenset[str]:
        ordered = sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return frozenset(head for head, _ in ordered[: self.limit])


# --- semnalul persistat -------------------------------------------------------------------------


def signal_name(facet: str, value: str) -> str:
    """Numele semnalului din `product_derived_signals`. Tabela are o singură coloană `signal`, deci
    fațeta și valoarea trăiesc împreună, iar `unique (business_id, product_id, signal, rule_id,
    locale)` devine exact cheia de idempotență de care are nevoie re-derivarea.

    Separatorul e `:` fiindcă nici cheile de fațetă (`[a-z_]`), nici valorile canonice nu-l conțin —
    deci descompunerea e neambiguă, iar o valoare care ar conține-o e un semn că vocabularul e
    stricat, nu ceva de tolerat."""
    return f"{facet}:{value}"
