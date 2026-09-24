"""NX-322 — nevoia clientului: meniu ÎNCHIS + citat, iar dreptul de a exclude e al codului.

Conversația reală `bc7a356e`: «pai mi se usuca pielea dupa dus» a ajuns la unelte ca «se usucă
după duș», iar rezoluția pe frază exactă n-a găsit `dry`. Testele fixează contractul:

  • citatul nu e al clientului ⇒ `rejected` (nici filtru, nici ordonare);
  • citatul conține aliasul ALTEI valori din aceeași dimensiune partiționantă ⇒ `rejected`;
  • citatul conține un alias al cheii ⇒ `hard` (filtru);
  • altfel ⇒ `soft` (doar ordonare).

Zero DB, zero model. Vocabularul și pachetul sunt mici și construite aici.
"""

from __future__ import annotations

import json

import pytest

from src.agent.tool_definitions import tool_schemas
from src.agent.tool_executor import _safe_tool_args
from src.catalog.need_menu import (
    NeedArg,
    build_menu,
    need_dimensions,
    need_verdict,
    quote_supported,
    split_args,
    split_needs,
)
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry, facet_overlays
from src.db.queries.catalog import _facet_prefer_rank
from src.db.queries.fusion import blended_rerank, deterministic_rerank, preference_level
from src.domain.facets import build_facets
from src.domain.pack import DomainPack, FacetSpec
from src.tools.catalog_tools import SearchArgs
from src.tools.routine_tools import RoutineArgs

PACK = DomainPack(
    vertical="ecommerce",
    concern_map={
        "ten uscat": "dry",
        "piele uscata": "dry",
        "uscaciune": "dry",
        "ten gras": "oily",
        "hidratare": "hydration",
        "roseata": "redness",
    },
    facets=build_facets(
        [
            {
                "key": "skin_type",
                "source": "attribute",
                "source_key": "skin_type",
                "value_type": "enum",
                "values": ["dry", "oily", "sensitive"],
                "operators": ["eq"],
                "binding": "partitioning",
            },
            {
                "key": "concerns",
                "source": "attribute",
                "source_key": "concerns",
                "value_type": "list",
                "values": ["hydration", "redness"],
                "operators": ["contains"],
                "binding": "additive",
            },
            {
                "key": "product_type",
                "source": "attribute",
                "source_key": "product_type",
                "value_type": "enum",
                "values": ["crema de fata"],
                "operators": ["eq"],
            },
        ]
    ),
    comparison_facets=(FacetSpec(key="skin_type", value_labels={"dry": {"ro": "ten uscat"}}),),
)

VOCAB = CatalogVocabulary(
    business_id="b",
    dimensions={
        "skin_type": (
            VocabEntry(key="dry", label="dry", count=429),
            VocabEntry(key="oily", label="oily", count=318),
            VocabEntry(key="sensitive", label="sensitive", count=0),  # fără produse ⇒ afară
        ),
        "concerns": (
            VocabEntry(key="hydration", label="hidratare", count=1929),
            VocabEntry(key="redness", label="roseata", count=1170),
        ),
        "product_type": (VocabEntry(key="crema de fata", label="crema de fata", count=300),),
    },
)

MENU = build_menu(PACK, VOCAB.dimensions, facet_overlays(PACK, VOCAB.facet_names), "ro")

#: Conversația reală, mesajele clientului (curentul primul).
TEXTS = ["fa mi o rutina", "pai mi se usuca pielea dupa dus", "vreau o crema de fata"]


# ── Meniul ────────────────────────────────────────────────────────────────────────────────────


def test_dimensiunile_de_nevoie_vin_din_harta_tenantului():
    """Nu o listă din cod: `product_type` are produse, dar harta de nevoi nu traduce în el."""
    assert need_dimensions(PACK, VOCAB.dimensions) == ("concerns", "skin_type")


def test_meniul_are_doar_valori_cu_produse_si_ordine_stabila():
    assert MENU.keys() == ("hydration", "redness", "dry", "oily")
    assert MENU.partitioning == frozenset({"skin_type"})


