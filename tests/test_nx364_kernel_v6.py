"""NX-364 — `kernel.v6.0`: ce s-a schimbat în sens, fiecare pe cazul real din care vine.

Conversațiile din 2026-09-30 (`sole-ro`): «am tenul uscat» ajungea `implicit` (deci doar
preferință) deși clientul numise tipul de ten; «am tenul gras» cu `dry` NU era prins ca
contradicție; «compara prima cu a treia» după un detaliu ieșea `ordinal_out_of_range` (testele
ordinalului sunt în `test_kernel_references.py` și în journey-ul `r13` al stratului `resolver`).
"""

from __future__ import annotations

import dataclasses

import pytest

from src.agent.turn_planner import plan_turn
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.conversation.ambiguity_gate import GateOutcome
from src.conversation.delta import RankingSignal
from src.conversation.interpretation import Act, AmbiguityDecision, StateChange, TurnInterpretation
from src.conversation.provenance import (
    PRICE_BAND_HIGH,
    PRICE_BAND_LOW,
    UserWords,
    check_changes,
)
from src.conversation.state_v2 import ConversationStateV2, Need, Topic
from src.tools import catalog_tools as ct
from tests.kernel import fixture_catalog as fc

VOCAB = CatalogVocabulary(
    business_id="b",
    dimensions={
        "skin_type": (
            VocabEntry(key="dry", label="dry", count=429),
            VocabEntry(key="oily", label="oily", count=300),
        ),
        "concerns": (VocabEntry(key="hydration", label="hydration", count=1929),),
    },
)


def _check(said: str, dimension: str, value: str, quote: str):
    change = StateChange(
        op="set",
        target=None,
        dimension=dimension,
        relation="eq",
        value=value,
        number=None,
        unit=None,
        relative_to=None,
        quote=quote,
    )
    interp = TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[], query=None)],
        changes=[change],
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )
    [c] = check_changes(
        interp, words=UserWords(said, ()), vocab=VOCAB, pack=fc.pack("sole-ro"), locale="ro"
    )
    return c


@pytest.mark.parametrize(
    ("said", "quote"),
    [
        ("am tenul uscat", "am tenul uscat"),  # turul `5c117067`
        ("am tenul uscat", "tenul uscat"),
        ("am ten uscat", "ten uscat"),  # forma din pachet, ca pe v5.1
    ],
)
def test_an_inflected_name_of_the_proposed_value_is_explicit(said, quote):
    c = _check(said, "skin_type", "dry", quote)
    assert (c.provenance, c.rejected) == ("explicit", None)
    assert c.matched  # cuvintele care au numit valoarea, pentru textul căutării


def test_a_description_is_still_implicit():
    """Verificarea pe vocabular rămâne: pe setul A, `implicit` are precizie 46% (descrieri)."""
    c = _check("mi se usuca pielea dupa dus", "skin_type", "dry", "mi se usuca pielea dupa dus")
    assert (c.provenance, c.rejected) == ("implicit", None)


def test_inflection_only_confirms_and_needs_an_exact_word():
    """Recenzia v6.0: o tulpină scurtă se potrivește și cu verbe («pare» = „par” + „e”). De aceea
    flexiunea (a) cere un cuvânt IDENTIC al numelui lângă cel flexionat și (b) doar confirmă,
    niciodată nu contrazice: «pielea mi se pare uscată» nu mai e contrazisă de „par uscat”, iar o
    nevoie spusă nu se pierde. Cu ambele cuvinte flexionate rămâne `implicit`, ca pe v5.1."""
    c = _check("pielea mi se pare uscata", "skin_type", "dry", "pielea mi se pare uscata")
    assert (c.provenance, c.rejected) == ("implicit", None)
    c = _check("am tenul gras", "skin_type", "dry", "tenul gras")  # o eroare de model rămâne soft
    assert (c.provenance, c.rejected) == ("implicit", None)
    c = _check("ceva pentru tenurile uscate", "skin_type", "dry", "tenurile uscate")
    assert c.provenance == "implicit"


def test_a_value_the_pack_does_not_name_stays_implicit():
    """«par gras» n-are frază în pachet: gol de DATE, nu de cod (nu inventăm un nume)."""
    c = _check("vreau un sampon pentru par gras", "skin_type", "oily", "par gras")
    assert (c.provenance, c.rejected) == ("implicit", None)


