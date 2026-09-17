"""NX-297 felia 3 — stiva învață din ce a CĂUTAT agentul, nu din ce a extras un model mic.

Două jumătăți, testate separat: funcția PURĂ (ce devine constrângere și ce nu) și poarta ancorată
din `deterministic.py` (care avea un singur producător: sloturile triajului).
"""

from types import SimpleNamespace

import pytest

from src.agent.deterministic import _turn_has_new_constraints
from src.config import get_settings
from src.conversation.observed_constraints import from_search_args
from src.domain.constraints import build_units
from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext

# ── funcția pură ───────────────────────────────────────────────────────────────────────────────


def test_a_spoken_budget_becomes_a_constraint():
    got, stats = from_search_args([{"price_max": 100}], "vreau o crema sub 100 lei")
    assert got == {"budget_max": 100}
    assert stats == {"kept": 1, "inferred": 0}


def test_an_invented_budget_does_not_stick():
    """Modelul poate căuta cu un prag pe care clientul nu l-a rostit — util pentru turul ăsta, dar
    lipicios peste ture ar filtra tăcut tot ce urmează."""
    got, stats = from_search_args([{"price_max": 250}], "vreau o crema hidratanta")
    assert got == {}
    assert stats["inferred"] == 1


def test_spoken_concerns_are_unioned_and_capped():
    calls = [
        {"concerns": ["ten uscat"]},
        {"concerns": ["ten uscat", "riduri"]},  # duplicat + unul nou, ambele rostite
    ]
    got, _ = from_search_args(calls, "am tenul uscat si riduri")
    assert got["concerns"] == ["ten uscat", "riduri"]


def test_the_last_spoken_value_wins_in_the_same_turn():
    """Clientul se poate corecta în aceeași frază; câștigă ultima cerere a modelului."""
    got, _ = from_search_args(
        [{"price_max": 100}, {"price_max": 150}], "sub 100 lei, ma rog, hai 150"
    )
    assert got == {"budget_max": 150}


def test_category_never_enters_the_stack():
    """Categoria e singura cheie care RESETEAZĂ stiva. Una aleasă de model, necoroborată, ar putea
    șterge constrângerile clientului — resetul are deja o sursă nano-free (`topic_switched`)."""
    got, _ = from_search_args([{"category": "ingrijirea-parului"}], "ceva de par")
    assert "category_key" not in got


def test_execution_arguments_are_not_constraints():
    got, _ = from_search_args(
        [{"sort_mode": "price_asc", "in_stock_only": True, "limit": 6}], "cel mai ieftin"
    )
    assert got == {}


# ── poarta ancorată ────────────────────────────────────────────────────────────────────────────


def _ctx(body: str, *, filters: dict | None = None) -> TurnContext:
    pack = SimpleNamespace(
        units=build_units(
            {"price": {"canonical": "lei", "factors": {"lei": 1, "ron": 1}, "default_op": "lte"}},
        )
    )
    ctx = TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo", domain_pack=pack),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
    )
    ctx.route = RouteDecision(route=Route.SALES, filters=filters or {})
    return ctx


def test_triage_slots_still_close_the_gate():
    """Cu nano viu, predicatul rămâne cel de azi — byte-identic."""
    ctx = _ctx("compara-le", filters={"budget_max": 100})
    assert _turn_has_new_constraints(ctx, ctx.route) is True


def test_a_spoken_threshold_closes_the_gate_without_triage():
    """«compară-le, dar sub 100 lei» nu e o comparație pe setul afișat, e o căutare nouă."""
    ctx = _ctx("compara-le dar sub 100 lei")
    assert _turn_has_new_constraints(ctx, ctx.route) is True


def test_a_pure_reference_leaves_the_gate_open():
    """«linkul la crema asta» n-are nicio valoare cu unitate: poarta ancorată rămâne deschisă."""
    ctx = _ctx("da-mi linkul la crema asta")
    assert _turn_has_new_constraints(ctx, ctx.route) is False


def test_a_number_without_a_unit_is_not_a_constraint():
    """„am 2 copii" nu e un buget. Registrul de unități e cheia întregii extrageri (NX-266)."""
    ctx = _ctx("compara-le, am 2 copii")
    assert _turn_has_new_constraints(ctx, ctx.route) is False


def test_a_tenant_without_units_keeps_the_old_behaviour():
    """Fără tabel de unități nu putem deosebi o cifră de o valoare. Fail-OPEN, ca înainte."""
    ctx = _ctx("compara-le dar sub 100 lei")
    ctx.business.domain_pack = None
    assert _turn_has_new_constraints(ctx, ctx.route) is False


def test_an_opaque_action_has_no_text_to_read():
    """NX-236: mesajul e gol prin construcție, comanda e DECLARATĂ, nu dedusă."""
    ctx = _ctx("")
    assert _turn_has_new_constraints(ctx, ctx.route) is False