def test_eticheta_vine_din_pachet_altfel_din_vocabular():
    labels = {o.key: o.label for o in MENU.options}
    assert labels["dry"] == "ten uscat"  # `FacetSpec.value_labels`
    assert labels["hydration"] == "hidratare"  # eticheta vocabularului


def test_descrierea_nu_are_punct_si_virgula():
    """Textul ajunge în promptul modelului, care se scrie în vocea cerută (P13)."""
    assert ";" not in MENU.description()


# ── Citatul ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("quote", "ok"),
    [
        ("mi se usuca pielea", True),
        ("mi se usucă pielea", True),  # diacritice în citat, nu în mesaj
        ("se usuca pielea dupa dus", True),
        ("pielea", False),  # un singur cuvânt de conținut
        ("calmarea roseții", False),  # l-a scris botul, nu clientul
        ("usuca piele", False),  # nu e în mesaj ca șir de cuvinte întregi
    ],
)
def test_quote_supported(quote, ok):
    assert quote_supported(quote, TEXTS, "ro") is ok


# ── Verdictul ─────────────────────────────────────────────────────────────────────────────────


def test_conversatia_reala_dry_e_soft():
    """«mi se usuca pielea» nu conține azi niciun alias al lui `dry`: ORDONEAZĂ, nu exclude."""
    v = need_verdict(NeedArg(key="dry", quote="mi se usuca pielea"), MENU, TEXTS, "ro")
    assert (v.dimension, v.strength, v.reason, v.source) == (
        "skin_type",
        "soft",
        "semantic_only",
        "user_explicit",
    )


def test_alias_al_tenantului_in_citat_e_hard():
    v = need_verdict(NeedArg(key="dry", quote="am tenul uscat"), MENU, ["am tenul uscat"], "ro")
    assert v.strength == "soft"  # «tenul» ≠ «ten»: potrivire pe cuvinte întregi, conservator
    v = need_verdict(NeedArg(key="dry", quote="am ten uscat"), MENU, ["am ten uscat rau"], "ro")
    assert (v.strength, v.reason) == ("hard", "alias_match")


def test_contraexemplul_codex_e_respins():
    """Citatul e al clientului, dar spune altă valoare a aceleiași dimensiuni partiționante."""
    v = need_verdict(NeedArg(key="dry", quote="am ten gras"), MENU, ["am ten gras"], "ro")
    assert (v.strength, v.reason) == ("rejected", "contradicted")


def test_nevoia_scrisa_de_bot_e_respinsa():
    v = need_verdict(NeedArg(key="redness", quote="calmarea roseții"), MENU, TEXTS, "ro")
    assert (v.strength, v.reason, v.source) == ("rejected", "quote_missing", "model_inferred")


def test_cheie_din_afara_meniului_e_respinsa():
    v = need_verdict(NeedArg(key="sensitive", quote="mi se usuca pielea"), MENU, TEXTS, "ro")
    assert (v.strength, v.reason) == ("rejected", "unknown_key")


def test_split_needs():
    verdicts = [
        need_verdict(NeedArg(key="dry", quote="mi se usuca pielea"), MENU, TEXTS, "ro"),
        need_verdict(NeedArg(key="hydration", quote="am ten uscat"), MENU, ["am ten uscat"], "ro"),
        need_verdict(NeedArg(key="redness", quote="calmarea roseții"), MENU, TEXTS, "ro"),
    ]
    hard, soft = split_needs(verdicts)
    assert hard == {}
    assert soft == {"skin_type": ["dry"], "concerns": ["hydration"]}


def test_hard_doar_cu_alias():
    """Proprietatea contractului: niciun `hard` fără `alias_match`."""
    for key in MENU.keys():
        for quote in ("mi se usuca pielea", "am ten uscat", "am ten gras", "hidratare buna"):
            v = need_verdict(NeedArg(key=key, quote=quote), MENU, [quote], "ro")
            assert v.strength != "hard" or v.reason == "alias_match"


# ── Argumentele: ambele forme se parsează ─────────────────────────────────────────────────────


def test_search_args_accepta_ambele_forme():
    a = SearchArgs(
        query="crema",
        concerns=["hidratare", {"key": "dry", "quote": "mi se usuca pielea"}],
    )
    legacy, needs = split_args(a.concerns)
    assert legacy == ["hidratare"]
    assert needs == [NeedArg(key="dry", quote="mi se usuca pielea")]