def test_a_short_stem_is_never_a_name():
    """Tulpina are cel puțin 3 litere (`_same_stem`): «te» nu e «ten»."""
    c = _check("ai ceva de te uscat repede", "skin_type", "dry", "te uscat")
    assert c.provenance == "implicit"


# ── excluderea («nu vreau X») devine filtru ─────────────────────────────────────────────────────


def _need(key, value, source="user_explicit"):
    return Need(
        key=key,
        operator="eq",
        normalized_value=value,
        strength="soft",
        status="active",
        source=source,
    )


def _plan_needs(needs, *, vocab=None, name="fashion"):
    interp = TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[], query="rochie")],
        changes=[],
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )
    state = ConversationStateV2(revision=1, topic=Topic(category_key="rochii"), needs=tuple(needs))
    gate = GateOutcome(AmbiguityDecision(verdict="act", reason="clear", question=None))
    planned = plan_turn(
        interp,
        state,
        (),
        (),
        gate,
        changed=True,
        pack=fc.pack(name),
        vocab=vocab or fc.vocabulary(name),
        locale="ro",
    )
    plan = planned.plans[planned.primary]
    assert plan.executor == "search", planned
    return plan.search_args, planned.gaps


def test_a_restriction_is_found_back_on_its_only_facet():
    """Delta scrie o excludere pe o fațetă ne-EXCLUSION pe cheia universală `restriction` (valoarea
    își pierde dimensiunea); plannerul o regăsește prin vocabular."""
    args, gaps = _plan_needs([_need("restriction", "poliester")])
    assert args.exclude == {"material": ["poliester"]}
    assert "exclusion" not in gaps


def test_a_restriction_on_a_value_two_facets_carry_stays_a_gap():
    base = fc.vocabulary("fashion")
    color = base.dimensions["color"]
    shared = dataclasses.replace(color[0], key="poliester", label="poliester")
    vocab = dataclasses.replace(base, dimensions={**base.dimensions, "color": (*color, shared)})
    args, gaps = _plan_needs([_need("restriction", "poliester")], vocab=vocab)
    assert args.exclude == {}
    assert "exclusion" in gaps


@pytest.mark.parametrize(
    "need",
    [
        _need("restriction", "poliester", source="model_inferred"),  # nespus de client
        _need("restriction", "pentru birou"),  # fără cheie de catalog
    ],
)
def test_an_unspoken_or_uncatalogued_exclusion_stays_a_gap(need):
    args, gaps = _plan_needs([need])
    assert args.exclude == {}
    assert "exclusion" in gaps


def test_excluded_hit_is_tri_state():
    exclude = {"key_ingredients": ["acid hialuronic"]}
    assert ct.excluded_hit({"key_ingredients": ["Acid Hialuronic", "ceramide"]}, exclude) is True
    assert ct.excluded_hit({"key_ingredients": ["ceramide"]}, exclude) is False
    assert ct.excluded_hit({"concerns": ["hydration"]}, exclude) is None  # necunoscut: rămâne
    assert ct.excluded_hit(None, exclude) is None
    assert ct.excluded_hit({"material": "poliester"}, {"material": ["poliester"]}) is True


def test_exclusion_on_a_partitioning_facet_removes_only_single_purpose_products():
    """«nu pentru ten gras» nu scoate o cremă declarată pentru toate tipurile de ten."""
    exclude, only = {"skin_type": ["oily"]}, frozenset({"skin_type"})
    assert ct.excluded_hit({"skin_type": ["oily"]}, exclude, only) is True
    assert ct.excluded_hit({"skin_type": "oily"}, exclude, only) is True
    assert ct.excluded_hit({"skin_type": ["dry", "normal", "oily"]}, exclude, only) is False
    assert (
        ct.excluded_hit({"skin_type": ["dry", "oily"]}, exclude) is True
    )  # aditiv: orice apariție


