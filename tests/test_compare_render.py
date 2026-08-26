"""IZI-compare — tabel comparativ structurat (P0: „Compară primele două" nu mai re-listează).

Pur, fără DB/Redis: `build_comparison` (din dict-uri de produs ca get_products_by_ids) →
`Comparison` (coloane + rânduri, fapte din date, ZERO proză LLM) → `render_web` (contract
frontend cu cheia `comparison`). Acoperă: construcția deterministă, anti-halucinație (celule doar
din date), anchor preț redus, floor aplatizat, roundtrip asdict (ruta async) și rutarea pe
capability (web = tabel; canale text = floor).
"""

import json
from dataclasses import asdict

from src.agent.deterministic import _CHEAPER_RE, _DETAIL_RE
from src.agent.fallbacks import _compare_chips
from src.channels.base import Capability
from src.channels.web.render import _web_chips, render_web, reply_from_outbox
from src.channels.web.sender import WebSender
from src.domain.pack import FacetSpec
from src.models import MAX_CHIP_LEN, Reply
from src.worker.compose import (
    build_comparison,
    comparison_cards,
    comparison_facts_block,
    comparison_wire,
    facet_summary,
    flatten_comparison,
)
from src.worker.dispatcher import _requested_render, choose_render


def _products() -> list[dict]:
    """Două produse ca din get_products_by_ids (ordine = cea cerută, păstrată de array_position).

    Ratingurile sunt deliberat DEPĂRTATE (4,8 față de 4,2): peste pragul de materialitate, deci
    rândul de rating rămâne în tabel și fixtura exersează forma completă. Cazul apropiat (zgomot de
    eșantion prezentat ca ierarhie) are testul lui, mai jos."""
    return [
        {
            "id": "p1",
            "name": "Crema A",
            "brand": "BrandX",
            "price": 58.99,
            "url": "https://shop/p1",
            "image": "https://cdn/p1.jpg",
            "availability": "in_stock",
            "rating": 4.8,
            "top_pros": ["hidratează intens", "fără parfum", "textură ușoară"],
            "top_cons": ["tub mic"],
        },
        {
            "id": "p2",
            "name": "Crema B",
            "brand": "BrandY",
            "price": 88.99,
            "url": "https://shop/p2",
            "image": "https://cdn/p2.jpg",
            "availability": "low_stock",
            "rating": 4.2,
            "top_pros": ["bogată", "pentru ten foarte uscat"],
            "top_cons": [],
        },
    ]


# --- build_comparison: determinist, fapte din date ---------------------------


def test_build_comparison_columns_and_rows():
    cmp = build_comparison(_products(), "ro")
    assert cmp is not None
    assert [c.product_id for c in cmp.columns] == ["p1", "p2"]  # ordine păstrată
    labels = {r.label for r in cmp.rows}
    assert {"Preț", "Rating", "Disponibilitate", "Avantaje", "Brand"} <= labels
    price_row = next(r for r in cmp.rows if r.label == "Preț")
    assert price_row.values == ["58,99 lei", "88,99 lei"]  # fapte din date, nu proză; format ro
    avail_row = next(r for r in cmp.rows if r.label == "Disponibilitate")
    assert avail_row.values == ["În stoc", "Stoc limitat"]


def test_build_comparison_drops_all_empty_row():
    # p1 are 1 con, p2 are 0 → rândul „De luat în calcul" rămâne (o celulă non-goală).
    # Dar dacă AMÂNDOUĂ ar fi goale, rândul dispare complet (vezi mai jos).
    prods = _products()
    prods[0]["top_cons"] = []  # acum AMBELE fără minusuri
    cmp = build_comparison(prods, "ro")
    assert cmp is not None
    assert all(r.label != "De luat în calcul" for r in cmp.rows)  # rând complet gol → sărit


def test_build_comparison_lead_has_data_verdict():
    cmp = build_comparison(_products(), "ro")
    # lead determinist: cel mai ieftin (Crema A) + cel mai bine cotat (Crema A) — derivat din date
    assert "Crema A" in (cmp.intro or "")
    assert "diferențele principale" in (cmp.intro or "")


