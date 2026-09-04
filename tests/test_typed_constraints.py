"""NX-266 — un număr nu se negociază.

Testele urmăresc lanțul întreg, în ordinea în care se poate rupe: conversia (dacă unitățile mint,
restul e degeaba), extragerea din mesaj, compunerea a două margini, legarea de fațete (unde se
opresc cererile neevaluabile), predicatul SQL și, la capăt, tool-ul real cu retrievere false.

Cazurile din card sunt marcate în docstringuri (happy 1-2, edge 3-5, failure 6-7)."""

from __future__ import annotations

import json
import types
from decimal import Decimal
from pathlib import Path

import pytest

from src.config import get_settings
from src.db.queries import catalog as cat
from src.domain.constraints import (
    MISMATCH,
    OP_BETWEEN,
    OP_GTE,
    OP_LTE,
    REASON_CONFLICTING_BOUNDS,
    REASON_FACET_NOT_DECLARED,
    REASON_FACET_NOT_NUMERIC,
    REASON_OP_NOT_ALLOWED,
    SOURCE_MODEL,
    SOURCE_USER,
    UNKNOWN,
    TypedConstraint,
    apply_constraints,
    bind_constraints,
    build_units,
    constraint_from_value,
    evaluate,
    extract_constraints,
    merge_constraints,
)
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps

PACK_PATH = Path(__file__).resolve().parent.parent / "db" / "seed" / "domain_pack_sole_ro.json"


def _pack():
    """Pachetul REAL al lui `sole-ro`, prin loader-ul de producție. Deliberat nu un fixture
    inventat: dacă tabelul de unități din seed se strică, testele astea trebuie să pice."""
    raw = json.loads(PACK_PATH.read_text(encoding="utf-8"))
    business = types.SimpleNamespace(vertical="ecommerce", settings={"domain_pack": raw})
    return load_domain_pack(business)


PACK = _pack()
UNITS = PACK.units
FACETS = PACK.facets


# --- conversie: unitatea canonică -------------------------------------------------------------


def test_conversia_intre_unitati_e_round_trip():
    """Edge 3 — „50 ml" și „0,05 l" sunt aceeași constrângere, iar drumul înapoi o confirmă."""
    volume = UNITS.specs["volume"]
    assert volume.to_canonical(Decimal("50"), "ml") == Decimal("50")
    assert volume.to_canonical(Decimal("0.05"), "l") == Decimal("50")
    assert volume.to_canonical(Decimal("5"), "cl") == Decimal("50")
    # round-trip: canonic → unitatea rostită → canonic, fără pierdere
    for unit in ("ml", "cl", "dl", "l"):
        back = volume.from_canonical(Decimal("50"), unit)
        assert volume.to_canonical(back, unit) == Decimal("50")


def test_banii_raman_exacti_pe_decimal():
    """`Decimal`, nu `float` — 89,90 lei trebuie să rămână 89,90, nu 89.90000000000001.

    Nu e pedanterie: filtrul `<= 89.9` pe un preț stocat ca 89.90 e exact locul unde o rotunjire
    binară scoate din rezultate produsul potrivit, iar clientul vede „n-am găsit"."""
    spoken, _ = extract_constraints("ceva sub 89,90 lei", units=UNITS, locale="ro")
    assert [c.value for c in spoken] == [Decimal("89.90")]
    assert str(spoken[0].value) == "89.90"
    # și pe drumul structurat (un `price_max` float venit de la model)
    made, _ = constraint_from_value("price", OP_LTE, 89.90, units=UNITS, source=SOURCE_MODEL)
    assert made.value == Decimal("89.90")


def test_unitatea_canonica_fara_factor_1_e_respinsa():
    """Fail-closed pe config: dacă unitatea canonică n-are factorul 1, conversia n-ar fi
    idempotentă și fiecare trecere ar înmulți valoarea. Tabelul e dropat, nu „corectat"."""
    registry = build_units({"price": {"canonical": "lei", "factors": {"lei": 100, "bani": 1}}})
    assert registry.specs == {}


