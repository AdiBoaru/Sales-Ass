"""NX-240 — projectorul `web-view.v2`: pur, display-ready, fără nimic de calculat în browser.

Trei familii de invarianți:

  1. **Puritate** — dacă projectorul atinge DB/HTTP/LLM/ceas, testul cade. Nu prin review, prin
     monkeypatch care aruncă.
  2. **Display-ready** — nu există `float` în ViewModel. Un test scanează recursiv tot payload-ul:
     dacă apare un număr acolo unde ar trebui text, frontendul ar putea face aritmetică.
  3. **Omisiune onestă** — pentru fiecare sursă scoasă pe rând, câmpul dependent ȘI CTA-ul lui
     dispar. Un card fără preț rămâne un card, nu devine „0,00 lei".
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.agent.answer_plan import PlanClarification, PlanNoResults, SelectedProduct
from src.agent.evidence_bundle import build_evidence_bundle
from src.agent.grounding_guard import GroundedAnswer, ground_answer
from src.channels.web.render_v2 import project, project_plan
from src.web.contracts_v2 import MAX_TEXT_LEN, parse_view
from tests.nx240_helpers import (
    BUSINESS_ID,
    NOW,
    PID_A,
    PID_B,
    FakeIssued,
    identity,
    plan,
    row,
)

SLA = 86_400


def bundle(rows=None, **kwargs):
    return build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=rows if rows is not None else [row()],
        now=NOW,
        sla_s=SLA,
        **kwargs,
    )


def render(p=None, b=None, *, locale="ro", actions=(), ident=None, **kwargs):
    answer = ground_answer(p or plan(locale=locale), b or bundle(), locale=locale, **kwargs)
    assert answer.ok, answer.failures
    view = project(
        answer, identity=ident or identity(), locale=locale, issued_actions=actions, now=NOW
    )
    return view.model_dump(mode="json", exclude_none=True)


def blocks(view) -> list[dict]:
    return view["messages"][0]["blocks"] if view["messages"] else []


def block_of(view, type_name):
    return next((b for b in blocks(view) if b["type"] == type_name), None)


def item_of(view, index=0):
    return block_of(view, "product_list")["items"][index]


# ── Puritate ────────────────────────────────────────────────────────────────────────────────
def test_projector_touches_no_io_at_runtime(monkeypatch):
    """Orice apel de DB/LLM din projector e un finding, nu o optimizare de discutat: un projector
    care poate CITI poate citi altceva decât s-a validat."""

    def boom(*args, **kwargs):  # pragma: no cover — chemarea lui E eșecul
        raise AssertionError("projectorul a atins o sursă externă")

    monkeypatch.setattr("src.db.connection.get_pool", boom, raising=False)
    monkeypatch.setattr("src.agent.llm.get_llm", boom, raising=False)
    assert blocks(render())


def test_projector_source_contains_no_clock_or_io_call():
    """`projector_io_violation` ca invariant MECANIC, nu ca alertă în producție: un ceas implicit
    (`datetime.now()`) ar rupe replay-ul exact fără să ridice nimic — două GET-uri ar produce
    texte de prospețime diferite pentru același turn, iar testul de mai sus n-ar prinde-o."""
    import ast
    import inspect

    import src.channels.web.render_v2 as module

    # AST, nu `in source`: un grep peste text ar confunda o EXPLICAȚIE din docstring cu un apel.
    # Verificăm apeluri reale — singurul lucru care poate face rău.
    forbidden = {"now", "utcnow", "time", "monotonic", "get_settings", "sleep"}
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Await, ast.AsyncFunctionDef)), "projectorul e sincron"
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in forbidden, f"apel interzis în projector: {name}"


def test_same_inputs_produce_identical_bytes():
    """Replay exact: aceleași fapte + aceleași acțiuni ⇒ aceiași bytes, oricând."""
    assert render() == render()


def test_projection_does_not_mutate_the_grounded_answer():
    answer = ground_answer(plan(), bundle(), locale="ro")
    before = answer_snapshot = (answer.direct_answer, answer.products, answer.omissions)
    project(answer, identity=identity(), locale="ro", now=NOW)
    assert (answer.direct_answer, answer.products, answer.omissions) == before == answer_snapshot


# ── Display-ready ───────────────────────────────────────────────────────────────────────────
def _numbers_in(node, path="$"):
    """Toate numerele din payload, cu calea lor. `revision` e singurul întreg legitim (e o
    versiune, nu o valoare comercială)."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _numbers_in(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _numbers_in(value, f"{path}[{index}]")
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        found.append(path)
    return found


def test_no_commercial_number_reaches_the_wire():
    view = render()
    assert _numbers_in(view) == ["$.conversation.revision"]


