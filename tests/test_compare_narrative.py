"""Comparația NARATIVĂ + porțile ei (`src/agent/compare_narrative.py`).

Pur: fără DB, fără Redis, fără OpenAI. `build_comparison` produce tabelul determinist (plasa), un
LLM fals întoarce narativul, iar porțile decid ce ajunge la client. Invariantul apărat peste tot:
**codul deține cifrele, modelul deține citirea lor, iar fiecare celulă își numește sursa**. Ce nu
poate cita o sursă reală nu poate ajunge în tabel; ce cade, cade LOCAL (celulă → „—", axă goală →
dispare, lead respins → tabelul determinist), niciodată în tăcere (P6).
"""

import pytest

from src.agent.compare_narrative import assemble_axes, compose_comparison, lead_failures
from src.domain.pack import FacetSpec
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    TurnContext,
)
from src.worker.compose import build_comparison, product_fact_sheet

_CLEAN = "Sunt foarte asemănătoare. Crema A merge pe textură ușoară, Crema B pe hrănire bogată."
_FACETS = (FacetSpec(key="finish", labels={"ro": "Finisaj"}),)


def _products() -> list[dict]:
    return [
        {
            "id": "p1",
            "name": "Crema A",
            "brand": "BrandX",
            "price": 58.99,
            "availability": "in_stock",
            "rating": 4.8,
            "ai_summary": "Textură ușoară, se absoarbe repede.",
            "attributes": {"finish": "mat", "specs": {"Volum": "4 g"}},
            "top_pros": ["textură ușoară"],
            "top_cons": ["tub mic"],
        },
        {
            "id": "p2",
            "name": "Crema B",
            "brand": "BrandY",
            "price": 88.99,
            "availability": "in_stock",
            "rating": 4.2,
            "ai_summary": "Formulă bogată pentru ten uscat.",
            "attributes": {"finish": "satinat", "specs": {"Volum": "4 g"}},
            "top_pros": ["bogată"],
            "top_cons": ["se absoarbe greu"],
        },
    ]


def _ctx(body: str = "care e diferența dintre ele?") -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="demo", name="Demo", vertical="beauty"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
    )


def _cmp(products=None, facets=_FACETS):
    return build_comparison(products or _products(), "ro", facets)


def _axis(label, *cells):
    return {
        "label": label,
        "cells": [{"product_id": p, "source": s, "text": t} for p, s, t in cells],
    }


def _narrative(**over):
    payload = {
        "lead": _CLEAN,
        "subtitle": "Una e ușoară și rapidă, cealaltă bogată și hrănitoare.",
        "axes": [
            _axis(
                "Textură și senzație",
                ("p1", "avantaje", "Ușoară, se absoarbe repede."),
                ("p2", "avantaje", "Bogată, rămâne pe piele."),
            )
        ],
        "closing": ["Gândește-te întâi cât de repede vrei să se absoarbă."],
    }
    payload.update(over)
    return payload


class _FakeLLM:
    def __init__(self, payload=None, boom: Exception | None = None) -> None:
        self.payload = _narrative() if payload is None else payload
        self.boom = boom
        self.calls = 0
        self.last_user = ""

    async def complete_schema(self, system, user, schema, *, model=None):
        self.calls += 1
        self.last_user = user
        if self.boom is not None:
            raise self.boom
        return self.payload


# --- fișa de fapte: vocabularul de surse ---------------------------------------------------------


def test_fact_sheet_lists_only_sources_that_have_content():
    sheet = product_fact_sheet(_products()[0], _FACETS, "ro")
    assert set(sheet) == {"finish", "avantaje", "de_luat_in_calcul", "descriere", "specificatii"}
    assert sheet["finish"] == "mat" and sheet["specificatii"] == "Volum: 4 g"


def test_fact_sheet_omits_empty_sources():
    """O sursă absentă LIPSEȘTE din fișă, nu apare goală: altfel modelul ar putea „cita" un fapt
    care nu există, iar verificarea sursei ar deveni o formalitate."""
    bare = {"id": "x", "name": "X", "price": 1.0, "top_pros": [], "top_cons": []}
    assert product_fact_sheet(bare, _FACETS, "ro") == {}


# --- assemble_axes: sursa e garanția --------------------------------------------------------------


def test_axis_cells_land_in_column_order_not_model_order():
    """Ordinea AXELOR e a modelului (el a văzut perechea), ordinea COLOANELOR e a codului (cea
    cerută de client). Un model care emite celulele invers nu are voie să inverseze tabelul."""
    payload = _narrative(
        axes=[
            _axis(
                "Textură",
                ("p2", "avantaje", "Bogată."),
                ("p1", "avantaje", "Ușoară."),
            )
        ]
    )
    rows, _ = assemble_axes(payload, _cmp(), _products(), set(), _FACETS, "ro")
    assert rows[0].values == ["Ușoară.", "Bogată."]


