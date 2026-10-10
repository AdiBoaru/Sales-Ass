"""NX-403 — răspunsul agentului unic la nivelul iZi: poarta pe fapte, faptele din recenzii, sfatul
de sub carduri, instrucțiunile pe forma cererii.

Poarta: ce era corect și era respins pe 9-10 oct (SPF-ul din catalog, recenziile, «cea mai
ieftină», regula de retur, «200 g» scris „200 gr” în catalog, «lasă-l 1–2 minute») trece; ce e
inventat (o cifră absentă, SPF-ul altui produs pe card, popularitatea, o livrare fără sursă, o
afirmație medicală) e respins. Zero apeluri de model și zero DB."""

from __future__ import annotations

import json

import pytest

from src.assistant import gate as agate
from src.assistant.prompt import PROMPT_VERSION, instructions
from src.assistant.schemas import tool_schemas
from src.domain.pack import DomainPack
from tests.test_nx396_assistant import (  # noqa: F401 — `_catalog` e fixture autouse
    MENUS,
    ROWS,
    ScriptedLLM,
    _call,
    _catalog,
    _ctx,
    _run,
    _search,
)

ALIASES = {"%": "%", "spf": "spf", "ip": "spf", "ml": "ml", "g": "g", "gr": "g", "grame": "g"}

SPF50 = {
    "id": "a",
    "name": "Crema Solara A SPF 50+",
    "price": 135.0,
    "url": "https://shop.test/a",
    "availability": "in_stock",
    "rating": 4.98,
    "review_count": 125,
    "attributes": {"spf": 50, "volume_raw": "50 ml"},
}
SPF30 = {
    "id": "b",
    "name": "Fluid Colorat B SPF30",
    "price": 100.0,
    "url": "https://shop.test/b",
    "availability": "in_stock",
    "rating": 4.9,
    "review_count": 40,
    "attributes": {"spf": 30, "volume_raw": "200 gr"},
}
# Faptele reale ale turului au și rândul văzut de model în căutare (ratingul brut, `reviews`).
FACTS_A = (
    "name: Crema Solara A SPF 50+\nrating: 5.0/5 from 125 reviews\n"
    '{"spf": 50, "volume_raw": "50 ml"}\n{"handle": "P", "rating": 4.98, "reviews": 125}'
)
FACTS_B = (
    "name: Fluid Colorat B SPF30\nrating: 4.9/5 from 40 reviews\n"
    '{"spf": 30, "volume_raw": "200 gr"}'
)


def _answer(text, cards=(), advice=""):
    return {
        "text": text,
        "advice": advice,
        "cards": [{"handle": h, "reason": r} for h, r in cards],
        "suggestions": [],
        "comparison": None,
        "notes": "",
    }


def _check(answer, *, rows=(SPF50, SPF30), sources=(), facts=None):
    rows = list(rows)
    handles = {f"P{i}": r["id"] for i, r in enumerate(rows, 1)}
    names = {f"P{i}": r["name"] for i, r in enumerate(rows, 1)}
    by_id = {"a": FACTS_A, "b": FACTS_B}
    return agate.check_answer(
        answer,
        handles=handles,
        names=names,
        short_names=names,
        rows={r["id"]: r for r in rows},
        shown_sets=[],
        grounded=set(),
        sources=list(sources),
        order_found=False,
        max_cards=6,
        max_suggestions=5,
        notes_max=500,
        facts=facts if facts is not None else "\n".join([FACTS_A, FACTS_B, *sources]),
        units=ALIASES,
        product_facts={r["id"]: by_id.get(r["id"], "") for r in rows},
    ).rejected