def test_build_comparison_needs_two_valid():
    assert build_comparison(_products()[:1], "ro") is None  # un singur produs
    assert build_comparison([{"id": "x"}, {"id": "y"}], "ro") is None  # fără name/price


def test_build_comparison_list_price_anchor():
    prods = _products()
    prods[0]["list_price"] = 79.99  # preț de listă > curent (58.99) → anchor reducere
    cmp = build_comparison(prods, "ro")
    col = cmp.columns[0]
    # convenție unică: `price` = CURENT (58.99), `list_price` = ORIGINAL tăiat (79.99)
    assert col.price == 58.99 and col.list_price == 79.99


# --- FOCUS: tabelul ține doar ce departajează, în ordinea greutății de decizie -------------------


def _finish_facet() -> FacetSpec:
    return FacetSpec(key="finish", labels={"ro": "Finisaj"})


def test_identical_row_leaves_the_table_and_becomes_common_ground():
    """Defectul raportat: „Finisaj: mat / mat" și „Disponibilitate: În stoc / În stoc" ocupau exact
    locul în care clientul caută diferența. Un rând identic pe toate coloanele nu departajează, deci
    iese din tabel și se spune o dată, în lead."""
    prods = _products()
    for p in prods:
        p["availability"] = "in_stock"
        p["attributes"] = {"finish": "mat"}
    cmp = build_comparison(prods, "ro", [_finish_facet()])
    labels = [r.label for r in cmp.rows]
    assert "Finisaj" not in labels and "Disponibilitate" not in labels
    assert {r.label for r in cmp.common} == {"Finisaj", "Disponibilitate"}
    assert "La amândouă la fel: mat, În stoc." in (cmp.intro or "")


def test_common_ground_is_generic_not_a_denylist_of_boring_rows():
    """Disponibilitatea nu e tăiată pentru că e „plictisitoare" — rămâne exact când desparte."""
    prods = _products()  # in_stock față de low_stock
    cmp = build_comparison(prods, "ro")
    assert any(r.label == "Disponibilitate" for r in cmp.rows)
    assert all(r.label != "Disponibilitate" for r in cmp.common)


def test_row_order_follows_decision_weight_not_construction_order():
    prods = _products()
    prods[0]["attributes"] = {"finish": "mat"}
    prods[1]["attributes"] = {"finish": "satinat"}
    cmp = build_comparison(prods, "ro", [_finish_facet()])
    assert [r.label for r in cmp.rows] == [
        "Preț",  # întrebarea de sub orice comparație
        "Finisaj",  # substanța produsului
        "Avantaje",  # vocea celorlalți clienți
        "De luat în calcul",
        "Disponibilitate",
        "Rating",  # deja pe cardul din antet
        "Brand",  # deja în numele produsului
    ]


def test_price_row_survives_even_when_the_two_prices_are_equal():
    """«Costă la fel» e un răspuns, nu un rând gol: prețul e singura excepție de la regula
    departajării."""
    prods = _products()
    prods[1]["price"] = prods[0]["price"]
    cmp = build_comparison(prods, "ro")
    assert cmp.rows[0].label == "Preț" and cmp.rows[0].values == ["58,99 lei", "58,99 lei"]
    assert all("accesibil" not in n for n in cmp.notes)  # prețuri egale → niciun verdict


def test_rating_gap_exactly_at_the_threshold_stays_in_the_table():
    """Regresie: `4.6 - 4.3` dă 0,29999999999999982 în virgulă mobilă, deci diferența de la LIMITĂ
    (cea mai frecventă într-un catalog unde totul e între 4,0 și 4,9) era declarată zgomot și
    rândul dispărea. Prins de golden-ul nx172-conv-compare-diffs."""
    prods = _products()
    prods[0]["rating"], prods[1]["rating"] = 4.6, 4.3
    cmp = build_comparison(prods, "ro")
    assert any(r.label == "Rating" for r in cmp.rows)
    assert "Cea mai bine cotată: Crema A." in cmp.notes