def test_routine_args_accepta_ambele_forme():
    a = RoutineArgs(family="fata", concerns=[{"key": "dry", "quote": "mi se usuca pielea"}])
    assert split_args(a.concerns) == ([], [NeedArg(key="dry", quote="mi se usuca pielea")])


# ── Schema ────────────────────────────────────────────────────────────────────────────────────


def test_fara_meniu_schema_e_byte_identica():
    names = ["search_products", "routine_plan"]
    kw = {"families": ("fata",)}
    assert json.dumps(tool_schemas(names, **kw)) == json.dumps(
        tool_schemas(names, **kw, need_menu=None)
    )


def test_cu_meniu_concerns_e_lista_de_key_quote():
    schemas = {
        s["function"]["name"]: s["function"]["parameters"]
        for s in tool_schemas(
            ["search_products", "routine_plan"], families=("fata",), need_menu=MENU
        )
    }
    for name in ("search_products", "routine_plan"):
        concerns = schemas[name]["properties"]["concerns"]
        item = concerns["items"]
        assert item["properties"]["key"]["enum"] == list(MENU.keys())
        assert item["required"] == ["key", "quote"]
        assert "dry = ten uscat" in concerns["description"]
    assert schemas["search_products"]["properties"]["concerns"]["type"] == ["array", "null"]
    assert schemas["routine_plan"]["properties"]["concerns"]["type"] == "array"


def test_alte_unelte_nu_se_ating():
    before = tool_schemas(["get_product_details"])
    assert tool_schemas(["get_product_details"], need_menu=MENU) == before


# ── Telemetria nu poartă citatul (P12) ────────────────────────────────────────────────────────


def test_tool_call_logheaza_cheia_nu_citatul():
    out = _safe_tool_args(
        "search_products",
        {"query": "x", "concerns": [{"key": "dry", "quote": "sunt insarcinata si mi se usuca"}]},
    )
    assert out["concerns"] == ["dry"]
    assert "insarcinata" not in json.dumps(out)


# ── Ordonarea soft ────────────────────────────────────────────────────────────────────────────


def _p(pid, skin=None):
    attrs = {"skin_type": skin} if skin else {}
    return {"id": pid, "attributes": attrs, "availability": "in_stock"}


def test_preference_level_trivalent():
    prefer = {"skin_type": ["dry"]}
    assert preference_level(_p("a", "dry"), prefer) == 1.0
    assert preference_level(_p("b"), prefer) == 0.5  # necunoscut la mijloc, nu jos
    assert preference_level(_p("c", "oily"), prefer) == 0.0
    assert preference_level(_p("c", "oily"), None) == 0.0


def test_ordonare_potrivire_necunoscut_contradictie():
    """Aceeași relevanță: potrivirea urcă, contradicția coboară, necunoscutul stă la mijloc."""
    products = [_p("oily", "oily"), _p("unknown"), _p("dry", "dry")]
    scores = {"oily": 1.0, "unknown": 1.0, "dry": 1.0}
    prefer = {"skin_type": ["dry"]}
    assert [p["id"] for p in deterministic_rerank(products, scores, prefer=prefer)] == [
        "dry",
        "unknown",
        "oily",
    ]
    assert [p["id"] for p in blended_rerank(products, scores, prefer=prefer)] == [
        "dry",
        "unknown",
        "oily",
    ]


def test_fara_preferinte_ordinea_nu_se_schimba():
    products = [_p("b", "oily"), _p("a", "dry")]
    scores = {"a": 1.0, "b": 2.0}
    assert deterministic_rerank(products, scores) == deterministic_rerank(
        products, scores, prefer=None
    )
    assert blended_rerank(products, scores) == blended_rerank(products, scores, prefer={})


def test_sql_de_ordonare_pune_necunoscutul_la_mijloc():
    params: list = []

    def ph(v):
        params.append(v)
        return f"${len(params)}"

    sql = _facet_prefer_rank({"skin_type": ["dry"]}, ph)
    assert "is null then 1" in sql and "then 0 else 2" in sql
    assert params == ["skin_type", ["dry"]]  # cheia e parametrizată, niciodată interpolată