def test_alias_ambiguu_nu_alege_tacut_o_fateta():
    """Același cuvânt la două fațete: a ghici ar produce un filtru greșit cu aer de certitudine."""
    registry = build_units(
        {
            "price": {"canonical": "lei", "factors": {"lei": 1, "g": 1}},
            "weight": {"canonical": "g", "factors": {"g": 1}},
        }
    )
    assert "g" in registry.ambiguous
    assert registry.facet_for_unit("g") is None
    assert registry.facet_for_unit("lei") == "price"


# --- extragere din mesaj -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("o crema sub 100 lei", [("price", OP_LTE, "100")]),
        ("cel mult 150 ron", [("price", OP_LTE, "150")]),
        ("vreau spf minim 30", [("spf", OP_GTE, "30")]),
        ("spf 50", [("spf", OP_GTE, "50")]),  # `default_op` din pachet, nu din cod
        ("crema de 100 lei", [("price", OP_LTE, "100")]),  # idem, pe cealaltă fațetă
        ("am 2 copii si ten gras", []),  # număr fără unitate = nu e o constrângere
        ("caut ceva bun", []),
    ],
)
def test_extragerea_citeste_numar_unitate_comparatie(message, expected):
    """Happy 1-2 la nivel de extragere. Comparația vine din vocabularul locale-i, unitatea din
    pachet, numărul din mesaj — niciuna din cele trei nu e scrisă în modulul care le combină."""
    spoken, _ = extract_constraints(message, units=UNITS, locale="ro")
    assert [(c.facet, c.op, str(c.value)) for c in spoken] == expected


def test_fara_locale_cunoscuta_cade_pe_operatorul_implicit():
    """P11 — o limbă necunoscută nu primește comparatorii românești. Numărul rămâne o
    constrângere (unitatea e a tenantului, nu a limbii), dar operatorul e cel declarat."""
    spoken, _ = extract_constraints("valami 100 lei alatt", units=UNITS, locale="hu")
    assert [(c.facet, c.op) for c in spoken] == [("price", OP_LTE)]  # `default_op` = lte


def test_registru_gol_nu_produce_nimic():
    """Un tenant fără tabel de unități se comportă ca azi: niciun număr nu devine filtru."""
    spoken, rejected = extract_constraints("sub 100 lei", units=build_units(None), locale="ro")
    assert spoken == () and rejected == ()


# --- compunere ---------------------------------------------------------------------------------


def test_doua_margini_pe_aceeasi_fateta_devin_between():
    """Edge 5 — „peste 30, sub 50" e o cerere, nu două. Fără compunere, ultima ar câștiga."""
    spoken, _ = extract_constraints("peste 30 lei dar sub 50 lei", units=UNITS, locale="ro")
    merged, rejected = merge_constraints(spoken)
    assert rejected == ()
    assert len(merged) == 1
    assert (merged[0].op, merged[0].value, merged[0].value_max) == (
        OP_BETWEEN,
        Decimal("30"),
        Decimal("50"),
    )


def test_marginile_se_strang_nu_se_inlocuiesc():
    """Două plafoane rostite se cumulează: rămâne cel mai strâns, nu ultimul."""
    merged, _ = merge_constraints(
        [
            TypedConstraint("price", OP_LTE, Decimal("200"), "lei", SOURCE_USER),
            TypedConstraint("price", OP_LTE, Decimal("120"), "lei", SOURCE_USER),
        ]
    )
    assert [(c.op, c.value) for c in merged] == [(OP_LTE, Decimal("120"))]


def test_margini_imposibile_sunt_respinse_cu_motiv():
    """„sub 50 și peste 100" nu se aproximează spre una dintre ele — nu se aplică deloc."""
    spoken, _ = extract_constraints("sub 50 lei si peste 100 lei", units=UNITS, locale="ro")
    merged, rejected = merge_constraints(spoken)
    assert merged == ()
    assert [r.reason for r in rejected] == [REASON_CONFLICTING_BOUNDS]


# --- legare de fațete --------------------------------------------------------------------------