def test_exclude_is_planner_only_and_defines_the_search_session():
    """Modelul nu-l poate trimite (I2), iar o excludere nouă e o sesiune nouă: fără ea în amprentă,
    «mai arată-mi» ar pagina pool-ul VECHI, nefiltrat. Gol ⇒ amprenta identică cu v5.1 (I16)."""
    assert "exclude" in ct.PLANNER_ONLY_FIELDS
    plain = ct._session_filters(ct.SearchArgs(query="rochie"), None)
    excluded = ct._session_filters(
        ct.SearchArgs(query="rochie", exclude={"material": ["Poliester"]}), None
    )
    assert "exclude" not in plain
    assert excluded["exclude"] == {"material": ["poliester"]}
    assert ct._fp(plain) != ct._fp(excluded)


# ── calificativul vag de preț devine bandă ─────────────────────────────────────────────────────


def _price_change(quote: str, relation: str = "lte") -> StateChange:
    return StateChange(
        op="set",
        target=None,
        dimension="price",
        relation=relation,
        value=quote,
        number=None,
        unit=None,
        relative_to=None,
        quote=quote,
    )


def _checked_price(said: str, quote: str, relation: str = "lte"):
    interp = TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[], query=None)],
        changes=[_price_change(quote, relation)],
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )
    [c] = check_changes(
        interp, words=UserWords(said, ()), vocab=VOCAB, pack=fc.pack("sole-ro"), locale="ro"
    )
    return c


def test_a_price_limit_without_a_number_is_a_band_not_an_unmapped_word():
    """Turul `8c578b1f` («sa nu fie ft scump»): pe v5.1 cobora pe `unmapped` „ft scump”, adică
    `rank_terms=["ft scump"]`. Acum e banda de jos, semnal al turului (nepersistat)."""
    c = _checked_price("vreau un sampon da sa nu fie ft scump", "sa nu fie ft scump")
    assert (c.dimension, c.canonical_value, c.strength, c.rejected) == (
        "price",
        PRICE_BAND_LOW,
        "ranking",
        None,
    )
    high = _checked_price("ceva premium, nu conteaza pretul", "premium", relation="gte")
    assert high.canonical_value == PRICE_BAND_HIGH


def _plan_ranking(signals):
    interp = TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[], query="rochie")],
        changes=[],
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )
    state = ConversationStateV2(revision=1, topic=Topic(category_key="rochii"))
    gate = GateOutcome(AmbiguityDecision(verdict="act", reason="clear", question=None))
    planned = plan_turn(
        interp,
        state,
        tuple(signals),
        (),
        gate,
        changed=True,
        pack=fc.pack("fashion"),
        vocab=fc.vocabulary("fashion"),
        locale="ro",
    )
    return planned.plans[planned.primary].search_args, planned.gaps


def test_the_low_band_reaches_the_search_and_the_high_band_stays_a_gap():
    args, gaps = _plan_ranking([RankingSignal("price", PRICE_BAND_LOW, "lte")])
    assert args.price_band == "low" and args.rank_terms == [] and "soft_budget" not in gaps
    args, gaps = _plan_ranking([RankingSignal("price", PRICE_BAND_HIGH, "gte")])
    assert args.price_band is None and "soft_budget" in gaps


def test_lower_price_band_keeps_the_cheaper_half_in_relevance_order():
    pool = [{"id": str(i), "price": p} for i, p in enumerate([180.0, 30.0, 130.0, 50.0, 119.0])]
    kept = ct.lower_price_band(pool)
    assert [p["id"] for p in kept] == ["1", "3", "4"]  # mediana 119: ordinea relevanței rămâne
    assert ct.lower_price_band(pool[:1]) == pool[:1]  # sub două prețuri nu există o jumătate
    assert ct.lower_price_band([{"id": "x"}, {"id": "y", "price": 10.0}]) == [
        {"id": "x"},
        {"id": "y", "price": 10.0},
    ]


def test_price_band_is_planner_only_and_defines_the_session():
    assert "price_band" in ct.PLANNER_ONLY_FIELDS
    plain = ct._session_filters(ct.SearchArgs(query="sampon"), None)
    band = ct._session_filters(ct.SearchArgs(query="sampon", price_band="low"), None)
    assert "price_band" not in plain and band["price_band"] == "low"
    assert ct._fp(plain) != ct._fp(band)


# ── recenzia v6.0: coșul nu ghicește pe un ordinal numărat pe listă ───────────────────────────


