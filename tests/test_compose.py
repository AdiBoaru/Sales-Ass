"""Teste pentru compoziția recomandării bogate (model iZi) — src/worker/compose.py.

Garanția centrală: faptele (preț/rating/link) vin DOAR din retrieval; proza LLM e
scrubuită; un product_id necunoscut e aruncat tăcut; motivul cardului e ancorat pe un
avantaj REAL (top_pro). Pur, fără I/O — zero DB/LLM.
"""

from types import SimpleNamespace

from src.models import Direction
from src.worker import compose


def _ctx(
    language: str = "ro",
    vertical: str = "beauty",
    body: str = "",
    history=None,
    constraints=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        language=language,
        business=SimpleNamespace(vertical=vertical),
        message=SimpleNamespace(body=body),
        history=history or [],
        state=SimpleNamespace(constraints=constraints or {}),
    )


def test_scrub_drops_numbers_claims_superlatives() -> None:
    assert compose.scrub_prose("pentru mâini foarte uscate") == "pentru mâini foarte uscate"
    assert compose.scrub_prose("4.9 stele") is None
    assert compose.scrub_prose("peste 500 de recenzii") is None
    assert compose.scrub_prose("livrare în 24h") is None
    assert compose.scrub_prose("are 15% reducere") is None
    assert compose.scrub_prose("cel mai bun produs") is None
    assert compose.scrub_prose("") is None
    assert compose.scrub_prose(None) is None


# --- R4: bugetul clientului permis în intro (nu „Ai ceva sub lei") -----------


def test_scrub_intro_keeps_client_budget_number() -> None:
    assert compose.scrub_intro("Pentru ten gras sub 80 lei", {"80"}) == "Pentru ten gras sub 80 lei"


def test_scrub_intro_drops_unknown_number() -> None:
    # cifră care NU e a clientului (preț inventat) → drop tot intro-ul
    assert compose.scrub_intro("Variante de la 30 lei", set()) is None
    assert compose.scrub_intro("Variante de la 30 lei", {"80"}) is None


def test_scrub_intro_drops_percent_claim_super_even_with_allowed() -> None:
    assert compose.scrub_intro("80% reducere", {"80"}) is None
    assert compose.scrub_intro("livrare în 80 ore", {"80"}) is None
    assert compose.scrub_intro("cel mai bun", set()) is None


def test_scrub_intro_empty_is_none() -> None:
    assert compose.scrub_intro("", {"80"}) is None
    assert compose.scrub_intro(None, {"80"}) is None


def test_allowed_client_numbers_excludes_bot_replies() -> None:
    ctx = _ctx(
        body="ai ceva sub 80 lei?",
        history=[
            SimpleNamespace(direction=Direction.INBOUND, body="vreau ceva de 50 lei"),
            SimpleNamespace(direction=Direction.OUTBOUND, body="Crema X costă 999 lei"),
        ],
        constraints={"buget": "120 lei"},
    )
    nums = compose._allowed_client_numbers(ctx)
    assert {"80", "50", "120"} <= nums
    assert "999" not in nums  # replica botului NU e sursă de cifre permise


def test_assemble_intro_keeps_budget_from_client_message() -> None:
    ctx = _ctx(body="ai ceva sub 80 de lei?")
    j = {
        "intro": "Pentru ten gras sub 80 lei, am ales:",
        "items": [],
        "pick": None,
        "education": None,
        "suggestions": [],
    }
    rich = compose.assemble(ctx, j, [])
    assert rich.intro == "Pentru ten gras sub 80 lei, am ales:"


def test_safe_badge_drops_discount_keeps_curation() -> None:
    assert compose._safe_badge("-50%") is None
    assert compose._safe_badge("Reducere") is None
    assert compose._safe_badge("Top Favorite") == "Top Favorite"
    assert compose._safe_badge(None) is None


def test_assemble_hydrates_facts_and_drops_unknown_ids() -> None:
    retrieved = [
        {
            "id": "A",
            "name": "Crema A",
            "price": 34.99,
            "url": "u/a",
            "rating": 4.7,
            "top_pros": ["hidratează intens", "se absoarbe repede"],
            "review_count": 12,
        },
        {
            "id": "B",
            "name": "Crema B",
            "price": 48.99,
            "url": "u/b",
            "rating": 4.8,
            "top_pros": ["fără parfum"],
        },
    ]
    j = {
        "intro": "Pentru mâini uscate, câteva variante:",
        "items": [
            {"product_id": "A", "pro_index": 0, "fit_clause": "pentru hidratare zilnică"},
            {"product_id": "ZZ", "pro_index": 0, "fit_clause": "inventat"},  # id necunoscut → drop
            {"product_id": "B", "pro_index": 0, "fit_clause": "dacă o vrei fără parfum"},
        ],
        "pick": {"product_id": "A", "justification": "acoperă bine uscăciunea"},
        "education": "Contează ingredientele care refac bariera.",
        "suggestions": ["Una mai ieftină", "Ceva fără parfum", "Compară primele două"],
    }
    rich = compose.assemble(_ctx(), j, retrieved)

    assert [it.product_id for it in rich.items] == ["A", "B"]  # ZZ aruncat
    a = rich.items[0]
    assert a.price == 34.99 and a.rating == 4.7  # din date, nu din LLM
    assert a.reason == "pentru hidratare zilnică — hidratează intens"  # fit + ancoră reală
    assert rich.pick[0] == "A" and "acoperă bine uscăciunea" in rich.pick[1]
    labels = [c.label for c in rich.chips]  # chips = sugestiile LLM, contextuale (nu hardcodate)
    assert "Una mai ieftină" in labels and "Ceva fără parfum" in labels
    assert any("Compară" in lbl for lbl in labels)