def test_unitate_cunoscuta_fara_fateta_e_respinsa_cu_motiv():
    """Failure 6 — pachetul știe ce e un mililitru, dar niciun produs nu declară volumul. Cererea
    NU se aplică aproximativ și nu dispare tăcut: iese cu `facet_not_declared`."""
    spoken, _ = extract_constraints("ceva de 50 ml", units=UNITS, locale="ro")
    assert [c.facet for c in spoken] == ["volume"]
    bounds, rejected = bind_constraints(spoken, FACETS)
    assert bounds == ()
    assert [(r.reason, r.facet) for r in rejected] == [(REASON_FACET_NOT_DECLARED, "volume")]


def test_unitate_pe_care_pachetul_n_o_cunoaste_nu_devine_constrangere():
    """Failure 6, cealaltă jumătate: „60 cm" într-un pachet fără lungimi nu se convertește în
    nimic. Un filtru inventat pe o unitate necunoscută ar fi mai rău decât absența lui."""
    spoken, _ = extract_constraints("sa incapa in 60 cm", units=UNITS, locale="ro")
    assert spoken == ()


def test_fateta_ne_numerica_nu_poate_purta_un_numar():
    """`category` e o etichetă, nu o măsurătoare — nicio compunere de operatori n-o face una."""
    bounds, rejected = bind_constraints(
        [TypedConstraint("category", OP_LTE, Decimal("3"), "lei", SOURCE_USER)], FACETS
    )
    assert bounds == ()
    assert rejected[0].reason == REASON_FACET_NOT_NUMERIC


def test_operator_nepermis_de_fateta_e_respins():
    """Registrul de fațete decide ce operatori sunt legali; `between` cere ambele margini."""
    only_lte = tuple(
        f if f.key != "spf" else type(f)(**{**f.__dict__, "operators": ("lte",)}) for f in FACETS
    )
    bounds, rejected = bind_constraints(
        [TypedConstraint("spf", OP_BETWEEN, Decimal("30"), "spf", SOURCE_USER, Decimal("50"))],
        only_lte,
    )
    assert bounds == ()
    assert rejected[0].reason == REASON_OP_NOT_ALLOWED


# --- evaluare peste produse (plasa de după rerankare) -------------------------------------------


def _bind(message: str):
    spoken, _ = extract_constraints(message, units=UNITS, locale="ro")
    merged, _ = merge_constraints(spoken)
    bounds, _ = bind_constraints(merged, FACETS)
    return bounds


def test_unknown_nu_e_mismatch():
    """D7 literal — un produs care nu declară SPF nu e un produs cu SPF mic."""
    (bound,) = _bind("vreau spf minim 30")
    assert evaluate(bound, {"attributes": {}}) == UNKNOWN
    assert evaluate(bound, {"attributes": {"spf": 15}}) == MISMATCH
    assert evaluate(bound, {"attributes": {"spf": 50}}) == "match"


def test_atribut_ne_numeric_e_unknown_nu_mismatch():
    """„SPF: ridicat" nu e o valoare mică, e o valoare pe care n-o putem compara."""
    (bound,) = _bind("vreau spf minim 30")
    assert evaluate(bound, {"attributes": {"spf": "ridicat"}}) == UNKNOWN
    # dar un număr scris ca text E comparabil (forma pe care o produce orice derivare prin text)
    assert evaluate(bound, {"attributes": {"spf": "50"}}) == "match"


def test_plasa_pastreaza_necunoscutul_si_taie_contrazicerea():
    """Happy 2 — SPF 15 iese, SPF necunoscut RĂMÂNE și e numărat separat."""
    (bound,) = _bind("vreau spf minim 30")
    products = [
        {"id": "a", "attributes": {"spf": 50}},
        {"id": "b", "attributes": {"spf": 15}},
        {"id": "c", "attributes": {}},
    ]
    kept, stats = apply_constraints(products, [bound])
    assert [p["id"] for p in kept] == ["a", "c"]
    assert stats["spf"] == {"match": 1, MISMATCH: 1, UNKNOWN: 1}


def test_pretul_lipsa_cade_pentru_ca_fateta_o_cere():
    """`price` declară `missing_value: skip` — un produs fără preț nu poate promite „sub 100".
    Politica e a FAȚETEI: același cod, alt verdict, fiindcă alt contract de date."""
    (bound,) = _bind("sub 100 lei")
    kept, _ = apply_constraints([{"id": "x", "price": None}, {"id": "y", "price": 80}], [bound])
    assert [p["id"] for p in kept] == ["y"]