def _detail_screen_state():
    from src.conversation.state_v2 import DisplayedRef, References

    def refs(*ids):
        return tuple(DisplayedRef(i, i, None) for i in ids)

    return ConversationStateV2(
        revision=3,
        topic=Topic(category_key="telefoane"),
        references=References(
            displayed_products=refs("el-02"),
            displayed_revision=3,
            recent_sets=(refs("el-01", "el-02", "el-03"),),
        ),
    )


def _ordinal_act(kind: str, ordinal: int):
    return TurnInterpretation.model_validate(
        {
            "thread": "continue",
            "acts": [{"kind": kind, "targets": ["r1"], "query": None}],
            "changes": [],
            "references": [
                {
                    "id": "r1",
                    "text": "primul",
                    "kind": "ordinal",
                    "ordinal": ordinal,
                    "name": None,
                    "dimension": None,
                    "value": None,
                    "direction": None,
                }
            ],
            "ambiguities": [],
            "corrects_previous_turn": False,
        }
    )


def test_a_read_on_a_zoomed_ordinal_acts_on_the_list():
    step = fc.kernel_step(
        "electronics", _detail_screen_state(), _ordinal_act("link", 1), "linkul la primul"
    )
    [r1] = step.resolved
    assert (r1.product_ids, r1.reason) == (["el-01"], "ordinal_in_zoomed_list")
    assert step.outcome.decision.verdict == "act"


def test_a_cart_on_a_zoomed_ordinal_asks_between_the_list_and_the_screen():
    """«Adaugă-l pe primul» pe un ecran de detaliu: primul din listă sau cardul de pe ecran? Pe
    o mutație nu se ghicește (I10): întrebarea oferă ambele."""
    step = fc.kernel_step(
        "electronics", _detail_screen_state(), _ordinal_act("cart", 1), "adauga-l pe primul in cos"
    )
    assert step.outcome.decision.verdict == "must_ask"
    assert step.outcome.decision.reason == "mutation_not_exact"


def test_a_cart_on_the_zoomed_product_itself_is_not_ambiguous():
    """«Al doilea» din listă e chiar produsul din detaliu: nu există ce întreba."""
    step = fc.kernel_step(
        "electronics", _detail_screen_state(), _ordinal_act("cart", 2), "adauga-l pe al doilea"
    )
    assert step.outcome.decision.verdict != "must_ask"


# ── recenzia v6.0: coada pool-ului (NX-303) trece prin aceleași filtre ────────────────────────


@pytest.fixture
def planned_search(monkeypatch):
    from tests.test_need_menu import PACK, VOCAB
    from tests.test_need_menu_tools import _ctx, _deps

    page = [
        {"id": "a", "name": "A", "price": 30.0, "attributes": {"key_ingredients": ["ceramide"]}},
        {"id": "b", "name": "B", "price": 50.0, "attributes": {"key_ingredients": ["pantenol"]}},
        {
            "id": "c",
            "name": "C",
            "price": 80.0,
            "attributes": {"key_ingredients": ["acid hialuronic"]},
        },
    ]
    tail = [
        {
            "id": "t-ha",
            "name": "T1",
            "price": 20.0,
            "attributes": {"key_ingredients": ["acid hialuronic"]},
        },
        {
            "id": "t-scump",
            "name": "T2",
            "price": 500.0,
            "attributes": {"key_ingredients": ["ceramide"]},
        },
        {
            "id": "t-ok",
            "name": "T3",
            "price": 25.0,
            "attributes": {"key_ingredients": ["ceramide"]},
        },
    ]

    async def fake_lexical(conn, business_id, **kw):
        return [dict(p) for p in page]

    async def fake_tail(conn, ctx, a, step, *, searchable_facets):
        return [dict(p) for p in tail]

    async def no_embeddings(conn, business_id):
        return False

    async def fake_vocab(deps, business_id):
        return VOCAB

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "_subject_filter_tail", fake_tail)
    monkeypatch.setattr(ct, "_text_gate_is_redundant", lambda *a, **k: True)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)

    async def run(**args):
        ctx = _ctx("ceva")
        ctx.business.domain_pack = PACK
        await ct.run_planned_search(ctx, _deps(), ct.SearchArgs(query="crema", limit=2, **args))
        pool = (ctx.state_patch.get("active_search") or {}).get("pool") or []
        return ctx, [str(i) for i in pool]

    return run


