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
from dataclasses import dataclass, replace

from src.catalog.vocabulary import CATEGORY_DIMENSION, CatalogVocabulary, topic_root_of
from src.conversation.delta import TurnDelta
from src.conversation.interpretation import Act, CheckedChange, TurnInterpretation
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import ConversationStateV2, Topic
from src.domain.routine_steps import SEP

#: Intrarea generală din `DomainPack.bundle_executors`: unealta rutinei pe orice raft.
WILDCARD = "*"
#: Dimensiunea tipului de produs într-o schimbare scrisă de model.
PRODUCT_TYPE_DIMENSION = "product_type"


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


#: NX-389b: actul pe care îl ține minte întrebarea de familie (`PendingClarification.resume_route`).
RESUME_BUNDLE = "bundle"
#: Actele unui răspuns la întrebarea de familie: o cerere fără țintă care nu e alt fel de act.
#: Clientul scrie «pentru față» (modelul poate citi `find`), «nu știu» (`other`, `chitchat` sau
#: niciun act) sau repetă rutina (`bundle`). Orice altceva (`detail`, `cart`, `store_info`…) e o
#: cerere nouă, servită ca ea însăși.
_ANSWER_ACTS = frozenset({"find", "bundle", "other", "chitchat"})


@dataclass(frozen=True)
class ResumedRoutine:
    """NX-389b: turul răspunde la întrebarea de familie a unei rutini. `chosen` = cheia raftului
    ales (una din opțiunile întrebării), `None` când clientul n-a ales (poarta face rutina pe
    familia majoritară și o spune); `others` = celelalte opțiuni numite («ambele»), oferite după;
    `asked_at_revision` = revizia turului întrebării (bugetul spus atunci e al rutinei)."""

    chosen: str | None
    others: tuple[str, ...]
    asked_at_revision: int


def resume_routine(
    interp: TurnInterpretation, state: ConversationStateV2
) -> tuple[TurnInterpretation, ResumedRoutine | None]:
    """NX-389b (`kernel.v9.0`): interpretarea EFECTIVĂ a unui tur care răspunde la întrebarea de
    familie. PUR. Întrebarea ține minte actul (`resume_route = "bundle"`) și opțiunile (cheile
    rafturilor, `options_refs`), deci CODUL decide că turul continuă rutina, nu actul pe care
    modelul l-a scris pentru un răspuns scurt («pentru față» citit ca `find`).

    Se aplică doar cu o întrebare de familie vie, pe un tur care nu e paranteză și ale cărui acte
    sunt toate un răspuns (`_ANSWER_ACTS`, fără ținte). Un raft din afara opțiunilor sau un tip de
    produs înseamnă o cerere nouă: interpretarea rămâne a modelului. Altfel actele devin un singur
    `bundle` (fără nicio opțiune aleasă, doar când modelul a repetat actul întrebării, regula
    promptului pentru «alege tu»); dintre rafturile numite care sunt opțiuni rămâne primul în
    ordinea opțiunilor (cea mai
    mare familie), iar celelalte se oferă după («ambele»). Rafturile se compară pe CHEIE, exact:
    «față» citit de model pe `machiaj-fata` nu e opțiunea `ten`, deci nu reia rutina de machiaj."""
    pending = state.pending_clarification
    if (
        pending is None
        or pending.expired_at(state.revision)
        or pending.resume_route != RESUME_BUNDLE
        or not pending.options_refs
        or interp.thread == "aside"
    ):
        return interp, None
    if any(a.kind not in _ANSWER_ACTS or a.targets for a in interp.acts):
        return interp, None
    options = tuple(pending.options_refs)
    named: list[str] = []
    for change in interp.changes:
        if change.dimension == PRODUCT_TYPE_DIMENSION:
            return interp, None
        if change.dimension != CATEGORY_DIMENSION:
            continue
        value = change.value if isinstance(change.value, str) else None
        if value not in options:
            return interp, None
        if value not in named:
            named.append(value)
    ordered = [o for o in options if o in named]
    chosen = ordered[0] if ordered else None
    if chosen is None and not any(a.kind == RESUME_BUNDLE for a in interp.acts):
        # Fără alegere, doar actul întrebării repetat («alege tu», «nu știu») reia rutina; un
        # refuz («nu mai vreau») sau o vorbă de politețe nu pornește o rutină pe care clientul n-o
        # mai cere (recenzia 389b).
        return interp, None
    kept = [
        c
        for c in interp.changes
        if c.dimension != CATEGORY_DIMENSION or (chosen is not None and c.value == chosen)
    ]
    # un singur `set` pe raftul ales (delta păstrează ULTIMA valoare, `subject_multiple`)
    seen_chosen = False
    changes = []
    for c in kept:
        if c.dimension == CATEGORY_DIMENSION:
            if seen_chosen:
                continue
            seen_chosen = True
        changes.append(c)
    effective = interp.model_copy(
        update={
            "acts": [Act(kind=RESUME_BUNDLE, targets=[], query=None)],
            "changes": changes,
        }
    )
    resumed = ResumedRoutine(
        chosen=chosen,
        others=tuple(o for o in ordered[1:]),
        asked_at_revision=pending.asked_at_revision,
    )
    return effective, resumed


def kept_checked(
    checked: Sequence[CheckedChange], interp: TurnInterpretation
) -> tuple[CheckedChange, ...]:
    """Verificările validatorului pentru schimbările păstrate de `resume_routine` (aceleași
    obiecte: interpretarea efectivă e o copie care le păstrează, fără rafturile în plus)."""
    kept = [id(c) for c in interp.changes]
    return tuple(c for c in checked if id(c.change) in kept)


def answer_topic(delta: TurnDelta, resumed: ResumedRoutine | None, turn_id: str) -> TurnDelta:
    """NX-389b: raftul ales la întrebarea de familie intră în subiect, chiar dacă validatorul a
    respins citatul (un omograf: «față» numește și subraftul de machiaj). Opțiunea e din meniul
    NOSTRU, iar clientul a ales-o: e un fapt spus de el (`user_explicit`, ca răspunsul la o
    clarificare pe calea v1). Orice altă propunere de subiect a turului cedează locul ei. Fără
    alegere (`None`), delta rămâne a turului; numărat `routine_resumed` în ambele cazuri."""
    if resumed is None:
        return delta
    counters = {**delta.counters, "routine_resumed": 1}
    if resumed.chosen is None:
        return replace(delta, counters=counters)
    topic = StateUpdateProposal(
        "set_topic",
        category_key=resumed.chosen,
        product_type=None,
        source="user_explicit",
        turn_id=turn_id,
        strength="hard",
        origin="interpretation",
    )
    proposals = tuple(p for p in delta.proposals if p.op != "set_topic")
    return replace(delta, proposals=(topic, *proposals), counters=counters)


__all__ = [
    "RESUME_BUNDLE",
    "WILDCARD",
    "FamilyOption",
    "ResumedRoutine",
    "resume_routine",
    "answer_topic",
    "kept_checked",
    "bundle_executor",
    "family_of_shelf",
    "routine_family",
    "routine_family_options",
    "subject_kinds",
]
