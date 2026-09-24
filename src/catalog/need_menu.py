"""NX-322 — nevoia clientului: modelul ALEGE din meniu și CITEAZĂ, codul decide excluderea.

Conversația reală `bc7a356e` (`sole-ro`, 2026-09-24): clientul a scris «pai mi se usuca pielea
dupa dus». Pachetul avea cheia (`concern_map`: «piele uscata», «uscaciune» → `dry`), dar schema
uneltei cerea nevoile „în cuvintele clientului", modelul a transcris «se usucă după duș», iar
rezoluția e pe frază exactă ⇒ `concern_unmapped` pe căutare și pe rutină. Clasa e „modelul
transcrie în loc să aleagă" (`concern-map-is-exact-phrase`), iar reparația are două jumătăți care
nu au voie să se confunde:

1. **Alegerea e a modelului**, dintr-un meniu ÎNCHIS de chei canonice (`enum`), ca la meniul de
   rafturi (fix 2026-09-24). Sensul frazei îl decide modelul, cu contextul, nu o listă de cuvinte.
2. **Dreptul de a EXCLUDE e al codului** (D7). Fiecare alegere vine cu citatul care o susține, iar
   codul verifică citatul, nu interpretarea:

   • citatul nu e al clientului ⇒ `rejected` (nici filtru, nici ordonare);
   • citatul conține un alias al ALTEI valori din aceeași dimensiune PARTIȚIONANTĂ ⇒ `rejected`
     (`{"key": "dry", "quote": "am ten gras"}` e o contradicție, nu o nevoie);
   • citatul conține un alias al cheii, din datele tenantului ⇒ `hard` (filtru);
   • altfel ⇒ `soft`: cheia ORDONEAZĂ, nu exclude.

O cheie aleasă semantic greșit costă astfel o ordine mai slabă, nu produse scoase pe nedrept.
Nimic din modul nu știe de cosmetice: dimensiunile de nevoie sunt cele în care traduce harta de
nevoi a tenantului, iar aliasurile sunt ale lui (P9/P11).

Tot modulul e PUR.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from src.catalog.folding import fold_text
from src.catalog.query_terms import content_terms

#: Câte cuvinte de conținut cere un citat. Unul singur («pielea», «ten») nu poate susține o nevoie:
#: se găsește în aproape orice mesaj și ar legitima orice cheie.
MIN_QUOTE_CONTENT_WORDS = 2

NEED_REASONS = frozenset(
    {"unknown_key", "quote_missing", "contradicted", "alias_match", "semantic_only"}
)

_NON_WORD = re.compile(r"[^0-9a-z]+")


class NeedArg(BaseModel):
    """O nevoie aleasă de model: cheia din meniu + cuvintele clientului care o susțin."""

    key: str
    quote: str


@dataclass(frozen=True)
class NeedOption:
    dimension: str
    key: str
    label: str


@dataclass(frozen=True)
class NeedMenu:
    """Meniul tenantului. Ordinea e stabilă (dimensiune, cheie), deci schema e byte-identică între
    ture pe același tenant, adică rămâne în prefixul cache-uit."""

    options: tuple[NeedOption, ...] = ()
    #: dimensiune → {frază normalizată → cheie}, restrânse la valorile din meniu.
    aliases: Mapping[str, Mapping[str, str]] | None = None
    partitioning: frozenset[str] = frozenset()

    def keys(self) -> tuple[str, ...]:
        return tuple(o.key for o in self.options)

    def by_key(self) -> dict[str, NeedOption]:
        return {o.key: o for o in self.options}

    def description(self) -> str:
        """`cheie = etichetă` pentru descrierea parametrului: modelul alege pe SENS. Virgulă, nu
        punct și virgulă: textul ajunge în prompt, iar promptul se scrie în vocea cerută (P13)."""
        return ", ".join(f"{o.key} = {o.label}" for o in self.options)


EMPTY_MENU = NeedMenu()


@dataclass(frozen=True)
class NeedVerdict:
    key: str
    dimension: str | None
    source: Literal["user_explicit", "model_inferred"]
    strength: Literal["hard", "soft", "rejected"]
    reason: str


def _words(text: str) -> str:
    """Forma de comparație: lower, fără diacritice, doar litere și cifre, spații simple."""
    return " ".join(_NON_WORD.sub(" ", fold_text(text or "")).split())


def _contains(haystack: str, phrase: str) -> bool:
    """`phrase` apare în `haystack` ca șir de CUVINTE ÎNTREGI consecutive (nu subșir: «ten» nu e
    în «tenul», «gras» nu e în «grasime»). Ambele deja trecute prin `_words`."""
    return bool(phrase) and f" {phrase} " in f" {haystack} "


def need_dimensions(pack: Any, vocab_dimensions: Iterable[str]) -> tuple[str, ...]:
    """Dimensiunile de NEVOIE ale tenantului: cele în care traduce harta lui de nevoi.

    Nu o listă din cod («skin_type», «concerns»): un tenant de electrocasnice are alte dimensiuni,
    iar harta de nevoi e locul unde își spune ce înseamnă o nevoie. Doar dimensiunile care există
    în vocabularul viu (au produse)."""
    concern_map = dict(getattr(pack, "concern_map", None) or {})
    targets = set(concern_map.values())
    present = set(vocab_dimensions)
    out: list[str] = []
    for facet in getattr(pack, "facets", ()) or ():
        values = set(getattr(facet, "values", ()) or ())
        if facet.key in present and values & targets:
            out.append(facet.key)
    return tuple(sorted(out))


def build_menu(
    pack: Any,
    entries: Mapping[str, Sequence[Any]],
    overlays: Mapping[str, Mapping[str, str]] | None,
    locale: str | None,
) -> NeedMenu:
    """Meniul: valorile CU PRODUSE ale dimensiunilor de nevoie, etichetate din pachet.

    `entries` = `dimensiune → VocabEntry` (vocabularul viu), `overlays` = `facet_overlays(...)`.
    O cheie care apare în două dimensiuni intră o singură dată (prima, în ordine stabilă): enumul
    trebuie să fie o mulțime, iar două sensuri sub același nume ar fi o alegere pe care modelul
    n-o poate face."""
    dims = need_dimensions(pack, entries.keys())
    partitioning = frozenset(
        f.key
        for f in (getattr(pack, "facets", ()) or ())
        if f.key in dims and getattr(f, "binding", "") == "partitioning"
    )
    seen: set[str] = set()
    options: list[NeedOption] = []
    for dim in dims:
        for entry in sorted(entries.get(dim, ()), key=lambda e: e.key):
            if entry.count <= 0 or entry.key in seen:
                continue
            seen.add(entry.key)
            label = (
                pack.value_label(dim, entry.key, locale) if hasattr(pack, "value_label") else None
            )
            options.append(NeedOption(dim, entry.key, label or entry.label or entry.key))
    valid = {o.dimension: set() for o in options}
    for o in options:
        valid[o.dimension].add(o.key)
    aliases = {
        dim: {
            _words(phrase): key
            for phrase, key in (overlays or {}).get(dim, {}).items()
            if key in keys and _words(phrase)
        }
        for dim, keys in valid.items()
    }
    return NeedMenu(tuple(options), aliases, partitioning)


def quote_supported(quote: str, texts: Sequence[str], locale: str | None) -> bool:
    """Citatul apare în vreun mesaj al CLIENTULUI, pe cuvinte întregi, și are măcar
    `MIN_QUOTE_CONTENT_WORDS` cuvinte de conținut (lista de cuvinte goale e a locale-i)."""
    q = _words(quote)
    if not q or len(content_terms(q, locale)) < MIN_QUOTE_CONTENT_WORDS:
        return False
    return any(_contains(_words(t), q) for t in texts)


def need_verdict(
    arg: NeedArg, menu: NeedMenu, texts: Sequence[str], locale: str | None
) -> NeedVerdict:
    """Contractul hard/soft/rejected (vezi docstring-ul modulului). PUR."""
    option = menu.by_key().get(arg.key)
    if option is None:
        return NeedVerdict(arg.key, None, "model_inferred", "rejected", "unknown_key")
    dim = option.dimension
    if not quote_supported(arg.quote, texts, locale):
        return NeedVerdict(arg.key, dim, "model_inferred", "rejected", "quote_missing")
    quote = _words(arg.quote)
    phrases = (menu.aliases or {}).get(dim, {})
    if dim in menu.partitioning and any(
        key != arg.key and _contains(quote, phrase) for phrase, key in phrases.items()
    ):
        return NeedVerdict(arg.key, dim, "user_explicit", "rejected", "contradicted")
    if any(key == arg.key and _contains(quote, phrase) for phrase, key in phrases.items()):
        return NeedVerdict(arg.key, dim, "user_explicit", "hard", "alias_match")
    return NeedVerdict(arg.key, dim, "user_explicit", "soft", "semantic_only")


def split_needs(
    verdicts: Iterable[NeedVerdict],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Verdictele → (`hard`: dimensiune → chei de FILTRU, `soft`: dimensiune → chei de ORDONARE)."""
    hard: dict[str, list[str]] = {}
    soft: dict[str, list[str]] = {}
    for v in verdicts:
        if v.dimension is None or v.strength == "rejected":
            continue
        bucket = hard if v.strength == "hard" else soft
        keys = bucket.setdefault(v.dimension, [])
        if v.key not in keys:
            keys.append(v.key)
    return hard, soft


def split_args(raw: Iterable[Any] | None) -> tuple[list[str], list[NeedArg]]:
    """`concerns` din argumentele modelului → (string-urile de azi, nevoile din meniu).

    Aceeași listă poate purta ambele forme: un model vechi sau un replay trimite string-uri chiar
    cu schema nouă, iar drumul lor rămâne cel de azi (`resolve_any`)."""
    legacy: list[str] = []
    needs: list[NeedArg] = []
    for item in raw or ():
        if isinstance(item, NeedArg):
            needs.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("key"), str):
            needs.append(NeedArg(key=item["key"], quote=str(item.get("quote") or "")))
        elif isinstance(item, str) and item.strip():
            legacy.append(item)
    return legacy, needs
