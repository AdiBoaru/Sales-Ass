"""NX-270 — graful de relații: o muchie e o afirmație pe care nimeni din aval n-o verifică.

Validatorul stagiului 8 verifică prețul, `grounding_guard` (NX-240) verifică afirmațiile din text.
Muchia „astea două sunt alternative" nu e nici preț, nici text — deci testele astea și motivul scris
pe muchie sunt singurele locuri unde greșeala mai poate fi văzută.

Cazurile din card sunt marcate (happy 1-2, edge 3-5, failure 6-7)."""

from __future__ import annotations

import json
import pathlib
import re

from src.db.queries.catalog import COMPLEMENTARY_KINDS
from src.domain.relation_kinds import TraversalMode, load_relation_kinds
from src.jobs.build_relations import (
    MAX_EDGES_PER_ANCHOR,
    PRICE_BAND,
    RULE_SUBSTITUTE,
    SOURCE_DERIVED,
    WRITTEN_KINDS,
    BuildReport,
    build_all,
    build_complements,
    build_routine,
    build_substitutes,
    need_rarity,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "docs" / "048_relation_provenance.sql"
PACK = ROOT / "db" / "seed" / "domain_pack_sole_ro.json"


def _p(pid, *, cat="creme", price=100.0, stock=True, needs=(), brand="b1", rating=4.5, reviews=50):
    return {
        "id": pid,
        "category_id": cat,
        "brand_id": brand,
        "availability": "in_stock" if stock else "out_of_stock",
        "price": price,
        "rating": rating,
        "review_count": reviews,
        "attributes": {"concerns": list(needs)} if needs else {},
    }


def _edges(report, kind=None):
    return [e for e in report.edges if kind is None or e.kind == kind]


# --- happy path ---------------------------------------------------------------------------------


def test_epuizat_cu_nevoie_primeste_substitut_cu_motiv():
    """Happy 1 — aceeași categorie, nevoie comună, preț în bandă, și motivul e SCRIS pe muchie."""
    report = build_all(
        [
            _p("epuizat", price=100, stock=False, needs=["hydration"]),
            _p("alternativa", price=110, needs=["hydration", "redness"]),
        ]
    )
    (edge,) = _edges(report, "substitute")
    assert (edge.product_id, edge.related_id) == ("epuizat", "alternativa")
    assert edge.reason["shared_needs"] == ["hydration"]
    assert edge.reason["same_category"] is True
    assert abs(edge.reason["price_delta_pct"] - 0.1) < 1e-9
    assert edge.rule_id == RULE_SUBSTITUTE and edge.source == SOURCE_DERIVED


def test_lantul_de_rutina_are_pasi_ordonati():
    """Happy 2 — curățare → tonic → tratament, ca muchii ordonate.

    Pașii referă TIPURI de produs, nu produse anume, deci se instanțiază pe un reprezentant, iar
    motivul o spune: `representative: true`. Fără asta, muchia ar afirma „exact produsul ăsta
    urmează", ceea ce conținutul nu susține."""
    products = [
        _p("cleanser", cat="curatare"),
        _p("tonic", cat="tonice"),
        _p("tratament", cat="tratamente"),
    ]
    report = BuildReport()
    build_routine(products, {"cleanser": ["tonice", "tratamente"]}, report)
    steps = sorted(_edges(report, "routine_next"), key=lambda e: e.position)
    assert [e.related_id for e in steps] == ["tonic", "tratament"]
    assert [e.reason["step"] for e in steps] == [1, 2]
    assert all(e.reason["representative"] is True for e in steps)


def test_complementul_cere_categorie_DIFERITA():
    """Complementul e „merge cu", nu „în loc de". Aceeași categorie + nevoie comună = substitut."""
    report = build_all(
        [
            _p("ser", cat="seruri", needs=["hydration"]),
            _p("crema", cat="creme", needs=["hydration"]),
            _p("alt_ser", cat="seruri", needs=["hydration"]),
        ]
    )
    complement = {(e.product_id, e.related_id) for e in _edges(report, "complement")}
    assert ("ser", "crema") in complement
    assert ("ser", "alt_ser") not in complement  # aceeași categorie → substitut, nu complement


def test_variantele_se_leaga_prin_grupul_de_nuanta():
    """`variant_of` din NX-269. Muchia asta e motivul pentru care migrarea 048 trebuia să vină
    ÎNAINTEA jobului: CHECK-ul de la 027 admitea patru valori."""
    a = _p("n1")
    b = _p("n2")
    a["attributes"] = {"shade_group": "g1", "shade": "116 Candid"}
    b["attributes"] = {"shade_group": "g1", "shade": "117 Harmony"}
    report = build_all([a, b])
    variants = _edges(report, "variant_of")
    assert {(e.product_id, e.related_id) for e in variants} == {("n1", "n2"), ("n2", "n1")}
    assert variants[0].reason["shade_group"] == "g1"


# --- edge ---------------------------------------------------------------------------------------


def test_epuizat_singur_in_categoria_lui_nu_e_impins_pe_alt_raft():
    """Edge 3 — zero muchii, RAPORTAT, nu forțat spre altă categorie."""
    report = build_all(
        [
            _p("singur", cat="unica", price=100, stock=False, needs=["hydration"]),
            _p("altceva", cat="alta", price=100, needs=["hydration"]),
        ]
    )
    assert _edges(report, "substitute") == []
    assert report.anchors_without_edges == ["singur"]


def test_pas_de_rutina_fara_stoc_lipseste_iar_lantul_continua():
    """Edge 4 — P6: un pas fără marfă lipsește din prezentare, nu rupe lanțul."""
    products = [
        _p("cleanser", cat="curatare"),
        _p("tonic_epuizat", cat="tonice", stock=False),
        _p("tratament", cat="tratamente"),
    ]
    report = BuildReport()
    build_routine(products, {"cleanser": ["tonice", "tratamente"]}, report)
    steps = _edges(report, "routine_next")
    assert [e.related_id for e in steps] == ["tratament"]  # pasul lipsă e sărit, nu blochează


def test_plafonul_de_muchii_e_respectat_si_determinist():
    """Edge 5 — 40 de vecini eligibili → exact 6 muchii, alese la fel la fiecare rulare.

    Fără plafon, prima pagină ar fi aleasă de ordinea din DB; fără determinism, „a doua rulare nu
    schimbă niciun rând" ar fi noroc."""
    anchor = _p("ancora", price=100, stock=False, needs=["hydration"])
    others = [_p(f"p{i:02d}", price=100 + i % 10, needs=["hydration"]) for i in range(40)]
    first = BuildReport()
    build_substitutes([anchor, *others], first)
    second = BuildReport()
    build_substitutes([anchor, *others], second)

    def _for_anchor(report):
        return [e.related_id for e in _edges(report, "substitute") if e.product_id == "ancora"]

    assert len(_for_anchor(first)) == MAX_EDGES_PER_ANCHOR
    assert _for_anchor(first) == _for_anchor(second)


def test_pretul_in_afara_benzii_nu_e_substitut():
    """Un „substitut" la jumătate de preț e alt segment, unul la dublu e un upsell deghizat."""
    report = build_all(
        [
            _p("ancora", price=100, stock=False, needs=["hydration"]),
            _p("prea_scump", price=100 * (1 + PRICE_BAND) + 1, needs=["hydration"]),
        ]
    )
    assert _edges(report, "substitute") == []


# --- failure ------------------------------------------------------------------------------------


def test_ancora_fara_nevoi_nu_cade_pe_acelasi_raft():
    """Failure 7 — cea mai importantă regulă a cardului. Fără nevoie comună, „substitutul" ar fi
    „altă cremă de 90 de lei din același raft", adică o resemnare prezentată ca recomandare.

    Măsurat pe catalogul real: 234 din cele 391 de produse epuizate n-au NICIO nevoie derivată.
    Plafonul grafului e acoperirea faptelor (NX-268), nu regula de aici — iar cifra din card
    („381 au un candidat în ±30% preț") a fost măsurată fără cerința de nevoie: măsura raftul."""
    report = build_all(
        [
            _p("fara_nevoi", price=100, stock=False),
            _p("acelasi_raft", price=100, needs=["hydration"]),
        ]
    )
    assert _edges(report, "substitute") == []
    assert report.out_of_stock_without_needs == ["fara_nevoi"]
    # și NU e numărat ca „fără alternativă în catalog": sunt două cauze diferite
    assert report.anchors_without_edges == []


def test_nicio_muchie_nu_se_leaga_de_ea_insasi():
    """CHECK-ul din schemă o interzice, dar un insert care crapă e o eroare de producție. Se
    verifică aici, unde e ieftin."""
    report = build_all(
        [
            _p("a", needs=["hydration"], stock=False),
            _p("b", needs=["hydration"]),
        ]
    )
    assert all(e.product_id != e.related_id for e in report.edges)


def test_fiecare_muchie_are_provenance_nevida():
    """DoD — `source`, `rule_id` și `reason` nevide pe FIECARE muchie. Fără ele, o regulă greșită
    n-ar putea fi ștearsă global, iar afirmația muchiei n-ar putea fi explicată nimănui."""
    report = build_all(
        [
            _p("a", cat="c1", needs=["hydration"], stock=False),
            _p("b", cat="c1", needs=["hydration"]),
            _p("c", cat="c2", needs=["hydration"]),
        ]
    )
    assert report.edges
    for edge in report.edges:
        assert edge.source and edge.rule_id and edge.reason


def test_derivarea_e_determinista():
    """A doua rulare produce exact aceleași muchii — condiția fără de care „idempotent" e noroc."""
    products = [
        _p("a", cat="c1", needs=["hydration"], stock=False),
        _p("b", cat="c1", needs=["hydration"]),
        _p("c", cat="c2", needs=["hydration"]),
    ]
    first = build_all(products)
    second = build_all(products)
    key = lambda r: sorted(  # noqa: E731
        (e.product_id, e.related_id, e.kind, e.position) for e in r.edges
    )
    assert key(first) == key(second)


# --- vocabularul de `kind`: TREI locuri care trebuie să coincidă ---------------------------------


def _schema_kinds() -> set[str]:
    """Valorile din CHECK-ul migrării 048, citite din SQL."""
    sql = MIGRATION.read_text(encoding="utf-8")
    # `add constraint`, nu orice `check (kind in ...)`: antetul migrării conține și ROLLBACK-ul,
    # care reface vocabularul VECHI. Un regex care ia prima potrivire ar testa exact ce dezinstalăm.
    match = re.search(r"add constraint\s+\w+\s+check \(kind in \(([^)]*)\)\)", sql)
    assert match, "CHECK-ul de `kind` nu se mai găsește în `add constraint`"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def _pack_kinds() -> set[str]:
    raw = json.loads(PACK.read_text(encoding="utf-8"))["relation_kinds"]
    return set(load_relation_kinds(raw).specs)


def test_vocabularul_de_kind_coincide_in_trei_locuri():
    """DoD — schema, pachetul și jobul. Două care coincid și una care minte e EXACT clasa de defect
    a lui `messages.content_type = 'action'` (NX-236): schema respingea o valoare pe care codul o
    scria, iar defectul a stat ascuns până a rulat gate-ul E2E pe Postgres real, fiindcă flagul era
    stins și suitele foloseau monkeypatch în loc de DB.

    Aici sunt trei locuri, nu două, fiindcă un tip nedeclarat în pachet NU crapă — cade tăcut pe
    comportamentul implicit (vecini direcți). Diferența dintre „am declarat" și „rulează" ar fi
    invizibilă."""
    schema, pack, job = _schema_kinds(), _pack_kinds(), set(WRITTEN_KINDS)
    assert job <= schema, f"jobul scrie tipuri pe care schema le refuză: {sorted(job - schema)}"
    assert job <= pack, f"jobul scrie tipuri nedeclarate în pachet: {sorted(job - pack)}"
    assert pack == schema, f"pachetul și schema diverg: {sorted(pack ^ schema)}"


def test_variant_of_e_citit_de_un_consumator_real():
    """DoD — o muchie scrisă corect și necitită de nimeni e a treia formă a aceleiași greșeli.

    `COMPLEMENTARY_KINDS` e lista pe care o folosesc AMBELE query-uri ale căii de complementaritate
    („are relații?" și „care sunt?"); înainte era scrisă de două ori, ca literale."""
    assert "variant_of" in COMPLEMENTARY_KINDS
    # `substitute` NU e acolo: un înlocuitor nu e un „merge bine cu", și are calea lui.
    assert "substitute" not in COMPLEMENTARY_KINDS


def test_toate_tipurile_scrise_sunt_declarate_cu_un_mod():
    """Un tip fără mod declarat cade pe „vecini direcți" — corect ca default, dar dacă jobul scrie
    lanțuri (`routine_next`) pe un tip căzut pe default, traversarea nu le va urma niciodată."""
    specs = load_relation_kinds(
        json.loads(PACK.read_text(encoding="utf-8"))["relation_kinds"]
    ).specs
    assert specs["routine_next"].mode is TraversalMode.CHAIN
    assert specs["substitute"].mode is TraversalMode.BOUNDED
    assert specs["variant_of"].mode is TraversalMode.NEIGHBORS


def test_migrarea_extinde_checkul_si_adauga_provenance():
    """Migrarea trebuie să facă amândouă lucrurile: fără coloane, muchia n-are provenance; fără
    CHECK extins, insertul cu `variant_of` crapă în producție."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for column in ("source", "rule_id", "reason"):
        assert f"add column if not exists {column}" in sql
    assert "variant_of" in _schema_kinds()


# --- NX-276: raritatea nevoii comune decide ordinea -----------------------------------------------


def test_o_nevoie_purtata_de_tot_catalogul_nu_informeaza():
    """DoD 4a — ponderea e `log(N/df)`, deci o nevoie universală valorează zero.

    Asta e toată ideea cardului: `hydration` la 69,9% din catalog nu deosebește doi candidați, dar
    `build_substitutes` o trata identic cu `dandruff` la 0,4%."""
    products = [_p(f"p{i}", needs=("universala",)) for i in range(10)]
    products.append(_p("rar", needs=("universala", "rara")))
    rarity = need_rarity(products)

    assert rarity.weight["universala"] == 0.0
    assert rarity.weight["rara"] > 0.0
    # Suma peste nevoile comune: nevoia rară e singura care mișcă scorul.
    assert rarity.score(["universala"]) == 0.0
    assert rarity.score(["universala", "rara"]) == rarity.score(["rara"])


def test_nevoia_rara_bate_ratingul_mai_bun_pe_o_nevoie_comuna():
    """DoD 4b — cazul real `ACROPASS`: singurul candidat care împarte `acne` cu ancora ieșea pe
    locul 3, fiindcă ceilalți doi aveau rating mai bun pe `hydration`."""
    anchor = _p("ancora", price=100.0, stock=False, needs=("comuna", "rara"))
    # Rating maxim, dar împarte doar nevoia comună.
    bun_dar_generic = _p("generic", price=105.0, needs=("comuna",), rating=5.0, reviews=500)
    # Rating slab, dar împarte nevoia rară.
    slab_dar_potrivit = _p("potrivit", price=95.0, needs=("comuna", "rara"), rating=3.5, reviews=10)
    # Zgomotul care face `comuna` comună și `rara` rară.
    umplutura = [_p(f"u{i}", needs=("comuna",)) for i in range(30)]

    report = BuildReport()
    build_substitutes([anchor, bun_dar_generic, slab_dar_potrivit, *umplutura], report)
    edges = [e for e in _edges(report, "substitute") if e.product_id == "ancora"]

    assert edges[0].related_id == "potrivit", "nevoia rară trebuie să bată rating-ul"
    assert edges[0].reason["match_strength"]["rarest_need"] == "rara"
    assert edges[0].reason["match_strength"]["score"] > edges[1].reason["match_strength"]["score"]


def test_match_strength_poarta_si_cat_de_comuna_e_nevoia():
    """`score` singur nu se poate citi de un om: „1,7" nu spune nimic. `rarest_share` spune „nevoia
    asta o poartă 4% din catalog", care e verificabil contra bazei."""
    anchor = _p("a", price=100.0, needs=("comuna", "rara"))
    other = _p("b", price=100.0, needs=("comuna", "rara"))
    umplutura = [_p(f"u{i}", needs=("comuna",)) for i in range(18)]

    report = BuildReport()
    build_substitutes([anchor, other, *umplutura], report)
    strength = _edges(report, "substitute")[0].reason["match_strength"]

    assert strength["rarest_need"] == "rara"
    assert 0.0 < strength["rarest_share"] < 0.2
    # Rotunjit, ca `reason` să nu difere între rulări (upsertul compară conținutul).
    assert strength["score"] == round(strength["score"], 4)


def test_ordinea_e_determinista_intre_rulari():
    """DoD 4c — două treceri pe aceleași date aleg aceiași șase, în aceeași ordine. Fără asta,
    fiecare rulare ar rescrie muchii identice și idempotența ar fi o iluzie."""
    products = [
        _p("ancora", price=100.0, needs=("c", "r")),
        *[
            _p(f"cand{i}", price=100.0 + i, needs=("c", "r") if i % 3 == 0 else ("c",))
            for i in range(12)
        ],
        *[_p(f"u{i}", needs=("c",)) for i in range(20)],
    ]
    first, second = BuildReport(), BuildReport()
    build_substitutes(products, first)
    build_substitutes(products, second)

    def key(report):
        return [(e.product_id, e.related_id, e.position) for e in _edges(report, "substitute")]

    assert key(first) == key(second)


def test_raritatea_nu_scade_numarul_de_muchii():
    """Ordinea decide CARE șase se scriu, nu câte. O scădere ar însemna că sortarea a pierdut
    candidați, nu că i-a re-ordonat."""
    products = [
        _p("ancora", price=100.0, needs=("c", "r")),
        *[_p(f"cand{i}", price=100.0, needs=("c", "r") if i % 2 else ("c",)) for i in range(10)],
    ]
    report = BuildReport()
    build_substitutes(products, report)
    anchor_edges = [e for e in _edges(report, "substitute") if e.product_id == "ancora"]
    assert len(anchor_edges) == MAX_EDGES_PER_ANCHOR


def test_complementele_raman_neatinse():
    """Non-regresie: `_rank` e partajat, deci schimbarea lui ar re-ordona tăcut și complementele —
    tip pe care NX-276 nu l-a măsurat. Raritatea intră DOAR în cheia de sortare a substituților."""
    products = [
        _p("a", cat="creme", needs=("c", "r")),
        _p("b", cat="seruri", needs=("c",), rating=5.0, reviews=500),
        _p("d", cat="seruri", needs=("c", "r"), rating=3.0, reviews=10),
    ]
    report = BuildReport()
    build_complements(products, report)
    order = [e.related_id for e in _edges(report, "complement") if e.product_id == "a"]
    # Ordinea complementelor rămâne cea de rating: „b" (5.0) înaintea lui „d" (3.0).
    assert order == ["b", "d"]


def test_niciun_nume_de_nevoie_nu_apare_in_cod():
    """Poarta NX-264, asertată local: dacă `hydration` ar apărea în `build_relations.py`, jobul ar
    fi cuplat la un vertical de beauty, iar pe alt catalog raritatea ar minți."""
    source = (ROOT / "src" / "jobs" / "build_relations.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    # Docstringurile citează măsurătoarea, deci verificăm codul executabil, fără ele.
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    for need in ("hydration", "acne", "dandruff", "redness"):
        assert need not in code, f"`{need}` e vocabular de tenant, n-are ce căuta în cod"
