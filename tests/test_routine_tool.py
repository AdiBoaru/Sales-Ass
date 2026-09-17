"""NX-292 felia 2 — `routine_plan`: compunerea ajunge la model, cu goluri declarate.

Fără DB și fără LLM: query-urile de catalog sunt monkeypatch-uite, ca la `test_tools.py`. Ce se
testează e CONTRACTUL tool-ului — ordinea sloturilor, motivele golurilor, bugetul pe sumă, poarta
de siguranță și degradarea onestă — nu SQL-ul, care are sondele lui.
"""

from __future__ import annotations

import json

import pytest

from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
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

#: Vocabularul fals al tenantului. Are DOUĂ dimensiuni cu intenție: nevoia clientului „ten uscat"
#: e `skin_type`, nu `concerns` — exact separarea din NX-257 pe care tool-ul o ignora, filtrând
#: totul pe `concerns` și golind astfel fiecare pas al rutinei.
VOCAB = CatalogVocabulary(
    business_id="b",
    dimensions={
        "concerns": (VocabEntry(key="hydration", label="hidratare", count=40),),
        "skin_type": (VocabEntry(key="dry", label="ten uscat", count=30),),
    },
)

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


def _ctx(*, families: dict | None = None, **extra: object) -> TurnContext:
    business = BusinessConfig(id="b", slug="s", name="n", vertical="beauty")
    raw = {**RAW, **extra}
    if families:
        raw["families"] = families
    spec = build_spec(raw)
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
    state = {
        "available": set(CATALOG),
        "concern_only": set(CATALOG),
        "seen_filters": [],
    }

    async def fake_candidates(
        conn, business_id, *, values, facet_filters=None, per_step=8, include_cheapest=False
    ):
        state["seen_filters"].append(facet_filters)
        pool = state["concern_only"] if facet_filters else state["available"]
        rows = [
            {"id": pid, "step": step, "price": price}
            for pid, (step, price) in CATALOG.items()
            if step in set(values) and pid in pool
        ]
        state["include_cheapest"] = include_cheapest
        return rows[:per_step] if per_step < len(rows) else rows

    async def fake_vocabulary(deps, business_id):
        return VOCAB

    async def fake_hydrate(conn, business_id, ids, *, limit=6, respect_content_status=False):
        return [
            {"id": pid, "name": f"Produs {pid} - crema formulata cu ceva", "price": CATALOG[pid][1]}
            for pid in ids
            if pid in CATALOG
        ]

    monkeypatch.setattr(rt, "routine_candidates", fake_candidates)
    monkeypatch.setattr(rt, "get_vocabulary", fake_vocabulary)
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


# ── Nevoia clientului → cheie de catalog ────────────────────────────────────────────────────────
#
# Defectul pe care secțiunea asta o pinuiește a existat în producție și NU putea fi prins de
# testele de mai sus: ele monkeypatch-uiau query-ul, deci nu se uitau NICIODATĂ la ce anume îi
# trimite tool-ul. Măsurat pe `sole-ro` la 2026-09-16, «rutina pentru ten uscat» scotea toți cei
# șase pași ai familiei `fata` ca `LIPSĂ (filtered)` — pe un catalog unde fiecare pas are peste o
# sută de produse vandabile. Garda e deci pe ARGUMENTELE primite de query, nu pe răspuns.


async def test_nevoia_scrisa_de_client_ajunge_canonica_la_query(_catalog):
    """Modelul trimite cuvintele clientului („hidratare"); query-ul trebuie să primească cheia
    reală („hydration"). Un termen brut în `attributes->'concerns' ?| ...` nu se potrivește cu
    nimic, iar golul rezultat arată identic cu „magazinul n-are produse"."""
    await _run(_ctx(), concerns=["hidratare"])

    assert _catalog["seen_filters"][0] == {"concerns": ["hydration"]}


async def test_nevoia_de_pe_alta_dimensiune_nu_se_filtreaza_ca_concern(_catalog):
    """«ten uscat» e `skin_type`, nu `concerns` (NX-257: partiționant, nu aditiv). Cât timp
    filtrarea era numită în cod pe o singură dimensiune, o rutină pentru ten uscat nu era
    exprimabilă NICI cu cheia canonică."""
    await _run(_ctx(), concerns=["ten uscat"])

    assert _catalog["seen_filters"][0] == {"skin_type": ["dry"]}


