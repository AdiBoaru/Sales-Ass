"""Drumul DB → tool → model, după sonda din docs/DB-QUERY-PROBE-2026-09-08.md.

Fiecare test ține o constatare MĂSURATĂ pe primul catalog real: ce vedea modelul (secțiuni care
cădeau tăcut, nume repetate, badge-uri fără informație, citate rupte) și ce plătea DB-ul
(substitute hidratate din tot catalogul, brațul semantic care sorta global, `has_embeddings` la
fiecare căutare). Toate sunt pure sau pe conexiuni false — zero DB, zero model.
"""

from __future__ import annotations

import pytest

from src.catalog.key_ingredients import canonical_values, parse_key_ingredients
from src.catalog.query_terms import relaxed_query, relaxed_query_any
from src.catalog.render_text import cut_at_sentence, display_name
from src.config import get_settings
from src.db.queries.catalog import _lexical_steps
from src.db.queries.fusion import OOS_RANK_OFFSET, demote_out_of_stock, fuse_candidates
from src.domain.loader import _norm_detail_sections
from src.domain.pack import DomainPack, FacetSpec, SectionSpec
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.catalog_tools import _brief, _compare_view, _detail_view
from src.worker.runner import PipelineDeps

# --- scara lexicală: relaxarea pe perechi ------------------------------------------------------


def test_relaxed_query_cere_perechi_de_la_trei_termeni():
    """„sampoon anti matreata": SAU pe singulari urca un aparat anti-îmbătrânire epuizat pe locul
    2, fiindcă `anti` e prefix în sute de nume. Perechile cer doi termeni pe același produs."""
    assert relaxed_query(["sampoon", "anti", "matreata"]) == (
        "sampoon anti or sampoon matreata or anti matreata"
    )
    assert relaxed_query(["sampon", "gras"]) == "sampon or gras"  # sub 3 termeni: neschimbat
    assert relaxed_query_any(["sampoon", "anti", "matreata"]) == "sampoon or anti or matreata"


def test_scara_lexicala_pastreaza_singularii_ca_treapta_separata():
    """Relaxarea nu are voie să producă zerouri noi (P6): după perechi vine SAU-ul pe singulari,
    apoi plasa de typo. Cu doi termeni perechea E singularul, deci treapta se sare."""
    assert _lexical_steps(True, ["a", "b", "c"]) == ("strict", "relaxed", "relaxed_any", "fuzzy")
    assert _lexical_steps(True, ["a", "b"]) == ("strict", "relaxed", "fuzzy")
    assert _lexical_steps(True, ["a"]) == ("strict", "fuzzy")


# --- fuziune: epuizatul coboară, dar rămâne candidat ------------------------------------------


def _p(pid: str, availability: str | None = "in_stock") -> dict:
    d = {"id": pid}
    if availability is not None:
        d["availability"] = availability
    return d


def test_epuizatul_coboara_cu_offset_de_rang_dar_nu_iese_din_pool():
    lexical = [_p("oos", "out_of_stock"), _p("a"), _p("b"), _p("c"), _p("d"), _p("e"), _p("f")]
    out = fuse_candidates(lexical, [], sort_mode="relevance")
    ids = [p["id"] for p in out]
    assert ids.index("oos") >= OOS_RANK_OFFSET  # nu mai e primul
    assert "oos" in ids  # dar e tot în pool: substitutele și „mai arată-mi" au nevoie de el
    assert ids[:2] == ["a", "b"]  # ordinea celor cumpărabile e neatinsă


def test_disponibilitatea_necunoscuta_nu_e_epuizat():
    """UNKNOWN ≠ 0 (NX-240/047): un produs fără `availability` nu coboară."""
    scores = {"x": 0.5, "u": 0.5}
    out = demote_out_of_stock([_p("x"), _p("u", None)], scores)
    assert out["u"] == scores["u"] and out["x"] == scores["x"]


# --- text: nume scurt și tăiere la propoziție -------------------------------------------------


def test_display_name_taie_coada_descriptiva():
    long = (
        "ANUA Peach 77 Niacin Enriched Cream - crema de fata formulata cu niacinamida si pantenol,"
        " care contribuie la hidratarea pielii - 50 ml"
    )
    assert display_name(long) == "ANUA Peach 77 Niacin Enriched Cream"
    assert display_name("DR.JART+ Vital Hydra Solution, 150 ml - emulsie") == (
        "DR.JART+ Vital Hydra Solution"
    )
    assert display_name("Ser - de fata") == "Ser - de fata"  # cap prea scurt → întregul
    assert display_name(None) == ""