# --- ce era corect și pica pe poarta veche -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # 6f80d5ea: SPF-ul e un fapt de catalog, nu „o cifră nefondată”
        "Pentru ten gras aș alege Crema Solara A, cu SPF 50+. Dacă preferi SPF 30, ai fluidul B.",
        # 8488dedc: recenziile sunt fapte (NX-279)
        "Crema Solara A are nota 4,98 din 125 de recenzii. Clienții spun că se absoarbe rapid.",
        # 450acf48 / 79344ceb: o judecată pe fapte, relativă la nevoia clientului
        "Cea mai ieftină dintre ele e fluidul B. Pentru hidratare, cea mai potrivită e A.",
        # b6113016: «200 g» când catalogul scrie „200 gr”, aceeași unitate
        "Fluidul B vine în 200 g.",
        # 1561353d: «lasă-l» nu e „litru”, iar o cifră mică fără unitate nu se judecă
        "Aplică-l pe lungimi, lasă-l 1–2 minute, apoi clătește. Folosește-l de 2 ori pe săptămână.",
        # forma iZi a unui „cum se folosește”: pașii numerotați
        "Pașii:\n1. Curățare\n2. Cremă\n3. SPF ca ultim pas dimineața",
    ],
)
def test_true_facts_and_judgements_pass(text):
    assert _check(_answer(text)) == []


def test_a_store_rule_paraphrased_with_its_numbers_passes():
    """10e13643: «30 de zile» redat din regula de retur, nu citat literal, pica pe `zile`."""
    rule = "Ai la dispoziție 30 de zile de la cumpărare pentru a returna produsul."
    assert _check(_answer("Da, ai 30 de zile de la cumpărare pentru retur."), sources=[rule]) == []


def test_medical_claim_is_judged_per_sentence():
    """68f9b60e: «3. Tratament: …» lângă numele „ABIB Acne Foam” se lega peste propoziții."""
    text = (
        "1. Curățare: ABIB Acne Foam Cleanser.\n2. Tratament: masca de pori, o dată pe săptămână."
    )
    assert "medical_claim" not in _check(_answer(text))
    assert "medical_claim" in _check(_answer("Serul acesta tratează acneea."))


def test_without_products_available_is_not_a_stock_claim():
    """e2208242: «din informațiile disponibile» după o trimitere la medic, fără niciun produs."""
    text = "Nu pot confirma din informațiile disponibile dacă ți se potrivește."
    assert _check(_answer(text), rows=()) == []
    oos = {**SPF50, "availability": "out_of_stock"}
    assert "stock_claim" in _check(_answer("Crema Solara A e disponibilă acum."), rows=[oos])


# --- ce e inventat rămâne respins ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Crema Solara A are SPF 70.", "ungrounded_number"),
        ("Conține 5% niacinamidă.", "ungrounded_number"),
        ("Vine în 75 ml.", "ungrounded_number"),
        ("Crema Solara A e best seller-ul magazinului.", "popularity_claim"),
        ("E cel mai vândut produs de protecție solară.", "popularity_claim"),
        ("Ai livrare gratuită dacă o comanzi azi.", "unsourced_claim"),
        ("Crema Solara A costă 99 lei.", "ungrounded_price"),
        ("Este sigură în sarcină.", "medical_claim"),
    ],
)
def test_invented_facts_are_rejected(text, reason):
    assert reason in _check(_answer(text))


def test_another_products_figure_on_a_card_is_rejected():
    """SPF 30 e un fapt al turului (al fluidului B), dar nu al cremei A, pe cardul căreia stă."""
    rejected = _check(_answer("Uite două.", [("P1", "Protecție SPF 30, lejeră.")]))
    assert rejected == ["card_number"]
    assert _check(_answer("Uite două.", [("P1", "Protecție SPF 50+, lejeră.")])) == []


def test_a_rule_number_is_grounded_only_by_a_rule_read_in_the_turn():
    assert _check(_answer("Livrarea e gratuită peste 199 lei.")) != []
    rule = "Livrarea este gratuită la comenzi peste 199 lei."
    assert _check(_answer("Livrarea e gratuită peste 199 lei."), sources=[rule]) == []


def test_the_advice_is_judged_like_the_text():
    assert "ungrounded_number" in _check(_answer("Uite.", advice="Alege una cu SPF 70."))
    assert _check(_answer("Uite.", advice="Pentru ten gras contează textura lejeră.")) == []


def test_a_suggestion_may_ask_about_reviews_but_not_claim_popularity():
    a = _answer("Uite.")
    a["suggestions"] = ["Ce spun recenziile?", "Care e cel mai vândut?", "Care e cea mai ieftină?"]
    rows = [SPF50, SPF30]
    checked = agate.check_answer(
        a,
        handles={"P1": "a", "P2": "b"},
        names={},
        short_names={},
        rows={r["id"]: r for r in rows},
        shown_sets=[],
        grounded=set(),
        sources=[],
        order_found=False,
        max_cards=6,
        max_suggestions=5,
        notes_max=500,
        facts=FACTS_A,
        units=ALIASES,
    )
    assert checked.kept_suggestions == ["Ce spun recenziile?", "Care e cea mai ieftină?"]