def test_suggestion_chips_are_normalized_not_hardcoded() -> None:
    chips = compose._suggestion_chips(
        [
            "Vreau una mai ieftină",
            "  vreau una mai ieftină ",  # dedupe (case + spații)
            "Compară CeraVe cu La Roche-Posay Cicaplast pentru mâinile foarte uscate ale tale",
            "Ceva fără parfum",
            "Hidratant de corp",
            "a cincea peste cap",
        ]
    )
    labels = [c.label for c in chips]
    assert len(chips) == 4  # cap 4
    assert labels.count("Vreau una mai ieftină") == 1  # de-duplicat
    assert any(lbl.endswith("…") for lbl in labels)  # cea lungă e scurtată
    assert all(c.payload == c.label for c in chips)  # tap → trimite labelul ca mesaj nou


def test_assemble_scrubs_bad_fit_but_keeps_real_anchor() -> None:
    retrieved = [{"id": "A", "name": "A", "price": 10.0, "top_pros": ["hidratează"]}]
    j = {
        "intro": None,
        "pick": None,
        "education": None,
        "chip_intents": [],
        "items": [{"product_id": "A", "pro_index": 0, "fit_clause": "4.9 stele garantat"}],
    }
    rich = compose.assemble(_ctx(), j, retrieved)
    assert rich.items[0].reason == "hidratează"  # fit scrubuit, ancora reală rămâne


def test_assemble_invalid_pro_index_falls_back_to_first() -> None:
    retrieved = [{"id": "A", "name": "A", "price": 10.0, "top_pros": ["primul", "al doilea"]}]
    j = {
        "intro": None,
        "items": [{"product_id": "A", "pro_index": 9, "fit_clause": "bun"}],
        "pick": None,
        "education": None,
        "suggestions": [],
    }
    rich = compose.assemble(_ctx(), j, retrieved)
    assert rich.items[0].reason == "bun — primul"


def test_flatten_renders_data_prices_and_disclaimer() -> None:
    retrieved = [
        {
            "id": "A",
            "name": "Crema A",
            "price": 34.99,
            "url": "u",
            "rating": 4.7,
            "top_pros": ["hidratează"],
        }
    ]
    j = {
        "intro": "Intro.",
        "education": "Educație.",
        "suggestions": ["Una mai ieftină"],
        "items": [{"product_id": "A", "pro_index": 0, "fit_clause": "pentru uscăciune"}],
        "pick": {"product_id": "A", "justification": "alegere bună"},
    }
    text = compose.flatten(compose.assemble(_ctx(), j, retrieved))
    assert "34.99 lei" in text and "⭐4.7" in text
    assert "Recomandarea mea: Crema A" in text
    assert "Funcționez cu inteligență" in text


def test_flatten_framing_omits_enumeration_and_chips() -> None:
    """Widget (carduri): flatten_framing = intro + pick + educație + disclaimer, FĂRĂ lista
    numerotată cu preț/rating și FĂRĂ „Poți cere și:" — le fac cardurile + butoanele."""
    retrieved = [
        {
            "id": "A",
            "name": "Crema A",
            "price": 34.99,
            "url": "u",
            "rating": 4.7,
            "top_pros": ["hidratează"],
        }
    ]
    j = {
        "intro": "Intro.",
        "education": "Educație.",
        "suggestions": ["Una mai ieftină"],
        "items": [{"product_id": "A", "pro_index": 0, "fit_clause": "pentru uscăciune"}],
        "pick": {"product_id": "A", "justification": "alegere bună"},
    }
    text = compose.flatten_framing(compose.assemble(_ctx(), j, retrieved))
    assert "Intro." in text  # framing
    assert "Recomandarea mea: Crema A" in text  # pick (numește produsul)
    assert "Educație." in text  # educație
    assert "Funcționez cu inteligență" in text  # disclaimer
    assert "34.99" not in text and "⭐" not in text  # FĂRĂ preț/rating (le fac cardurile)
    assert "1. Crema A" not in text  # FĂRĂ lista numerotată
    assert "Poți cere și" not in text  # chips = butoane, nu text


def test_card_products_has_signature_keys() -> None:
    retrieved = [{"id": "A", "name": "A", "price": 10.0, "url": "u", "top_pros": ["x"]}]
    j = {
        "intro": None,
        "pick": None,
        "education": None,
        "chip_intents": [],
        "items": [{"product_id": "A", "pro_index": 0, "fit_clause": "x"}],
    }
    cards = compose.card_products(compose.assemble(_ctx(), j, retrieved).items)
    assert cards[0]["product_id"] == "A" and cards[0]["price"] == 10.0
