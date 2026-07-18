"""NX-175 — reranker FAQ pur: calificatori + marjă → serve/clarify/miss.

Scorurile din fixture sunt cele MĂSURATE LIVE (top-5 cosine pe embeddings reale), ca testul să
reproducă exact bug-ul, nu unul inventat.
"""

from src.knowledge.faq_rerank import FaqCandidate, rerank

# Măsurat live pentru „Cum pot face un retur?" (vezi tasks/NX-175.md).
RETUR_GENERIC = [
    FaqCandidate("ex", "Pot returna un produs desfăcut?", "Nu. Produsele desigilate…", 0.619),
    FaqCandidate("gen", "Cum returnez un produs?", "Ai 14 zile calendaristice…", 0.592),
    FaqCandidate("ex2", "Pot returna un produs cosmetic desigilat sau deschis?", "Nu. …", 0.572),
    FaqCandidate("bani", "În cât timp primesc banii înapoi la retur?", "Îți rambursăm…", 0.537),
    FaqCandidate("transp", "Cine plătește transportul de retur?", "Costul returului…", 0.513),
]

# Măsurat live pentru „pot da inapoi un produs desfacut?".
RETUR_EXCEPTIE = [
    FaqCandidate("ex", "Pot returna un produs desfăcut?", "Nu. Produsele desigilate…", 0.820),
    FaqCandidate("ex2", "Pot returna un produs cosmetic desigilat sau deschis?", "Nu. …", 0.709),
    FaqCandidate("gen", "Cum returnez un produs?", "Ai 14 zile calendaristice…", 0.684),
]


# --- bug-ul măsurat: generic → procedura GENERALĂ, nu excepția ---------------------------------


def test_generic_return_query_picks_general_procedure():
    """«Cum pot face un retur?» (fără calificator) → procedura generală, NU excepția «Nu.»."""
    d = rerank("cum pot face un retur", RETUR_GENERIC)
    assert d.action == "serve"
    assert d.faq_id == "gen"
    assert d.answer.startswith("Ai 14 zile")
    assert not d.answer.startswith("Nu.")


def test_generic_return_confidence_is_original_cosine():
    """Pragul caller-ului se aplică pe cosine-ul ORIGINAL, nu pe scorul ajustat de rerank."""
    d = rerank("cum pot face un retur", RETUR_GENERIC)
    assert d.confidence == 0.592  # cosine original al FAQ-ului ales


# --- calificator explicit → excepția -----------------------------------------------------------


def test_qualified_query_picks_exception():
    """«pot da inapoi un produs desfacut?» (are calificator) → excepția, corect."""
    d = rerank("pot da inapoi un produs desfacut", RETUR_EXCEPTIE)
    assert d.action == "serve" and d.faq_id == "ex"
    assert d.answer.startswith("Nu.")


def test_qualified_query_desigilat_matches_exception():
    d = rerank("pot returna ceva desigilat", RETUR_EXCEPTIE)
    assert d.action == "serve" and d.answer.startswith("Nu.")


# --- fără regresie pe clusterele care merg azi (marje mari) -------------------------------------


def test_unambiguous_cluster_serves_top1():
    """«cine plateste returul» → top-1 clar (marjă 0.307 live) → serve direct, fără clarify."""
    cands = [
        FaqCandidate("transp", "Cine plătește transportul de retur?", "Costul returului…", 0.847),
        FaqCandidate("bani", "În cât timp primesc banii înapoi la retur?", "Rambursăm…", 0.540),
        FaqCandidate("ex", "Pot returna un produs desfăcut?", "Nu. …", 0.526),
    ]
    d = rerank("cine plateste returul", cands)
    assert d.action == "serve" and d.faq_id == "transp"


def test_direct_return_query_still_works():
    """«cum returnez» (marjă 0.286 live) → procedura, neschimbat."""
    cands = [
        FaqCandidate("gen", "Cum returnez un produs?", "Ai 14 zile…", 0.725),
        FaqCandidate("ex", "Pot returna un produs desfăcut?", "Nu. …", 0.439),
    ]
    d = rerank("cum returnez", cands)
    assert d.action == "serve" and d.faq_id == "gen"


# --- ambiguitate reală → clarify ---------------------------------------------------------------


def test_true_ambiguity_asks_clarify():
    """Două răspunsuri DIFERITE la DEAD-HEAT (< margin_eps), fără calificator → clarificare."""
    cands = [
        FaqCandidate("a", "Cum aleg crema pentru tenul meu?", "Depinde de tipul de ten…", 0.70),
        FaqCandidate("b", "Ce garanție au produsele?", "Produsele sunt garantate…", 0.685),
    ]
    d = rerank("ceva despre produse", cands)
    assert d.action == "clarify"
    assert {o[0] for o in d.clarify_options} == {"a", "b"}
    assert [o[1] for o in d.clarify_options]  # întrebările, ca chips


def test_related_but_distinct_subtopics_serve_top1_not_clarify():
    """«cum pot face un retur» → procedura (0.592) e la 0.055 de «când primesc banii» (0.537):
    sub-topicuri înrudite, NU alternative → serve top-1, fără clarify inutil."""
    d = rerank("cum pot face un retur", RETUR_GENERIC)
    assert d.action == "serve" and d.faq_id == "gen"


def test_near_tie_with_identical_answers_does_not_clarify():
    """Duplicate (răspuns identic) la egalitate → servim, NU întrebăm (n-are rost)."""
    cands = [
        FaqCandidate("p1", "Cum plătesc?", "Card sau ramburs.", 0.70),
        FaqCandidate("p2", "Ce metode de plată acceptați?", "Card sau ramburs.", 0.67),
    ]
    d = rerank("cum platesc", cands)
    assert d.action == "serve"


# --- degradare ---------------------------------------------------------------------------------


def test_empty_candidates_is_miss():
    assert rerank("orice", []).action == "miss"


def test_single_candidate_serves_without_clarify():
    d = rerank(
        "cum returnez", [FaqCandidate("gen", "Cum returnez un produs?", "Ai 14 zile…", 0.72)]
    )
    assert d.action == "serve" and d.faq_id == "gen"


def test_ranking_exposed_for_observability():
    d = rerank("cum pot face un retur", RETUR_GENERIC)
    assert d.ranking and d.ranking[0][0] == "gen"  # primul după rerank
    # excepția a fost demotată sub general
    scores = dict(d.ranking)
    assert scores["gen"] > scores["ex"]