def test_cut_at_sentence_nu_rupe_propozitia():
    text = "Pielea e mai neteda dupa o saptamana. Mirosul e discret. Il recomand cu incredere."
    assert cut_at_sentence(text, 60) == "Pielea e mai neteda dupa o saptamana. Mirosul e discret."
    assert cut_at_sentence(text, 200) == text
    bullets = "Este potrivita daca: • ai ten uscat • vrei textura lejera • cauti confort zilnic"
    assert cut_at_sentence(bullets, 45).endswith("ai ten uscat")
    # fără graniță în a doua jumătate → ultimul spațiu + „…", niciodată cuvânt rupt
    run_on = "a" * 30 + " " + "b" * 30 + " " + "c" * 30
    got = cut_at_sentence(run_on, 50)
    assert got.endswith("…") and " " not in got.rstrip("…")[-1]


# --- vederile modelului ---------------------------------------------------------------------


def _pack(**over) -> DomainPack:
    base = dict(
        vertical="ecommerce",
        comparison_facets=(
            FacetSpec(key="routine_time", labels={"ro": "Moment din rutina"}),
            FacetSpec(key="skin_type", labels={"ro": "Tip de ten"}),
            FacetSpec(key="concerns", labels={"ro": "Potrivit pentru"}),
            FacetSpec(key="spf", labels={"ro": "SPF"}),
        ),
    )
    base.update(over)
    return DomainPack(**base)


def _prod(**over) -> dict:
    p = {
        "id": "1",
        "name": "ANUA Peach 77 Niacin Enriched Cream - crema de fata cu niacinamida, 50 ml",
        "brand": "ANUA",
        "price": 155.0,
        "availability": "in_stock",
        "rating": 4.9,
        "attributes": {
            "routine_time": ["am", "pm"],
            "skin_type": ["dry"],
            "concerns": ["hydration"],
            "spf": "50",
        },
    }
    p.update(over)
    return p


def test_brief_ia_fatetele_din_pachet_in_ordinea_lui(monkeypatch):
    """Înainte, un set fix de chei scris pentru catalogul demo lăsa afară `skin_type`/`spf`/
    `routine_time` — exact fațetele derivate pe catalogul real."""
    monkeypatch.setattr(ct, "_projection_on", lambda: True)
    v = _brief([_prod()], _pack(), "ro")
    assert "Moment din rutina: am, pm" in v
    assert "Tip de ten: dry" in v
    assert "Potrivit pentru: hydration" in v
    assert "SPF" not in v  # a patra fațetă nu încape în buget (max 3)
    assert not v.rstrip().endswith("|")  # fără separator gol când `ai_summary` lipsește


def test_detail_randeaza_sectiunile_declarate_de_pachet_in_ordine(monkeypatch):
    monkeypatch.setattr(ct, "_projection_on", lambda: True)
    pack = _pack(
        detail_sections=(SectionSpec("summary", 60), SectionSpec("fit", 80), SectionSpec("usage"))
    )
    p = _prod(
        sections=[
            {"kind": "usage", "title": "Cum se foloseste", "body": "Aplica seara. Evita ochii."},
            {"kind": "storage", "title": "Depozitare", "body": "La loc uscat."},
            {
                "kind": "fit",
                "title": "Cui i se potrivește",
                "body": "Potrivita daca: • ai ten uscat",
            },
            {"kind": "summary", "title": "Pe scurt", "body": "Crema lejera. Hidrateaza intens."},
        ]
    )
    v = _detail_view(p, pack, "ro")
    assert "pe scurt: Crema lejera. Hidrateaza intens." in v
    assert "cui i se potrivește: Potrivita daca: • ai ten uscat" in v
    assert "cum se foloseste: Aplica seara. Evita ochii." in v
    assert "depozitare" not in v  # nedeclarat → nu ajunge la model
    assert v.index("pe scurt:") < v.index("cui i se potrivește:") < v.index("cum se foloseste:")


def test_detail_fara_declaratie_cade_pe_lista_istorica(monkeypatch):
    monkeypatch.setattr(ct, "_projection_on", lambda: True)
    p = _prod(
        sections=[
            {"kind": "warnings", "title": "De reținut", "body": "Evită zona ochilor."},
            {"kind": "summary", "title": "Pe scurt", "body": "Nu apare fără declarație."},
        ]
    )
    v = _detail_view(p, _pack(), "ro")
    assert "de reținut: Evită zona ochilor." in v
    assert "pe scurt" not in v