async def test_termenul_necunoscut_nu_filtreaza_si_i_se_spune_modelului(_catalog):
    """Un termen pe care catalogul nu-l cunoaște nu are voie să devină filtru (ar goli tăcut), dar
    nici să dispară: fără rândul din `llm_view`, modelul ar confirma o cerință pe care nimeni n-a
    aplicat-o, pe produse alese fără ea. Validatorul nu poate prinde asta, prețurile fiind reale."""
    result = await _run(_ctx(), concerns=["fara parfum"])

    assert _catalog["seen_filters"][0] is None
    assert "Nu am putut filtra pe: «fara parfum»" in result.llm_view
    assert "Nu confirma" in result.llm_view


async def test_nevoile_de_pe_dimensiuni_diferite_se_cumuleaza(_catalog):
    """Două dimensiuni = AND (cerințe care se adună), nu OR. Valorile aceleiași dimensiuni rămân
    alternative; asta o decide `_facet_filter_clause`, partajat cu `search_products`."""
    await _run(_ctx(), concerns=["ten uscat", "hidratare"])

    assert _catalog["seen_filters"][0] == {"concerns": ["hydration"], "skin_type": ["dry"]}


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


# ── Lungimea rutinei: o consecință a bugetului, nu o constantă ───────────────────────────────────
#
# Pașii unei familii erau ceruți TOȚI, mereu, iar un buget prea mic producea „nu se poate" cu o
# cifră care era artefactul insistenței pe lungimea maximă. Măsurat pe `sole-ro`: rutina de față
# pentru ten uscat cerea 375 lei pe șase pași, iar patru costă 165 — deci „rutină sub 200" ERA
# posibilă. Ordinea în care pașii cedează vine din pachet, derivată din conținutul tenantului
# (`scripts/derive_routine_priority.py`), niciodată din ordinea de APLICARE.

#: Ordinea de sacrificiu pentru familia de test: tonifierea cedează prima, curățarea ultima.
#: Deliberat DIFERITĂ de ordinea de aplicare (curatare, tonifiere, tratament, hidratare), ca un
#: test care trece să nu poată trece din întâmplare.
_PRIORITY = {"fata": {"default": ["curatare", "hidratare", "tratament", "tonifiere"]}}


async def test_bugetul_scurteaza_rutina_in_ordinea_declarata():
    """Podeaua e 200, bugetul 150. Cu prioritate declarată, rutina se scurtează în loc să refuze.

    Cade tonifierea (30) → 170, apoi tratamentul (90) → 80. Re-adăugarea readuce tonifierea (110
    încape), tratamentul nu (170 n-ar încăpea). Deci: trei pași, nu un refuz."""
    result = await _run(_ctx(priority=_PRIORITY), budget_max=150)

    assert result.ok
    assert "3. tratament — LIPSĂ (budget)" in result.llm_view
    assert sum(CATALOG[p["id"]][1] for p in result.products) <= 150
    assert "Am scurtat rutina ca să încapă în buget" in result.llm_view
    # „de la", nu „+": cifra e cel mai MIC preț de pe pasul scos, deci un prag inferior. Un „ar
    # costa 90 lei" ar fi luat ca exact, iar nimic din aval nu poate contrazice un preț real citit
    # ca altceva.
    assert "tratament (de la 90,00 lei)" in result.llm_view
    assert "cel puțin" in result.llm_view


async def test_fara_prioritate_declarata_nu_se_scurteaza_nimic():
    """Kill-switch prin DATE, nu prin flag: un tenant fără `priority` primește exact
    comportamentul de dinainte — rutină întreagă la minim, plus cifra minimului.

    A scurta după ordinea de APLICARE ar fi tăiat protecția solară prima, fiindcă e ultimul pas
    aplicat și aproape primul în importanță. Mai bine niciun scurtat decât unul arbitrar."""
    result = await _run(_ctx(), budget_max=150)

    assert result.ok
    assert len(result.products) == 4  # toți pașii familiei
    assert "Cel mai ieftin se face cu 200,00 lei" in result.llm_view
    assert "Am scurtat" not in result.llm_view