async def test_the_pool_tail_honours_exclusions_and_the_price_band(planned_search):
    """«mai arată-mi» pagina pool-ul întreg: fără filtrul pe coadă, pagina 2 servea exact
    produsele cu acid hialuronic și cele peste bandă."""
    _, pool = await planned_search(
        exclude={"key_ingredients": ["acid hialuronic"]}, price_band="low"
    )
    assert "c" not in pool and "t-ha" not in pool  # excluse, pe pagină și pe coadă
    assert "t-scump" not in pool  # peste plafonul benzii (mediana paginii filtrate)
    assert "t-ok" in pool


async def test_without_new_fields_the_tail_is_untouched(planned_search):
    _, pool = await planned_search()
    assert {"t-ha", "t-scump", "t-ok"} <= set(pool)


def test_a_price_value_with_digits_is_not_a_band():
    """Recenzia v6.0: «100» scris fără `number` e o sumă greșit încadrată, nu o dorință vagă."""
    c = _checked_price("sub 100 lei", "100")
    assert c.canonical_value not in (PRICE_BAND_LOW, PRICE_BAND_HIGH)


def test_a_routine_discloses_the_band_it_cannot_apply():
    """Recenzia v6.0: `RoutineArgs` n-are încă bandă sau excluderi; planul rutinei le declară ca
    goluri în loc să le piardă în tăcere."""
    from tests import test_kernel_planner as kp

    shelf = kp._shelf("sole-ro")
    pid = kp._ids("sole-ro")[0]
    interp = kp._interp(
        acts=[{"kind": "bundle", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "asta", "kind": "deictic"}],
    )
    planned = kp._plan(
        "sole-ro",
        interp,
        kp._state(shelf),
        resolved=(kp._ref("r1", "exact", [pid]),),
        loaded=kp._sole_with_family(shelf),
        ranking=(RankingSignal("price", PRICE_BAND_LOW, "lte"),),
    )
    plan = planned.plans[planned.primary]
    assert plan.executor == "bundle" and plan.search_args.price_band == "low"
    assert "soft_budget" in planned.gaps


# ── verificarea independentă v6.0: două detalii la rând + scurtăturile exacte ──────────────────


def test_the_zoom_survives_two_details_in_a_row():
    """Lista → detaliul lui #2 → «și a treia?»: `recent_sets` = (detaliul de dinainte, lista).
    Regula caută cea mai recentă listă de ≥ 2, nu doar setul imediat anterior."""
    from src.conversation.references import ShownItem, zoomed_list

    lst = tuple(ShownItem(f"p{i}", f"p{i}", None) for i in range(1, 6))
    assert zoomed_list((ShownItem("p3", "p3", None),), ((ShownItem("p2", "p2", None),), lst)) == lst
    assert zoomed_list((ShownItem("x", "x", None),), ((ShownItem("p2", "p2", None),), lst)) is None
    assert zoomed_list(lst[:2], (lst,)) is None  # două pe ecran: nu e un detaliu


def _shortcut_ctx(body: str, *, zoom: bool):
    from src.conversation.state_v2 import DisplayedRef, References
    from src.models import (
        BusinessConfig,
        Contact,
        ConversationState,
        InboundMessage,
        Route,
        RouteDecision,
        TurnContext,
    )

    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.state.displayed_products = [
        type("R", (), {"product_id": "p2", "name": "P2", "price": 10.0})()
    ]
    earlier = (tuple(DisplayedRef(f"p{i}", f"P{i}", None) for i in range(1, 4)),) if zoom else ()
    ctx.state_v2 = ConversationStateV2(
        revision=2,
        references=References(
            displayed_products=(DisplayedRef("p2", "P2", None),), recent_sets=earlier
        ),
    )
    return ctx


async def test_exact_shortcuts_defer_an_ordinal_on_a_zoomed_screen_to_the_kernel():
    """Verificarea independentă: cu kernelul servind, scurtăturile EXACTE rulau întâi și numărau
    ecranul de un card («linkul la primul» → produsul din detaliu). Acum renunță, iar kernelul
    aplică regula listei."""
    from src.agent import deterministic as det

    ctx = _shortcut_ctx("trimite-mi linkul la primul", zoom=True)
    assert await det._pre_intents(ctx, object(), exact_only=True) is False
    assert [
        e.properties.get("reason") for e in ctx.events if e.type == "shortcut_deferred_to_kernel"
    ] == ["zoomed_ordinal"]


