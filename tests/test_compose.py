"""Teste pentru compoziția recomandării bogate (model iZi) — src/worker/compose.py.

Garanția centrală: faptele (preț/rating/link) vin DOAR din retrieval; proza LLM e
scrubuită; un product_id necunoscut e aruncat tăcut; motivul cardului e ancorat pe un
avantaj REAL (top_pro). Pur, fără I/O — zero DB/LLM.
"""

from types import SimpleNamespace

from src.models import Direction, RichItem, RichReply
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


# --- NX-118: stoc availability-aware pe calea bogată ------------------------


def _enable_stock(monkeypatch, on=True):
    monkeypatch.setattr(
        compose,
        "get_settings",
        lambda: SimpleNamespace(
            validator_stock_claims_enabled=on,
            ai_disclaimer_enabled=False,
            card_badges_enabled=False,  # aceste teste nu testează badge-uri → fără interferență
            rich_pick_deterministic_enabled=True,
            safety_medical_guardrail_enabled=True,
        ),
    )


def test_assemble_drops_stock_claim_when_all_out_of_stock(monkeypatch) -> None:
    _enable_stock(monkeypatch)
    retrieved = [{"id": "A", "name": "Crema A", "price": 34.99, "availability": "out_of_stock"}]
    j = {
        "intro": "Avem produsul pe stoc:",
        "items": [{"product_id": "A", "pro_index": 0, "fit_clause": "este disponibil acum"}],
        "education": "Produsul este în stoc.",
    }
    rich = compose.assemble(_ctx(), j, retrieved)
    assert rich.intro is None  # „pe stoc" nefondat (nimic in_stock) → drop
    assert rich.education is None
    assert rich.items[0].reason is None  # fit „disponibil" nefondat → drop


def test_assemble_keeps_stock_claim_when_in_stock(monkeypatch) -> None:
    _enable_stock(monkeypatch)
    retrieved = [
        {
            "id": "A",
            "name": "Crema A",
            "price": 34.99,
            "availability": "in_stock",
            "top_pros": ["bun"],
        }
    ]
    j = {
        "intro": "Avem produsul pe stoc:",
        "items": [{"product_id": "A", "pro_index": 0, "fit_clause": "disponibil acum"}],
    }
    rich = compose.assemble(_ctx(), j, retrieved)
    assert rich.intro == "Avem produsul pe stoc:"  # grounded (in_stock) → păstrat
    assert "disponibil acum" in (rich.items[0].reason or "")


def test_assemble_keeps_negated_stock_when_out_of_stock(monkeypatch) -> None:
    _enable_stock(monkeypatch)
    retrieved = [{"id": "A", "name": "Crema A", "price": 34.99, "availability": "out_of_stock"}]
    j = {"intro": "Din păcate nu mai este pe stoc momentan.", "items": []}
    rich = compose.assemble(_ctx(), j, retrieved)
    # afirmație ONESTĂ de indisponibilitate (negată) → NU se respinge (negation-aware)
    assert rich.intro == "Din păcate nu mai este pe stoc momentan."


def test_assemble_stock_kill_switch_off_keeps_claim(monkeypatch) -> None:
    _enable_stock(monkeypatch, on=False)
    retrieved = [{"id": "A", "name": "Crema A", "price": 34.99, "availability": "out_of_stock"}]
    j = {"intro": "Este pe stoc:", "items": []}
    rich = compose.assemble(_ctx(), j, retrieved)
    assert rich.intro == "Este pe stoc:"  # kill-switch off → byte-identic


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


def test_suggestion_chips_are_normalized_capped_not_hardcoded() -> None:
    chips = compose._suggestion_chips(
        [
            "Vreau una mai ieftină",
            "  vreau una mai ieftină ",  # dedupe (case + spații)
            "Compară CeraVe cu La Roche-Posay Cicaplast pentru mâinile foarte uscate ale tale",
            "Ceva fără parfum",
            "Hidratant de corp",
            "Pentru ten sensibil",
            "Are protecție SPF?",  # al 6-lea unic → cap atins aici
            "Cum îl folosesc?",  # peste cap → exclus
            "a noua peste cap",  # peste cap → exclus
        ]
    )
    labels = [c.label for c in chips]
    assert len(chips) == 6  # cap 6 (IZI-parity), nu 4
    assert labels.count("Vreau una mai ieftină") == 1  # de-duplicat
    assert "Cum îl folosesc?" not in labels and "a noua peste cap" not in labels  # peste cap
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


# --- ARCH-2026 P0: ordine de carduri + pick DETERMINISTE (model narează, cod clasează) ----------


def test_assemble_orders_cards_by_retrieval_rank_not_model() -> None:
    # retrieved e RANKAT (B primul, mai bine clasat; A al doilea). Modelul le dă în ordine INVERSĂ
    # și alege A ca pick. Codul trebuie să respecte rankingul de retrieval, nu modelul.
    retrieved = [
        {"id": "B", "name": "Produs B", "price": 50.0, "top_pros": ["b1"]},
        {"id": "A", "name": "Produs A", "price": 40.0, "top_pros": ["a1"]},
    ]
    j = {
        "items": [
            {"product_id": "A", "pro_index": 0, "fit_clause": "a"},
            {"product_id": "B", "pro_index": 0, "fit_clause": "b"},
        ],
        "pick": {"product_id": "A", "justification": "modelul vrea A"},
    }
    rich = compose.assemble(_ctx(), j, retrieved)
    assert [it.product_id for it in rich.items] == ["B", "A"]  # ordinea de retrieval, nu modelul
    assert rich.pick[0] == "B"  # pick = cel mai bine clasat AFIȘAT, nu alegerea liberă a modelului
    assert rich.pick[1] == "b1"  # model-pick ≠ top → ancora reală a top-ului (top_pro)


