"""Detecție de limbă RO/HU/EN — determinist, fără LLM (G5c).

Pe stopwords + diacritice specifice. Folosit de `language_stage` (stagiul 3) ca să
seteze `ctx.language` corect ÎNAINTE de straturile locale-keyed (cache, faqs, triaj) —
principiul 11 („limba e parte din cheie"). Precision-first: la incertitudine întoarce
None (= păstrăm limba curentă, NU ghicim). Cod pur: niciun I/O, nu aruncă pe input.
"""

import re

# Stopwords compacte, frecvente, cât mai DISTINCTE între limbi (constante lingvistice,
# nu config de tenant). Cuvinte tokenizate, lowercase, cu diacritice păstrate.
_STOPWORDS: dict[str, frozenset[str]] = {
    "ro": frozenset(
        {
            "și",
            "sau",
            "este",
            "sunt",
            "vreau",
            "caut",
            "salut",
            "bună",
            "mulțumesc",
            "preț",
            "prețul",
            "cât",
            "costă",
            "pentru",
            "ce",
            "cu",
            "la",
            "un",
            "o",
            "te",
            "mă",
            "îmi",
            "aveți",
            "cumpăr",
            "doresc",
            "despre",
            "meu",
            "mea",
            "fără",
            "vă",
            "rog",
            "bine",
            "da",
            "nu",
        }
    ),
    "hu": frozenset(
        {
            "szia",
            "köszönöm",
            "kérek",
            "kérem",
            "hogyan",
            "van",
            "nincs",
            "igen",
            "nem",
            "és",
            "vagy",
            "ár",
            "ára",
            "szállítás",
            "szeretnék",
            "mennyibe",
            "kérdés",
            "akarok",
            "keresek",
            "egy",
            "egész",
            "kérlek",
            "köszi",
            "rendelés",
            "termék",
            "vásárolni",
            "milyen",
        }
    ),
    "en": frozenset(
        {
            "the",
            "you",
            "what",
            "price",
            "do",
            "have",
            "hello",
            "want",
            "need",
            "how",
            "much",
            "can",
            "is",
            "are",
            "for",
            "with",
            "please",
            "looking",
            "buy",
            "your",
            "and",
            "or",
            "thanks",
            "hi",
            "would",
            "like",
            "about",
        }
    ),
}

# Diacritice specifice → bonus de scor (semnal tare). HU `ő ű` sunt distincte de RO.
_DIACRITICS: dict[str, frozenset[str]] = {
    "ro": frozenset("ăâîșț"),
    "hu": frozenset("őű"),
    "en": frozenset(),
}
_DIACRITIC_BONUS = 2

_WORD_RE = re.compile(r"[a-zà-ÿ]+")


def detect_language(text: str | None, supported: list[str]) -> str | None:
    """Limba mesajului dintre cele `supported` (RO/HU/EN), sau None dacă semnalul
    nu e clar. Scor per limbă = nr. stopwords + bonus diacritice specifice; întoarce
    limba cu scorul maxim DOAR dacă e ≥ 1 și strict peste a doua (margine)."""
    if not text:
        return None
    lowered = text.lower()
    tokens = set(_WORD_RE.findall(lowered))
    chars = set(lowered)

    scores: dict[str, int] = {}
    for lang in ("ro", "hu", "en"):
        if lang not in supported:
            continue
        score = len(tokens & _STOPWORDS[lang])
        if chars & _DIACRITICS[lang]:
            score += _DIACRITIC_BONUS
        scores[lang] = score

    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_lang, best_score = ranked[0]
    if best_score < 1:
        return None
    # margine: a doua limbă strict sub cea mai bună (tie → incertitudine → None).
    if len(ranked) > 1 and ranked[1][1] >= best_score:
        return None
    return best_lang