# --- predicatul SQL ----------------------------------------------------------------------------


def _sql(bounds):
    params: list[object] = []

    def placeholder(value):
        params.append(value)
        return f"${len(params)}"

    return cat._constraint_clause(bounds, placeholder), params


def test_predicatul_e_tri_state_dupa_politica_fatetei():
    """Ramura `else` a CASE-ului e tot ce separă D7 de un filtru obișnuit."""
    sql_spf, _ = _sql(_bind("spf minim 30"))
    assert "else true end" in sql_spf  # missing_value=unknown → necunoscutul RĂMÂNE
    sql_price, _ = _sql(_bind("sub 100 lei"))
    assert "else false end" in sql_price  # missing_value=skip → fără preț, cade


def test_cheia_de_atribut_e_parametrizata_nu_interpolata():
    """Aceeași regulă ca la `_facet_filter_clause`: nicio cheie de config în textul SQL."""
    sql, params = _sql(_bind("spf minim 30"))
    assert "'spf'" not in sql and "spf" not in sql.replace("jsonb", "")
    assert "spf" in params


def test_intervalul_produce_ambele_margini():
    sql, params = _sql(_bind("peste 30 lei dar sub 50 lei"))
    assert ">=" in sql and "<=" in sql
    assert Decimal("30") in params and Decimal("50") in params


# --- tool: cele două puncte de aplicare ---------------------------------------------------------


def _ctx(body: str) -> TurnContext:
    business = BusinessConfig(id="biz-1", slug="sole-ro", name="SOLE", vertical="ecommerce")
    business.domain_pack = PACK
    return TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        language="ro",
    )


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


@pytest.fixture()
def _no_embeddings(monkeypatch):
    async def _false(conn, business_id):
        return False

    monkeypatch.setattr(ct, "has_embeddings", _false)


@pytest.fixture()
def _lexical(monkeypatch):
    """Retriever lexical fals care ÎNREGISTREAZĂ ce a primit și întoarce ce i se spune.

    Întoarce candidați care ÎNCALCĂ constrângerea, deliberat: exact scenariul pentru care există
    plasa de după rerankare (candidați veniți din altă cale decât predicatul SQL)."""
    captured: dict = {"rows": []}

    async def fake(conn, business_id, **kwargs):
        captured.update(kwargs)
        return list(captured["rows"])

    monkeypatch.setattr(ct, "search_products_lexical", fake)
    return captured


@pytest.fixture()
def _on(monkeypatch):
    monkeypatch.setattr(get_settings(), "typed_constraints_enabled", True)


@pytest.fixture()
def _off(monkeypatch):
    monkeypatch.setattr(get_settings(), "typed_constraints_enabled", False)


def _p(pid: str, price: float, **attrs):
    return {
        "id": pid,
        "name": f"Produs {pid}",
        "brand": "B",
        "price": price,
        "url": f"https://shop/{pid}",
        "availability": "in_stock",
        "attributes": attrs or {},
    }


async def test_flag_off_nu_schimba_nimic(_off, _no_embeddings, _lexical):
    """DoD — cu flagul stins retrieverele primesc `constraints=()` și bugetul curge ca azi."""
    _lexical["rows"] = [_p("a", 80), _p("b", 500)]
    ctx = _ctx("o crema sub 100 lei")
    res = await run_tool(
        ctx, _deps(), "search_products", {"query": "crema", "price_max": 100, "limit": 6}
    )
    assert _lexical["constraints"] == ()
    assert _lexical["price_max"] == 100  # calea veche, neatinsă
    assert {p["id"] for p in res.products} == {"a", "b"}  # niciun filtru în cod
    assert not [e for e in ctx.events if e.type == "constraint_applied"]


async def test_pretul_rostit_migreaza_pe_contractul_tipizat(_on, _no_embeddings, _lexical):
    """DoD — `budget_max` nu mai pleacă și ca `price_max`: valoarea autoritară e una singură."""
    _lexical["rows"] = [_p("a", 80)]
    ctx = _ctx("o crema sub 100 lei")
    await run_tool(ctx, _deps(), "search_products", {"query": "crema", "price_max": 100})
    assert _lexical["price_max"] is None
    (bound,) = _lexical["constraints"]
    assert (bound.facet, bound.constraint.op, bound.constraint.value) == (
        "price",
        OP_LTE,
        Decimal("100"),
    )


