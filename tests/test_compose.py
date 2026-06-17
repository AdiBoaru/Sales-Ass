"""Teste pentru compoziția recomandării bogate (model iZi) — src/worker/compose.py.

Garanția centrală: faptele (preț/rating/link) vin DOAR din retrieval; proza LLM e
scrubuită; un product_id necunoscut e aruncat tăcut; motivul cardului e ancorat pe un
avantaj REAL (top_pro). Pur, fără I/O — zero DB/LLM.
"""

from types import SimpleNamespace

from src.worker import compose


def _ctx(language: str = "ro", vertical: str = "beauty") -> SimpleNamespace:
    return SimpleNamespace(language=language, business=SimpleNamespace(vertical=vertical))


def test_scrub_drops_numbers_claims_superlatives() -> None:
    assert compose.scrub_prose("pentru mâini foarte uscate") == "pentru mâini foarte uscate"
    assert compose.scrub_prose("4.9 stele") is None
    assert compose.scrub_prose("peste 500 de recenzii") is None
    assert compose.scrub_prose("livrare în 24h") is None
    assert compose.scrub_prose("are 15% reducere") is None
    assert compose.scrub_prose("cel mai bun produs") is None
    assert compose.scrub_prose("") is None
    assert compose.scrub_prose(None) is None


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
