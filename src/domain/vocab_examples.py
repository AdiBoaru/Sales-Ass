"""NX-273 — exemplele care ajung la model vin din pachetul tenantului. Pur, fără I/O.

Poarta NX-264, la prima rulare, a găsit 36 de scurgeri de domeniu în `src/`, și toate erau în
același loc conceptual: **textele care ajung la model**. Promptul agentului, descrierile schemei de
tool-uri, sugestiile de start, promptul de triaj — toate cu exemple de beauty scrise de mână.

Nu sunt greșeli de neatenție. Sunt exemple puse ca să funcționeze bine pe clientul de azi, și exact
de aia sunt periculoase: fac sistemul mai bun pe SOLE și mai prost pe altcineva, **fără niciun
semnal că s-a întâmplat**. Un magazin de electrocasnice ar primi exemple despre ten gras, iar
modelul le-ar citi ca indicii despre ce se caută acolo.

Cel mai subtil e `tool_definitions`: exemplele din descrierea unui parametru nu sunt documentație,
sunt INSTRUCȚIUNI. Un model care citește „ex. «ten gras»" învață ce fel de valori se așteaptă acolo.

## De ce selecția e deterministă

Prefixul static byte-identic e ce dă reducerea de 75-90% la prompt caching (stagiul 6). Un prompt
compus dinamic o pierde dacă selecția sau ordinea variază de la tur la tur. Deci: aceleași intrări
⇒ aceiași octeți, verificat prin test, nu prin intenție.

Ordinea de selecție e **ordinea din pachet**, nu alfabetică. Un pachet e scris de om sau derivat cu
frecvențele în față, deci primele intrări sunt cele reprezentative; alfabetic ar alege arbitrar
(„acnee" înaintea a orice, oricât de marginală ar fi). Ordinea din pachet e o decizie a tenantului,
și e la fel de stabilă.

## Fără pachet, fără exemple

Un pachet gol sau stricat produce `VocabExamples()` — adică nicio clauză „(ex. …)" în prompt, nu o
clauză cu exemple de beauty. Textul rămâne valid și neutru: e diferența dintre a nu ști ce vinde
clientul și a presupune greșit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Câte exemple per categorie. Două ajung ca modelul să înțeleagă FORMA valorii („o frază scurtă în
# cuvintele clientului"), iar mai multe ar umfla prefixul static pe care îl plătim la fiecare tur.
DEFAULT_LIMIT = 2


@dataclass(frozen=True, slots=True)
class VocabExamples:
    """Exemplele de vocabular ale unui tenant, gata de pus în prompt. HASHABLE: instanța ajunge
    câmp în `PromptInputs`, care e cheie de `lru_cache` (vezi `prompt_builder`)."""

    needs: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.needs or self.features or self.categories)


EMPTY_EXAMPLES = VocabExamples()


def _first_distinct(values: Iterable[str], limit: int) -> tuple[str, ...]:
    """Primele `limit` valori distincte, în ordinea dată. Ordinea E selecția — vezi antetul."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


def from_pack(pack: Any, *, limit: int = DEFAULT_LIMIT) -> VocabExamples:
    """DomainPack → exemple. Pachet absent/gol/stricat → `EMPTY_EXAMPLES` (fail-safe, P6).

    `needs` vin din frazele lui `concern_map`, adică din CUVINTELE CLIENTULUI — exact ce trebuie să
    pasezi în `concerns`, nu cheia canonică. `features` vin din valorile fațetelor declarate ca
    `searchable_facets`: sunt lucrurile pe care clientul le poate cere explicit („cu X")."""
    if pack is None:
        return EMPTY_EXAMPLES
    concern_map = getattr(pack, "concern_map", None)
    needs = _first_distinct(concern_map, limit) if isinstance(concern_map, Mapping) else ()

    searchable = getattr(pack, "searchable_facets", ()) or ()
    facets = {f.key: f for f in (getattr(pack, "facets", ()) or ())}
    feature_values: list[str] = []
    for key in searchable:
        spec = facets.get(key)
        for value in getattr(spec, "values", ()) or ():
            feature_values.append(str(value))
    return VocabExamples(needs=needs, features=_first_distinct(feature_values, limit))


def with_categories(examples: VocabExamples, categories: Sequence[str], *, limit: int = 2):
    """Adaugă exemple de CATEGORIE. Vin din catalog (nu din pachet), deci se transmit separat —
    un magazin fără pachet de domeniu are totuși categorii, iar sugestiile de start se pot compune
    doar pe ele."""
    return VocabExamples(
        needs=examples.needs,
        features=examples.features,
        categories=_first_distinct(categories, limit),
    )


def clause(values: Sequence[str], *, prefix: str = "ex. ") -> str:
    """Clauza „(ex. „a", „b")" sau ȘIRUL GOL. Golul e important: fără exemple, propoziția trebuie
    să rămână corectă, nu să conțină un „(ex. )" care arată a bug și îl învață pe model că lista e
    goală. Ghilimelele sunt cele românești folosite deja în prompturi (P13 le lasă neatinse)."""
    kept = [v for v in values if v]
    if not kept:
        return ""
    inner = ", ".join(f"„{v}”" for v in kept)
    return f" ({prefix}{inner})"