def test_cell_citing_a_source_the_product_lacks_is_dropped():
    """Stratul 1: un model care vrea să inventeze o axă trebuie mai întâi să inventeze o sursă,
    iar sursele se verifică. Sancțiunea e locală — celula devine „—", axa rămâne."""
    payload = _narrative(
        axes=[
            _axis(
                "Rezistență",
                ("p1", "avantaje", "Rezistă bine."),
                ("p2", "rezistenta", "Rezistă mai puțin."),  # sursă inexistentă
            )
        ]
    )
    rows, rejected = assemble_axes(payload, _cmp(), _products(), set(), _FACETS, "ro")
    assert rows[0].values == ["Rezistă bine.", None]
    assert rejected["cell_unknown_source"] == 1


def test_cell_for_a_product_outside_the_comparison_is_dropped():
    payload = _narrative(
        axes=[_axis("Textură", ("p9", "avantaje", "Al altcuiva."), ("p1", "avantaje", "Ușoară."))]
    )
    rows, rejected = assemble_axes(payload, _cmp(), _products(), set(), _FACETS, "ro")
    assert rows[0].values == ["Ușoară.", None]
    assert rejected["cell_foreign_product"] == 1


def test_axis_with_no_surviving_cell_disappears_entirely():
    payload = _narrative(axes=[_axis("Rezistență", ("p1", "inventata", "x"), ("p2", "alta", "y"))])
    rows, rejected = assemble_axes(payload, _cmp(), _products(), set(), _FACETS, "ro")
    assert rows == [] and rejected["axis_empty"] == 1


def test_axis_where_both_columns_say_the_same_thing_is_dropped():
    """Aceeași regulă ca la tabelul determinist, aplicată pe ce compune modelul. Promptul o cere,
    dar un prompt e o rugăminte, nu o garanție — defectul a apărut la prima rulare a probei
    (`scripts/compare_drive.py`): un rând „Format: 4 g, 3 variante" identic pe ambele coloane."""
    payload = _narrative(
        axes=[
            _axis(
                "Formatul",
                ("p1", "specificatii", "4 g, format clasic."),
                ("p2", "specificatii", "4 g, format clasic."),
            )
        ]
    )
    from src.agent.compare_narrative import _allowed_numbers

    allowed = _allowed_numbers(_ctx(), _cmp(), _products(), _FACETS)
    rows, rejected = assemble_axes(payload, _cmp(), _products(), allowed, _FACETS, "ro")
    assert rows == [] and rejected["axis_not_discriminating"] == 1


def test_axis_with_one_known_and_one_unknown_cell_survives():
    """Parțial gol NU e „la fel": „știm despre unul, nu știm despre celălalt" e informație pe care
    clientul o poate cântări, iar frontendul o randează „—", adică necunoscut, nu absent."""
    payload = _narrative(axes=[_axis("Textură", ("p1", "avantaje", "Ușoară."))])
    rows, _ = assemble_axes(payload, _cmp(), _products(), set(), _FACETS, "ro")
    assert rows[0].values == ["Ușoară.", None]


def test_axis_label_that_smuggles_a_superlative_is_dropped():
    payload = _narrative(axes=[_axis("Cea mai bună textură", ("p1", "avantaje", "Ușoară."))])
    rows, rejected = assemble_axes(payload, _cmp(), _products(), set(), _FACETS, "ro")
    assert rows == [] and rejected["axis_label"] == 1


def test_cell_with_an_ungrounded_number_is_dropped_but_its_sibling_survives():
    payload = _narrative(
        axes=[
            _axis(
                "Textură",
                ("p1", "avantaje", "Ușoară."),
                ("p2", "avantaje", "Rezistă 12 ore."),  # cifra nu e în nicio fișă
            )
        ]
    )
    rows, rejected = assemble_axes(payload, _cmp(), _products(), set(), _FACETS, "ro")
    assert rows[0].values == ["Ușoară.", None]
    assert rejected["cell_ungrounded"] == 1


def test_numbers_that_exist_in_a_fact_sheet_are_allowed_in_cells():
    """«4 g» e o specificație reală a produsului, nu un preț: e grounded."""
    cmp = _cmp()
    from src.agent.compare_narrative import _allowed_numbers

    allowed = _allowed_numbers(_ctx(), cmp, _products(), _FACETS)
    payload = _narrative(axes=[_axis("Format", ("p1", "specificatii", "Tub mic, 4 g."))])
    rows, _ = assemble_axes(payload, cmp, _products(), allowed, _FACETS, "ro")
    assert rows[0].values == ["Tub mic, 4 g.", None]


