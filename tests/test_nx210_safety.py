from src.agent.query_spec import RuntimeQuerySpec
from src.domain.search_entities import SearchEntitiesResult, SearchEntityCandidate
from src.evals.nx210_prototype import run_offline_prototype


def _spec() -> RuntimeQuerySpec:
    return RuntimeQuerySpec(
        raw_query="ser",
        normalized_query="ser",
        search_text="ser",
    )


def _candidate(product_id: str, evidence_id: str) -> SearchEntityCandidate:
    return SearchEntityCandidate(
        product_id=product_id,
        match_class="exact",
        constraint_results=(),
        soft_penalty=0,
        reason_codes=(),
        warning=None,
        evidence_ids=(evidence_id,),
    )


def _result(*candidates: SearchEntityCandidate) -> SearchEntitiesResult:
    return SearchEntitiesResult(
        candidates=tuple(candidates),
        constraint_coverage=(),
        missing_information=(),
        needs_refinement=False,
        identifier_status="not_found",
        evidence=(),
    )


class _Agent:
    async def initial_query_spec(self, *_args, **_kwargs):
        return _spec()

    async def refine_query_spec(self, *_args, **_kwargs):
        return None


async def test_report_redacts_identifier_pii_and_validator_falls_back():
    async def search(_query_spec):
        return _result(_candidate("0712345678", "evidence-0712345678"))

    run = await run_offline_prototype(
        "ser",
        history=(),
        profile={},
        agent=_Agent(),
        search=search,
    )

    assert run.plan.disposition == "fallback"
    assert run.plan.stop_reason == "validator_rejected"
    assert run.plan.degradations == (
        "report_identifier_redacted",
        "answer_plan_invalid",
    )
    assert "0712345678" not in str(run.report_dict())


async def test_cost_meter_failure_is_named_and_does_not_silence_run():
    async def search(_query_spec):
        return _result(
            _candidate("p-1", "e-1"),
            _candidate("p-2", "e-2"),
        )

    def broken_cost_meter():
        raise RuntimeError("meter unavailable")

    run = await run_offline_prototype(
        "ser",
        history=(),
        profile={},
        agent=_Agent(),
        search=search,
        cost=broken_cost_meter,
    )

    assert run.plan.disposition == "answer"
    assert run.plan.degradations == ("cost_meter_failed",)
    assert run.cost_usd is None