def test_price_previous_and_discount_are_all_localized_text():
    price = item_of(render())["price"]
    assert price == {"current": "89,00 lei", "previous": "120,00 lei", "discount": "-25%"}


def test_locale_changes_separators_currency_word_and_copy():
    view = render(locale="en")
    assert item_of(view)["price"]["current"] == "89.00 RON"
    assert view["chrome"]["dialog_title"] == "Shopping assistant"
    assert item_of(view)["rating"] == "4.8 out of 5 (120 reviews)"


def test_romanian_plural_uses_all_three_forms():
    assert item_of(render())["rating"] == "4,8 din 5 (120 de recenzii)"
    assert item_of(render(b=bundle([row(review_count=1)])))["rating"] == "4,8 din 5 (1 recenzie)"
    assert item_of(render(b=bundle([row(review_count=5)])))["rating"] == "4,8 din 5 (5 recenzii)"


def test_low_stock_becomes_a_sentence_not_a_number():
    assert item_of(render())["availability"] == "Ultimele 3 bucăți"
    assert item_of(render(b=bundle([row(stock=42)])))["availability"] == "În stoc"


# ── Omisiuni per sursă ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("missing", "field"),
    [
        ({"price": None}, "price"),
        ({"currency": None}, "price"),
        ({"rating": None, "review_count": 0}, "rating"),
        ({"availability": None}, "availability"),
        ({"image": None}, "image"),
    ],
)
def test_missing_source_removes_the_field_without_a_placeholder(missing, field):
    item = item_of(render(b=bundle([row(**missing)])))
    assert field not in item
    assert item["title"]  # cardul rămâne randabil


def test_stale_price_disappears_entirely_rather_than_being_shown_as_old():
    item = item_of(render(b=bundle([row(synced_at=NOW - timedelta(days=5))])))
    assert "price" not in item


def test_list_price_below_current_yields_no_previous_and_no_discount():
    price = item_of(render(b=bundle([row(price=120.0, list_price=89.0)])))["price"]
    assert price == {"current": "120,00 lei"}


def test_product_with_only_a_title_still_renders_as_a_card():
    """«All-unknown»: un produs despre care nu știm decât cum îl cheamă rămâne afișabil. E onest
    și e util — clientul vede că există."""
    item = item_of(
        render(
            b=bundle(
                [
                    row(
                        price=None,
                        currency=None,
                        rating=None,
                        review_count=0,
                        availability=None,
                        stock=None,
                        image=None,
                        url=None,
                        brand=None,
                    )
                ]
            )
        )
    )
    assert set(item) == {"view_id", "title", "reason", "badges", "actions"}
    assert item["actions"] == []  # fără URL nu există nici măcar butonul de navigare


# ── URL-uri și injecție ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "//evil.example/x",
        "http://insecure.example/x",
        "https://evil.example/a b",
        "https://evil.example\\x",
    ],
)
def test_dangerous_urls_drop_the_link_without_breaking_the_card(bad_url):
    item = item_of(render(b=bundle([row(url=bad_url, image=bad_url)])))
    assert "image" not in item
    assert item["actions"] == []  # fără URL valid nu există nici butonul „Vezi produsul"
    assert item["title"]


def test_markup_in_a_title_is_carried_as_text_not_as_structure():
    """Contractul nu are câmp de HTML: un `<script>` rămâne exact ce e — caractere într-un string.
    Rendererul pasiv îl afișează ca text, iar asta e tot ce se poate întâmpla."""
    item = item_of(render(b=bundle([row(name="<script>alert(1)</script>")])))
    assert item["title"] == "<script>alert(1)</script>"


def test_oversized_text_is_clipped_to_the_contract_limit():
    """Planul e deja mărginit (`direct_answer` ≤ 900), dar projectorul nu se bazează pe asta: un
    `GroundedAnswer` construit direct trebuie tot să producă un envelope valid, nu o excepție."""
    answer = GroundedAnswer(
        ok=True,
        locale="ro",
        business_id=BUSINESS_ID,
        as_of=NOW,
        direct_answer="x" * (MAX_TEXT_LEN * 3),
    )
    view = project(answer, identity=identity(), locale="ro", now=NOW).model_dump(mode="json")
    assert len(view["messages"][0]["blocks"][0]["text"]) == MAX_TEXT_LEN


