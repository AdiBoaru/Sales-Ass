"""NX-280 — pașii de rutină: harta din pachet, promovarea mărginită, poarta de secvență.

Testele sunt PURE (zero DB, zero LLM): modulul e o funcție de la config + atribute la o valoare.
Cazurile nu sunt inventate — fiecare corespunde unei constatări măsurate pe catalogul SOLE, notate
în `tasks/stage1/NX-280.md`.
"""

from __future__ import annotations

import pytest

from src.domain.routine_steps import (
    EMPTY_ROUTINE_STEPS,
    RoutineStepConfigError,
    build_spec,
    distinct_steps,
    load_routine_steps,
    resolve,
)

RAW = {
    "families": {
        "fata": ["curatare", "tonifiere", "tratament", "hidratare", "protectie"],
        "par": ["spalare", "conditionare"],
        "corp": ["curatare", "hidratare"],
        "machiaj": ["ten", "buze"],
    },
    "by_product_type": {
        "gel de curatare": "fata:curatare",
        "toner de fata": "fata:tonifiere",
        "ser de fata": "fata:tratament",
        "crema de fata": "fata:hidratare",
        "sampon": "par:spalare",
        "balsam": "par:conditionare",
        "lotiune de corp": "corp:hidratare",
        "gel de dus": "corp:curatare",
        "fond de ten": "machiaj:ten",
        "ruj": "machiaj:buze",
    },
    "promotions": [{"when_attribute": "spf", "within_family": "fata", "to_step": "protectie"}],
    "not_a_step": ["set", "pasta de dinti"],
}


@pytest.fixture
def spec():
    return build_spec(RAW)


# --- maparea de bază ---------------------------------------------------------------------


def test_tipul_da_pasul(spec):
    assert resolve({"product_type": "gel de curatare"}, spec) == "fata:curatare"
    assert resolve({"product_type": "sampon"}, spec) == "par:spalare"


def test_cheia_e_compusa_deci_samponul_nu_e_in_rutina_fetei(spec):
    """Invariantul central: „sampon" și „gel de curatare" sunt amândouă curățare, dar familia le
    ține separate. Fără cheia compusă, un filtru pe „curatare" ar amesteca rutinele."""
    assert resolve({"product_type": "sampon"}, spec) == "par:spalare"
    assert spec.family_of(resolve({"product_type": "sampon"}, spec)) == "par"
    assert spec.family_of(resolve({"product_type": "gel de curatare"}, spec)) == "fata"


def test_ordinea_vine_din_indexul_familiei(spec):
    assert spec.order_of("fata:curatare") == 1
    assert spec.order_of("fata:protectie") == 5
    assert spec.order_of("par:spalare") == 1
    assert spec.order_of("fata:inexistent") is None
    assert spec.order_of("inexistent:curatare") is None
    assert spec.order_of("fara-separator") is None


# --- promovarea pe atribut ---------------------------------------------------------------


def test_spf_promoveaza_in_familia_fetei(spec):
    """O cremă de față cu SPF 50 e pasul de protecție, nu cel de hidratare."""
    assert resolve({"product_type": "crema de fata", "spf": "50"}, spec) == "fata:protectie"


def test_spf_NU_scoate_produsul_din_familia_lui(spec):
    """Regula naivă («spf ⇒ protecție, indiferent de tip») ar muta 48 de produse din familia lor pe
    catalogul SOLE: 35 de bază de machiaj, 9 de corp, 4 balsamuri de buze. O loțiune de corp cu SPF
    nu e un pas din rutina feței, iar un fond de ten cu SPF 30 rămâne bază de machiaj."""
    assert resolve({"product_type": "lotiune de corp", "spf": "30"}, spec) == "corp:hidratare"
    assert resolve({"product_type": "fond de ten", "spf": "30"}, spec) == "machiaj:ten"
    assert resolve({"product_type": "ruj", "spf": "15"}, spec) == "machiaj:buze"


def test_spf_gol_sau_fals_nu_promoveaza(spec):
    for value in (None, "", False):
        assert resolve({"product_type": "crema de fata", "spf": value}, spec) == "fata:hidratare"


