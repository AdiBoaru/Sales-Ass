"""NX-292 felia 2 — `routine_plan`: compunerea ajunge la model, cu goluri declarate.

Fără DB și fără LLM: query-urile de catalog sunt monkeypatch-uite, ca la `test_tools.py`. Ce se
testează e CONTRACTUL tool-ului — ordinea sloturilor, motivele golurilor, bugetul pe sumă, poarta
de siguranță și degradarea onestă — nu SQL-ul, care are sondele lui.
"""

from __future__ import annotations

import pytest

from src.domain.pack import DomainPack
from src.domain.routine_steps import build_spec
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import routine_tools as rt
from src.worker.runner import PipelineDeps

RAW = {
    "families": {
        "fata": ["curatare", "tonifiere", "tratament", "hidratare"],
        "par": ["spalare", "conditionare"],
    },
    "by_product_type": {"gel de curatare": "fata:curatare", "sampon": "par:spalare"},
}

#: Catalogul fals: `{id: (pas, preț)}`. Ordinea din listă = ordinea de ranking.
CATALOG = {
    "c1": ("fata:curatare", 40),
    "c2": ("fata:curatare", 25),
    "t1": ("fata:tonifiere", 60),
    "t2": ("fata:tonifiere", 30),
    "r1": ("fata:tratament", 200),
    "r2": ("fata:tratament", 90),
    "h1": ("fata:hidratare", 120),
    "h2": ("fata:hidratare", 55),
}


def _ctx(*, families: dict | None = None) -> TurnContext:
    business = BusinessConfig(id="b", slug="s", name="n", vertical="beauty")
    spec = build_spec({**RAW, "families": families} if families else RAW)
    business.domain_pack = DomainPack(vertical="beauty_salon", routine_steps=spec)
    return TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="vreau o rutina"),
        conversation_id="conv",
        language="ro",
    )


def _ctx_without_pack() -> TurnContext:
    business = BusinessConfig(id="b", slug="s", name="n", vertical="ecommerce")
    business.domain_pack = DomainPack(vertical="ecommerce")
    return TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
        language="ro",
    )


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    """Query-urile de catalog, scriptate. `available` e mutabil per test (ca să putem goli pași)."""
    state = {"available": set(CATALOG), "concern_only": set(CATALOG)}

    async def fake_candidates(
        conn, business_id, *, values, concerns=None, per_step=8, include_cheapest=False
    ):
        pool = state["concern_only"] if concerns else state["available"]
        rows = [
            {"id": pid, "step": step, "price": price}
            for pid, (step, price) in CATALOG.items()
            if step in set(values) and pid in pool
        ]
        state["include_cheapest"] = include_cheapest
        return rows[:per_step] if per_step < len(rows) else rows

    async def fake_hydrate(conn, business_id, ids, *, limit=6, respect_content_status=False):
        return [
            {"id": pid, "name": f"Produs {pid} - crema formulata cu ceva", "price": CATALOG[pid][1]}
            for pid in ids
            if pid in CATALOG
        ]

    monkeypatch.setattr(rt, "routine_candidates", fake_candidates)
    monkeypatch.setattr(rt, "get_products_by_ids", fake_hydrate)
    monkeypatch.setattr(rt, "_safety_gate", lambda ctx, products, purpose: (products, ""))
    return state


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


async def _run(ctx, **args):
    payload = {"family": "fata", "concerns": [], "budget_max": None, "anchor_id": None, **args}
    return await rt.routine_plan_tool(ctx, _deps(), payload)


# ── Forma răspunsului ───────────────────────────────────────────────────────────────────────────


async def test_rutina_completa_in_ordinea_pasilor():
    result = await _run(_ctx())

    assert result.ok
    assert [p["id"] for p in result.products] == ["c1", "t1", "r1", "h1"]
    # Numerotarea e a SERVERULUI: dacă modelul ar deduce ordinea din listă, un pas lipsă ar
    # renumerota tăcut restul, iar „pasul 3" din conversație n-ar mai fi „pasul 3" la turul următor.
    assert "1. curatare" in result.llm_view
    assert "4. hidratare" in result.llm_view


async def test_pasul_fara_produs_e_declarat_nu_sarit(_catalog):
    _catalog["available"] -= {"t1", "t2"}

    result = await _run(_ctx())

    assert result.ok  # 3 din 4 pași: se poate prezenta onest
    assert "2. tonifiere — LIPSĂ" in result.llm_view
    assert "3. tratament" in result.llm_view  # restul NU se renumerotează
    assert "nu inventa unul și nu renumerota restul" in result.llm_view