def test_unit_aliases_come_from_the_pack():
    from src.domain.constraints import build_units

    pack = DomainPack(vertical="ecommerce")
    pack_units = build_units(
        {"weight": {"canonical": "g", "default_op": "eq", "factors": {"g": 1, "gr": 1}}}
    )
    object.__setattr__(pack, "units", pack_units)
    assert agate.unit_aliases(pack) == {"%": "%", "g": "g", "gr": "g"}


# --- faptele pe care le vede agentul -------------------------------------------------------------


async def test_search_results_carry_reviews_and_key_ingredients_without_catalog_codes(_catalog):  # noqa: F811
    _catalog["search"] = [
        {
            **ROWS[0],
            "rating": 4.9,
            "review_count": 90,
            "top_pros": ["se absoarbe rapid", "hidratează bine", "textură ușoară", "miros plăcut"],
            "attributes": {
                "concerns": ["hydration"],
                "key_ingredients": ["pantenol", "ceai verde", "a", "b", "c", "d"],
                "mpn": "23595",
                "sku": "F55837",
                "price_per_unit_source": "222.70 lei/100ml",
            },
        }
    ]
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [_call("answer", _answer("Uite."), "c2")],
    )
    await _run(llm)
    out = [i for i in llm.inputs[1] if isinstance(i, dict) and i.get("call_id") == "c1"][-1]
    [p] = json.loads(out["output"])["products"]
    assert p["reviews"] == 90
    assert p["reviews_praise"] == ["se absoarbe rapid", "hidratează bine", "textură ușoară"]
    assert p["key_ingredients"] == ["pantenol", "ceai verde", "a", "b", "c"]
    assert p["attributes"] == {"concerns": ["hydration"]}


# --- sfatul de sub carduri -----------------------------------------------------------------------


async def test_the_advice_goes_under_the_cards():
    llm = ScriptedLLM(
        [_call("search_catalog", _search(), "c1")],
        [
            _call(
                "answer",
                _answer(
                    "Ți-am pus două creme lejere.\n\nUna mai bogată, una mai ieftină.",
                    [("P1", "Textură lejeră."), ("P2", "Mai ieftină.")],
                    advice="Pentru ten gras contează textura. Eu aș lua COSRX Crema A.",
                ),
                "c2",
            )
        ],
    )
    served, ctx = await _run(llm)
    rich = ctx.reply.rich
    assert served and rich.education == "Pentru ten gras contează textura. Eu aș lua COSRX Crema A."
    from src.worker.compose import flatten_framing

    framing = flatten_framing(rich, "ro")
    assert framing.index("Una mai bogată") < framing.index("Pentru ten gras contează")
    [ev] = [e.properties for e in ctx.events if e.type == "assistant_turn"]
    assert ev["served"] and ctx.trace["assistant"]["advice"] is True


async def test_without_cards_the_advice_follows_the_text():
    llm = ScriptedLLM([_call("answer", _answer("Se aplică seara.", advice="Fă un test întâi."))])
    served, ctx = await _run(llm, _ctx("cum se foloseste?"))
    assert served and ctx.reply.text == "Se aplică seara.\n\nFă un test întâi."


# --- contractul cu modelul -----------------------------------------------------------------------


def test_the_answer_schema_asks_for_the_advice_strictly():
    [answer] = [t for t in tool_schemas(MENUS) if t["name"] == "answer"]
    params = answer["parameters"]
    assert "advice" in params["required"] and params["properties"]["advice"]["type"] == "string"


def test_the_instructions_describe_the_answer_shapes_generically():
    text = instructions(store="X", locale="ro", families=(), max_shown=6, chip_count=5)
    assert PROMPT_VERSION == "assistant.v4"
    for needle in ("advice", "reviews_praise", "two short paragraphs", "numbered list"):
        assert needle in text
    # P11: instrucțiunile rămân generice, fără exemple de magazin sau de raft
    for word in ("ten gras", "SOLE", "crema", "SPF 50"):
        assert word not in text