# --- coroborarea pe nume: o promovare e la fel de bună ca atributul ei -------------------

COR = {
    **RAW,
    "promotions": [
        {
            "when_attribute": "spf",
            "within_family": "fata",
            "to_step": "protectie",
            "corroborate_name": ["spf", "sun", "solar", "uv"],
        }
    ],
}


def test_promovarea_cere_ca_numele_sa_confirme_atributul():
    """Măsurat pe SOLE: derivarea lui `spf` citește numărul din fraze de SFAT — «Obligatoriu:
    foloseste crema cu SPF 50 in fiecare dimineata, deoarece retinolul poate sensibiliza pielea la
    soare» pune `spf=50` pe un ser cu RETINOL. Opt produse, șapte în familia feței. Un retinol
    prezentat ca protecție solară contrazice propria fișă a produsului."""
    s = build_spec(COR)
    attrs = {"product_type": "ser de fata", "spf": "50"}
    assert resolve(attrs, s, name="MEDICUBE Deep Vita A Retinol Serum") == "fata:tratament"
    assert resolve(attrs, s, name="SKIN1004 Centella Glow Sun Ampoule") == "fata:protectie"


def test_coroborarea_prinde_SPF50_lipit_de_cifra():
    """`\\bspf\\b` NU potrivește „SPF50+" (F și 5 sunt amândouă caractere de cuvânt), iar greșeala
    asta a produs o măsurătoare falsă în timpul proiectării regulii. Granița e doar la început."""
    s = build_spec(COR)
    attrs = {"product_type": "crema de fata", "spf": "50"}
    for name in ("TFIT Tone Up Sun Fluid SPF50+ PA++++", "COSRX Aloe Tone-up SPF50", "X SPF 30"):
        assert resolve(attrs, s, name=name) == "fata:protectie", name


def test_coroborarea_accepta_si_alti_tokeni_decat_cheia():
    """Protecțiile solare reale spun adesea „Sun", nu „SPF" — a cere doar cheia ar fi eliminat 16
    produse corecte („Relief Sun Cream", „Sunscreen Mousse", „Sunstick")."""
    s = build_spec(COR)
    attrs = {"product_type": "crema de fata", "spf": "50"}
    for name in ("BEAUTY OF JOSEON Relief Sun Cream", "EVY Sunscreen Mousse Daily UV"):
        assert resolve(attrs, s, name=name) == "fata:protectie", name


def test_fara_tokeni_declarati_promovarea_ramane_pe_atribut_simplu(spec):
    """Gol → comportamentul de dinainte. Coroborarea e opt-in, per promovare."""
    assert resolve({"product_type": "crema de fata", "spf": "50"}, spec, name="orice") == (
        "fata:protectie"
    )


def test_corroborate_name_invalid_e_respins():
    with pytest.raises(RoutineStepConfigError, match="corroborate_name"):
        build_spec(
            {
                **RAW,
                "promotions": [
                    {
                        "when_attribute": "spf",
                        "within_family": "fata",
                        "to_step": "protectie",
                        "corroborate_name": "spf",
                    }
                ],
            }
        )


# --- ce NU face: nu ghicește -------------------------------------------------------------


def test_fara_product_type_nu_exista_pas_nici_cu_spf(spec):
    """Cele 24 de produse SOLE cu `spf` și fără tip. Familia ar fi o ghicitoare, iar pasul ar
    ajunge în catalog ca fapt."""
    assert resolve({"spf": "50"}, spec) is None
    assert resolve({}, spec) is None
    assert resolve(None, spec) is None


def test_not_a_step_e_distinct_de_nemapat(spec):
    """Un «set» e un ambalaj, nu un pas — declarat explicit, ca să se distingă de o scăpare."""
    assert resolve({"product_type": "set"}, spec) is None
    assert resolve({"product_type": "tip inexistent"}, spec) is None