def test_detail_omite_badge_urile_de_zgomot_si_taie_citatul_la_propozitie(monkeypatch):
    monkeypatch.setattr(ct, "_projection_on", lambda: True)
    p = _prod(
        badges=["CPNP", "SOLE Exclusiv", "Cadou"],
        reviews_list=[
            {
                "author": "Ana",
                "rating": 5,
                "body": "Pielea e mai neteda si mai luminoasa dupa doar o saptamana. "
                "Am comandat pentru prima data de pe site si navigarea a fost usoara. "
                "Il voi recomanda tuturor prietenelor mele fara nicio retinere.",
            }
        ],
    )
    v = _detail_view(p, _pack(), "ro", noise_badges=frozenset({"CPNP", "Cadou"}))
    assert "etichete: SOLE Exclusiv" in v and "CPNP" not in v and "Cadou" not in v
    assert "dupa doar o saptamana." in v
    assert "fara nicio retinere" not in v  # peste plafon
    assert "dupa doar”" not in v  # nu mai taie la mijloc de propoziție


def test_compare_numeste_scurt_pe_axe(monkeypatch):
    monkeypatch.setattr(ct, "_projection_on", lambda: True)
    a = _prod(id="1", price=155.0)
    b = _prod(
        id="2",
        name="DR.JART+ Vital Hydra Solution, 150 ml - emulsie de fata cu acid hialuronic",
        price=179.0,
    )
    v = _compare_view([a, b], _pack(), "ro")
    assert "preț: ANUA Peach 77 Niacin Enriched Cream=155,00 lei vs DR.JART+ Vital Hydra" in v
    assert v.count("crema de fata cu niacinamida") == 1  # numele întreg doar în antet


# --- pachet: `detail_sections` din JSON -------------------------------------------------------


def test_norm_detail_sections_fail_safe_per_intrare():
    got = _norm_detail_sections(
        [
            {"kind": "summary", "max_chars": 240},
            {"kind": "fit"},
            {"kind": "summary", "max_chars": 10},  # duplicat → prima câștigă
            {"kind": "bad", "max_chars": 0},
            {"kind": "worse", "max_chars": True},
            {"max_chars": 5},
            "nu e dict",
        ]
    )
    assert got == (SectionSpec("summary", 240), SectionSpec("fit", SectionSpec.max_chars))
    assert _norm_detail_sections(None) == ()


def test_pachetul_ecommerce_declara_sectiunile_fisei():
    from src.domain.loader import load_domain_pack

    pack = load_domain_pack(BusinessConfig(id="b", slug="s", name="n", vertical="ecommerce"))
    kinds = [s.kind for s in pack.detail_sections]
    assert kinds[:3] == ["summary", "fit", "anti_fit"]
    assert "usage" in kinds and "features" in kinds  # și tipurile catalogului demo


# --- ingrediente-cheie din secțiune ------------------------------------------------------------


def test_parse_key_ingredients_pe_cele_trei_forme_reale():
    with_desc = (
        "Extract de yuja din Jeju - Ingredient bogat in\nvitamina C\nsi antioxidanti\n"
        "Extract de miere Manuka - Ingredient nutritiv\n"
        "Filtre UV moderne UVA si UVB - Complex de protectie 50%"
    )
    assert parse_key_ingredients(with_desc) == [
        "Extract de yuja din Jeju",
        "Extract de miere Manuka",
        "Filtre UV moderne UVA si UVB",
    ]
    split_by_links = (
        "Madecassoside - ingredient calmant\nDerivati de\nacid hialuronic\n- complex hidratant\n"
        "Betaina - agent hidratant\nPantenol\n- provitamina B5"
    )
    assert parse_key_ingredients(split_by_links) == [
        "Madecassoside",
        "Derivati de acid hialuronic",
        "Betaina",
        "Pantenol",
    ]
    # linkul rupe numele DUPĂ un cuvânt de legătură, iar partea legată e cu majusculă; dar
    # „Vitamina C" urmat de alt nume NU se lipește (majuscula „C" nu e cuvânt de legătură)
    link_after_preposition = "Extract de\nCentella Asiatica\nVitamina C\nGlicerina"
    assert parse_key_ingredients(link_after_preposition) == [
        "Extract de Centella Asiatica",
        "Vitamina C",
        "Glicerina",
    ]
    names_only = "Carbune activ\nGlicerina\nBetaina\nVezi mai multe detalii\nAscunde"
    assert parse_key_ingredients(names_only, drop=("Vezi mai multe detalii", "Ascunde")) == [
        "Carbune activ",
        "Glicerina",
        "Betaina",
    ]
    assert parse_key_ingredients(None) == []


