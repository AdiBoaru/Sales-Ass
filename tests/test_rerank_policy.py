from src.agent.query_spec import Constraint
from src.domain.rerank_policy import decide_adaptive_rerank


def test_exact_identifier_never_requests_rerank():
    decision = decide_adaptive_rerank(
        identifier_status="resolve",
        constraints=(Constraint(facet="concern", op="contains", value="oily"),),
        lexical_ids=["p1", "p2"],
        semantic_ids=["p2", "p1"],
    )

    assert decision.requested is False
    assert decision.reasons == ("exact_identifier",)


def test_multiple_constraints_or_disagreement_request_rerank_without_query_text():
    decision = decide_adaptive_rerank(
        identifier_status="not_found",
        constraints=(
            Constraint(facet="concern", op="contains", value="oily"),
            Constraint(facet="price", op="lte", value=80),
        ),
        lexical_ids=["p1", "p2"],
        semantic_ids=["p2", "p1"],
    )

    assert decision.requested is True
    assert decision.reasons == ("multiple_constraints", "lexical_semantic_disagreement")


def test_close_scores_request_rerank_but_large_or_invalid_margins_do_not():
    close = decide_adaptive_rerank(
        identifier_status="not_found",
        constraints=(),
        lexical_ids=["p1", "p2"],
        semantic_ids=[],
        close_score_margin=0.03,
    )
    far = decide_adaptive_rerank(
        identifier_status="not_found",
        constraints=(),
        lexical_ids=["p1", "p2"],
        semantic_ids=[],
        close_score_margin=0.04,
    )
    invalid = decide_adaptive_rerank(
        identifier_status="not_found",
        constraints=(),
        lexical_ids=["p1", "p2"],
        semantic_ids=[],
        close_score_margin=-0.01,
    )

    assert close.reasons == ("close_scores",)
    assert close.requested is True
    assert far.requested is False
    assert invalid.requested is False


def test_insufficient_candidates_skips_rerank():
    decision = decide_adaptive_rerank(
        identifier_status="not_found",
        constraints=(Constraint(facet="price", op="lte", value=80),),
        lexical_ids=["p1"],
        semantic_ids=["p1"],
    )

    assert decision.requested is False
    assert decision.reasons == ("insufficient_candidates",)