# ── lipitura: stiva chiar reține pragul peste ture ─────────────────────────────────────────────


def _run_with(args: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(search_args=args)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Felia are flagul ei (`OBSERVED_CONSTRAINTS_ENABLED`), deci secțiunea asta îl aprinde.

    Autouse doar de aici în jos ar cere alt fișier; cum funcția pură și poarta ancorată nu citesc
    settings, aprinderea lor e un no-op. Testul de kill-switch îl stinge înapoi explicit."""
    monkeypatch.setattr(get_settings(), "observed_constraints_enabled", True)


def test_kill_switch_leaves_the_stack_exactly_as_it_was(monkeypatch):
    """OFF = byte-identic. Perechea obligatorie: o sursă nouă care scrie în aceeași stivă nu are
    voie să pornească fără ca cineva s-o aprindă — `brand` nu se relaxează niciodată în căutare,
    deci o valoare lipită acolo filtrează tăcut turele următoare."""
    from src.worker.stages.agent import _learn_constraints

    monkeypatch.setattr(get_settings(), "observed_constraints_enabled", False)
    ctx = _ctx("caut ceva de la Kundal sub 100 lei")
    _learn_constraints(ctx, _run_with([{"price_max": 100, "brand": "Kundal"}]), ctx.message.body)
    assert ctx.state.search_constraints == {}
    assert ctx.state_proposals == []
    assert not any(e.type == "constraint_source" for e in ctx.events)


def test_the_learned_constraint_survives_the_v2_state_writer(monkeypatch):
    """Sub `CONVERSATION_STATE_V2_WRITE_ENABLED` docul persistat se re-derivă la commit din
    PROPUNERI, din starea proaspăt citită — nu din `ctx.state`. O felie care scrie doar dicționarul
    e inertă exact pe profilul care rulează azi, și tace în loc să pice.

    Testul merge până la docul chiar persistat, nu până la propunere: între ele stă reducerul, care
    are dreptul să respingă."""
    from src.worker.processor import _build_new_state
    from src.worker.stages.agent import _learn_constraints

    s = get_settings()
    monkeypatch.setattr(s, "conversation_state_v2_enabled", True)
    monkeypatch.setattr(s, "conversation_state_v2_write_enabled", True)

    ctx = _ctx("caut o crema sub 100 lei")
    _learn_constraints(ctx, _run_with([{"price_max": 100}]), ctx.message.body)
    ctx.set_reply("ok")
    doc = _build_new_state({}, ctx, is_rich=False, has_products=False)

    needs = {n["key"]: n for n in doc.get("needs") or []}
    assert "budget_max" in needs, f"pragul nu s-a persistat: {doc}"
    assert needs["budget_max"]["normalized_value"] == 100
    # Sursa e ce contează la fel de mult ca valoarea: `corroborated_by` a confirmat că pragul a fost
    # ROSTIT, deci nevoia are voie să fie `hard`. O valoare inferată n-ar fi ajuns aici deloc.
    assert needs["budget_max"]["source"] == "user_explicit"


def test_the_stack_remembers_what_the_agent_searched_with():
    """DoD-ul feliei: «caut o cremă sub 100 lei» → «mai arată-mi» păstrează pragul, fără nano."""
    from src.worker.stages.agent import _learn_constraints

    ctx = _ctx("caut o crema sub 100 lei")
    _learn_constraints(ctx, _run_with([{"price_max": 100}]), ctx.message.body)
    assert ctx.state.search_constraints == {"budget_max": 100}

    # Turul următor: clientul nu repetă nimic, agentul nu mai caută cu prag.
    nxt = _ctx("mai arata-mi")
    nxt.state.search_constraints = dict(ctx.state.search_constraints)
    _learn_constraints(nxt, _run_with([{"query": "crema"}]), nxt.message.body)
    assert nxt.state.search_constraints["budget_max"] == 100


def test_a_turn_without_searches_leaves_the_stack_alone():
    from src.worker.stages.agent import _learn_constraints

    ctx = _ctx("multumesc")
    ctx.state.search_constraints = {"budget_max": 100}
    _learn_constraints(ctx, _run_with([]), ctx.message.body)
    assert ctx.state.search_constraints == {"budget_max": 100}
    assert not any(e.type == "constraint_source" for e in ctx.events)


def test_an_inferred_value_is_counted_but_not_persisted():
    from src.worker.stages.agent import _learn_constraints

    ctx = _ctx("vreau o crema hidratanta")
    _learn_constraints(ctx, _run_with([{"price_max": 250}]), ctx.message.body)
    assert ctx.state.search_constraints == {}
    ev = [e for e in ctx.events if e.type == "constraint_source"]
    assert ev and (ev[0].properties["kept"], ev[0].properties["inferred"]) == (0, 1)