def test_canonical_values_sunt_forma_pe_care_o_compara_filtrul():
    # minuscule, fără diacritice, fără procente: exact `normalize(termenul clientului)`
    assert canonical_values(["Niacinamidă 2%", "Acid Hialuronic", "niacinamida"]) == [
        "niacinamida",
        "acid hialuronic",
    ]


# --- brațul semantic stins: niciun embed, niciun checkout ------------------------------------


class _NoConn:
    """Providerul de DB al testului: orice checkout e o eroare — cu brațul stins nu are ce cere."""

    def __init__(self):
        self.ops: list[str] = []

    def __call__(self, operation="unlabeled"):
        self.ops.append(operation)
        raise AssertionError(f"checkout neașteptat: {operation}")


class _LLM:
    def __init__(self):
        self.embed_calls = 0

    async def embed(self, texts, *, model=None):
        self.embed_calls += 1
        return [[0.0] * 8 for _ in texts]


async def test_flag_stins_nu_face_embed_si_nu_verifica_embeddings(monkeypatch):
    get_settings().search_semantic_enabled = False
    ct.clear_embeddings_cache()
    llm = _LLM()

    async def _has_emb(conn, business_id):  # dacă e chemat, e defectul
        raise AssertionError("has_embeddings nu trebuie chemat cu brațul stins")

    async def _lexical(conn, business_id, **k):
        return [{"id": "p1", "name": "Crema A", "brand": "B", "price": 10.0}]

    monkeypatch.setattr(ct, "has_embeddings", _has_emb)
    monkeypatch.setattr(ct, "search_products_lexical", _lexical)

    from src.catalog.vocabulary import CatalogVocabulary

    async def _vocab(deps, business_id):
        return CatalogVocabulary(business_id=business_id)

    monkeypatch.setattr(ct, "get_vocabulary", _vocab)

    class _Provider:
        """Doar scara lexicală are voie să ceară o conexiune."""

        def __init__(self):
            self.ops: list[str] = []

        def __call__(self, operation="unlabeled"):
            self.ops.append(operation)
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _cm():
                yield object()

            return _cm()

    provider = _Provider()
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="crema"),
        conversation_id="conv",
    )
    res = await ct.search_products_tool(ctx, PipelineDeps(llm=llm, db=provider), {"query": "crema"})
    assert res.ok and [p["id"] for p in res.products] == ["p1"]
    assert llm.embed_calls == 0
    assert "has_embeddings" not in provider.ops
    ev = next(e for e in ctx.events if e.type == "product_search")
    assert ev.properties["mode"] == "lexical" and ev.properties["vector_pool"] == 0


async def test_has_embeddings_e_cache_uit_per_tenant(monkeypatch):
    get_settings().search_semantic_enabled = True
    ct.clear_embeddings_cache()
    calls = {"n": 0}

    async def _has_emb(conn, business_id):
        calls["n"] += 1
        return True

    monkeypatch.setattr(ct, "has_embeddings", _has_emb)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm(operation="unlabeled"):
        yield object()

    deps = PipelineDeps(db=_cm)
    assert await ct._embeddings_available(deps, "biz-1") is True
    assert await ct._embeddings_available(deps, "biz-1") is True
    assert await ct._embeddings_available(deps, "biz-2") is True
    assert calls["n"] == 2  # o dată per tenant, nu per căutare


# --- substitute: id-urile întâi, hidratarea după ------------------------------------------------


class _ScriptedConn:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return self.batches.pop(0)


async def test_get_substitutes_hidrateaza_doar_id_urile_din_relatii():
    from src.db.queries.catalog import get_substitutes

    conn = _ScriptedConn(
        [
            [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
            [
                {"id": "s1", "name": "A", "availability": "out_of_stock"},
                {"id": "s2", "name": "B", "availability": "in_stock"},
                {"id": "s3", "name": "C", "availability": None},
            ],
        ]
    )
    out = await get_substitutes(conn, "biz-1", "anchor", limit=2)
    assert [p["id"] for p in out] == ["s2", "s3"]  # epuizatul cade, necunoscutul rămâne
    first_sql, first_params = conn.calls[0]
    assert "from product_relations r" in first_sql and "business_id = $1" in first_sql
    assert "kind = 'substitute'" in first_sql and first_params[:2] == ("biz-1", "anchor")
    second_sql, second_params = conn.calls[1]
    assert "from products p" in second_sql and "product_relations" not in second_sql
    assert list(second_params[1]) == ["s1", "s2", "s3"]  # DOAR id-urile din relații


@pytest.fixture(autouse=True)
def _reset_semantic_flag():
    before = get_settings().search_semantic_enabled
    yield
    get_settings().search_semantic_enabled = before
    ct.clear_embeddings_cache()