async def test_vederea_numeste_pasul_nu_doar_produsul():
    """Fără numele pasului, modelul are iar o listă plată și trebuie să ghicească ce e fiecare
    produs — exact eroarea pe care tool-ul o repară."""
    view = (await _run(_ctx())).llm_view

    for step in ("curatare", "tonifiere", "tratament", "hidratare"):
        assert step in view


# ── Motivele golurilor: `no_candidate` ≠ `filtered` ─────────────────────────────────────────────


async def test_nevoia_care_goleste_un_pas_da_filtered(_catalog):
    """Există produse pe pas, dar niciunul pentru nevoia cerută. `filtered` și `no_candidate` cer
    reparații diferite (una e a catalogului, alta e a potrivirii)."""
    _catalog["concern_only"] = set(CATALOG) - {"t1", "t2"}

    result = await _run(_ctx(), concerns=["hydration"])

    assert "2. tonifiere — LIPSĂ (filtered)" in result.llm_view


async def test_pasul_inexistent_in_catalog_da_no_candidate(_catalog):
    _catalog["available"] -= {"t1", "t2"}
    _catalog["concern_only"] -= {"t1", "t2"}

    result = await _run(_ctx(), concerns=["hydration"])

    assert "2. tonifiere — LIPSĂ (no_candidate)" in result.llm_view


async def test_sonda_de_motiv_nu_relaxeaza_nevoia(_catalog):
    """A doua interogare (fără nevoi) e DOAR ca să aflăm motivul. Dacă rezultatul ei ar intra în
    candidați, o nevoie s-ar relaxa tăcut — adică exact ce interzice D7."""
    _catalog["concern_only"] = set(CATALOG) - {"t1", "t2"}

    result = await _run(_ctx(), concerns=["hydration"])

    assert all(p["id"] not in {"t1", "t2"} for p in result.products)


# ── Bugetul ─────────────────────────────────────────────────────────────────────────────────────


async def test_bugetul_nu_taie_pasi_si_spune_minimul():
    """Podeaua (25+30+90+55 = 200) depășește bugetul cerut. Rutina rămâne întreagă, la minim, iar
    vederea spune cifra: un client care cere «sub 150» și primește doi pași, fără explicație, crede
    că atâtea există."""
    result = await _run(_ctx(), budget_max=150)

    assert result.ok
    assert [p["id"] for p in result.products] == ["c2", "t2", "r2", "h2"]
    assert "Cel mai ieftin se face cu 200,00 lei" in result.llm_view


async def test_bugetul_urca_pasii_cat_incape():
    """Podeaua e 200, bugetul 240: primii pași urcă spre candidatul mai bine cotat cât timp încape.
    Un plafon PER PRODUS ar fi răspuns la altă întrebare decât a pus clientul."""
    result = await _run(_ctx(), budget_max=240)

    total = sum(CATALOG[p["id"]][1] for p in result.products)
    assert total <= 240
    assert [p["id"] for p in result.products] != ["c2", "t2", "r2", "h2"]  # a urcat ceva


async def test_fara_buget_se_aleg_cei_mai_bine_cotati():
    result = await _run(_ctx())

    assert [p["id"] for p in result.products] == ["c1", "t1", "r1", "h1"]


async def test_bugetul_cere_si_candidatii_ieftini_din_catalog(_catalog):
    """Pool-ul de candidați e ales pe RANG. Fără cererea explicită a celor mai ieftini, minimul
    raportat e minimul celor bine cotați, nu al catalogului.

    Găsit pe date REALE, nu presupus: pe catalogul SOLE, «rutină de față sub 200 lei» raporta
    minimul 291 (cei mai bine cotați opt de pe fiecare pas sunt scumpi), deși o rutină completă de
    187 există. Un «nu se poate sub 200» fals închide vânzarea cu o cifră inventată de selecția
    noastră de candidați — și trece de validator, fiindcă toate prețurile sunt reale."""
    await _run(_ctx(), budget_max=150)
    assert _catalog["include_cheapest"] is True

    await _run(_ctx())
    assert _catalog["include_cheapest"] is False  # fără buget, pool-ul rămâne cel de dinainte


# ── Degradare onestă ────────────────────────────────────────────────────────────────────────────


async def test_sub_doi_pasi_nu_e_rutina(_catalog):
    _catalog["available"] = {"c1"}

    result = await _run(_ctx())

    assert not result.ok
    assert result.error == "no_sequence"
    assert "nu inventa pași" in result.llm_view


async def test_familie_necunoscuta_numeste_familiile_reale():
    result = await _run(_ctx(), family="unghii")

    assert not result.ok
    assert result.error == "unknown_family"
    assert "fata" in result.llm_view and "par" in result.llm_view


async def test_tenant_fara_rutine_declarate():
    result = await _run(_ctx_without_pack())

    assert not result.ok
    assert result.error == "no_routine_support"
    assert "Recomandă produse, nu pași" in result.llm_view