def test_axes_are_capped():
    payload = _narrative(
        axes=[_axis(f"Axa {chr(97 + i)}", ("p1", "avantaje", "Ușoară.")) for i in range(10)]
    )
    rows, _ = assemble_axes(payload, _cmp(), _products(), set(), _FACETS, "ro")
    assert len(rows) == 6


# --- poarta de proză ------------------------------------------------------------------------------


def test_clean_lead_passes_the_gate():
    assert lead_failures(_CLEAN, _ctx(), _cmp(), _products(), _FACETS) == ()


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Crema A costă 58 lei, Crema B 88.", "ungrounded_number"),
        ("Crema A e cea mai bună alegere.", "unverifiable_superlative"),
        ("Crema B are 30% mai mult produs.", "ungrounded_percentage"),
        ("Ambele ajung mâine la tine.", "unsourced_delivery_claim"),
        ("Îți dau un cod de reducere pentru Crema A.", "unsourced_promo_claim"),
        ("Crema A vine cu garanție extinsă.", "unsourced_warranty_claim"),
        ("Crema B tratează dermatita.", "medical_claim"),
        ("", "empty_lead"),
    ],
)
def test_gate_rejects_what_the_facts_cannot_back(text, code):
    assert code in lead_failures(text, _ctx(), _cmp(), _products(), _FACETS)


def test_gate_rejects_a_lead_longer_than_the_sentences_it_promised():
    assert lead_failures("Da. " * 200, _ctx(), _cmp(), _products(), _FACETS) == ("lead_too_long",)


def test_price_digits_are_not_borrowable_by_the_prose():
    """Prețul e în tabel, pus de cod. Dacă am permite cifrele din celulele de preț, „44,99 lei" ar
    deschide și „44", iar un preț pe jumătate citat e o cifră greșită cu aparență de fapt."""
    assert "ungrounded_number" in lead_failures(
        "Crema A e la 58.", _ctx(), _cmp(), _products(), _FACETS
    )


def test_client_own_budget_number_is_allowed():
    ctx = _ctx("am buget 80 lei, care e diferența?")
    assert lead_failures("Amândouă intră în 80 lei.", ctx, _cmp(), _products(), _FACETS) == ()


def test_stock_claim_needs_a_stock_fact():
    unavailable = [{**p, "availability": "out_of_stock"} for p in _products()]
    text = "Amândouă sunt pe stoc acum."
    assert lead_failures(text, _ctx(), _cmp(), _products(), _FACETS) == ()
    assert "unsupported_stock_claim" in lead_failures(
        text, _ctx(), _cmp(unavailable), unavailable, _FACETS
    )


# --- integrarea: ce ajunge la client --------------------------------------------------------------


async def test_narrative_replaces_catalog_rows_and_code_keeps_the_numbers():
    ctx, base = _ctx(), _cmp()
    out = await compose_comparison(_FakeLLM(), ctx, base, _products(), facets=_FACETS)
    labels = [r.label for r in out.rows]
    # axa semantică a modelului, apoi cifrele codului — nu mai proiectăm coloane de catalog
    assert labels == ["Textură și senzație", "Preț", "Rating"]
    assert "Brand" not in labels and "Finisaj" not in labels
    assert out.rows[1].values == ["58,99 lei", "88,99 lei"]  # exacte, nu benzi calitative
    assert out.intro == _CLEAN and out.subtitle and out.closing
    evt = next(e for e in ctx.events if e.type == "comparison_narrative")
    assert evt.properties["source"] == "model"


async def test_rating_row_is_omitted_when_the_gap_is_immaterial():
    prods = _products()
    prods[0]["rating"], prods[1]["rating"] = 4.5, 4.6
    out = await compose_comparison(_FakeLLM(), _ctx(), _cmp(prods), prods, facets=_FACETS)
    assert [r.label for r in out.rows] == ["Textură și senzație", "Preț"]


async def test_model_cannot_smuggle_a_price_axis_next_to_the_real_one():
    """Prețul nu e o sursă din fișa de fapte, deci o axă de preț rămâne fără celule și dispare,
    iar rândul de preț rămâne unul singur, al codului, cu cifra exactă. Exemplul țintă scria
    „Foarte ieftin (sub 10 lei)"; noi avem cifra, iar o bandă calitativă ar fi un regres."""
    payload = _narrative(
        axes=[
            _axis(
                "Textură și senzație",
                ("p1", "avantaje", "Ușoară, se absoarbe repede."),
                ("p2", "avantaje", "Bogată, rămâne pe piele."),
            ),
            _axis(
                "Preț aproximativ", ("p1", "pret", "Foarte ieftin."), ("p2", "pret", "Mai scump.")
            ),
        ]
    )
    out = await compose_comparison(_FakeLLM(payload), _ctx(), _cmp(), _products(), facets=_FACETS)
    assert [r.label for r in out.rows] == ["Textură și senzație", "Preț", "Rating"]
    assert out.rows[1].values == ["58,99 lei", "88,99 lei"]