# --- validarea configului: fail-closed la SCRIERE ----------------------------------------


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda r: r.pop("families"), "families"),
        (lambda r: r.update(families={}), "families"),
        (lambda r: r.update(families={"fata": []}), "n-are pași"),
        (lambda r: r.update(families={"fa:ta": ["x"]}), "familie invalid"),
        (lambda r: r.update(families={"fata": ["a", "a"]}), "duplicați"),
        (lambda r: r.update(by_product_type={"x": "fata:inexistent"}), "nu e un pas declarat"),
        (lambda r: r.pop("by_product_type"), "by_product_type"),
        (
            lambda r: r.update(
                promotions=[{"when_attribute": "spf", "within_family": "zzz", "to_step": "x"}]
            ),
            "familia necunoscută",
        ),
        (
            lambda r: r.update(
                promotions=[{"when_attribute": "spf", "within_family": "par", "to_step": "zzz"}]
            ),
            "absent din",
        ),
        (lambda r: r.update(not_a_step=["ruj"]), "și mapate, și declarate"),
    ],
)
def test_config_invalid_e_respins(mutate, fragment):
    raw = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in RAW.items()}
    mutate(raw)
    with pytest.raises(RoutineStepConfigError) as e:
        build_spec(raw)
    assert fragment in str(e.value)


def test_un_pas_declarat_fara_tipuri_e_legal(spec):
    """`fata:protectie` n-are niciun `product_type`: se ajunge la el DOAR prin promovare, fiindcă
    nu există `product_type = "protectie solara"` în catalog."""
    assert "fata:protectie" in spec.canonical_values()
    assert "fata:protectie" not in spec.by_product_type.values()


# --- citirea de runtime: tolerantă -------------------------------------------------------


def test_load_tolerant_nu_darama_pachetul():
    """Asimetria față de `build_spec`: la CITIRE o hartă stricată costă o capabilitate, nu tot
    pachetul. Un `raise` aici ar lăsa tenantul fără `concern_map` pentru o virgulă greșită."""
    assert load_routine_steps({"families": "gunoi"}) is EMPTY_ROUTINE_STEPS
    assert load_routine_steps(None) is EMPTY_ROUTINE_STEPS
    assert load_routine_steps({}) is EMPTY_ROUTINE_STEPS
    assert load_routine_steps(RAW).families == build_spec(RAW).families


def test_spec_gol_nu_da_niciun_pas():
    """Comportamentul de dinaintea NX-280, byte-identic."""
    assert resolve({"product_type": "gel de curatare"}, EMPTY_ROUTINE_STEPS) is None


# --- poarta de secvență (ce consumă `answer_plan`) ---------------------------------------


def test_doi_pasi_distincti_din_aceeasi_familie_sunt_o_secventa(spec):
    got = distinct_steps(["fata:curatare", "fata:hidratare"], spec)
    assert got == {"fata": {"curatare", "hidratare"}}
    assert max(len(s) for s in got.values()) >= 2


def test_doua_produse_din_ACELASI_pas_nu_sunt_o_rutina(spec):
    """Două creme nu sunt o rutină. Pragul pe cardinalitate (≥2 produse) le-ar accepta."""
    got = distinct_steps(["fata:hidratare", "fata:hidratare"], spec)
    assert max(len(s) for s in got.values()) == 1


def test_familii_amestecate_nu_sunt_o_rutina(spec):
    """Un șampon și un ser de față nu sunt pași unul după altul."""
    got = distinct_steps(["par:spalare", "fata:tratament"], spec)
    assert got == {"par": {"spalare"}, "fata": {"tratament"}}
    assert max(len(s) for s in got.values()) == 1


def test_valorile_necunoscute_sunt_ignorate_nu_numarate(spec):
    assert distinct_steps(["fata:inexistent", "gunoi", "", "fata:curatare"], spec) == {
        "fata": {"curatare"}
    }


def test_rutina_de_machiaj_cere_pasi_distincti(spec):
    """Cu harta veche (2 pași: bază + culoare), «fond de ten + ruj» trecea drept rutină. Cu 5 pași
    tot trece, dar cere pași chiar distincți — iar testul fixează intenția."""
    got = distinct_steps(["machiaj:ten", "machiaj:buze"], spec)
    assert max(len(s) for s in got.values()) == 2