# ── Siguranță (NX-173) ──────────────────────────────────────────────────────────────────────────


async def test_produsul_contraindicat_e_inlocuit_nu_lasat(monkeypatch):
    """Poarta de siguranță rulează pe produsele HIDRATATE (are nevoie de ingrediente), deci după
    alegere. Un pas al cărui produs cade primește următorul candidat — nu rămâne gol dacă are pe
    ce cădea, și nu se înlocuiește tăcut cu ceva din alt pas."""
    monkeypatch.setattr(
        rt,
        "_safety_gate",
        lambda ctx, products, purpose: ([p for p in products if p["id"] != "r1"], ""),
    )

    result = await _run(_ctx())

    ids = [p["id"] for p in result.products]
    assert "r1" not in ids
    assert "r2" in ids
    assert len(ids) == 4  # rutina rămâne întreagă


async def test_pasul_ramane_gol_daca_si_inlocuitorul_cade(monkeypatch, _catalog):
    """O singură rundă de re-alegere. Dacă și al doilea candidat e contraindicat, pasul se declară
    neacoperit — o buclă nemărginită ar plăti hidratări la infinit pe un tur."""
    _catalog["available"] = {"c1", "t1", "r1", "r2", "h1"}
    monkeypatch.setattr(
        rt,
        "_safety_gate",
        lambda ctx, products, purpose: ([p for p in products if not p["id"].startswith("r")], ""),
    )

    result = await _run(_ctx())

    assert all(not p["id"].startswith("r") for p in result.products)
    assert "3. tratament — LIPSĂ" in result.llm_view


# ── Poarta de boot ──────────────────────────────────────────────────────────────────────────────


def test_routine_enabled_cere_single_brain():
    """Profilul și unealta se atașează pe promptul MainBrain. Aprins singur, `ROUTINE_ENABLED` ar
    fi un flag care nu poate face nimic — și, mai rău, ar sugera în config o capabilitate care nu
    rulează.

    Validatorul se apelează DIRECT pe un obiect construit, nu prin `Settings()`: construcția reală
    citește `.env`-ul mașinii, deci testul ar pica din cauza altui flag și ar raporta altceva decât
    măsoară."""
    from src.config import Settings

    obj = Settings.model_construct(routine_enabled=True, single_brain_enabled=False)
    with pytest.raises(ValueError, match="ROUTINE_ENABLED cere SINGLE_BRAIN_ENABLED"):
        Settings._web_turn_relations(obj)

    ok = Settings.model_construct(routine_enabled=True, single_brain_enabled=True)
    assert Settings._web_turn_relations(ok) is ok


# ── Ancora de graf ──────────────────────────────────────────────────────────────────────────────


async def test_ancora_din_graf_bate_fateta(monkeypatch):
    """Graful e autoritatea pe FORMĂ: un hop spune „aici urmează un pas", iar CARE pas se citește
    din fațeta produsului-țintă (muchiile țintesc un reprezentant al categoriei, nu un produs)."""
    from src.domain.relation_kinds import load_relation_kinds

    ctx = _ctx()
    ctx.business.domain_pack = DomainPack(
        vertical="beauty_salon",
        routine_steps=build_spec(RAW),
        # `ordered: true` e obligatoriu, nu implicit din `mode` — `sequences()` filtrează pe el, iar
        # pachetul REAL al tenantului îl declară explicit. Fără el, seed-ul n-ar rula deloc.
        relation_kinds=load_relation_kinds(
            {"routine_next": {"mode": "chain", "ordered": True, "max_depth": 4}}
        ),
    )

    async def fake_chain(conn, business_id, *, anchor_id, kind, max_depth):
        return [{"id": "t2"}]

    async def fake_steps(conn, business_id, ids):
        return {pid: CATALOG[pid][0] for pid in ids if pid in CATALOG}

    monkeypatch.setattr(rt, "traverse_relation_chain", fake_chain)
    monkeypatch.setattr(rt, "routine_steps_of", fake_steps)

    result = await _run(ctx, anchor_id="c1")

    ids = [p["id"] for p in result.products]
    assert ids[1] == "t2"  # ancora a impus pasul de tonifiere, nu rankingul
    assert ids[0] == "c1"  # ancora însăși rămâne pe pasul ei


async def test_fara_muchii_declarate_ancora_e_ignorata_nu_ghicita():
    """Pachetul din `_ctx()` n-are `relation_kinds` ordonate. Rutina se compune oricum, din fațetă:
    condiția profilului e pe FAMILII, nu pe muchii."""
    result = await _run(_ctx(), anchor_id="c1")

    assert result.ok
    assert len(result.products) == 4
