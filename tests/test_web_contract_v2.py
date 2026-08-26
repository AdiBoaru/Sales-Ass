"""NX-228 — teste pentru contractul `web-turn.v2` / `web-view.v2`.

Ce apără suita, în ordinea în care contează:
  1. terminalele nu pot fi goale (P6) — nici prin lipsă de mesaje, nici prin blocuri fără conținut;
  2. unionul e FINIT — tip necunoscut sau câmp extra respinge ÎNTREG payloadul;
  3. tot ce e afișabil e text deja localizat — niciun număr pe care browserul l-ar putea calcula;
  4. copy-ul de chrome/a11y e obligatoriu — FE nu compune microcopy;
  5. v1 rămâne neatins.

Pur: fără DB, fără LLM, fără rețea.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from src.evals.web_response import validate_web_payload, validate_web_view_v2
from src.web.contracts_v2 import (
    MAX_ACTION_LABEL_LEN,
    MAX_PRODUCT_ITEMS,
    MAX_TEXT_LEN,
    TERMINAL_STATUSES,
    VIEW_SCHEMA_VERSION,
    ActionView,
    Block,
    NavigateActivation,
    WebViewV2,
    negotiate_schema,
    parse_turn_request,
    parse_view,
    schema_hash,
    turn_json_schema,
    view_json_schema,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "web_v2"

# Hash-ul schema-ului curent, fixat ca SNAPSHOT. Nu e o formalitate: e moneda negocierii de
# capabilitate din NX-228. Dacă acest test pică, contractul s-a schimbat — actualizarea
# constantei e o decizie CONȘTIENTĂ care obligă la rollout cu schema negociat, nu un fix de
# copiat orbește.
# Actualizat o dată, conștient: chips-urile au devenit mesaje de client, nu etichete de două
# cuvinte, deci `MAX_ACTION_LABEL_LEN` 40 → 56 și `MAX_ACTIONS_PER_ROW` 4 → 5. Schimbarea e
# LĂRGIRE pură (un payload valid înainte rămâne valid) și cade pe un contract care n-a servit
# trafic încă (`WEB_TURN_V2_ENABLED` OFF, cutoverul e al NX-249) — deci nu există client căruia
# să-i negociem schema. Când v2 va fi live, o schimbare aici cere negociere, nu un hash nou.
EXPECTED_VIEW_SCHEMA_HASH = "0b0b2694c46a3c257bd34144a5b9cd1c9b2f5c9da9c2d13b6056b35c3cc77ede"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _valid_views() -> dict[str, dict]:
    return {k: v for k, v in _load("valid_views.json").items() if not k.startswith("_")}


def _shell(status: str = "completed", **over) -> dict:
    """Envelope minim VALID, ca fiecare test negativ să schimbe exact un lucru."""
    base = {
        "schema_version": "web-view.v2",
        "conversation": {"id": "c", "revision": 1},
        "turn": {
            "id": "t",
            "client_turn_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            "status": status,
        },
        "messages": [
            {"id": "m", "role": "assistant", "blocks": [{"id": "b", "type": "text", "text": "ok"}]}
        ],
        "composer": {
            "enabled": True,
            "label": "Mesaj",
            "placeholder": "Scrie…",
            "send_label": "Trimite",
        },
        "chrome": {
            "launcher_label": "L",
            "dialog_title": "T",
            "dialog_description": "D",
            "close_label": "C",
            "new_chat_label": "N",
        },
        "a11y": {
            "announcements": {
                "accepted": "a",
                "working": "w",
                "validating": "v",
                "completed": "c",
                "failed": "f",
                "cancelled": "x",
            }
        },
    }
    base.update(over)
    return base


# ── Fixturi ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(_valid_views()))
def test_valid_fixture_parses(name: str) -> None:
    view = parse_view(_valid_views()[name])
    assert view.schema_version == VIEW_SCHEMA_VERSION


@pytest.mark.parametrize("case", _load("invalid_views.json")["cases"], ids=lambda c: c["name"])
def test_invalid_fixture_is_rejected(case: dict) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_view(case["payload"])


@pytest.mark.parametrize("name", sorted(_load("requests.json")["valid"]))
def test_valid_request_parses(name: str) -> None:
    req = parse_turn_request(_load("requests.json")["valid"][name])
    assert req.client_turn_id is not None


@pytest.mark.parametrize("case", _load("requests.json")["invalid"], ids=lambda c: c["reason"][:40])
def test_invalid_request_is_rejected(case: dict) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_turn_request(case["payload"])


def test_every_block_type_is_covered_by_a_fixture() -> None:
    """O fixtură lipsă înseamnă un bloc pe care nimeni nu l-a văzut randat niciodată.

    Tipurile se citesc din UNION, nu din schema: în schema apar și discriminatoarele de
    activation (`navigate`/`submit`), care nu sunt blocuri.
    """
    declared = {
        get_args(member.model_fields["type"].annotation)[0]
        for member in get_args(get_args(Block)[0])
    }
    seen = {
        b["type"]
        for view in _valid_views().values()
        for m in view.get("messages", [])
        for b in m["blocks"]
    }
    assert declared - seen == set(), f"blocuri fără fixtură: {sorted(declared - seen)}"


# ── JSON Schema, hash, negociere ────────────────────────────────────────────────────────────
def test_schema_hash_is_stable() -> None:
    assert schema_hash() == EXPECTED_VIEW_SCHEMA_HASH, (
        "contractul s-a schimbat. Nu actualiza constanta reflex: un rollout aditiv cere "
        "capability/schema-hash negotiation înainte de trafic (NX-228)."
    )


def test_schema_is_deterministic() -> None:
    assert schema_hash() == schema_hash(view_json_schema())
    assert view_json_schema() == view_json_schema()


def test_both_schemas_are_self_consistent() -> None:
    Draft202012Validator.check_schema(view_json_schema())
    Draft202012Validator.check_schema(turn_json_schema())


@pytest.mark.parametrize("name", sorted(_valid_views()))
def test_fixture_validates_against_published_json_schema(name: str) -> None:
    """Fixturile trec ȘI modelul, ȘI schema publicată — altfel FE ar valida altceva decât noi."""
    Draft202012Validator(view_json_schema()).validate(_valid_views()[name])


def test_json_schema_is_necessary_but_not_sufficient() -> None:
    """Allowlistul de URL trăiește într-un `model_validator`, deci NU apare în JSON Schema.

    Consecință operațională, scrisă aici ca să nu fie descoperită în producție: un client care
    validează DOAR cu JSON Schema ar accepta `javascript:`. De aceea serverul validează
    întotdeauna prin Pydantic înainte de livrare, iar schema publicată e contract de FORMĂ, nu
    poarta de securitate.
    """
    payload = _shell(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [
                    {
                        "id": "b",
                        "type": "action_row",
                        "actions": [
                            {
                                "id": "a",
                                "label": "Click",
                                "activation": {
                                    "type": "navigate",
                                    "href": "javascript:alert(1)",
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    )
    Draft202012Validator(view_json_schema()).validate(payload)  # schema singură: trece
    with pytest.raises(ValidationError):  # modelul: nu trece
        parse_view(payload)


def test_negotiate_returns_current_when_client_is_silent() -> None:
    assert negotiate_schema(None) == schema_hash()
    assert negotiate_schema([]) == schema_hash()


def test_negotiate_accepts_matching_hash() -> None:
    assert negotiate_schema([schema_hash()]) == schema_hash()


def test_negotiate_refuses_unknown_capability() -> None:
    """Serverul nu livrează un schema pe care clientul nu l-a acceptat."""
    with pytest.raises(ValueError, match="niciun schema comun"):
        negotiate_schema(["deadbeef" * 8])


# ── Lifecycle ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["accepted", "working", "validating"])
def test_non_terminal_may_be_empty(status: str) -> None:
    view = parse_view(_shell(status=status, messages=[]))
    assert view.turn.status == status


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_terminal_requires_a_renderable_block(status: str) -> None:
    extra = {"error": {"code": "e", "message": "m"}} if status == "failed" else {}
    with pytest.raises(ValidationError, match="terminal"):
        parse_view(_shell(status=status, messages=[], **extra))


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_terminal_with_only_contentless_blocks_is_rejected(status: str) -> None:
    extra = {"error": {"code": "e", "message": "m"}} if status == "failed" else {}
    payload = _shell(
        status=status,
        messages=[{"id": "m", "role": "assistant", "blocks": [{"id": "b", "type": "divider"}]}],
        **extra,
    )
    with pytest.raises(ValidationError, match="terminal"):
        parse_view(payload)


def test_all_six_wire_statuses_are_accepted() -> None:
    seen = set()
    for status in ("accepted", "working", "validating", "completed", "cancelled"):
        seen.add(parse_view(_shell(status=status)).turn.status)
    seen.add(parse_view(_shell(status="failed", error={"code": "e", "message": "m"})).turn.status)
    assert seen == {"accepted", "working", "validating", "completed", "failed", "cancelled"}


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_view(_shell(status="thinking"))


# ── Chrome / a11y ───────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "field",
    ["launcher_label", "dialog_title", "dialog_description", "close_label", "new_chat_label"],
)
def test_missing_chrome_label_is_contract_error(field: str) -> None:
    payload = _shell()
    del payload["chrome"][field]
    with pytest.raises(ValidationError):
        parse_view(payload)


@pytest.mark.parametrize(
    "status", ["accepted", "working", "validating", "completed", "failed", "cancelled"]
)
def test_missing_announcement_is_contract_error(status: str) -> None:
    payload = _shell()
    del payload["a11y"]["announcements"][status]
    with pytest.raises(ValidationError):
        parse_view(payload)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_copy_is_rejected_like_missing(blank: str) -> None:
    payload = _shell()
    payload["chrome"]["close_label"] = blank
    with pytest.raises(ValidationError):
        parse_view(payload)


def test_composer_can_be_disabled_by_server() -> None:
    """Single-flight-ul e o decizie de server, nu un flag local (NX-243/245)."""
    payload = _shell(status="working", messages=[])
    payload["composer"]["enabled"] = False
    assert parse_view(payload).composer.enabled is False


# ── Adversarial ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "file:///etc/passwd",
        "vbscript:msgbox(1)",
        "blob:https://x.invalid/abc",
        "about:blank",
        "//evil.invalid/p",
        "http://shop.invalid/p",
        "java\nscript:alert(1)",
        "https://x.invalid/\\..\\p",
    ],
)
def test_forbidden_url_schemes(href: str) -> None:
    with pytest.raises(ValidationError):
        ActionView(id="a", label="Click", activation=NavigateActivation(type="navigate", href=href))


@pytest.mark.parametrize("href", ["https://shop.invalid/p", "/p/ser-x", "/"])
def test_allowed_url_schemes(href: str) -> None:
    action = ActionView(
        id="a", label="Click", activation=NavigateActivation(type="navigate", href=href)
    )
    assert action.activation.href == href


def test_html_in_text_stays_literal_text() -> None:
    """Contractul NU sanitizează prozа și nici nu pretinde că o face.

    Nu există niciun câmp care să însemne „randează ca HTML": `text` e text, iar escaparea e
    treaba rendererului. Testul fixează faptul că unghiularele NU sunt un canal de markup
    ascuns — ajung la FE ca șir literal, unde React le escapează.
    """
    payload = _shell(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [
                    {"id": "b", "type": "text", "text": "<script>alert(1)</script> costă 5 lei"}
                ],
            }
        ]
    )
    view = parse_view(payload)
    assert view.messages[0].blocks[0].text == "<script>alert(1)</script> costă 5 lei"


def test_no_field_carries_style_or_markup() -> None:
    """Niciun câmp din contract nu poate cara CSS/HTML/JS: nu există `className`, `style`,
    `html` sau `script` nicăieri în schema."""
    blob = json.dumps(view_json_schema()).lower()
    for banned in ('"classname"', '"style"', '"html"', '"script"', '"css"', '"dangerously'):
        assert banned not in blob, f"schema expune un câmp de markup: {banned}"


def test_action_cannot_be_both_navigate_and_submit() -> None:
    payload = _shell(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [
                    {
                        "id": "b",
                        "type": "action_row",
                        "actions": [
                            {
                                "id": "a",
                                "label": "Click",
                                "activation": {
                                    "type": "submit",
                                    "token": "opq",
                                    "href": "https://x.invalid/p",
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    )
    with pytest.raises(ValidationError):
        parse_view(payload)


# ── Limite ──────────────────────────────────────────────────────────────────────────────────
def test_text_over_limit_is_rejected() -> None:
    payload = _shell(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [{"id": "b", "type": "text", "text": "x" * (MAX_TEXT_LEN + 1)}],
            }
        ]
    )
    with pytest.raises(ValidationError):
        parse_view(payload)


def test_too_many_product_items_is_rejected() -> None:
    items = [{"view_id": f"pv{i}", "title": f"P{i}"} for i in range(MAX_PRODUCT_ITEMS + 1)]
    payload = _shell(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [{"id": "b", "type": "product_list", "items": items}],
            }
        ]
    )
    with pytest.raises(ValidationError):
        parse_view(payload)


def test_action_label_is_a_label_not_a_sentence() -> None:
    """Pragul din v1 (`_MAX_WEB_CHIP_LEN`) a existat pentru că nano genera întrebări lungi
    drept chips și rupeau widgetul. În v2 e o limită de tip, nu o tăiere tăcută la randare."""
    with pytest.raises(ValidationError):
        ActionView(
            id="a",
            label="x" * (MAX_ACTION_LABEL_LEN + 1),
            activation=NavigateActivation(type="navigate", href="/p"),
        )


# ── Round-trip ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(_valid_views()))
def test_round_trip_is_lossless(name: str) -> None:
    original = _valid_views()[name]
    view = parse_view(original)
    dumped = json.loads(view.model_dump_json(exclude_none=True))
    assert parse_view(dumped) == view


def test_models_are_frozen() -> None:
    """Un ViewModel se construiește o dată și se livrează. Mutarea lui după validare ar ocoli
    exact validatoarele care fac contractul să însemne ceva."""
    view = parse_view(_shell())
    with pytest.raises(ValidationError):
        view.turn = view.turn  # type: ignore[misc]


# ── Regresie v1 ─────────────────────────────────────────────────────────────────────────────
def test_v1_fixtures_still_pass_v1_checker() -> None:
    """V1 rămâne neatins până la cutoverul NX-249."""
    doc = json.loads(
        (Path(__file__).parent / "fixtures" / "web_response" / "payloads.json").read_text(
            encoding="utf-8"
        )
    )
    fixtures = doc["fixtures"]
    assert set(fixtures) == {
        "text_only",
        "products",
        "offer",
        "comparison",
        "no_match",
        "fallback_error",
        "rate_limit",
    }, "setul de fixturi v1 s-a schimbat — v1 trebuie să rămână neatins până la NX-249"
    for name, payload in fixtures.items():
        result = validate_web_payload(
            payload, allow_stock_claim=True, allow_delivery_claim=True, allow_empty=True
        )
        assert result.passed, f"fixtura v1 {name!r} nu mai trece checkerul v1: {result.failures}"


def test_v1_payload_is_not_a_v2_view() -> None:
    """V1 și v2 au randori și endpointuri separate; niciun alias care ghicește câmpuri."""
    v1 = {"content": "salut", "products": [], "suggestions": []}
    with pytest.raises((ValidationError, ValueError)):
        parse_view(v1)


def test_v2_view_is_not_a_v1_payload() -> None:
    view = _valid_views()["recommendation"]
    assert "content" not in view
    assert "suggestions" not in view
    assert WebViewV2.model_validate(view).schema_version == "web-view.v2"


# ── Checkerul de grounding v2 ───────────────────────────────────────────────────────────────
# Un checker care nu pică niciodată e teatru. Fiecare test de aici MUTĂ payloadul valid și cere
# ca verificarea să prindă mutația — nu se mulțumește cu „a trecut pe cazul bun".
_SOURCE = [
    {
        "product_id": "p1",
        "name": "Petala Rich Cremă hidratantă",
        "price": 89.0,
        "list_price": 109.0,
        "url": "https://shop.example.invalid/p/petala-rich",
    },
    {
        "product_id": "p2",
        "name": "Auralis Daily Cremă hidratantă",
        "price": 64.0,
        "url": "https://shop.example.invalid/p/auralis-daily",
    },
]
_INDEX = {"pv_1": "p1", "pv_2": "p2"}


def _grounded(view: dict):
    return validate_web_view_v2(view, source_products=_SOURCE, view_index=_INDEX)


def _mutated(**_unused) -> dict:
    return json.loads(json.dumps(_valid_views()["recommendation"]))


def test_grounding_passes_on_the_honest_view() -> None:
    result = _grounded(_valid_views()["recommendation"])
    assert result.passed, result.failures


def test_grounding_catches_invented_price() -> None:
    view = _mutated()
    view["messages"][0]["blocks"][1]["items"][0]["price"]["current"] = "79,00 lei"
    result = _grounded(view)
    assert not result.passed
    assert any("nu exist" in f for f in result.failures), result.failures


def test_grounding_catches_wrong_discount() -> None:
    """Exact regula pe care v1 o lăsa în browser: aici e o afirmație, deci se verifică."""
    view = _mutated()
    view["messages"][0]["blocks"][1]["items"][0]["price"]["discount"] = "-50%"
    result = _grounded(view)
    assert not result.passed
    assert any("discount" in f for f in result.failures), result.failures


def test_grounding_catches_fake_markdown() -> None:
    view = _mutated()
    price = view["messages"][0]["blocks"][1]["items"][0]["price"]
    price["previous"], price["current"] = "89,00 lei", "89,00 lei"
    price.pop("discount")
    result = _grounded(view)
    assert not result.passed
    assert any("nu e peste" in f for f in result.failures), result.failures


def test_grounding_catches_product_without_catalog_trace() -> None:
    view = _mutated()
    view["messages"][0]["blocks"][1]["items"][0]["view_id"] = "pv_ghost"
    result = _grounded(view)
    assert not result.passed
    assert any("view_index" in f for f in result.failures), result.failures


def test_grounding_catches_invented_link() -> None:
    view = _mutated()
    view["messages"][0]["blocks"][1]["items"][0]["actions"][0]["activation"]["href"] = (
        "https://shop.example.invalid/p/inventat"
    )
    result = _grounded(view)
    assert not result.passed
    assert any("catalog" in f for f in result.failures), result.failures


def test_grounding_catches_price_invented_in_prose() -> None:
    view = _mutated()
    view["messages"][0]["blocks"][0]["text"] = "Ți-o dau la 19 lei, ofertă specială."
    result = _grounded(view)
    assert not result.passed
    assert any("din text" in f for f in result.failures), result.failures


def test_grounding_allows_relative_links() -> None:
    """O rută relativă n-are host, deci nu poate scoate clientul din magazin."""
    view = _mutated()
    view["messages"][0]["blocks"][1]["items"][0]["actions"][0]["activation"] = {
        "type": "navigate",
        "href": "/p/petala-rich",
    }
    assert _grounded(view).passed


def test_grounding_flags_empty_terminal_before_persist() -> None:
    """Modelul apără livrarea; checkerul apără și un dict construit de mână înainte de persist."""
    view = _mutated()
    view["messages"] = []
    result = _grounded(view)
    assert not result.passed
    assert any("P6" in f for f in result.failures), result.failures


def test_grounding_without_source_does_not_invent_a_pass() -> None:
    """Fără sursă verificăm doar consistența internă — și tot prindem un discount fals."""
    view = _mutated()
    view["messages"][0]["blocks"][1]["items"][0]["price"]["discount"] = "-90%"
    result = validate_web_view_v2(view)
    assert not result.passed


def test_v1_checker_is_untouched_by_v2() -> None:
    """Cele două checkere sunt funcții separate; v2 nu schimbă verdictul lui v1."""
    v1_payload = {"content": "Salut!", "products": [], "suggestions": []}
    assert validate_web_payload(v1_payload).passed