def test_immaterial_rating_gap_drops_the_row_and_says_it_honestly():
    """4,5 față de 4,6 nu e o ierarhie, e zgomot de eșantion. Rândul dispare, iar leadul spune că
    sunt cotate la fel, în loc să sugereze un câștigător."""
    prods = _products()
    prods[0]["rating"], prods[1]["rating"] = 4.5, 4.6
    cmp = build_comparison(prods, "ro")
    assert all(r.label != "Rating" for r in cmp.rows)
    assert "Sunt cotate la fel." in cmp.notes
    assert all("bine cotată" not in n for n in cmp.notes)


def test_immaterial_price_gap_reports_closeness_instead_of_a_verdict():
    """Cazul din conversația reală: 42,99 față de 44,99. „Cea mai accesibilă" peste 2 lei sună a
    recomandare și nu e una."""
    prods = _products()
    prods[0]["price"], prods[1]["price"] = 42.99, 44.99
    cmp = build_comparison(prods, "ro")
    assert "Diferența de preț e mică, 2,00 lei." in cmp.notes
    assert all("Cea mai accesibilă" not in n for n in cmp.notes)


def test_material_price_gap_still_names_the_cheaper_one():
    cmp = build_comparison(_products(), "ro")  # 58,99 față de 88,99 → peste prag
    assert "Cea mai accesibilă: Crema A." in cmp.notes


def test_focus_kill_switch_restores_the_previous_table_and_lead(monkeypatch):
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("COMPARISON_FOCUS_ENABLED", "false")
    try:
        prods = _products()
        for p in prods:
            p["availability"] = "in_stock"
        prods[0]["rating"], prods[1]["rating"] = 4.5, 4.6
        cmp = build_comparison(prods, "ro")
        labels = [r.label for r in cmp.rows]
        # rândul identic rămâne, rândul de rating rămâne, ordinea e cea de construcție
        assert labels[:3] == ["Preț", "Rating", "Disponibilitate"]
        assert cmp.common == [] and cmp.notes == []
        # verdict la ORICE diferență, ca înainte de praguri
        assert "Cea mai bine cotată: Crema B." in (cmp.intro or "")
    finally:
        get_settings.cache_clear()


def test_comparison_facts_block_carries_differences_common_and_notes():
    """Bundle-ul modelului = exact ce se randează sub textul lui. Un fapt care n-a intrat în tabel
    n-are voie să legitimeze o frază din lead."""
    prods = _products()
    for p in prods:
        p["attributes"] = {"finish": "mat"}
    cmp = build_comparison(prods, "ro", [_finish_facet()])
    block = comparison_facts_block(cmp, "ro")
    assert "Produse comparate, în ordinea coloanelor: Crema A | Crema B" in block
    assert "- Preț: 58,99 lei | 88,99 lei" in block
    assert "La amândouă la fel: Finisaj: mat" in block
    assert "Observații derivate din date: Cea mai accesibilă: Crema A." in block
    # celula lipsă e declarată necunoscută, nu omisă tăcut (p2 n-are minusuri)
    assert "| necunoscut" in block


# --- Tier 2: fațete de DOMENIU în tabel (din products.attributes, generic DomainPack) ----------


def _concerns_facet() -> FacetSpec:
    return FacetSpec(
        key="concerns",
        labels={"ro": "Potrivit pentru", "en": "Suitable for"},
        value_labels={"oily": {"ro": "ten gras", "en": "oily skin"}, "dry": {"ro": "ten uscat"}},
    )


def test_build_comparison_facet_row_from_attributes():
    prods = _products()
    prods[0]["attributes"] = {"concerns": ["oily", "dry"]}
    prods[1]["attributes"] = {"concerns": ["dry"]}
    cmp = build_comparison(prods, "ro", [_concerns_facet()])
    row = next(r for r in cmp.rows if r.label == "Potrivit pentru")
    assert row.values == ["ten gras, ten uscat", "ten uscat"]  # listă → etichete unite, per-locale
    labels = [r.label for r in cmp.rows]
    # Substanța produsului (fațeta de domeniu) urcă imediat după preț, înaintea disponibilității.
    assert labels.index("Potrivit pentru") < labels.index("Disponibilitate")


