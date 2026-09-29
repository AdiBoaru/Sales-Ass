"""NX-329 — catalogul unui pachet de fixture, OFFLINE, în tipurile resolverului.

Resolverul de referințe primește faptele ca argument (`ReferenceFacts`), deci suita lui și stratul
de replay nu au nevoie de DB: faptele se construiesc aici din produsele pachetului, cu ACELEAȘI
reguli ca interogările din `src/db/queries/catalog.py`:

- `available`: `in_stock` ⇒ True, `out_of_stock` ⇒ False, altceva ⇒ necunoscut;
- `named`: numele DISTINCTIV (capul dinaintea lui „ - " și a primei virgule, ≥ 8 caractere) conținut
  în numele cerut, cel mai lung primul (predicatul lui `find_product_named_in_query`);
- vocabularul: fiecare cheie din `attributes`, cu numărul de produse pe valoare (`load_vocabulary`
  fără pragurile de catalog mare, care pe 30 de produse ar tăia tot)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

from src.agent.turn_planner import PlannedTurn, plan_turn
from src.catalog.folding import fold_text
from src.catalog.vocabulary import CATEGORY_DIMENSION, CatalogVocabulary, VocabEntry
from src.conversation.ambiguity_gate import (
    GateOutcome,
    decide_ambiguity,
    lookup_attributes,
    question_answered,
)
from src.conversation.ambiguity_gate import memory_proposal as _memory_proposal
from src.conversation.answer_policy import answer_policy
from src.conversation.clarification_policy import ClarificationPolicy
from src.conversation.delta import TurnDelta, to_delta
from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    AmbiguityDecision,
    AnswerPolicy,
    CheckedChange,
    ResolvedRef,
    TurnInterpretation,
    TurnPlan,
)
from src.conversation.kernel_trace import KernelTrace
from src.conversation.kernel_trace import state_view as _state_view
from src.conversation.needs import NeedVocabulary
from src.conversation.provenance import UserWords, check_changes, need_handles
from src.conversation.references import (
    CatalogLookup,
    ProductFacts,
    ReferenceFacts,
    ReferenceSources,
    ShownItem,
    name_key,
    plan_lookup,
    resolve_references,
    sources_from_state,
)
from src.conversation.state_reducer import (
    ReducerPolicy,
    StateUpdateProposal,
    reduce_all,
    reduce_turn,
)
from src.conversation.state_v2 import ConversationStateV2
from src.domain.loader import load_domain_pack
from src.domain.pack import DomainPack
from src.models import BusinessConfig
from tests.kernel import replay

#: Pragul de nume distinctiv din `db/queries/catalog.py` (`MIN_ANCHOR_NAME_LEN`).
MIN_DISTINCTIVE = 8
_AVAILABLE = {"in_stock": True, "out_of_stock": False}


@lru_cache(maxsize=8)
def _doc(name: str) -> dict[str, Any]:
    return replay.load_pack(name)


def products(name: str) -> dict[str, dict[str, Any]]:
    """Produsele pachetului. Pachetul REAL `sole-ro` (din `db/seed/`) n-are produse de fixture:
    vocabularul lui iese gol, deci pe el proveniența nu poate trece de `implicit` în replay."""
    return {p["id"]: p for p in _doc(name).get("products", [])}


def pack(name: str) -> DomainPack:
    doc = _doc(name)
    business = BusinessConfig(
        id=f"b-{name}",
        slug=name,
        name=name,
        vertical=doc["vertical"],
        settings={"domain_pack": doc["domain_pack"]},
    )
    loaded = load_domain_pack(business)
    assert loaded is not None
    return loaded


def shown(name: str, *ids: str, prices: dict[str, float] | None = None) -> tuple[ShownItem, ...]:
    """Produsele arătate, ca ref-uri; `prices` suprascrie prețul VĂZUT (un preț schimbat de
    atunci)."""
    catalog = products(name)
    return tuple(
        ShownItem(pid, catalog[pid]["name"], (prices or {}).get(pid, float(catalog[pid]["price"])))
        for pid in ids
    )


def _distinctive(product_name: str) -> str:
    head = product_name.split(" - ", 1)[0]
    return head.split(",", 1)[0].strip()


def _facts(product: dict[str, Any], read: tuple[str, ...] | None = None) -> ProductFacts:
    """`read` = cheile cerute de citire: ca SQL-ul (`reference_facts`), din `attributes` vin DOAR
    ele (NX-332, recenzia: altfel testele vedeau atribute pe care producția nu le aduce). Marca e
    coloană în producție, deci se citește oricum."""
    full = dict(product.get("attributes") or {})
    attributes = full if read is None else {k: v for k, v in full.items() if k in read}
    return ProductFacts(
        product_id=product["id"],
        name=product["name"],
        price=float(product["price"]) if product.get("price") is not None else None,
        available=_AVAILABLE.get(product.get("availability", "")),
        rating=product.get("rating"),
        brand=full.get("brand"),
        attributes=attributes,
        variant_labels=tuple(product.get("variants") or ()),
    )


def named_in_products(
    items: Iterable[dict[str, Any]], requested: str
) -> tuple[tuple[str, int], ...]:
    """Țintele unui nume cerut, pe o listă de produse (predicatul `find_product_named_in_query`).
    NX-336: extras din `named_in_catalog`, ca și catalogul sintetic SOLE să folosească ACEEAȘI
    regulă."""
    wanted = fold_text(requested)
    hits = [
        (p["id"], len(head))
        for p in items
        if len(head := _distinctive(p["name"])) >= MIN_DISTINCTIVE and fold_text(head) in wanted
    ]
    return tuple(sorted(hits, key=lambda h: (-h[1], h[0])))


def named_in_catalog(name: str, requested: str) -> tuple[tuple[str, int], ...]:
    return named_in_products(products(name).values(), requested)


def facts_of(
    items: dict[str, dict[str, Any]],
    lookup: CatalogLookup | None = None,
    *,
    snapshot: str,
    drop: Iterable[str] = (),
    override: dict[str, dict[str, Any]] | None = None,
) -> ReferenceFacts:
    """Faptele unui catalog dat ca produse (NX-336: extras din `facts`, pentru catalogul sintetic
    SOLE din harnessul de stagiu). Aceleași reguli ca `fetch_reference_facts`."""
    catalog = {pid: dict(p) for pid, p in items.items()}
    for pid, fields in (override or {}).items():
        catalog[pid] = {**catalog[pid], **fields}
    for pid in drop:
        catalog.pop(pid, None)
    named = {}
    read: tuple[str, ...] | None = None
    if lookup is not None:
        # Numele se caută pe catalogul NEMODIFICAT, ca înainte de extragere (un produs scos cu
        # `drop` rămâne o țintă numită, fără fapte: exact „șters între ture").
        named = {name_key(n): named_in_products(items.values(), n) for n in lookup.names}
        wanted = set(lookup.ids) | {pid for hits in named.values() for pid, _ in hits}
        catalog = {pid: p for pid, p in catalog.items() if pid in wanted}
        # Ca SQL-ul (`reference_facts`): din `attributes` vin DOAR cheile cerute. Altfel testele ar
        # vedea atribute pe care producția nu le aduce (NX-332, recenzia: poarta număra pe ele).
        read = tuple(lookup.attributes)
    return ReferenceFacts(
        products={pid: _facts(p, read) for pid, p in catalog.items()},
        named={k: v for k, v in named.items() if v},
        snapshot=snapshot,
        attributes_read=read,
    )


def facts(
    name: str,
    lookup: CatalogLookup | None = None,
    *,
    drop: Iterable[str] = (),
    override: dict[str, dict[str, Any]] | None = None,
) -> ReferenceFacts:
    """Faptele catalogului. Cu `lookup`: DOAR id-urile cerute plus țintele numelor, exact ca
    `fetch_reference_facts`. `drop` scoate produse din catalog (șterse între ture), `override`
    schimbă câmpuri (un preț schimbat, un stoc epuizat)."""
    return facts_of(
        products(name), lookup, snapshot=f"fixture:{name}", drop=drop, override=override
    )


def vocabulary_of(
    business_id: str, items: Iterable[dict[str, Any]], categories: Iterable[dict[str, Any]]
) -> CatalogVocabulary:
    """Vocabularul unui catalog dat ca produse + rafturi (NX-336: extras din `vocabulary`)."""
    items = list(items)
    counts: dict[str, Counter[str]] = {}
    for product in items:
        for key, value in (product.get("attributes") or {}).items():
            if isinstance(value, str):
                counts.setdefault(key, Counter())[value] += 1
    dimensions = {
        key: tuple(VocabEntry(key=v, label=v, count=n) for v, n in sorted(c.items()))
        for key, c in counts.items()
    }
    # NX-331: rafturile, ca în `load_vocabulary` (`CATEGORY_DIMENSION`, doar cele cu produse). Fără
    # ele o schimbare de subiect pe un pachet de fixture ieșea `unmapped`, deci stratul `reducer`
    # n-ar fi avut ce parca.
    shelves = Counter(p.get("category") for p in items if p.get("category"))
    shelf_entries = tuple(
        VocabEntry(key=c["key"], label=c.get("name") or c["key"], count=shelves[c["key"]])
        for c in categories
        if shelves[c["key"]] > 0
    )
    if shelf_entries:
        dimensions[CATEGORY_DIMENSION] = shelf_entries
    return CatalogVocabulary(business_id=business_id, dimensions=dimensions)


def vocabulary(name: str) -> CatalogVocabulary:
    return vocabulary_of(f"b-{name}", products(name).values(), _doc(name).get("categories", []))


def sources_of(name: str, raw: dict[str, Any]) -> ReferenceSources:
    """`JourneyTurn.sources` (id-uri de fixture) → `ReferenceSources`, cu prețurile din pachet."""
    page = raw.get("page")
    return ReferenceSources(
        shown_now=shown(name, *raw.get("shown_now", ())),
        shown_earlier=tuple(shown(name, *s) for s in raw.get("shown_earlier", ())),
        parked=shown(name, *raw.get("parked", ())),
        page=shown(name, page)[0] if page else None,
        focus=raw.get("focus"),
        thread=raw.get("thread", "continue"),
    )


def resolver_trace(journey: replay.Journey, index: int) -> KernelTrace:
    """Pipeline-ul de replay pentru stratul `resolver` (NX-329): interpretarea e cea etichetată
    (adaptorul vine la pasul 5), iar referințele ei trec prin resolverul REAL, pe faptele
    pachetului. Straturile de după au valori neutre până le construiesc pașii 3-6."""
    turn = journey.turns[index]
    interpretation = turn.expect["interpretation"]
    sources = sources_of(journey.pack, turn.sources)
    loaded = pack(journey.pack)
    refs = interpretation.references
    lookup = plan_lookup(refs, sources, pack=loaded, locale=journey.locale)
    resolved = resolve_references(
        refs,
        sources,
        facts(journey.pack, lookup),
        vocab=vocabulary(journey.pack),
        pack=loaded,
        locale=journey.locale,
    )
    return KernelTrace(
        contract_version=KERNEL_CONTRACT_VERSION,
        vocabulary_snapshot=f"fixture:{journey.pack}",
        interpretation=interpretation,
        checked_changes=[],
        resolved_refs=resolved,
        state_before={},
        proposals=[],
        rejected=[],
        state_after={},
        ambiguity=AmbiguityDecision(verdict="act", reason="step-2", question=None),
        plan=TurnPlan(executor="reply_only", product_ids=[], search_args=None, depends_on=None),
        executor="none",
        answer_policy=None,
    )


def checked_trace(journey: replay.Journey, index: int) -> KernelTrace:
    """Pipeline-ul pentru straturile `checked` (NX-330) și `resolver` (NX-329): schimbările
    interpretării etichetate trec prin validatorul de proveniență REAL, pe vocabularul, unitățile și
    pachetul de fixture. Cuvintele clientului sunt mesajul turului + mesajele turelor anterioare ale
    journey-ului (niciodată ale botului: journey-ul nu le are)."""
    turn = journey.turns[index]
    earlier = tuple(t.user_input for t in journey.turns[:index])[::-1]
    checked = check_changes(
        turn.expect["interpretation"],
        words=UserWords(turn.user_input, earlier),
        vocab=vocabulary(journey.pack),
        pack=pack(journey.pack),
        locale=journey.locale,
    )
    return resolver_trace(journey, index).model_copy(update={"checked_changes": checked})


#: NX-336: forma compactă a stării e a traceului (`kernel_trace.state_view`), aceeași în
#: producție și în replay.
state_view = _state_view


#: NX-335: sursele resolverului din starea redusă sunt acum o funcție PURĂ din `references.py`
#: (`sources_from_state`), folosită de aici și de replay-ul offline: o singură copie.
_sources_from_state = sources_from_state


def category_menu(name: str) -> tuple[tuple[str, int], ...]:
    """Meniul de rafturi al pachetului, în forma lui `list_category_menu` (cheie, mărime),
    ordonat pe cheie ca SQL-ul, din vocabularul de fixture (doar rafturile cu produse)."""
    return tuple(sorted((e.key, e.count) for e in vocabulary(name).categories))


def _shown_proposal(name: str, ids: tuple[str, ...]) -> StateUpdateProposal:
    catalog = products(name)
    return StateUpdateProposal(
        "set_references",
        source="catalog",
        payload={
            "displayed_products": [
                {"product_id": pid, "name": catalog[pid]["name"], "price": catalog[pid]["price"]}
                for pid in ids
            ]
        },
    )


def reducer_trace(journey: replay.Journey, index: int) -> KernelTrace:
    """Pipeline-ul pentru stratul `reducer` (NX-331), cu `checked` și `resolver` pe drum: turele
    journey-ului rulează ÎN LANȚ de la starea goală, fiecare prin validatorul de proveniență, prin
    resolver (pe sursele din starea redusă, nu din `sources`), prin `to_delta` și prin
    `reduce_turn`, cu ce a arătat executorul turului (`shown`). Straturile de după reducer au valori
    neutre până la pașii 4-6."""
    loaded = pack(journey.pack)
    vocab = vocabulary(journey.pack)
    needs = NeedVocabulary.from_pack(loaded)
    policy = ReducerPolicy(vocabulary=needs)
    state = ConversationStateV2()
    trace: KernelTrace | None = None
    for i, turn in enumerate(journey.turns[: index + 1]):
        interpretation = turn.expect["interpretation"]
        handles = need_handles(state.needs, needs)
        checked = check_changes(
            interpretation,
            words=UserWords(turn.user_input, tuple(t.user_input for t in journey.turns[:i])[::-1]),
            handles=handles,
            vocab=vocab,
            pack=loaded,
            locale=journey.locale,
        )
        sources = _sources_from_state(state, interpretation.thread)
        refs = interpretation.references
        lookup = plan_lookup(refs, sources, pack=loaded, locale=journey.locale)
        known = facts(journey.pack, lookup)
        resolved = resolve_references(
            refs, sources, known, vocab=vocab, pack=loaded, locale=journey.locale
        )
        delta = to_delta(
            interpretation, checked, resolved, known, handles=handles, needs=needs, turn_id=f"t{i}"
        )
        executor = (_shown_proposal(journey.pack, turn.shown),) if turn.shown else ()
        primary = (
            interpretation.acts[-1].targets[0]
            if interpretation.acts and (interpretation.acts[-1].targets)
            else None
        )
        before = state
        reduced = reduce_turn(
            state,
            delta,
            executor,
            resolved,
            primary,
            interpretation.corrects_previous_turn,
            policy,
        )
        state = reduced.state
        trace = KernelTrace(
            contract_version=KERNEL_CONTRACT_VERSION,
            vocabulary_snapshot=f"fixture:{journey.pack}",
            interpretation=interpretation,
            checked_changes=checked,
            resolved_refs=resolved,
            state_before=state_view(before),
            proposals=[{"op": p.op, "key": p.key or p.category_key} for p in delta.proposals],
            rejected=[{"op": r.op, "reason": r.reason} for r in reduced.rejected],
            state_after=state_view(state),
            ambiguity=AmbiguityDecision(verdict="act", reason="step-3", question=None),
            plan=TurnPlan(executor="reply_only", product_ids=[], search_args=None, depends_on=None),
            executor="none",
            answer_policy=None,
        )
    assert trace is not None
    return trace


# --- NX-332: poarta de ambiguitate și politica de răspuns ----------------------------------------


@dataclass(frozen=True)
class KernelStep:
    """Un tur prin kernel, pe pachetul de fixture: ce a intrat în poartă și ce a ieșit.

    `gate_state` = starea DUPĂ reducer și ÎNAINTEA executorilor (ecranul e cel de dinaintea
    turului), adică exact ce vede poarta la pasul 6. `state_after` = starea de la sfârșitul turului:
    reducerul cu ce a arătat executorul, plus memoria întrebării scrisă din `GateOutcome` într-o a
    doua trecere (decizia de arhitectură 1 din cardul NX-332)."""

    interpretation: TurnInterpretation
    checked: tuple[CheckedChange, ...]
    resolved: tuple[ResolvedRef, ...]
    facts: ReferenceFacts
    gate_state: ConversationStateV2
    outcome: GateOutcome
    answer_policy: AnswerPolicy | None
    state_after: ConversationStateV2
    memory: StateUpdateProposal | None
    # NX-333: ce primește plannerul în plus (`ranking`, „schimbări în tur"), și planul lui.
    delta: TurnDelta | None = None
    planned: PlannedTurn | None = None


GATE_POLICY = ClarificationPolicy()


#: NX-336: memoria întrebării are UN proprietar, în producție (`ambiguity_gate`).
memory_proposal = _memory_proposal


def kernel_step(
    name: str,
    state: ConversationStateV2,
    interpretation: TurnInterpretation,
    user_input: str,
    *,
    earlier: tuple[str, ...] = (),
    shown_ids: tuple[str, ...] = (),
    turn_id: str = "t",
    locale: str = "ro",
    drop: Iterable[str] = (),
    override: dict[str, dict[str, Any]] | None = None,
    loaded: DomainPack | None = None,
    vocab: CatalogVocabulary | None | bool = True,
    policy: ClarificationPolicy = GATE_POLICY,
    catalog: Callable[[CatalogLookup], ReferenceFacts] | None = None,
    answer_pending: bool = False,
    pairs: Sequence[tuple[str, str]] | None = None,
) -> KernelStep:
    """Un tur complet pe calea interpretată, fără executori reali: validatorul de proveniență,
    resolverul (pe sursele stării), `to_delta`, reducerul, poarta, politica de răspuns și memoria
    întrebării. Faptele „după unelte" ale politicii sunt faptele pachetului pentru țintele rezolvate
    (fixture-ul n-are executor)."""
    loaded = loaded or pack(name)
    voc: CatalogVocabulary | None = vocabulary(name) if vocab is True else (vocab or None)
    needs = NeedVocabulary.from_pack(loaded)
    reducer_policy = ReducerPolicy(vocabulary=needs)
    handles = need_handles(state.needs, needs)
    checked = check_changes(
        interpretation,
        words=UserWords(user_input, earlier),
        handles=handles,
        vocab=voc,
        pack=loaded,
        locale=locale,
    )
    sources = _sources_from_state(state, interpretation.thread)
    refs = interpretation.references
    lookup = plan_lookup(
        refs,
        sources,
        pack=loaded,
        locale=locale,
        extra_attributes=lookup_attributes(interpretation, vocab=voc, pack=loaded),
    )
    # NX-336: `catalog` = alt catalog decât al pachetului (harnessul de stagiu, catalogul SOLE
    # sintetic), cu aceleași reguli ca `fetch_reference_facts`.
    known = (
        catalog(lookup)
        if catalog is not None
        else facts(name, lookup, drop=drop, override=override)
    )
    resolved = resolve_references(refs, sources, known, vocab=voc, pack=loaded, locale=locale)
    delta = to_delta(
        interpretation, checked, resolved, known, handles=handles, needs=needs, turn_id=turn_id
    )
    if pairs is not None:
        # NX-348: aceeași verificare a perechii (raft, tip) ca orchestratorul, pe catalogul dat.
        from src.catalog.subject_pairs import mark_pairs  # noqa: PLC0415

        delta = mark_pairs(delta, state, pairs, voc)
    changed = bool(delta.proposals)
    # NX-336 §1: cu `answer_pending`, o întrebare VIE se închide pe un tur care nu e paranteză, ca
    # în orchestrator (paritatea pe care o cere harnessul de stagiu). Implicit oprit: suitele NX-332
    # modelează explicit ambele cazuri (întrebarea rămasă vie ⇒ `already_pending`). `changed`
    # rămâne al propunerilor de nevoi.
    answered = question_answered(state, delta.thread, turn_id) if answer_pending else None
    if answered is not None:
        delta = replace(delta, proposals=(answered, *delta.proposals))
    primary = (
        interpretation.acts[-1].targets[0]
        if interpretation.acts and interpretation.acts[-1].targets
        else None
    )
    corrects = interpretation.corrects_previous_turn
    gate_state = reduce_turn(state, delta, (), resolved, primary, corrects, reducer_policy).state
    outcome = decide_ambiguity(
        interpretation,
        checked,
        resolved,
        gate_state,
        known,
        vocab=voc,
        pack=loaded,
        locale=locale,
        policy=policy,
    )
    remaining = [a for i, a in enumerate(interpretation.acts) if i not in outcome.skipped_acts]
    policy_result = None
    if remaining:
        act = remaining[-1]
        by_id = {r.ref_id: r for r in resolved}
        targeted = {
            pid: known.products[pid]
            for t in act.targets
            if t in by_id
            for pid in by_id[t].product_ids
            if pid in known.products
        }
        policy_result = answer_policy(
            act,
            resolved,
            targeted,
            vocab=voc,
            pack=loaded,
            references=interpretation.references,
            ambiguity=outcome.decision,
            locale=locale,
        )
    planned = plan_turn(
        interpretation,
        gate_state,
        delta.ranking,
        resolved,
        outcome,
        changed=changed,
        pack=loaded,
        vocab=voc,
        locale=locale,
    )
    executor = _executor_proposals(name, shown_ids, planned)
    after = reduce_turn(state, delta, executor, resolved, primary, corrects, reducer_policy).state
    memory = memory_proposal(outcome, turn_id)
    if memory is not None:
        # NX-336 PR B: forma PRODUCȚIEI (`kernel_commit`): a doua trecere e `reduce_all` cu revizia
        # fixată pe cea a turului (fără bump; plafoanele se aplică), nu `reduce` pe o propunere. O
        # respingere lasă starea neatinsă, ca înainte.
        after = reduce_all(after, (memory,), reducer_policy, revision=after.revision).state
    return KernelStep(
        interpretation=interpretation,
        checked=tuple(checked),
        resolved=tuple(resolved),
        facts=known,
        gate_state=gate_state,
        outcome=outcome,
        answer_policy=policy_result,
        state_after=after,
        memory=memory,
        delta=delta,
        planned=planned,
    )


#: Executorii care CITESC catalogul (`tools.base.CATALOG_READ_TOOLS`, pe executorii planului).
_CATALOG_EXECUTORS = frozenset({"search", "page", "compare", "detail", "link", "bundle"})


def _executor_proposals(
    name: str, shown_ids: tuple[str, ...], planned: PlannedTurn
) -> tuple[StateUpdateProposal, ...]:
    """Ce scrie EXECUTORUL turului în stare, DERIVAT din plan, ca în producție (recenzia NX-333:
    sesiunea era setată de mână, pe stări pe care producția nu le produce):

    - referințele arătate, când turul a arătat produse;
    - o căutare CU produse deschide sesiunea (`_search` scrie `active_search`);
    - o paginare o continuă (`continue_search_session` o rescrie; aici rămâne cea din stare);
    - un tur fără produse care a citit catalogul, sau o întrebare, o închide
      (`processor._turn_proposals` + `_keeps_search_session`, NX-326);
    - o paranteză fără catalog (faq, comandă, conversație) o păstrează."""
    executors = {p.executor for p in planned.plans}
    out = [_shown_proposal(name, shown_ids)] if shown_ids else []
    if "search" in executors and shown_ids:
        out.append(
            StateUpdateProposal(
                "set_active_search",
                source="catalog",
                payload={"fp": f"fixture:{name}", "pool": list(shown_ids), "cursor": 0, "page": 0},
            )
        )
    elif (
        "page" not in executors
        and not shown_ids
        and (executors & _CATALOG_EXECUTORS or "ask" in executors)
    ):
        out.append(StateUpdateProposal("set_active_search", source="catalog", payload=None))
    return tuple(out)


def gate_trace(journey: replay.Journey, index: int) -> KernelTrace:
    """Pipeline-ul pentru straturile `ambiguity` și `answer_policy` (NX-332), cu `checked`,
    `resolver` și `reducer` pe drum: turele rulează ÎN LANȚ, ca la `reducer_trace`, iar memoria
    întrebării (`set_pending_question` / `note_asked`) trece prin reducer între ture, deci replay-ul
    vede și anti-bucla (I11) pe mai multe ture, nu doar în tur.

    NX-333: plannerul rulează în `kernel_step`, deci și stratul `plan` e cel real (un journey
    etichetat pe `ambiguity` și pe `plan`, ca §C.12, se compară pe amândouă)."""
    return _plan_chain(journey, index)[1]


def plan_trace(journey: replay.Journey, index: int) -> KernelTrace:
    """Pipeline-ul pentru stratul `plan` (NX-333), cu toate straturile de dinainte pe drum:
    `gate_trace` + plannerul, în lanț. `plan` = planul actului PRINCIPAL (`PlannedTurn.primary`);
    un tur cu două acte își verifică ambele planuri în `tests/test_kernel_planner.py`, fiindcă
    traceul contractului are un singur `TurnPlan`."""
    return _plan_chain(journey, index)[1]


def planned_turn(journey: replay.Journey, index: int) -> PlannedTurn:
    """Planul COMPLET al turului `index` (toate planurile, golurile, dezvăluirile), în lanț."""
    return _plan_chain(journey, index)[0]


def _plan_chain(journey: replay.Journey, index: int) -> tuple[PlannedTurn, KernelTrace]:
    state = ConversationStateV2()
    step: KernelStep | None = None
    before = state
    for i, turn in enumerate(journey.turns[: index + 1]):
        before = state
        step = kernel_step(
            journey.pack,
            state,
            turn.expect["interpretation"],
            turn.user_input,
            earlier=tuple(t.user_input for t in journey.turns[:i])[::-1],
            shown_ids=turn.shown,
            turn_id=f"t{i}",
            locale=journey.locale,
        )
        state = step.state_after
    assert step is not None and step.planned is not None
    planned = step.planned
    trace = KernelTrace(
        contract_version=KERNEL_CONTRACT_VERSION,
        vocabulary_snapshot=f"fixture:{journey.pack}",
        interpretation=step.interpretation,
        checked_changes=list(step.checked),
        resolved_refs=list(step.resolved),
        state_before=state_view(before),
        proposals=[],
        rejected=[],
        state_after=state_view(state),
        ambiguity=step.outcome.decision,
        plan=planned.plans[planned.primary],
        executor="none",
        answer_policy=step.answer_policy,
    )
    return planned, trace


__all__ = [
    "GATE_POLICY",
    "KernelStep",
    "checked_trace",
    "facts",
    "gate_trace",
    "kernel_step",
    "memory_proposal",
    "name_key",
    "named_in_catalog",
    "pack",
    "plan_trace",
    "planned_turn",
    "products",
    "reducer_trace",
    "resolver_trace",
    "shown",
    "sources_of",
    "state_view",
    "vocabulary",
]