def test_more_products_than_the_cap_are_truncated_not_rejected():
    """Capul e dublu: bundle-ul reține 6 produse, iar blocul afișează cel mult 6 itemi. Un
    catalog care întoarce 10 nu produce un payload invalid, ci unul mărginit."""
    rows = [row(f"p{i}") for i in range(10)]
    selected = tuple(
        SelectedProduct(product_id=f"p{i}", variant_id=None, evidence_ids=("e",)) for i in range(6)
    )
    view = render(plan(selected_products=selected, recommendations=()), bundle(rows))
    assert len(block_of(view, "product_list")["items"]) == 6


# ── Blocuri ─────────────────────────────────────────────────────────────────────────────────
def test_no_results_becomes_an_honest_notice_per_class():
    view = render(
        plan(
            selected_products=(),
            recommendations=(),
            no_results=PlanNoResults(
                reason_class="dependency_unavailable", criteria=(), alternatives=()
            ),
        ),
        bundle([]),
    )
    notice = block_of(view, "notice")
    assert notice["level"] == "info" and "nu e disponibil" in notice["text"]


def test_clarification_is_a_lead_text_block():
    view = render(
        plan(
            clarification=PlanClarification(
                question="Cauți ceva pentru zi sau pentru noapte?",
                target_need="usage",
                reason="gain",
                options=("zi", "noapte"),
            )
        )
    )
    lead = next(b for b in blocks(view) if b["type"] == "text" and b["variant"] == "lead")
    assert lead["text"].startswith("Cauți")


def test_disclosures_render_as_their_own_variant():
    view = render(plan(disclosures=("Informațiile vin din catalogul magazinului.",)))
    assert any(b.get("variant") == "disclosure" for b in blocks(view))


def test_memory_block_only_appears_with_displayable_criteria():
    assert block_of(render(), "memory") is None
    view = render(memory_criteria=("Buget: până în 200,00 lei",))
    assert block_of(view, "memory")["criteria"] == ["Buget: până în 200,00 lei"]


def test_reading_order_puts_the_answer_before_the_cards():
    view = render()
    assert [b["type"] for b in blocks(view)] == ["text", "product_list"]


# ── Acțiuni ─────────────────────────────────────────────────────────────────────────────────
def test_product_actions_attach_to_the_card_that_names_them():
    actions = (FakeIssued("request_details", "a1", "tok1", product_ref=PID_A),)
    item = item_of(render(actions=actions))
    labels = [a["label"] for a in item["actions"]]
    assert labels == ["Vezi produsul", "Spune-mi mai multe"]
    assert item["actions"][1]["activation"] == {"type": "submit", "token": "tok1"}


def test_cart_cta_is_dropped_when_the_guard_did_not_allow_commerce():
    """Un token emis pentru un produs devenit nevandabil NU se afișează. Tokenul rămâne valid
    criptografic — dar butonul care l-ar purta n-are voie să existe."""
    actions = (FakeIssued("cart_add_line", "a2", "tok2", product_ref=PID_A),)
    item = item_of(
        render(b=bundle([row(availability="out_of_stock")]), actions=actions, commerce_enabled=True)
    )
    assert [a["label"] for a in item["actions"]] == ["Vezi produsul"]


def test_cart_cta_is_dropped_when_facts_expired_between_emission_and_projection():
    """Cazul temporal: acțiunea a fost planificată când prețul era proaspăt, iar între timp a
    depășit SLA-ul. Butonul dispare odată cu prețul — nu rămâne singur, peste un card mut."""
    actions = (FakeIssued("cart_add_line", "a2", "tok2", product_ref=PID_A),)
    item = item_of(
        render(
            b=bundle([row(synced_at=NOW - timedelta(days=5))]),
            actions=actions,
            commerce_enabled=True,
        )
    )
    assert [a["label"] for a in item["actions"]] == ["Vezi produsul"]


def test_cart_cta_appears_when_facts_and_service_allow_it():
    actions = (FakeIssued("cart_add_line", "a2", "tok2", product_ref=PID_A),)
    item = item_of(render(actions=actions, commerce_enabled=True))
    assert [a["label"] for a in item["actions"]] == ["Vezi produsul", "Adaugă în coș"]


def test_clarification_options_become_chips_labelled_by_our_own_options():
    p = plan(
        clarification=PlanClarification(
            question="Zi sau noapte?",
            target_need="usage",
            reason="gain",
            options=("pentru zi", "pentru noapte"),
        )
    )
    actions = (FakeIssued("answer_clarification", "a3", "tok3", option_ref=1),)
    view = render(p, actions=actions)
    assert block_of(view, "action_row")["actions"][0]["label"] == "pentru noapte"


def test_option_pointing_past_the_list_is_dropped_not_guessed():
    p = plan(
        clarification=PlanClarification(
            question="Zi sau noapte?", target_need="usage", reason="gain", options=("zi",)
        )
    )
    actions = (FakeIssued("answer_clarification", "a3", "tok3", option_ref=5),)
    assert block_of(render(p, actions=actions), "action_row") is None


