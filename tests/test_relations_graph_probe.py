"""NX-278 — proba de graf trebuie să poată rula pe graful pe care îl măsoară.

Contextul, fiindcă e ce dă sens testelor de mai jos: pe graful real `sole-ro` (37.082 de muchii)
proba CRĂPA cu `DiskFullError`. Nu disc plin — explozie combinatorie: interogarea enumera fiecare
drum din fiecare ancoră, iar cu grad de ieșire 6 și adâncime 6 asta înseamnă 6^6 = 46.656 de
drumuri per ancoră × ~2.500 de ancore ≈ 117 milioane de rânduri. O bază de 387 MB a produs 27 GB
de fișiere temporare.

Forma defectului contează mai mult decât cifra: proba trecea instantaneu pe un graf GOL. Cod
exersat într-un singur regim — cel în care nu era nimic de făcut.

ZERO DB pentru testele pure; cel de la final e `integration` (are nevoie de Postgres real) și
construiește exact graful care ar fi crăpat: ciclic ȘI ramificat.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "relations_graph_probe",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "relations_graph_probe.py",
)
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def _kind(**over):
    """Un rând de raport per tip, cu valorile neutre completate."""
    base = {
        "kind": "k",
        "edges": 1000,
        "anchors": 500,
        "chain_max_depth": 3,
        "anchors_with_chain_ge2": 300,
        "anchors_with_chain_ge3": 100,
        "anchors_hitting_probe_cap": 0,
        "cyclic_anchors": 0,
        "measured": {
            "anchor_sample": 400,
            "anchor_population": 500,
            "branch_cap": 3,
            "observed_max_out_degree": 3,
            "completeness": "sampled",
        },
    }
    base.update(over)
    return base


# ── Bugetul e DECLARAT, nu implicit ─────────────────────────────────────────


def test_bugetul_marginit_e_cu_ordine_de_marime_sub_cel_nemarginit():
    """Cifra care explică de ce proba crăpa: enumerarea crește ca PRODUS al evantaiului, nu ca
    sumă, deci un plafon doar pe adâncime nu mărginește nimic.

    Comparăm bugetul declarat al probei cu ce ar fi cerut graful real nemărginit (grad 6, 2.506
    ancore, adâncime 6). Pragul e generos deliberat: testul apără ORDINUL de mărime, nu o
    constantă care ar rugini la prima ajustare de default."""
    bounded = probe.DEFAULT_SAMPLE * (probe.DEFAULT_MAX_BRANCH**probe.DEFAULT_MAX_DEPTH)
    unbounded = 2506 * (6**6)
    assert bounded < unbounded / 100


def test_plafoanele_implicite_sunt_declarate_ca_nume_nu_ingropate_in_sql():
    """Un plafon scris direct în SQL n-ar putea fi nici raportat, nici schimbat de la linia de
    comandă — iar o măsurătoare al cărei buget nu se vede nu poate fi judecată."""
    assert probe.DEFAULT_SAMPLE > 0 and probe.DEFAULT_MAX_BRANCH > 0
    assert "$4" in probe._CHAIN_SQL and "$5" in probe._CHAIN_SQL  # evantai + eșantion
    assert "with recursive" in probe._CHAIN_SQL.lower()
    assert "cycle" in probe._CHAIN_SQL.lower()  # garda de ciclu nu s-a pierdut la rescriere


def test_niciun_tip_de_muchie_nu_e_numit_in_cod():
    """Proprietatea pe care cardul cere s-o păstrăm: un instrument care întreabă „câte lanțuri
    `routine_next` există" e el însuși cuplat la un vertical, iar atunci instrumentul de măsură
    minte înaintea codului măsurat."""
    import ast

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "scripts" / "relations_graph_probe.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Docstringurile EXPLICĂ domeniul (util, și nu ajunge în comportament), iar comentariile nici
    # măcar nu apar în AST. Ne uităm la string-urile care chiar ajung în cod — inclusiv SQL-ul,
    # care e exact locul unde un tip de muchie hardcodat ar face instrumentul să mintă.
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    identifiers = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
    haystack = "\n".join([*literals, *identifiers])
    for vertical_word in ("routine_next", "complement", "substitute", "variant_of"):
        assert vertical_word not in haystack, f"tip de muchie numit în cod: {vertical_word}"


# ── Completitudinea: ce fel de cifră a ieșit ────────────────────────────────


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        (dict(sampled=500, population=500, branch_cap=6, max_out_degree=6), "exact"),
        (dict(sampled=400, population=500, branch_cap=6, max_out_degree=6), "sampled"),
        (dict(sampled=500, population=500, branch_cap=3, max_out_degree=6), "branch_capped"),
        (
            dict(sampled=400, population=500, branch_cap=3, max_out_degree=6),
            "sampled+branch_capped",
        ),
    ],
)
def test_cele_doua_surse_de_incompletitudine_se_raporteaza_separat(kw, expected):
    """Nu un singur bit „aproximativ": eșantionul face din numărători ESTIMĂRI (corectabile
    statistic), iar plafonul de evantai face din adâncime o LIMITĂ INFERIOARĂ (necorectabilă — un
    lanț mai lung poate trece exact prin ramura tăiată). Consumatorul trebuie să știe care."""
    assert probe._completeness(**kw) == expected


# ── Clasificarea nu are voie să mintă pe un eșantion ────────────────────────


def test_pragul_absolut_se_compara_cu_extrapolarea_pesimista():
    """Pragul e „câte ancore au lanț", dar numărătoarea vine dintr-un eșantion. Comparate direct,
    ar respinge un graf bun doar fiindcă am privit o parte din el.

    Aici: 300/400 din eșantion au lanț ≥2, populația e 5.000 ⇒ chiar capătul de JOS al intervalului
    Wilson trece pragul, deci traversarea se acordă."""
    k = _kind(
        anchors=5000,
        anchors_with_chain_ge2=300,
        measured={
            "anchor_sample": 400,
            "anchor_population": 5000,
            "branch_cap": 3,
            "observed_max_out_degree": 3,
            "completeness": "sampled",
        },
    )
    traversal, _, _ = probe._classify(k, 6)
    assert traversal == "chain"


def test_un_esantion_slab_nu_devine_o_certitudine():
    """Invers: 2 ancore cu lanț din 400 măsurate nu au voie să fie extrapolate în „destule".
    Wilson, nu Wald — Wald la capete produce certitudini care nu există."""
    k = _kind(
        anchors=1000,
        anchors_with_chain_ge2=1,
        anchors_with_chain_ge3=0,
        measured={
            "anchor_sample": 400,
            "anchor_population": 1000,
            "branch_cap": 3,
            "observed_max_out_degree": 3,
            "completeness": "sampled",
        },
    )
    traversal, depth, reason = probe._classify(k, 6)
    assert traversal == "neighbors-only" and depth == 1
    assert "Wilson" in reason  # motivul spune că cifra e extrapolată, nu numărată


def test_ciclurile_si_plafonul_probei_nu_mai_inseamna_acelasi_lucru():
    """Defectul de verdict pe care l-a scos la iveală rularea reală.

    Un tip cu cicluri trebuie mărginit (`bounded`). Un tip FĂRĂ niciun ciclu, ale cărui lanțuri
    ajung la plafonul probei, nu e o relație care trebuie mărginită — e una al cărei capăt nu l-am
    privit. Amestecate, `routine_next` (aciclic, 7.244 de muchii) primea `bounded 2` și rămânea
    netraversabil degeaba."""
    cyclic = _kind(cyclic_anchors=399, chain_max_depth=6, anchors_hitting_probe_cap=0)
    assert probe._classify(cyclic, 6)[0] == "bounded"

    deep = _kind(cyclic_anchors=0, chain_max_depth=6, anchors_hitting_probe_cap=171)
    traversal, depth, reason = probe._classify(deep, 6)
    assert traversal == "chain" and depth == 6
    assert "CEL PUȚIN" in reason  # limită inferioară declarată, nu cifră exactă (DoD 3)


def test_adancimea_e_declarata_ca_limita_inferioara_cand_evantaiul_a_fost_taiat():
    """DoD 3: ce nu s-a putut enumera complet nu iese ca o cifră exactă."""
    k = _kind(
        chain_max_depth=4,
        anchors_hitting_probe_cap=0,
        measured={
            "anchor_sample": 400,
            "anchor_population": 400,
            "branch_cap": 3,
            "observed_max_out_degree": 6,
            "completeness": "branch_capped",
        },
    )
    _, _, reason = probe._classify(k, 6)
    assert "cel puțin" in reason


# ── Graful care ar fi crăpat ────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.slow
async def test_un_graf_ciclic_si_ramificat_nu_mai_face_proba_sa_crape():
    """Singurul test care ar fi prins defectul: un graf CICLIC și RAMIFICAT, adică exact forma pe
    care proba veche o transforma în 27 GB de fișiere temporare.

    200 de noduri, fiecare cu 6 succesori, într-un inel — deci fiecare drum se poate continua la
    infinit. Nemărginită, enumerarea la adâncime 6 ar cere 200 × 6^6 ≈ 9,3 milioane de rânduri;
    mărginită, cel mult `sample × branch^depth`.
    """
    import uuid

    from src.db.connection import admin_conn, close_pool, get_pool

    # `product_relations.kind` are CHECK pe vocabular ÎNCHIS, deci un tip inventat („seq")
    # e respins de schemă. Alegem unul REAL, coerent cu graful pe care îl construim aici:
    # `complement` e singurul în care ciclurile sunt semantice, nu o eroare de date.
    # (Proba însăși rămâne agnostică de tip — asta o apără testul de mai sus.)
    _KIND = "complement"
    biz = str(uuid.uuid4())
    nodes = [str(uuid.uuid4()) for _ in range(200)]
    edges = [
        (biz, nodes[i], nodes[(i + step) % len(nodes)], _KIND)
        for i in range(len(nodes))
        for step in range(1, 7)
    ]
    # Muchiile de mai sus fac graful ciclic GLOBAL, dar nu și în adâncimea pe care o măsoară proba:
    # cu pași de cel mult 6 și 200 de noduri, șase salturi adună maximum 36 — prea puțin ca să te
    # întorci de unde ai plecat. Deci aserțiunea „graful trebuia să fie ciclic" n-avea cum să
    # treacă, iar asta n-a ieșit la iveală fiindcă testul nu apuca să ruleze (poarta de migrări
    # pica înaintea lui). Muchia inversă adaugă cicluri de lungime 2, vizibile la orice adâncime,
    # fără să schimbe evantaiul care produce explozia combinatorie.
    edges += [(biz, nodes[(i + 1) % len(nodes)], nodes[i], _KIND) for i in range(len(nodes))]
    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            # `products.business_id` are FK către `businesses`, deci tenantul sintetic trebuie
            # să EXISTE, nu doar să aibă un uuid. (Testul inventa un id și se baza pe faptul că
            # nimeni nu verifică — FK-ul verifică.)
            await conn.execute(
                "insert into businesses (id, name, slug) values ($1::uuid, 'probe', $2)",
                biz,
                f"probe-{biz[:8]}",
            )
            # Coloanele OBLIGATORII fără default ale lui `products` sunt exact patru:
            # `business_id`, `name`, `slug`, `price` (verificat în `information_schema`, nu ghicit).
            # Inserția le dădea doar pe primele două, deci testul crăpa de fiecare dată când
            # ajungea să ruleze — adică abia după ce poarta de migrări a încetat să pice înaintea
            # lui. `slug` se derivă din id: unic prin construcție, fără secvență separată.
            await conn.execute(
                "insert into products (id, business_id, name, slug, price, status)"
                " select n, $2::uuid, 'n', 'probe-' || n::text, 0, 'active'"
                " from unnest($1::uuid[]) as n",
                nodes,
                biz,
            )
            await conn.executemany(
                "insert into product_relations (business_id, product_id, related_id, kind)"
                " values ($1::uuid, $2::uuid, $3::uuid, $4)",
                edges,
            )
            report = await probe.measure(biz, 6, sample=50, max_branch=3)
        kinds = {k["kind"]: k for k in report["kinds"]}
        assert _KIND in kinds, "proba n-a văzut muchiile"
        assert kinds[_KIND]["cyclic_anchors"] > 0, "graful sintetic trebuia să fie ciclic"
        assert report["probe_budget"]["max_rows_per_kind"] == 50 * 3**6
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from product_relations where business_id = $1::uuid", biz)
            await conn.execute("delete from products where business_id = $1::uuid", biz)
            await conn.execute("delete from businesses where id = $1::uuid", biz)
        await close_pool()
