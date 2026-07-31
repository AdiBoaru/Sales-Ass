from src.domain.identifier_resolution import IdentifierCandidate, resolve_identifier

_CANDIDATES = (
    IdentifierCandidate("p-1", "Ser Niacinamida 10%", skus=("SER-NIA-10",)),
    IdentifierCandidate("p-2", "Ser Niacinamida 5%", aliases=("ser pentru pori",)),
    IdentifierCandidate("p-3", "Crema cu Ceramide"),
)


def test_exact_sku_and_approved_alias_resolve_without_fuzzy_guessing():
    sku = resolve_identifier("ser-nia-10", _CANDIDATES)
    alias = resolve_identifier("ser pentru pori", _CANDIDATES)

    assert sku.status == "resolve" and sku.product_id == "p-1" and sku.score == 100.0
    assert alias.status == "resolve" and alias.product_id == "p-2" and alias.score == 100.0


def test_high_confidence_name_resolves_but_ambiguous_or_medium_match_clarifies():
    resolved = resolve_identifier("ser niacinamida 10", _CANDIDATES)
    ambiguous = resolve_identifier("ser niacinamida", _CANDIDATES)

    assert resolved.status == "resolve" and resolved.product_id == "p-1"
    assert ambiguous.status == "clarify" and set(ambiguous.candidate_ids) == {"p-1", "p-2"}


def test_low_confidence_match_is_not_found():
    result = resolve_identifier("sampon violet", _CANDIDATES)

    assert result.status == "not_found"
    assert result.product_id is None and result.candidate_ids == ()


def test_fuzzy_scores_skus_and_aliases_not_only_the_name():
    """Modulul rezolvă nume/SKU/alias, dar fuzzy-ul scora exclusiv `name` — un SKU tastat greșit nu
    se potrivea niciodată, adică exact cazul pentru care există fuzzy (tastare umană)."""
    candidates = [
        IdentifierCandidate(
            product_id="p-1",
            name="Ser cu Niacinamidă 10% pentru ten gras și mixt",
            skus=("ABC-1234",),
            aliases=("serul cu niacinamida",),
        ),
        IdentifierCandidate(product_id="p-2", name="Cremă hidratantă de zi"),
    ]

    # SKU greșit cu un caracter: produsul corect e OFERIT, dar ca `clarify`, nu ales în locul
    # clientului. Înainte scorul se calcula contra numelui lung → `not_found`, deci produsul nu
    # apărea deloc. Diferența nu e „mai multă încredere", e „candidatul există".
    typo_sku = resolve_identifier("ABC-1235", candidates)
    assert typo_sku.status == "clarify" and typo_sku.candidate_ids == ("p-1",)

    # Aliasul aproximat e destul de lung ca potrivirea să fie neechivocă → se rezolvă direct.
    typo_alias = resolve_identifier("serul cu niacinamda", candidates)
    assert typo_alias.status == "resolve" and typo_alias.product_id == "p-1"


def test_exact_sku_still_wins_before_any_fuzzy():
    candidates = [
        IdentifierCandidate(product_id="p-1", name="Altceva complet", skus=("ABC-1234",)),
        IdentifierCandidate(product_id="p-2", name="ABC 1234 crema"),
    ]

    assert resolve_identifier("ABC-1234", candidates).product_id == "p-1"