def test_build_comparison_facet_raw_value_and_en_label():
    prods = _products()
    f = FacetSpec(key="finish", labels={"ro": "Finisaj", "en": "Finish"})  # fără value_labels
    prods[0]["attributes"] = {"finish": "mat"}
    prods[1]["attributes"] = {"finish": "satinat"}
    cmp = build_comparison(prods, "en", [f])
    row = next(r for r in cmp.rows if r.label == "Finish")  # eticheta EN
    assert row.values == ["mat", "satinat"]  # display-ready → valoarea ca atare


def test_build_comparison_facet_all_empty_dropped_partial_dash():
    prods = _products()
    prods[0]["attributes"] = {"concerns": ["oily"]}
    prods[1]["attributes"] = {}  # lipsă pe coloana a doua
    spf = FacetSpec(key="spf", labels={"ro": "SPF"})  # niciun produs n-are spf
    cmp = build_comparison(prods, "ro", [_concerns_facet(), spf])
    assert all(r.label != "SPF" for r in cmp.rows)  # TOT-gol → rând sărit
    row = next(r for r in cmp.rows if r.label == "Potrivit pentru")
    assert row.values == ["ten gras", None]  # parțial gol → None („—" pe frontend)


def test_build_comparison_no_facets_unchanged():
    cmp = build_comparison(_products(), "ro")  # facets implicit () → tabel ca azi
    assert all(r.label not in ("Potrivit pentru", "Finisaj", "SPF") for r in cmp.rows)


# --- Tier 2b: facet_summary (fațete în bundle-ul rich = input pentru model) ---------------------


def test_facet_summary_compact_grounded_and_localized():
    facets = [
        FacetSpec(key="key_ingredients", labels={"ro": "Ingrediente cheie"}),
        FacetSpec(key="spf", labels={"ro": "SPF"}),  # lipsă pe produs → sărit
        FacetSpec(
            key="concerns",
            labels={"ro": "Potrivit pentru"},
            value_labels={"oily": {"ro": "ten gras"}},
        ),
    ]
    prod = {
        "attributes": {"key_ingredients": ["acid hialuronic"], "concerns": ["oily", "ten uscat"]}
    }
    # „oily" → value_label „ten gras"; „ten uscat" fără mapare → ca atare; SPF absent → omis
    assert (
        facet_summary(prod, facets, "ro")
        == "Ingrediente cheie: acid hialuronic; Potrivit pentru: ten gras, ten uscat"
    )


def test_facet_summary_empty_on_sparse_data():
    facets = [FacetSpec(key="key_ingredients", labels={"ro": "Ingrediente cheie"})]
    assert facet_summary({}, facets, "ro") == ""  # fără attributes
    assert facet_summary({"attributes": {}}, facets, "ro") == ""  # attributes gol


# --- flatten_comparison: floor pt canale fără tabel --------------------------


def test_flatten_comparison_floor_text():
    cmp = build_comparison(_products(), "ro")
    floor = flatten_comparison(cmp, "ro")
    assert "Crema A" in floor and "Crema B" in floor
    assert "Preț: 58,99 lei · 88,99 lei" in floor
    # celulă lipsă (p2 fără minusuri, p1 cu „tub mic") → randată „—" pe partea goală
    cons_row = next(r for r in cmp.rows if r.label == "De luat în calcul")
    assert cons_row.values == ["tub mic", None]
    assert "tub mic · —" in floor


# --- render_web: contractul frontend cu cheia `comparison` -------------------


def _comparison_reply() -> Reply:
    cmp = build_comparison(_products(), "ro")
    return Reply(
        text=flatten_comparison(cmp, "ro"),
        products=comparison_cards(cmp),
        comparison=cmp,
        suggestions=["Adaugă Crema A", "Ceva mai ieftin"],
        cacheable=False,
    )