# --- ambiguu ≠ „nu e un pas" -------------------------------------------------------------


def test_ambiguu_si_not_a_step_dau_amandoua_None_dar_sunt_DECIZII_diferite(spec):
    """`UNKNOWN ≠ MISMATCH` aplicat la config: un «set» nu e un pas (afirmație), o «lotiune de
    fata» e sigur un pas dar nu se știe care (ignoranță). Măsurat pe SOLE: cele 18 produse
    tipizate „lotiune de fata" se împrăștie pe ȘASE categorii, deci un pas majoritar ar da unei
    treimi pasul greșit. Ambele întorc None; diferă în RAPORT, unde o scăpare cere reparație iar
    o ambiguitate declarată e o decizie luată."""
    raw = {**RAW, "ambiguous": ["lotiune de fata"]}
    s = build_spec(raw)
    assert resolve({"product_type": "lotiune de fata"}, s) is None
    assert resolve({"product_type": "set"}, s) is None
    assert "lotiune de fata" in s.ambiguous
    assert "lotiune de fata" not in s.not_a_step
    assert "set" in s.not_a_step and "set" not in s.ambiguous


def test_un_tip_nu_poate_fi_si_ambiguu_si_not_a_step():
    """Contradicție în config: raportul ar minți indiferent de ramura pe care ar merge."""
    with pytest.raises(RoutineStepConfigError, match="și not_a_step, și ambiguous"):
        build_spec({**RAW, "ambiguous": ["set"]})


def test_ambiguu_nu_poate_fi_si_mapat():
    with pytest.raises(RoutineStepConfigError, match="ambiguous"):
        build_spec({**RAW, "ambiguous": ["ruj"]})


def test_spf_nu_salveaza_un_tip_ambiguu(spec):
    """Un produs ambiguu cu SPF rămâne fără pas: promovarea are nevoie de familie, iar familia e
    exact ce nu știm. Concret pe SOLE: TFIT Airy Sun Fluid SPF 50 e tipizat „lotiune de fata"."""
    s = build_spec({**RAW, "ambiguous": ["lotiune de fata"]})
    assert resolve({"product_type": "lotiune de fata", "spf": "50"}, s) is None


# --- poarta din `answer_plan` (felia 4) --------------------------------------------------


def _routine_case(steps: list[str | None]):
    """Un plan `routine` cu N produse, fiecare cu pasul dat, plus contextul server-side."""
    from src.agent.answer_plan import (
        AnswerPlanContext,
        AnswerPlanV2,
        EvidenceRecord,
        GroundedProduct,
        PlanFacts,
        PlanObligation,
        PlanRecommendation,
        SelectedProduct,
        StyleSignals,
    )

    ids = [f"p{i}" for i in range(len(steps))]
    plan = AnswerPlanV2(
        schema_version=2,
        business_id="b1",
        locale="ro",
        intent_summary="rutina pentru ten uscat",
        obligations=(PlanObligation(kind="routine", key="routine"),),
        direct_answer="Pentru ten uscat, iată pașii pe care ți-i recomand din catalog.",
        selected_products=tuple(
            SelectedProduct(product_id=i, variant_id=None, evidence_ids=(f"product:{i}:identity",))
            for i in ids
        ),
        claims=(),
        facts=PlanFacts(prices=(), stocks=(), urls=()),
        recommendations=tuple(
            PlanRecommendation(
                product_id=i,
                variant_id=None,
                reason="potrivit pentru ten uscat",
                evidence_ids=(f"product:{i}:identity",),
                need_ids=("concerns",),
            )
            for i in ids
        ),
        comparison=None,
        constraints_applied=(),
        unknowns=(),
        relaxations=(),
        clarification=None,
        no_results=None,
        state_update_proposals=(),
        action_intents=(),
        disclosures=(),
        confirmed_actions=(),
        style_signals=StyleSignals(tone="neutral", verbosity="short"),
    )
    context = AnswerPlanContext(
        business_id="b1",
        locale="ro",
        products=tuple(
            GroundedProduct(
                product_id=i,
                business_id="b1",
                resolution="exact",
                variant_ids=(),
                routine_step=step,
            )
            for i, step in zip(ids, steps, strict=True)
        ),
        evidence=tuple(
            EvidenceRecord(
                evidence_id=f"product:{i}:identity",
                business_id="b1",
                product_id=i,
                variant_id=None,
                kind="identity",
                value=i,
                source_version="live",
                current=True,
            )
            for i in ids
        ),
        hard_constraints=(),
        successful_action_ids=(),
        known_need_ids=("concerns",),
    )
    return plan, context


