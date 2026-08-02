from src.agent.match_gate import ConstraintResult, FacetCoverage
from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.domain.search_entities import SearchEntitiesResult, SearchEntityCandidate
from src.evals.nx210_prototype import (
    AnswerPlanV0,
    PrototypeBudget,
    preserves_hard_constraints,
    run_offline_prototype,
    validate_answer_plan_v0,
)


def _spec(search_text: str, constraints=()) -> RuntimeQuerySpec:
    return RuntimeQuerySpec(
        raw_query="telefon 0712 345 678 ser",
        normalized_query="telefon 0712 345 678 ser",
        search_text=search_text,
        constraints=tuple(constraints),
    )


def _candidate(product_id: str, match_class: str = "exact", evidence=()):
    return SearchEntityCandidate(
        product_id=product_id,
        match_class=match_class,
        constraint_results=(),
        soft_penalty=0,
        reason_codes=(),
        warning=None,
        evidence_ids=tuple(evidence),
    )


def _result(*candidates, missing=(), needs_refinement=False, identifier_status="not_found"):
    return SearchEntitiesResult(
        candidates=tuple(candidates),
        constraint_coverage=(),
        missing_information=tuple(missing),
        needs_refinement=needs_refinement,
        identifier_status=identifier_status,
        evidence=(),
    )


class _Agent:
    def __init__(self, specs):
        self.specs = list(specs)
        self.refinements = 0

    async def initial_query_spec(self, raw_message, *, history, profile):
        assert raw_message and isinstance(history, tuple) and profile == {"skin": "oily"}
        return self.specs[0]

    async def refine_query_spec(self, *_args, **_kwargs):
        self.refinements += 1
        return self.specs[self.refinements]


async def test_offline_prototype_stops_on_grounded_candidates_and_drops_raw_from_report():
    agent = _Agent([_spec("ser")])

    async def search(_query_spec):
        return _result(
            _candidate("p-1", evidence=("e-1",)),
            _candidate("p-2", evidence=("e-2",)),
        )

    run = await run_offline_prototype(
        "telefon 0712 345 678 ser",
        history=(),
        profile={"skin": "oily"},
        agent=agent,
        search=search,
    )

    assert run.plan.disposition == "answer"
    assert run.plan.stop_reason == "sufficient_grounded_candidates"
    assert run.plan.selected_product_ids == ("p-1", "p-2")
    assert run.plan.search_attempts == 1
    assert "0712" not in str(run.report_dict())
    assert [span.stage for span in run.spans] == ["query_spec", "search_entities"]


async def test_refinement_cannot_relax_initial_hard_constraint():
    hard = Constraint(facet="price", op="lte", value=100, strength="hard")
    agent = _Agent([_spec("ser", (hard,)), _spec("ser ieftin")])

    async def search(_query_spec):
        return _result(
            _candidate("p-1", match_class="alternative"),
            missing=("price",),
            needs_refinement=True,
        )

    run = await run_offline_prototype(
        "ser sub 100",
        history=(),
        profile={"skin": "oily"},
        agent=agent,
        search=search,
    )

    assert run.plan.stop_reason == "hard_constraint_relaxation"
    assert run.plan.disposition == "clarify"
    assert run.plan.search_attempts == 1


async def test_search_failure_returns_visible_fallback():
    async def search(_query_spec):
        raise RuntimeError("database unavailable")

    run = await run_offline_prototype(
        "ser",
        history=(),
        profile={"skin": "oily"},
        agent=_Agent([_spec("ser")]),
        search=search,
    )

    assert run.plan.disposition == "fallback"
    assert run.plan.stop_reason == "search_unavailable"
    assert run.plan.degradations == ("search_entities_failed",)


async def test_prototype_never_searches_more_than_three_times():
    agent = _Agent([_spec("q1"), _spec("q2"), _spec("q3")])
    calls = 0

    async def search(_query_spec):
        nonlocal calls
        calls += 1
        return _result(
            _candidate(f"p-{calls}", match_class="alternative"),
            missing=("finish",),
            needs_refinement=True,
        )

    run = await run_offline_prototype(
        "ser",
        history=(),
        profile={"skin": "oily"},
        agent=agent,
        search=search,
        budget=PrototypeBudget(max_searches=3),
    )

    assert calls == 3
    assert run.plan.search_attempts == 3
    assert run.plan.stop_reason == "max_searches"


def test_hard_constraint_guard_is_multiset_aware():
    hard = Constraint(facet="price", op="lte", value=100, strength="hard")
    initial = _spec("ser", (hard, hard))
    one_copy = _spec("ser", (hard,))

    assert preserves_hard_constraints(initial, one_copy) is False


def test_minimal_validator_rejects_rejected_product_and_unknown_evidence():
    result = _result(_candidate("blocked", match_class="rejected", evidence=("real",)))
    plan = AnswerPlanV0(
        disposition="answer",
        selected_product_ids=("blocked",),
        evidence_ids=("invented",),
        stop_reason="complete_coverage",
        search_attempts=1,
    )

    assert validate_answer_plan_v0(plan, result) == ("rejected_product", "unknown_evidence")


def test_constraint_types_used_by_fixture_remain_canonical():
    result = ConstraintResult(facet="price", status="UNKNOWN", strength="hard")
    coverage = FacetCoverage(facet="price", match=0, mismatch=0, unknown=1)

    assert result.status == "UNKNOWN" and coverage.unknown == 1