async def test_re_adaugarea_recupereaza_pasul_care_incape():
    """Renunțarea în ordine poate tăia mai mult decât trebuie. Măsurat pe `sole-ro`: „rutină de
    dimineață sub 150" scotea patru pași și lăsa 25 de lei nefolosiți, deși tratamentul costă 10.

    Aici: bugetul 150 lasă loc tonifierii (30) după ce tratamentul (90) a căzut."""
    result = await _run(_ctx(priority=_PRIORITY), budget_max=150)

    served = {CATALOG[p["id"]][0] for p in result.products}
    assert "fata:tonifiere" in served
    assert "fata:tratament" not in served


async def test_pasul_mai_esential_nu_cedeaza_inaintea_unuia_mai_putin_esential():
    """Garda împotriva sfatului prost. Cu prioritatea de dimineață a lui `sole-ro`, protecția
    solară e a DOUA, deci un buget strâns taie hidratarea și tratamentul, nu SPF-ul.

    Ordonarea pe frecvență GLOBALĂ ar fi dat exact invers: pe toate secvențele la un loc protecția
    apare în 44,2%, sub tonifiere (51,2%), deci ar fi căzut prima. Aceleași date, separate pe
    momente, spun 97,3% dimineața."""
    priority = {
        "fata": {
            "default": ["curatare", "hidratare", "tratament", "tonifiere"],
            # `tonifiere` joacă rolul pasului legat de moment (SPF-ul catalogului real): al doilea
            # în importanță dimineața, deci trebuie să SUPRAVIEȚUIASCĂ tăierii.
            "am": ["curatare", "tonifiere", "hidratare", "tratament"],
        }
    }
    ctx = _ctx(
        priority=priority,
        time_markers={"am": ["dimineata"], "pm": ["seara"]},
    )
    result = await _run(ctx, budget_max=120, moment="am")

    served = {CATALOG[p["id"]][0] for p in result.products}
    assert "fata:tonifiere" in served, "pasul al doilea în importanță a fost tăiat"
    assert sum(CATALOG[p["id"]][1] for p in result.products) <= 120


# ── Momentul zilei: un pas care nu se aplică nu e un gol ─────────────────────────────────────────


async def test_pasul_din_alt_moment_nu_apare_ca_lipsa():
    """O rutină de seară nu «ratează» protecția solară. `UNKNOWN ≠ MISMATCH`, aplicat la timp: un
    pas care nu se aplică iese din secvență, cu poziții RENUMEROTATE consecutiv, și se declară
    separat — altfel modelul l-ar putea adăuga singur ca să pară rutina completă."""
    ctx = _ctx(
        priority=_PRIORITY,
        time_markers={"am": ["dimineata"], "pm": ["seara"]},
        step_time={"tonifiere": "am"},
    )
    result = await _run(ctx, moment="pm")

    assert result.ok
    assert "tonifiere" not in [CATALOG[p["id"]][0].partition(":")[2] for p in result.products]
    assert "LIPSĂ" not in result.llm_view
    assert "NU se aplică în momentul cerut, deci nu lipsesc: tonifiere" in result.llm_view
    # Pozițiile rămân consecutive: „pasul 3" din conversație trebuie să însemne ceva la turul
    # următor, iar o filtrare de după compunere ar fi lăsat 1, 3, 4.
    assert "1. curatare" in result.llm_view
    assert "2. tratament" in result.llm_view
    assert "3. hidratare" in result.llm_view


async def test_bugetul_nu_e_depasit_niciodata_cand_s_a_scurtat():
    """Invariantul algoritmului greedy, pe o plajă de bugete: dacă s-a renunțat la vreun pas,
    suma servită NU depășește bugetul. Dacă nu s-a renunțat, rutina e întreagă la minim (și atunci
    poate depăși, declarat).

    Testat pe plajă, nu pe un buget, fiindcă bugul unei greedy cu renunțare, re-adăugare și urcare
    ar fi o depășire la o singură valoare, exact între treptele de preț ale catalogului."""
    for budget in (50, 80, 110, 140, 170, 200, 230, 260, 300):
        result = await _run(_ctx(priority=_PRIORITY), budget_max=budget)
        total = sum(CATALOG[p["id"]][1] for p in result.products)
        if "Am scurtat" in (result.llm_view or ""):
            assert total <= budget, f"buget {budget}: s-a scurtat dar s-a cheltuit {total}"
        else:
            # Nimic de renunțat ⇒ rutină întreagă la minim; depășirea e declarată, nu ascunsă.
            assert len(result.products) == 4 or not result.ok