class _FakeRedis:
    """Redis fake minimal: captează publish (SSE) + pipeline no-op pt backlog."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, message):
        self.published.append((channel, message))
        return 1

    def pipeline(self, transaction: bool = True):
        return _FakePipe()


class _FakePipe:
    def rpush(self, *a):
        return self

    def ltrim(self, *a):
        return self

    def expire(self, *a):
        return self

    async def execute(self):
        return [1, 1, 1]


def test_render_web_comparison_shape():
    out = render_web(_comparison_reply(), "ro")
    assert "comparison" in out
    assert [c["product_id"] for c in out["comparison"]["columns"]] == ["p1", "p2"]
    assert any(r["label"] == "Preț" for r in out["comparison"]["rows"])
    # cardurile produselor comparate (header poză+preț) + lead în content
    assert len(out["products"]) == 2 and out["products"][0]["image_url"] == "https://cdn/p1.jpg"
    assert "diferențele principale" in out["content"]
    assert out["suggestions"] == ["Adaugă Crema A", "Ceva mai ieftin"]


def test_render_web_comparison_roundtrip_async():
    # ruta async: comparison_wire → outbox → reply_from_outbox → render_web = shape ca sync
    rep = _comparison_reply()
    sync = render_web(rep, "ro")
    payload = {
        "comparison": comparison_wire(rep.comparison),
        "products": rep.products,
        "text": rep.text,
    }
    rebuilt = render_web(reply_from_outbox(payload), "ro")
    assert rebuilt["comparison"] == sync["comparison"]
    assert rebuilt["products"] == sync["products"]


def test_wire_payload_omits_the_prose_inputs_of_the_lead():
    """`common` și `notes` alimentează leadul, nu randarea. Ruta sincronă nu le trimitea oricum
    (`_comparison_payload` alege explicit câmpurile), dar ruta async serializează cu `asdict`, deci
    ar fi plecat spre browser DOAR acolo. În plus, `notes` e deja în `intro`: un renderer care le-ar
    afișa ar repeta verdictul de două ori."""
    prods = _products()
    for p in prods:
        p["availability"] = "in_stock"
    cmp = build_comparison(prods, "ro")
    assert cmp.common and cmp.notes  # chiar avem ce scurge
    wire = comparison_wire(cmp)
    # `subtitle`/`closing` SUNT contract (frontendul le randează); `common`/`notes` nu.
    assert set(wire) == {"columns", "rows", "intro", "subtitle", "closing"}
    assert set(asdict(cmp)) - set(wire) == {"common", "notes"}  # prinde un câmp intern NOU


async def test_send_rich_publishes_comparison_event():
    r = _FakeRedis()
    s = WebSender(r)
    rep = _comparison_reply()
    payload = {
        "to": "v1",
        "comparison": comparison_wire(rep.comparison),
        "products": rep.products,
        "text": rep.text,
        "language": "ro",
    }
    await s.send_rich("tok", "v1", payload)
    evt = json.loads(r.published[0][1])
    assert evt["type"] == "rich" and "comparison" in evt
    assert len(evt["comparison"]["columns"]) == 2


# --- dispatcher: tabel pe web (COMPARISON), floor pe canale text -------------


def test_choose_render_comparison_web_vs_text():
    payload = {"comparison": {"columns": [], "rows": []}}
    web = frozenset({Capability.TEXT, Capability.RICH, Capability.CARDS, Capability.COMPARISON})
    wa = frozenset({Capability.TEXT})  # WhatsApp: fără COMPARISON → floor text
    assert choose_render(payload, "text", web) == "rich"  # web randează tabelul (send_rich)
    assert choose_render(payload, "text", wa) == "text"  # WhatsApp → floor aplatizat
    assert _requested_render(payload, "text") == "rich"  # comparația se CERE ca rich (degradare)


# --- end-to-end: agent_stage construiește comparația (fix P0 „Compară primele două") ---


async def test_agent_stage_builds_comparison_not_recommendation(monkeypatch):
    """Calea MODEL-DRIVEN: când mesajul NU declanșează gate-ul determinist de comparație (G2), dar
    modelul DECIDE să cheme compare_products (rezolvă „astea două" din displayed) → turul devine o
    COMPARAȚIE structurată, NU o re-recomandare (bug-ul iZi). reply.comparison setat. (Calea
    deterministă „compară primele două" e acoperită în test_agent.py.)"""
    from src.models import (
        BusinessConfig,
        Contact,
        ConversationState,
        InboundMessage,
        ProductRef,
        Route,
        RouteDecision,
        TurnContext,
    )
    from src.tools import catalog_tools
    from src.worker.runner import PipelineDeps
    from src.worker.stages import agent as agent_mod
    from src.worker.stages.agent import agent_stage

    async def _cats(conn, bid):
        return ["Creme"]

    async def _aliases(conn, bid, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)

    prods = _products()

    async def _by_ids(conn, bid, ids, *, limit=6):
        order = {pid: i for i, pid in enumerate(ids)}
        return sorted([p for p in prods if p["id"] in ids], key=lambda p: order[p["id"]])[:limit]

    monkeypatch.setattr(catalog_tools, "get_products_by_ids", _by_ids)

    class FakeLLM:
        async def embed(self, texts, *, model=None):
            return [[0.0] * 8 for _ in texts]

        async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
            await execute("compare_products", {"product_ids": ["p1", "p2"]})
            return "Crema A e mai ușoară, Crema B mai bogată."

    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        # frazare care NU declanșează _COMPARE_RE (gate determinist) → exersează calea model-driven.
        message=InboundMessage(provider_msg_id="m", body="care din astea două mi se potrivește?"),
        conversation_id="conv",
        state=ConversationState(
            displayed_products=[
                ProductRef(product_id="p1", name="Crema A", price=58.99),
                ProductRef(product_id="p2", name="Crema B", price=88.99),
            ]
        ),
    )
    ctx.route = RouteDecision(route=Route.SALES)
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=FakeLLM()))

    assert ctx.reply is not None and ctx.reply.comparison is not None
    assert [c.product_id for c in ctx.reply.comparison.columns] == ["p1", "p2"]  # ordine păstrată
    assert ctx.reply.cacheable is False  # relativ la setul afișat
    assert any("Adaugă" in s for s in ctx.reply.suggestions)  # chips deterministe
    assert any(e.type == "agent_compared" for e in ctx.events)