def _validate(steps: list[str | None]):
    from src.agent.answer_plan import validate_answer_plan_v2

    plan, context = _routine_case(steps)
    return validate_answer_plan_v2(plan, context, required_obligations=(("routine", "routine"),))


def test_doi_pasi_distincti_trec_poarta():
    assert "routine_without_sequence" not in _validate(["fata:curatare", "fata:hidratare"]).failures


def test_doua_produse_din_acelasi_pas_NU_trec():
    """Pragul de cardinalitate (≥2 produse) le accepta. Două creme nu sunt o rutină."""
    assert "routine_without_sequence" in _validate(["fata:hidratare", "fata:hidratare"]).failures


def test_familii_amestecate_NU_trec():
    """Un șampon și un ser de față nu sunt pași unul după altul."""
    assert "routine_without_sequence" in _validate(["par:spalare", "fata:tratament"]).failures


def test_produse_fara_pas_NU_constituie_dovada():
    """Eșecul MĂSURAT pe SOLE: «rutina ten uscat» întorcea același ruj în două nuanțe. Produsele
    fără pas nu contrazic nimic (`UNKNOWN ≠ MISMATCH`), dar nici nu dovedesc o secvență."""
    assert "routine_without_sequence" in _validate([None, None]).failures


def test_un_singur_pas_cunoscut_nu_ajunge():
    assert "routine_without_sequence" in _validate(["fata:curatare", None]).failures


def test_trei_pasi_distincti_trec():
    got = _validate(["fata:curatare", "fata:tonifiere", "fata:hidratare"])
    assert "routine_without_sequence" not in got.failures


def test_no_results_onest_NU_e_respins_de_poarta():
    """P6: „nu pot compune o rutină pentru asta" e acoperire validă. A-l trece prin poarta de
    secvență ar transforma degradarea corectă în eșec — exact invers decât scopul porții."""
    from src.agent.answer_plan import PlanNoResults, validate_answer_plan_v2

    plan, context = _routine_case([None])
    plan = plan.model_copy(
        update={
            "recommendations": (),
            "selected_products": (),
            "no_results": PlanNoResults(
                reason_class="no_match",
                criteria=("ten uscat", "rutina completa"),
                alternatives=(),
            ),
        }
    )
    got = validate_answer_plan_v2(plan, context, required_obligations=(("routine", "routine"),))
    assert "routine_without_sequence" not in got.failures


def test_poarta_stinsa_restaureaza_pragul_vechi(monkeypatch):
    """Kill-switch: OFF → comportamentul de dinainte, byte-identic."""
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ROUTINE_EVIDENCE_REQUIRED", "false")
    try:
        assert (
            "routine_without_sequence"
            not in _validate(["fata:hidratare", "fata:hidratare"]).failures
        )
    finally:
        get_settings.cache_clear()


def test_un_plan_care_nu_afirma_rutina_nu_e_atins():
    """Poarta se aplică DOAR planurilor care chiar afirmă o rutină."""
    from src.agent.answer_plan import PlanObligation, validate_answer_plan_v2

    plan, context = _routine_case(["fata:hidratare", "fata:hidratare"])
    plan = plan.model_copy(
        update={"obligations": (PlanObligation(kind="recommend", key="recommend_0"),)}
    )
    got = validate_answer_plan_v2(
        plan, context, required_obligations=(("recommend", "recommend_0"),)
    )
    assert "routine_without_sequence" not in got.failures