async def test_no_deferral_without_a_list_behind_the_screen():
    from src.agent import deterministic as det

    ctx = _shortcut_ctx("trimite-mi linkul la primul", zoom=False)
    assert det._zoom_screen(ctx) is False
    zoomed = _shortcut_ctx("trimite-mi linkul la primul", zoom=True)
    assert det._zoom_screen(zoomed) is True


# ── verificarea pe conversația reală `625ab925` și pe catalogul real ─────────────────────────


@pytest.mark.parametrize(
    ("value", "hit"),
    [
        ("acid hialuronic", True),
        ("complex de 8 tipuri de acid hialuronic", True),  # 51 de forme pe SOLE
        ("acidul hialuronic", True),  # forma articulată
        ("acid salicilic", False),
        ("hialuronat de sodiu", False),  # sinonim chimic: declarat, nu prins
    ],
)
def test_an_additive_exclusion_matches_the_phrase_inside_catalog_values(value, hit):
    """Pe catalogul real, potrivirea exactă prindea 590 din 726 de apariții; la o EXCLUDERE,
    greșeala sigură e să scoți în plus."""
    exclude = {"key_ingredients": ["acid hialuronic"]}
    assert ct.excluded_hit({"key_ingredients": ["ceramide", value]}, exclude) is hit


def test_an_exclusion_turn_without_a_subject_still_searches():
    """`cae37e6a`: «nu vreau cu acid hialuronic» după «ceva de hidratare», fără raft și fără tip.
    Pe v5.1/v6.0 inițial planul era `reply_only` + `no_query`, deci excluderea nu rula niciodată.
    Textul devine eticheta locale-i a filtrului din stare."""
    from dataclasses import replace

    from src.domain.pack import FacetSpec

    pack = fc.pack("fashion")
    labelled = replace(
        pack,
        comparison_facets=(
            *tuple(pack.comparison_facets or ()),
            FacetSpec(key="material", value_labels={"bumbac": {"ro": "bumbac organic"}}),
        ),
    )
    interp = TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[], query=None)],
        changes=[],
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )
    state = ConversationStateV2(
        revision=1,
        needs=(
            Need(
                key="material",
                operator="eq",
                normalized_value="bumbac",
                strength="hard",
                status="active",
                source="user_explicit",
            ),
            _need("restriction", "poliester"),
        ),
    )
    gate = GateOutcome(AmbiguityDecision(verdict="act", reason="clear", question=None))
    planned = plan_turn(
        interp,
        state,
        (),
        (),
        gate,
        changed=True,
        pack=labelled,
        vocab=fc.vocabulary("fashion"),
        locale="ro",
    )
    plan = planned.plans[planned.primary]
    assert plan.executor == "search", planned
    assert plan.search_args.query == "bumbac organic"
    assert plan.search_args.exclude == {"material": ["poliester"]}
    assert "no_query" not in planned.gaps


@pytest.mark.parametrize(
    ("said", "value", "expected"),
    [
        ("mi se pare uscat", "dry", "implicit"),  # «pare» e verbul, nu „par” + „e”
        ("am tenul uscat", "dry", "explicit"),  # «tenul» = „ten” + articolul „ul”
    ],
)
def test_a_three_letter_stem_needs_a_real_suffix(said, value, expected):
    pack_vocab = CatalogVocabulary(
        business_id="b",
        dimensions={"skin_type": (VocabEntry(key="dry", label="dry", count=1),)},
    )
    change = StateChange(
        op="set",
        target=None,
        dimension="skin_type",
        relation="eq",
        value=value,
        number=None,
        unit=None,
        relative_to=None,
        quote=said,
    )
    interp = TurnInterpretation(
        thread="continue",
        acts=[Act(kind="find", targets=[], query=None)],
        changes=[change],
        references=[],
        ambiguities=[],
        corrects_previous_turn=False,
    )
    from dataclasses import replace

    pack = replace(fc.pack("sole-ro"), concern_map={"par uscat": "dry", "ten uscat": "dry"})
    [c] = check_changes(interp, words=UserWords(said, ()), vocab=pack_vocab, pack=pack, locale="ro")
    assert c.provenance == expected
