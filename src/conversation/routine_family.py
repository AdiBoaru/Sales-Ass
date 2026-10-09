"""Familia unei rutini (față, păr, corp, machiaj): din subiect, din nevoi sau din răspunsul
clientului. PUR.

Mutat din `src/agent/turn_planner.py` la NX-389 (`kernel.v9.0`), ca poarta de ambiguitate
(`ambiguity_gate`, pe care plannerul o importă) să poată citi aceeași regulă fără import circular.
Plannerul re-exportă numele, deci apelanții de azi rămân neschimbați.

Familia e o dată a TENANTULUI (P9): `routine_steps.family_by_shelf` (raft → familie),
`by_product_type` (tip → `familie:pas`), `family_by_need` (nevoie → familie, majoritatea clară pe
catalog) și, de la NX-389, `family_counts_by_need` / `family_counts` (câte produse poate servi
fiecare familie), toate derivate din catalog de scripturile din `scripts/`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.catalog.vocabulary import CatalogVocabulary, topic_root_of
from src.conversation.state_v2 import ConversationStateV2, Topic
from src.domain.routine_steps import SEP

#: Intrarea generală din `DomainPack.bundle_executors`: unealta rutinei pe orice raft.
WILDCARD = "*"


def subject_kinds(topic: Topic) -> tuple[str, ...]:
    """NX-350: tipurile pe care le ordonează subiectul. Fără tip spus clar, UMBRELA (toate codurile
    cuvântului clientului); un tip DEDUS de cod nu o bate (recenzia NX-350, constatarea 7)."""
    stated = topic.product_type if not (topic.type_learned and topic.type_umbrella) else None
    return (stated,) if stated else topic.type_umbrella


def routine_family(
    state: ConversationStateV2, *, pack: object | None, vocab: CatalogVocabulary | None
) -> str | None:
    """Familia rutinei pentru subiectul stării. PUR. Ordinea, de la cel mai precis la cel mai
    grosier (recenzia D3):

    1. TIPUL subiectului, prin `routine_steps.by_product_type` (tipul → `familie:pas`): o «cremă de
       față» e a rutinei de față chiar pe raftul `machiaj-fata` (NX-313: „Fata" e machiaj);
    2. cheia raftului în `family_by_shelf` (o intrare pe un subraft bate rădăcina lui);
    3. rădăcina raftului în `family_by_shelf`.

    4. NX-386 (`kernel.v7.1`): fără tip și fără raft cu familie, nevoile ACTIVE ale stării, prin
       `routine_steps.family_by_need` (dată derivată din catalog), doar când toate cele cunoscute
       duc la aceeași familie («o rutină de seară pentru pete» ⇒ fața).

    `None` fără nicio potrivire: planul `bundle` rămâne pe calea de azi (sau, cu NX-389, poarta
    întreabă familia)."""
    spec = getattr(pack, "routine_steps", None)
    families = getattr(spec, "families", None) or {}
    by_type = getattr(spec, "by_product_type", None) or {}
    # NX-350: tipurile subiectului (`subject_kinds`: tipul spus clar, altfel umbrela; un tip DEDUS
    # nu bate umbrela). Mai multe decid familia doar când TOATE cele cunoscute sunt ale aceleiași.
    seen = {str(by_type[k]).partition(SEP)[0] for k in subject_kinds(state.topic) if k in by_type}
    if len(seen) == 1 and next(iter(seen)) in families:
        return next(iter(seen))
    key = state.topic.category_key
    shelf_family = family_of_shelf(key, pack=pack, vocab=vocab)
    if shelf_family is not None:
        return shelf_family
    by_need = getattr(spec, "family_by_need", None) or {}
    if key or not isinstance(by_need, Mapping) or not by_need:
        # un raft numit fără familie nu e luat de nevoi: clientul a spus unde, iar ce nu e o
        # familie de rutină (ex. un raft de accesorii) rămâne pe calea de azi
        return None
    found = {
        str(by_need[f"{n.key}:{n.normalized_value}"])
        for n in state.active_needs()
        if isinstance(n.normalized_value, str) and f"{n.key}:{n.normalized_value}" in by_need
    }
    if len(found) == 1 and next(iter(found)) in families:
        return next(iter(found))
    return None


def family_of_shelf(
    key: str | None, *, pack: object | None, vocab: CatalogVocabulary | None
) -> str | None:
    """Familia unui raft prin `family_by_shelf`: cheia lui, apoi rădăcina (regulile 2 și 3)."""
    table = getattr(getattr(pack, "routine_steps", None), "family_by_shelf", None)
    if not key or not isinstance(table, Mapping) or not table:
        return None
    usable = vocab if vocab is not None and not vocab.is_empty() else None
    root = topic_root_of(usable, key) if usable is not None else None
    for shelf in (key, root):
        if shelf and shelf in table:
            return str(table[shelf])
    return None


def bundle_executor(
    state: ConversationStateV2,
    *,
    pack: object | None,
    vocab: CatalogVocabulary | None,
    family: str | None = None,
) -> str | None:
    """Numele uneltei care servește `bundle` pe subiectul stării, sau None: fără subiect, sau fără
    intrare în `DomainPack.bundle_executors` pentru rădăcina raftului, raft sau `"*"`. PUR; pasul 6
    îl cheamă ca să afle CE unealtă execută planul `bundle`. Fără subiect: doar intrarea `"*"`, și
    doar când familia rutinei e cunoscută: din nevoi (NX-386) sau decisă de poartă (`family`,
    NX-389: singura familie servibilă sau răspunsul clientului)."""
    topic = state.topic
    table = getattr(pack, "bundle_executors", None)
    if not isinstance(table, Mapping) or not table:
        return None
    if not topic.has_subject:
        # NX-386 (`kernel.v7.1`): fără subiect, executorul general al pachetului, DOAR când familia
        # rutinei e cunoscută; altfel o căutare, ca până acum
        known = family or routine_family(state, pack=pack, vocab=vocab)
        if WILDCARD in table and known is not None:
            return str(table[WILDCARD])
        return None
    usable = vocab if vocab is not None and not vocab.is_empty() else None
    root = topic_root_of(usable, topic.category_key) if usable is not None else None
    for key in (root, topic.category_key, WILDCARD):
        if key and key in table:
            return str(table[key])
    return None


@dataclass(frozen=True)
class FamilyOption:
    """O familie pe care rutina o poate servi: raftul care o numește (cheia din
    `family_by_shelf`, ce se oferă clientului) și câte produse are pentru nevoile turului."""

    family: str
    shelf: str
    count: int


def routine_family_options(
    state: ConversationStateV2,
    *,
    pack: object | None,
    families: Sequence[str] | None = None,
) -> tuple[FamilyOption, ...]:
    """NX-389: familiile între care se poate alege o rutină fără familie, cele mai mari întâi. PUR.

    Numărătoarea unei familii e suma, pe nevoile ACTIVE cu intrare în `family_counts_by_need`, a
    produselor ei; fără nicio nevoie cunoscută, totalul din `family_counts`. O familie intră doar cu
    cel puțin `min_family_products` produse (altfel n-ar putea servi o rutină) și doar dacă are un
    raft în `family_by_shelf` (răspunsul clientului e un raft). `families` restrânge alegerea la
    lecturile modelului (contextul acestui client bate statistica pe catalog). Fără date ⇒ ()."""
    spec = getattr(pack, "routine_steps", None)
    declared = getattr(spec, "families", None) or {}
    by_need = getattr(spec, "family_counts_by_need", None) or {}
    totals = getattr(spec, "family_counts", None) or {}
    minimum = int(getattr(spec, "min_family_products", 0) or 0)
    shelves = getattr(spec, "family_by_shelf", None) or {}
    keys = [
        f"{n.key}:{n.normalized_value}"
        for n in state.active_needs()
        if isinstance(n.normalized_value, str)
    ]
    known = [by_need[k] for k in keys if k in by_need]
    counts: dict[str, int] = {}
    if known:
        for row in known:
            for fam, n in row.items():
                counts[fam] = counts.get(fam, 0) + int(n)
    else:
        counts = {fam: int(n) for fam, n in totals.items()}
    shelf_of: dict[str, str] = {}
    for shelf, fam in shelves.items():
        shelf_of.setdefault(str(fam), str(shelf))
    allowed = set(families) if families is not None else None
    options = [
        FamilyOption(family=fam, shelf=shelf_of[fam], count=n)
        for fam, n in counts.items()
        if fam in declared
        and fam in shelf_of
        and n >= max(1, minimum)
        and (allowed is None or fam in allowed)
    ]
    return tuple(sorted(options, key=lambda o: (-o.count, o.family)))


__all__ = [
    "WILDCARD",
    "FamilyOption",
    "bundle_executor",
    "family_of_shelf",
    "routine_family",
    "routine_family_options",
    "subject_kinds",
]