def test_assemble_pick_reuses_model_justification_when_agrees() -> None:
    # modelul alege TOP (= cel mai bine clasat) → îi păstrăm justificarea (copy mai bun) + ancoră
    retrieved = [
        {"id": "TOP", "name": "Top", "price": 50.0, "top_pros": ["pro real"]},
        {"id": "OTHER", "name": "Other", "price": 40.0, "top_pros": ["x"]},
    ]
    j = {
        "items": [
            {"product_id": "TOP", "pro_index": 0, "fit_clause": "t"},
            {"product_id": "OTHER", "pro_index": 0, "fit_clause": "o"},
        ],
        "pick": {"product_id": "TOP", "justification": "intră repede în piele"},
    }
    rich = compose.assemble(_ctx(), j, retrieved)
    assert rich.pick[0] == "TOP"
    assert "intră repede în piele" in rich.pick[1]  # justificarea modelului (non-superlativ)
    assert "pro real" in rich.pick[1]  # + ancora factuală reală


def test_assemble_killswitch_off_keeps_model_order_and_pick(monkeypatch) -> None:
    monkeypatch.setattr(
        compose,
        "get_settings",
        lambda: SimpleNamespace(
            validator_stock_claims_enabled=False,
            ai_disclaimer_enabled=False,
            card_badges_enabled=False,
            rich_pick_deterministic_enabled=False,  # OFF → comportament vechi (model)
            safety_medical_guardrail_enabled=True,
        ),
    )
    retrieved = [
        {"id": "B", "name": "Produs B", "price": 50.0, "top_pros": ["b1"]},
        {"id": "A", "name": "Produs A", "price": 40.0, "top_pros": ["a1"]},
    ]
    j = {
        "items": [
            {"product_id": "A", "pro_index": 0, "fit_clause": "a"},
            {"product_id": "B", "pro_index": 0, "fit_clause": "b"},
        ],
        "pick": {"product_id": "A", "justification": "alegere bună"},
    }
    rich = compose.assemble(_ctx(), j, retrieved)
    assert [it.product_id for it in rich.items] == ["A", "B"]  # ordinea modelului (legacy)
    assert rich.pick[0] == "A"  # pick-ul liber al modelului (legacy)


def test_flatten_renders_data_prices_and_disclaimer(monkeypatch) -> None:
    # disclaimer-ul e OFF default → îl PORNIM aici ca să verificăm că `flatten` îl randează când e.
    monkeypatch.setattr(
        compose,
        "get_settings",
        lambda: SimpleNamespace(
            validator_stock_claims_enabled=False,
            ai_disclaimer_enabled=True,
            card_badges_enabled=False,
            rich_pick_deterministic_enabled=True,
            safety_medical_guardrail_enabled=True,
        ),
    )
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


def test_flatten_framing_light_and_variable_single_item() -> None:
    """Widget (#4): la UN singur produs framing-ul = intro + coaching de final (IZI: `education`
    revine pe widget). FĂRĂ „Recomandarea mea" (cardul ESTE recomandarea), FĂRĂ disclaimer (default
    off), FĂRĂ enumerare/preț/rating, FĂRĂ „Poți cere și:"."""
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
    assert "Recomandarea mea" not in text  # un singur produs → fără pick separat
    assert "Educație." in text  # IZI: coaching de final randat acum pe widget (era omis, NX-134)
    assert "Funcționez cu inteligență" not in text  # disclaimer OFF default (#2)
    assert "34.99" not in text and "⭐" not in text  # FĂRĂ preț/rating (le fac cardurile)
    assert "1. Crema A" not in text and "Poți cere și" not in text


def test_flatten_framing_hides_pick_on_web_by_default() -> None:
    """IZI-parity (feedback Adi 2026-06-30): pe WEB pick-ul („Recomandarea mea") e ASCUNS by
    default — rămân intro + education (advisory-ul), cardurile + chips fac restul."""
    retrieved = [
        {"id": "A", "name": "Crema A", "price": 34.99, "top_pros": ["x"]},
        {"id": "B", "name": "Crema B", "price": 48.99, "top_pros": ["y"]},
    ]
    j = {
        "intro": "Două variante:",
        "items": [
            {"product_id": "A", "pro_index": 0, "fit_clause": "a"},
            {"product_id": "B", "pro_index": 0, "fit_clause": "b"},
        ],
        "pick": {"product_id": "A", "justification": "alegere bună"},
        "education": "Educație.",
        "suggestions": [],
    }
    text = compose.flatten_framing(compose.assemble(_ctx(), j, retrieved))
    assert "Recomandarea mea" not in text  # pick ascuns pe web (default)
    assert "Două variante:" in text and "Educație." in text  # intro + coaching rămân


def test_flatten_framing_shows_pick_when_web_flag_on(monkeypatch) -> None:
    """Kill-switch `rich_pick_web_enabled` ON → pick-ul revine în framing (comportament vechi)."""
    rich = RichReply(
        intro="Două variante:",
        items=[
            RichItem(product_id="A", name="Crema A", price=34.99, reason="a"),
            RichItem(product_id="B", name="Crema B", price=48.99, reason="b"),
        ],
        pick=("A", "alegere bună"),
        education="Educație.",
        chips=[],
        disclaimer="",
    )
    monkeypatch.setattr(
        compose, "get_settings", lambda: SimpleNamespace(rich_pick_web_enabled=True)
    )
    text = compose.flatten_framing(rich)
    assert "Recomandarea mea: Crema A" in text  # flag ON → pick prezent
    assert "alegere bună" in text


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