async def test_niciun_pas_in_momentul_cerut_nu_ridica_excepție():
    """Toți pașii familiei sunt ai celuilalt moment. `compose` refuză o subsecvență goală, deci
    fără garda din tool ar ieși un `ValueError` NECAPTURAT dintr-un tool — adică tăcere pentru
    client (P6). Nu e atins pe pachetul de azi, dar e o configurare validă."""
    ctx = _ctx(
        families={"fata": ["tonifiere", "tratament"]},
        by_product_type={"toner de fata": "fata:tonifiere", "ser de fata": "fata:tratament"},
        time_markers={"am": ["dimineata"], "pm": ["seara"]},
        step_time={"tonifiere": "am", "tratament": "am"},
    )
    result = await _run(ctx, moment="pm")

    assert result.ok is False
    assert result.error == "no_step_at_moment"
    assert "nu se aplică în momentul «pm»" in result.llm_view
    assert "nu inventa pași" in result.llm_view


async def test_momentul_necunoscut_se_ignora_nu_respinge_turul():
    """Un moment care nu e în `time_markers` e un rafinament nereușit, nu o constrângere ratată: se
    cade pe ordinea de zi întreagă. Un 422 aici ar pierde turul pentru un cuvânt în plus."""
    result = await _run(_ctx(priority=_PRIORITY), moment="la_pranz")

    assert result.ok
    assert len(result.products) == 4
    assert "momentul" not in result.llm_view


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


def test_routine_enabled_nu_mai_cere_single_brain():
    """NX-297 felia 5 — rutina are acum DOUĂ gazde, deci poarta de boot a dispărut.

    Cât timp singura gazdă era profilul de tur al creierului unic, poarta era corectă: un flag care
    nu poate face nimic trebuie refuzat la boot. Acum `routine_plan` intră în toolsetul v1, iar
    randarea secvenței exista deja acolo — combinația nu mai e imposibilă, iar poarta ar fi
    interzis o configurație validă.

    Validatorul se apelează DIRECT pe un obiect construit, nu prin `Settings()`: construcția reală
    citește `.env`-ul mașinii, deci testul ar pica din cauza altui flag și ar raporta altceva decât
    măsoară."""
    from src.config import Settings

    on_v1 = Settings.model_construct(routine_enabled=True, single_brain_enabled=False)
    assert Settings._web_turn_relations(on_v1) is on_v1

    on_brain = Settings.model_construct(routine_enabled=True, single_brain_enabled=True)
    assert Settings._web_turn_relations(on_brain) is on_brain


def test_routine_plan_e_chemabil_de_model_cand_flagul_e_aprins():
    """Gaura pe care felia o închide: unealta era ÎNREGISTRATĂ, dar niciun toolset n-o numea, deci
    modelul n-o putea chema deloc. Aceeași clasă cu `related_products`."""
    from src.config import get_settings
    from src.tools.base import enabled_tools

    settings = get_settings()
    before_routine = settings.routine_enabled
    before_relations = settings.relation_traversal_enabled
    try:
        object.__setattr__(settings, "routine_enabled", False)
        object.__setattr__(settings, "relation_traversal_enabled", False)
        offered = enabled_tools(None)
        assert "routine_plan" not in offered and "related_products" not in offered

        object.__setattr__(settings, "routine_enabled", True)
        object.__setattr__(settings, "relation_traversal_enabled", True)
        offered = enabled_tools(None)
        assert "routine_plan" in offered and "related_products" in offered
    finally:
        object.__setattr__(settings, "routine_enabled", before_routine)
        object.__setattr__(settings, "relation_traversal_enabled", before_relations)


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


# ── Schema tool-ului: enumurile tenantului ──────────────────────────────────────────────────────


def test_momentul_dispare_la_tenantul_fara_momente():
    """Un RAFINAMENT nu are voie să omoare tool-ul. Furnizorul refuză un enum vid, deci dacă
    `moment` ar rămâne cu `enum: []` la un vertical fără momente ale zilei (un service auto n-are
    dimineață), TOT `routine_plan` ar crăpa — pentru un parametru opțional."""
    from src.agent.tool_definitions import tool_schemas

    params = tool_schemas(["routine_plan"], families=("fata",), moments=())[0]["function"][
        "parameters"
    ]

    assert "moment" not in params["properties"]
    assert "moment" not in params["required"]
    assert "family" in params["properties"]  # restul schemei, neatins