async def test_a_narrative_that_is_only_a_price_axis_falls_back():
    """Dacă tot ce a compus modelul dispare la porți, un tabel cu doar preț și rating ar fi mai
    sărac decât cel determinist. Cădem pe el, nu livrăm rămășița."""
    payload = _narrative(axes=[_axis("Preț", ("p1", "pret", "Ieftin."), ("p2", "pret", "Scump."))])
    base = _cmp()
    out = await compose_comparison(_FakeLLM(payload), _ctx(), base, _products(), facets=_FACETS)
    assert out is base


async def test_rejected_lead_keeps_the_deterministic_table_whole():
    ctx, base = _ctx(), _cmp()
    llm = _FakeLLM(_narrative(lead="Crema A e cea mai bună și costă 58 lei."))
    out = await compose_comparison(llm, ctx, base, _products(), facets=_FACETS)
    assert out is base  # neatinsă: tabelul determinist, cu leadul lui
    evt = next(e for e in ctx.events if e.type == "comparison_narrative")
    assert "unverifiable_superlative" in evt.properties["reasons"]


async def test_no_surviving_axis_falls_back_instead_of_shipping_a_price_only_table():
    ctx, base = _ctx(), _cmp()
    llm = _FakeLLM(_narrative(axes=[_axis("Rezistență", ("p1", "inventata", "x"))]))
    out = await compose_comparison(llm, ctx, base, _products(), facets=_FACETS)
    assert out is base
    evt = next(e for e in ctx.events if e.type == "comparison_narrative")
    assert evt.properties["reasons"] == ["no_axis_survived"]


async def test_a_bad_closing_paragraph_falls_alone():
    """Sancțiune LOCALĂ: un paragraf de îndrumare care strecoară o cifră cade singur, restul
    răspunsului se livrează. Altfel un adjectiv nefericit ar arunca un tabel corect."""
    ctx = _ctx()
    llm = _FakeLLM(
        _narrative(closing=["Alege după textură.", "Ține 12 ore pe piele."]),
    )
    out = await compose_comparison(llm, ctx, _cmp(), _products(), facets=_FACETS)
    assert out.closing == ["Alege după textură."]
    assert [r.label for r in out.rows][0] == "Textură și senzație"


async def test_a_bad_subtitle_falls_alone():
    llm = _FakeLLM(_narrative(subtitle="Cea mai bună dintre toate."))
    out = await compose_comparison(llm, _ctx(), _cmp(), _products(), facets=_FACETS)
    assert out.subtitle is None and out.intro == _CLEAN


async def test_prose_is_naturalized_before_it_reaches_the_frontend():
    """`set_comparison_reply` naturalizează floor-ul aplatizat, dar `intro`/`subtitle`/`closing`
    pleacă spre frontend pe câmpurile lor (principiul 13)."""
    llm = _FakeLLM(
        _narrative(
            lead="Crema A e ușoară — Crema B e bogată.",
            closing=["Alege ușoara — sau bogata."],
        )
    )
    out = await compose_comparison(llm, _ctx(), _cmp(), _products(), facets=_FACETS)
    assert "—" not in (out.intro or "") and "—" not in out.closing[0]


async def test_llm_failure_never_breaks_the_comparison_turn():
    ctx, base = _ctx(), _cmp()
    out = await compose_comparison(
        _FakeLLM(boom=RuntimeError("timeout")), ctx, base, _products(), facets=_FACETS
    )
    assert out is base
    assert next(e for e in ctx.events if e.type == "comparison_narrative").properties["source"] == (
        "deterministic"
    )


async def test_kill_switch_spends_no_model_call(monkeypatch):
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("COMPARISON_NARRATIVE_ENABLED", "false")
    try:
        base = _cmp()
        llm = _FakeLLM()
        assert await compose_comparison(llm, _ctx(), base, _products(), facets=_FACETS) is base
        assert llm.calls == 0
    finally:
        get_settings.cache_clear()


async def test_the_model_sees_the_fact_sheets_it_must_cite():
    llm = _FakeLLM()
    await compose_comparison(
        llm, _ctx(), _cmp(), _products(), facets=_FACETS, query="care e diferența?"
    )
    assert "[p1] Crema A" in llm.last_user and "avantaje: textură ușoară" in llm.last_user
    assert "Surse disponibile în acest tur:" in llm.last_user
    assert "care e diferența?" in llm.last_user
