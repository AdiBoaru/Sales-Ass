"""NX-369 — o regulă a magazinului parafrazată se servește în cuvintele MAGAZINULUI.

Turul c14 din 2026-10-01 („daca nu-mi place o crema o pot returna?"): modelul a chemat
`faq_lookup` și a răspuns corect („Da, poți solicita returul în 30 de zile… pe
www.service-return.com…"), dar parafrazat. Validatorul de proză acceptă din regulile magazinului
doar propoziția citată ÎNTREAGĂ (NX-346, `strip_quoted`), deci a respins „30" și „zile", iar
clientul a primit „Momentan n-am găsit produse potrivite". Pe c7 („cat costa livrarea?") același
eșec a reîncărcat cardurile de dinainte sub răspuns.

Aici fiecare propoziție a prozei respinse se leagă de regula pe care o redă, pe cuvintele de
conținut ale locale-i (`query_terms.content_terms`, aceeași tokenizare ca la căutare), iar
răspunsul devine textul ACELOR reguli, întreg, în ordinea în care modelul le-a folosit. Nicio
afirmație a modelului nu ajunge la client: doar alegerea regulilor e a lui, textul e al
magazinului. PUR.

Prag (măsurat pe c14 și c7): o propoziție se leagă doar dacă cel puțin jumătate din cuvintele ei
de conținut sunt în regulă, iar regula câștigă cu cel puțin 0,1 față de următoarea. La egalitate
rămâne regula deja aleasă; o egalitate exactă (FAQ-uri care spun același lucru) ia prima regulă.
Propozițiile cu sub trei cuvinte de conținut („Vrei și altceva?") nu leagă nimic.

Recenzia adversarială: (1) o regulă se servește doar dacă și MESAJUL CLIENTULUI are un cuvânt comun
cu ÎNTREBAREA ei (alegerea modelului singură nu ajunge); (2) `complete` spune dacă fiecare
propoziție substanțială a prozei s-a legat de o regulă, ca apelantul să nu piardă tăcut jumătatea
de produs a unei întrebări mixte; (3) o regulă cuprinsă ≥ 80% într-una deja aleasă nu se repetă;
(4) propozițiile se despart și pe rânduri și liste.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.catalog.query_terms import any_locale_stopwords, content_terms, stopwords
from src.catalog.render_text import display_name, name_keys, unique_prefixes

MIN_SHARE = 0.5
MIN_MARGIN = 0.1
MIN_TERMS = 3
#: O regulă ale cărei rădăcini sunt în proporția asta într-o regulă deja aleasă spune același lucru.
CONTAINED = 0.8
#: Comparațiile pe proporții sunt pe float: 0,6 − 0,5 iese 0,0999…, deci marja de 0,1 ar pica.
_EPS = 1e-9
#: Rădăcina unui cuvânt: primele caractere, ca flexiunea („returul"/„returna", „cutia"/„cutie") să
#: nu rupă potrivirea. Doar pentru potrivire, niciodată pentru text servit.
_STEM = 5
_SENTENCE_SPLIT = re.compile(r"(?<![0-9].)(?<=[.!?])\s+")
_LINE_SPLIT = re.compile(r"\n+")
_BULLET = re.compile(r"^\s*(?:[-*•·]|\d{1,2}[.)])\s+")


def _stems(text: str, locale: str | None) -> set[str]:
    return {t[:_STEM] for t in content_terms(text, locale)}


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in _LINE_SPLIT.split(text):
        line = _BULLET.sub("", line)
        out.extend(s for s in _SENTENCE_SPLIT.split(line) if s.strip())
    return out


@dataclass(frozen=True)
class RuleMatch:
    """`rules` = textele regulilor de servit, în ordinea primei folosiri. `complete` = fiecare
    propoziție substanțială a prozei redă o regulă (niciuna nu vorbește despre altceva)."""

    rules: tuple[str, ...] = ()
    complete: bool = False
    unmapped: int = 0
    unasked: int = 0


def match_rules(
    text: str | None,
    sources: Sequence[str],
    locale: str | None,
    *,
    client: str | None = None,
    questions: Mapping[str, str] | None = None,
) -> RuleMatch:
    """Regulile magazinului (din `sources`, textul exact arătat modelului) pe care `text` le
    parafrazează. Cu `client`, o regulă rămâne doar dacă mesajul clientului are o rădăcină comună cu
    întrebarea ei (`questions`: răspuns → întrebare; fără întrebare cunoscută, cu răspunsul)."""
    if not text or not sources:
        return RuleMatch()
    rules = [_stems(s, locale) for s in sources]
    chosen: list[int] = []
    unmapped = 0
    for sentence in _sentences(text):
        words = _stems(sentence, locale)
        if len(words) < MIN_TERMS:
            continue
        scored = sorted(
            ((len(words & r) / len(words), i) for i, r in enumerate(rules)),
            key=lambda x: (-x[0], x[1]),
        )
        best, idx = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best < MIN_SHARE - _EPS:
            unmapped += 1  # propoziția vorbește despre altceva decât regulile
            continue
        if best - second < MIN_MARGIN - _EPS:
            close = [i for s, i in scored if best - s < MIN_MARGIN - _EPS]
            already = [i for i in close if i in chosen]
            if already:
                idx = already[0]  # aceeași regulă, folosită deja de o propoziție anterioară
            elif abs(best - second) > _EPS:
                continue  # două reguli diferite, aproape la egalitate: ambiguu, nu leagă nimic
            # egalitate EXACTĂ: reguli care spun același lucru; rămâne prima (`idx`)
        if idx in chosen:
            continue
        if any(
            rules[idx] and len(rules[idx] & rules[c]) / len(rules[idx]) >= CONTAINED - _EPS
            for c in chosen
        ):
            continue  # aceeași regulă, în alte cuvinte ale magazinului
        chosen.append(idx)
    unasked = 0
    if client is not None:
        asked = _stems(client, locale)
        kept = []
        for i in chosen:
            question = (questions or {}).get(sources[i]) or sources[i]
            if asked & _stems(question, locale):
                kept.append(i)
            else:
                unasked += 1
        chosen = kept
    return RuleMatch(
        rules=tuple(sources[i] for i in chosen),
        complete=bool(chosen) and unmapped == 0,
        unmapped=unmapped,
        unasked=unasked,
    )


def quoted_rules(
    text: str | None,
    sources: Sequence[str],
    locale: str | None,
    *,
    client: str | None = None,
    questions: Mapping[str, str] | None = None,
) -> list[str]:
    """Doar regulile din `match_rules` (vezi acolo). Gol = nicio propoziție nu se leagă clar."""
    return list(match_rules(text, sources, locale, client=client, questions=questions).rules)


def names_any(text: str | None, names: Sequence[str], locale: str | None) -> bool:
    """`True` dacă `text` numește măcar unul dintre produse prin prefixul lui UNIC în set
    (`render_text.unique_prefixes`, NX-318, pe numele scurt de pe card). Un prefix de un cuvânt
    trebuie să aibă măcar trei caractere și să nu fie cuvânt gol pe locale. PURĂ."""
    if not text or not names:
        return False
    short = {str(i): display_name(n) for i, n in enumerate(names) if n}
    prefixes = unique_prefixes(short, locale=locale)
    keys = tuple(k for k in name_keys(text) if k)
    empty = stopwords(locale) if locale else any_locale_stopwords()
    for prefix in prefixes.values():
        prefix = tuple(k for k in prefix if k)
        if not prefix or (len(prefix) == 1 and (len(prefix[0]) < 3 or prefix[0] in empty)):
            continue
        n = len(prefix)
        if any(keys[i : i + n] == prefix for i in range(len(keys) - n + 1)):
            return True
    return False