def test_actions_without_a_label_are_not_rendered_as_empty_buttons():
    actions = (FakeIssued("refine_search", "a4", "tok4"),)
    assert block_of(render(actions=actions), "action_row") is None


# ── Terminale ───────────────────────────────────────────────────────────────────────────────
def test_failed_turn_carries_a_stable_code_and_localized_message():
    answer = ground_answer(plan(), bundle(), locale="ro")
    view = project(
        answer,
        identity=identity(status="failed", error_code="deadline_exceeded"),
        locale="ro",
        now=NOW,
    ).model_dump(mode="json", exclude_none=True)
    assert view["error"]["code"] == "deadline_exceeded"
    assert view["error"]["retryable"] is True
    assert view["error"]["message"].startswith("Răspunsul a durat")


def test_unknown_error_code_still_gets_human_copy():
    answer = ground_answer(plan(), bundle(), locale="ro")
    view = project(
        answer, identity=identity(status="failed", error_code="cod-inventat"), locale="ro", now=NOW
    ).model_dump(mode="json", exclude_none=True)
    assert view["error"]["code"] == "cod-inventat"
    assert view["error"]["message"]  # clientul nu vede niciodată doar un cod


def test_terminal_without_content_still_renders_a_notice():
    """P6 la nivel de projector: contractul REFUZĂ un terminal gol, deci îl facem imposibil de
    produs — nu îl lăsăm să pice validarea."""
    empty = GroundedAnswer(ok=True, locale="ro", business_id=BUSINESS_ID, as_of=NOW)
    view = project(empty, identity=identity(status="cancelled"), locale="ro", now=NOW)
    payload = view.model_dump(mode="json", exclude_none=True)
    assert payload["messages"][0]["blocks"][0]["type"] == "notice"


# ── Chrome / a11y ───────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("locale", ["ro", "en", "hu"])
def test_chrome_and_announcements_are_complete_in_every_locale(locale):
    view = render(locale=locale)
    assert set(view["chrome"]) == {
        "launcher_label",
        "dialog_title",
        "dialog_description",
        "close_label",
        "new_chat_label",
    }
    assert all(view["chrome"].values())
    assert set(view["a11y"]["announcements"]) == {
        "accepted",
        "working",
        "validating",
        "completed",
        "failed",
        "cancelled",
    }
    assert all(view["a11y"]["announcements"].values())
    assert all(view["composer"][k] for k in ("label", "placeholder", "send_label"))


def test_unknown_locale_falls_back_to_the_pilot_not_to_a_key_error():
    view = render(locale="de-DE")
    assert view["chrome"]["dialog_title"] == "Asistent de cumpărături"


def test_no_chrome_field_carries_markup_or_selectors():
    view = render()
    for value in list(view["chrome"].values()) + list(view["a11y"]["announcements"].values()):
        assert "<" not in value and "{" not in value and "javascript" not in value.lower()


# ── Contract ────────────────────────────────────────────────────────────────────────────────
def test_result_always_parses_as_web_view_v2():
    parse_view(render())


def test_project_plan_returns_none_when_grounding_rejects():
    view, answer = project_plan(
        plan(direct_answer="Costă 12 lei."),
        bundle(),
        identity=identity(),
        locale="ro",
        now=NOW,
    )
    assert view is None and "ungrounded_prose" in answer.failures


def test_project_plan_renders_when_grounding_passes():
    view, answer = project_plan(plan(), bundle(), identity=identity(), locale="ro", now=NOW)
    assert view is not None and answer.ok


# ── Comparație ──────────────────────────────────────────────────────────────────────────────
def test_comparison_unknown_cell_is_an_explicit_gap_not_a_zero():
    from src.agent.answer_plan import ComparisonCell, PlanComparison

    p = plan(
        selected_products=(
            SelectedProduct(product_id=PID_A, variant_id=None, evidence_ids=("e",)),
            SelectedProduct(product_id=PID_B, variant_id=None, evidence_ids=("e",)),
        ),
        recommendations=(),
        comparison=PlanComparison(
            product_ids=(PID_A, PID_B),
            axes=("volum",),
            cells=(ComparisonCell(product_id=PID_A, axis="volum", value="50 ml", evidence_id="e"),),
        ),
    )
    view = render(p, bundle([row(PID_A), row(PID_B, name="Ser B")]))
    comparison = block_of(view, "comparison")
    assert comparison["headers"] == ["Ser hidratant LumaDerm", "Ser B"]
    assert comparison["rows"][0]["cells"] == [{"text": "50 ml"}, {}]