async def test_toate_rezultatele_respecta_pragul_de_pret(_on, _no_embeddings, _lexical):
    """Happy 1 — chiar dacă retrieverul întoarce un produs peste prag, el nu ajunge la client."""
    _lexical["rows"] = [_p("ieftin", 80), _p("scump", 149)]
    ctx = _ctx("o crema sub 100 lei")
    res = await run_tool(ctx, _deps(), "search_products", {"query": "crema"})
    assert [p["id"] for p in res.products] == ["ieftin"]
    (ev,) = [e for e in ctx.events if e.type == "constraint_applied"]
    assert ev.properties["facet"] == "price" and ev.properties["mismatched"] == 1
    assert "value" not in ev.properties  # P12: numărătoare, nu valoarea rostită


async def test_spf_sub_prag_nu_ajunge_la_client_iar_necunoscutul_ramane(
    _on, _no_embeddings, _lexical
):
    """Happy 2 — cazul pentru care cardul vine ÎNAINTEA rerankerului."""
    _lexical["rows"] = [_p("bun", 90, spf=50), _p("slab", 70, spf=15), _p("mut", 60)]
    ctx = _ctx("vreau o crema cu spf minim 30")
    res = await run_tool(ctx, _deps(), "search_products", {"query": "crema"})
    assert [p["id"] for p in res.products] == ["bun", "mut"]
    (ev,) = [e for e in ctx.events if e.type == "constraint_applied"]
    assert ev.properties["facet"] == "spf"
    assert (ev.properties["mismatched"], ev.properties["unknown"]) == (1, 1)


async def test_constrangerea_dedusa_de_model_nu_exclude(_on, _no_embeddings, _lexical):
    """Edge 4 — un `price_max` pe care clientul NU l-a rostit rămâne inferență: nu devine
    constrângere tipizată, deci nu capătă putere de excludere. Bugetul continuă pe calea veche,
    fiindcă a i-o tăia ar fi o schimbare de comportament străină de card."""
    _lexical["rows"] = [_p("a", 80)]
    ctx = _ctx("caut ceva accesibil pentru ten uscat")
    await run_tool(ctx, _deps(), "search_products", {"query": "crema", "price_max": 100})
    assert _lexical["constraints"] == ()
    assert _lexical["price_max"] == 100


async def test_setul_golit_de_o_constrangere_numeste_cerinta(_on, _no_embeddings, _lexical):
    """Failure 7 — nu se repopulează tăcut, iar modelul află EXACT ce cerință n-a putut fi
    îndeplinită. Fără asta, „n-am găsit" arată identic cu un catalog gol."""
    _lexical["rows"] = [_p("scump", 300)]
    ctx = _ctx("o crema sub 100 lei")
    res = await run_tool(ctx, _deps(), "search_products", {"query": "crema"})
    assert res.products == []
    assert "Pret ≤ 100 lei" in res.llm_view
    assert "nu îndeplinește" in res.llm_view


async def test_o_constrangere_noua_sparge_sesiunea(_on, _no_embeddings, _lexical):
    """NX-223, aplicat numerelor: „mai arată-mi, dar cu SPF minim 50" nu are voie să pagineze
    pool-ul vechi, nefiltrat pe număr."""
    args = ct.SearchArgs(query="crema")
    fara = ct._fp(ct._session_filters(args, None, None))
    cu = ct._fp(ct._session_filters(args, None, None, _bind("spf minim 50")))
    alta = ct._fp(ct._session_filters(args, None, None, _bind("spf minim 30")))
    assert fara != cu and cu != alta


def test_fp_ul_ramane_identic_fara_constrangeri():
    """Cheia lipsește când nu sunt constrângeri — altfel toate sesiunile în curs ar fi invalidate
    de un card care, cu flagul stins, nu face nimic."""
    keys = set(ct._session_filters(ct.SearchArgs(query="q"), None, None))
    assert "constraints" not in keys