# --- Follow-up-urile de după tabel: mesaje de client, dar încă rutabile ------------------------


def test_compare_chips_are_client_messages_that_still_route():
    """Chips-urile de după o comparație au devenit mesaje complete („Adaugă Crema A în coș") în loc
    de etichete („Adaugă Crema A"). Naturalețea nu are voie să coste rutarea: la tap textul se
    întoarce în pipeline și trebuie să cadă tot pe intenția deterministă, altfel un buton care
    „sună mai bine" ajunge la bucla LLM și răspunde altceva."""
    cmp_ = build_comparison(_products(), "ro", ())
    assert cmp_ is not None
    chips = _compare_chips(cmp_.columns, "ro")

    assert chips == [
        "Adaugă Crema A în coș",
        "Adaugă Crema B în coș",
        "Spune-mi mai multe despre Crema A",
        "Vreau ceva mai ieftin decât astea",
    ]
    assert _CHEAPER_RE.search(chips[-1])  # → cheaper_intent, nu bucla LLM
    assert _DETAIL_RE.search(chips[2])  # → detail_intent pe primul produs
    assert all(len(c) <= MAX_CHIP_LEN for c in chips)
    assert _web_chips(chips) == chips  # niciunul nu e dropat de garda widgetului


def test_compare_chips_shorten_the_name_not_the_verb():
    """Un nume lung cedează primul: tăierea la coadă ar mânca fix „în coș" / „Tedd a kosárba",
    adică partea care poartă intenția."""
    products = _products()
    products[0]["name"] = "Crema Hidratantă Foarte Foarte Lungă Pentru Ten Uscat Și Sensibil"
    cmp_ = build_comparison(products, "ro", ())
    assert cmp_ is not None
    chips = _compare_chips(cmp_.columns, "ro")

    assert chips[0].startswith("Adaugă ") and chips[0].endswith(" în coș")
    assert "…" in chips[0]
    assert all(len(c) <= MAX_CHIP_LEN for c in chips)