def test_momentul_nu_are_enum_ci_valori_in_descriere():
    """Decizie de RISC, nu de stil. Parametrul trebuie să accepte `null` („n-a precizat"), iar
    `enum` restrânge TOATE valorile, deci `null` ar fi trebuit pus în enum ca să rămână valid — o
    construcție neverificabilă fără un apel real, al cărei eșec ar fi un 400 pe FIECARE tur de
    rutină al oricărui tenant cu momente (vezi 2026-08-24, când un 400 a omorât toată calea de
    vânzare și numai pe ea).

    Câștigul enum-ului e mic aici: un moment inventat e ignorat de handler și se cade pe ordinea de
    zi întreagă. La `family` e invers, de-aia acolo enum-ul rămâne."""
    from src.agent.tool_definitions import tool_schemas

    params = tool_schemas(["routine_plan"], families=("fata",), moments=("am", "pm"))[0][
        "function"
    ]["parameters"]
    moment = params["properties"]["moment"]

    assert "enum" not in moment
    assert "am" in moment["description"] and "pm" in moment["description"]
    assert moment["type"] == ["string", "null"]
    assert "moment" in params["required"]
    # `family` rămâne cu mulțime ÎNCHISĂ: o familie inventată ar fi o interogare pe gol
    # prezentată ca răspuns onest.
    assert params["properties"]["family"]["enum"] == ["fata"]


def test_schema_altui_tool_nu_e_atinsa_de_parametrul_nou():
    """Garda împotriva efectului colateral: rescrierea lui `required` se face doar unde exista."""
    from src.agent.tool_definitions import _SCHEMAS, tool_schemas

    got = tool_schemas(["related_products"], relation_kinds=("complement",))[0]
    original = _SCHEMAS["related_products"]["function"]["parameters"]

    assert got["function"]["parameters"]["required"] == original["required"]
    assert sorted(got["function"]["parameters"]["properties"]) == sorted(original["properties"])


async def test_antetul_nu_promite_pasi_pe_care_nu_i_a_dat(_catalog):
    """Antetul e prima linie pe care o citește modelul. „Rutina fata, 6 pași" urmat de patru LIPSĂ
    îl invită să anunțe o rutină în șase pași. Și e formulat fără cifră lipită de substantiv, ca să
    nu apară „1 pași" în promptul din care modelul copiază tonul."""
    complet = await _run(_ctx())
    assert complet.llm_view.startswith("Rutina fata, 4 pași:")

    _catalog["available"] -= {"t1", "t2"}
    parțial = await _run(_ctx())
    assert parțial.llm_view.startswith("Rutina fata, pași acoperiți: 3 din 4:")
    assert "3 pași acoperiți" not in parțial.llm_view  # fără acord greșit de plural


def test_nicio_schema_generata_nu_mai_conține_marcatori():
    """Garda pentru defectul care NU dă eroare: un marcator scris într-o schemă și uitat din
    registru pleacă LITERAL în descrierea citită de model („{MOMENT_VALUES}"). Schema e validă,
    testele trec, doar instrucțiunea e absurdă. S-a întâmplat exact așa la NX-292."""
    import re

    from src.agent.tool_definitions import TOOL_NAMES, tool_schemas

    schemas = tool_schemas(
        list(TOOL_NAMES), relation_kinds=("complement",), families=("fata",), moments=("am", "pm")
    )
    leftovers = re.findall(r"\{[A-Z_]+\}", json.dumps(schemas, ensure_ascii=False))

    assert leftovers == [], f"marcatori neumpluți în schemele trimise modelului: {leftovers}"


def test_poarta_de_marcatori_prinde_unul_nedeclarat():
    """Poarta e la IMPORT, deci un marcator nou nedeclarat oprește procesul, nu primul tur."""
    import src.agent.tool_definitions as td

    td._SCHEMAS["__probe__"] = {"function": {"description": "ceva {NEDECLARAT} aici"}}
    try:
        with pytest.raises(ValueError, match="nedeclarați"):
            td._assert_markers_declared()
    finally:
        td._SCHEMAS.pop("__probe__", None)
