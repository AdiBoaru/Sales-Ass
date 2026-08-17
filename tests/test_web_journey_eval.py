"""NX-246 (felia 3) — gate-ul de calitate: fail-closed, orb, determinist înaintea stilului.

Testele urmăresc exact modurile în care un gate de calitate minte:
  • trece fiindcă n-a găsit datele (fail-open);
  • lasă un scor bun de ton să acopere o halucinație;
  • măsoară poziția (order bias) în loc de conținut;
  • combină doi evaluatori care nu sunt de acord;
  • acceptă un holdout care s-a schimbat între timp;
  • raportează „92% pass" fără să spună pe ce familii.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.evals.pairwise import (
    RUBRIC_DIMENSIONS,
    BlindLeak,
    JourneyPacket,
    Rating,
    WebRubricScores,
    aggregate,
    assert_blind,
    build_packets,
    needs_adjudication,
    win_score,
)
from src.evals.quality_gate import (
    DETERMINISTIC_CHECKS,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT,
    VERDICT_NOT_READY,
    VERDICT_PASS,
    DeterministicResult,
    GatePolicy,
    evaluate_gate,
)
from src.evals.web_journeys import (
    FAMILIES,
    MIN_DEV_JOURNEYS,
    MIN_FAMILY_IN_HOLDOUT,
    DuplicateJourney,
    HoldoutManifest,
    Journey,
    JourneyTurn,
    coverage,
    load_journeys,
    seal_holdout,
    verify_holdout,
)

SUITE = Path("tests/golden/web_journeys")


def _turn(**over) -> JourneyTurn:
    base = {"user_input": "caut o cremă", "success_criteria": "propune ceva"}
    base.update(over)
    return JourneyTurn(**base)


def _journey(**over) -> Journey:
    """Un journey valid pentru ORICE familie: helperul respectă coerența familie/conținut pe care
    o impune modelul (page_context cere context, cart_mutation cere refs etc.)."""
    family = over.get("family", "elliptical_followup")
    turns = (_turn(), _turn(user_input="și ceva mai ieftin?"))
    extra: dict = {}
    if family == "page_context":
        turns = (_turn(page_context={"surface": "pdp", "product_ref": "p-1"}), turns[1])
    elif family == "cart_mutation":
        extra["cart_refs"] = ("cart-1",)
    elif family == "hard_constraint":
        turns = (_turn(hard_constraints=("preț ≤ 100",)), turns[1])
    base = dict(
        journey_id="j-1",
        family=family,
        catalog_snapshot="demo-2026-08",
        turns=turns,
        **extra,
    )
    base.update(over)
    return Journey(**base)


# ── Corpusul din repo ───────────────────────────────────────────────────────────────────────


def test_corpusul_de_development_e_valid():
    journeys = load_journeys(SUITE / "dev")
    assert journeys, "corpusul seed lipsește"
    assert all(j.schema_version == "web-journey.v1" for j in journeys)


def test_schema_json_nu_a_divergat_de_model():
    """Fișierul de schemă e GENERAT din model. Dacă diverge, cineva a editat unul din ele —
    iar un corpus validat cu altă schemă decât cea a runnerului e o iluzie de verificare."""
    on_disk = json.loads((SUITE / "schema.json").read_text(encoding="utf-8"))
    generated = Journey.model_json_schema()
    assert on_disk["properties"] == generated["properties"]
    assert on_disk["required"] == generated["required"]


def test_manifestul_de_holdout_e_valid_dar_nesigilat():
    """Starea de AZI, afirmată explicit: holdoutul nu există. Testul se schimbă când e construit."""
    manifest = HoldoutManifest.model_validate(
        json.loads((SUITE / "holdout_manifest.json").read_text(encoding="utf-8"))
    )
    assert manifest.sealed is False
    assert manifest.journey_count == 0


def test_seed_ul_acopera_toate_familiile_dar_nu_e_suficient():
    """Cele două afirmații trebuie să coexiste: schema funcționează pe toate cele 10 familii,
    ȘI suita e prea mică pentru un verdict."""
    cov = coverage(load_journeys(SUITE / "dev"), holdout=False)
    assert not cov.missing_families
    assert cov.complete is False
    assert any(f"/{MIN_DEV_JOURNEYS}" in gap for gap in cov.gaps)


# ── Schema: ce refuză ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n", [1, 7])
def test_un_journey_are_intre_doua_si_sase_ture(n):
    with pytest.raises(ValidationError):
        _journey(turns=tuple(_turn() for _ in range(n)))


def test_contextul_de_pagina_nu_poate_purta_fapte_comerciale():
    """Aceeași regulă ca la runtime: corpusul nu testează cu date pe care serverul le refuză."""
    with pytest.raises(ValidationError, match="comerciale"):
        _turn(page_context={"surface": "pdp", "price": 99})


def test_textul_de_journey_nu_poate_contine_pii():
    with pytest.raises(ValidationError):
        _turn(user_input="sună-mă la 0721 345 678")


def test_familia_trebuie_sa_descrie_continutul():
    """O etichetă care nu descrie journey-ul face acoperirea să MINTĂ (raportezi 4 cazuri de
    `page_context` care de fapt n-au niciun context)."""
    # Construite DIRECT (nu prin helper, care le-ar completa): eticheta e pusă, conținutul lipsește.
    plain = (_turn(), _turn())
    common = {"journey_id": "j-x", "catalog_snapshot": "demo", "turns": plain}
    with pytest.raises(ValidationError, match="page_context"):
        Journey(family="page_context", **common)
    with pytest.raises(ValidationError, match="cart_refs"):
        Journey(family="cart_mutation", **common)
    with pytest.raises(ValidationError, match="constrângeri"):
        Journey(family="hard_constraint", **common)


def test_duplicatele_sunt_refuzate(tmp_path):
    (tmp_path / "a.json").write_text(
        json.dumps([_journey().model_dump(mode="json"), _journey().model_dump(mode="json")]),
        encoding="utf-8",
    )
    with pytest.raises(DuplicateJourney):
        load_journeys(tmp_path)


def test_acelasi_continut_cu_alt_id_e_tot_duplicat(tmp_path):
    """Umflă numărul fără să adauge semnal — adică exact ce ar face cineva grăbit să atingă 60."""
    a = _journey(journey_id="j-a").model_dump(mode="json")
    b = dict(a, journey_id="j-b")
    (tmp_path / "a.json").write_text(json.dumps([a, b]), encoding="utf-8")
    with pytest.raises(DuplicateJourney, match="identic"):
        load_journeys(tmp_path)


# ── Acoperire ───────────────────────────────────────────────────────────────────────────────


def test_holdoutul_cere_praguri_per_familie_si_adversarial():
    journeys = [
        _journey(journey_id=f"j-{i}", family=fam, adversarial=False)
        for i, fam in enumerate(sorted(FAMILIES) * 5)
    ]
    cov = coverage(journeys, holdout=True)
    assert cov.total >= 40 and not cov.missing_families
    # Fiecare familie are 5 ≥ 4, dar zero adversarial ⇒ tot incomplet.
    assert not cov.thin_families
    assert any("adversarial" in gap for gap in cov.gaps)


def test_o_familie_subtire_e_numita_explicit():
    journeys = [
        _journey(journey_id=f"j-{i}", family=fam, adversarial=True)
        for i, fam in enumerate([*sorted(FAMILIES) * 5, "no_results"])
    ]
    thin = coverage(journeys[:-1] + [], holdout=True)
    assert thin.total == 50
    scarce = coverage([j for j in journeys if j.family != "no_results"][:44], holdout=True)
    assert "no_results" in scarce.thin_families
    assert any(str(MIN_FAMILY_IN_HOLDOUT) in gap for gap in scarce.gaps)


# ── Sigiliul de holdout ─────────────────────────────────────────────────────────────────────


def test_sigiliul_prinde_o_schimbare_de_continut():
    original = [_journey(journey_id=f"j-{i}") for i in range(3)]
    digest = seal_holdout(original)
    manifest = HoldoutManifest(
        suite_id="suite-a",
        journey_count=3,
        adversarial_count=0,
        content_sha256=digest,
        by_family={"elliptical_followup": 3},
    )
    assert verify_holdout(manifest, original).ok

    tampered = [*original[:-1], _journey(journey_id="j-2", catalog_snapshot="alt-snapshot")]
    check = verify_holdout(manifest, tampered)
    assert not check.ok and check.reason == "hash de holdout diferit"


def test_sigiliul_e_stabil_la_reordonare():
    """Peste amprente, nu peste fișiere: un holdout re-serializat rămâne același holdout."""
    journeys = [_journey(journey_id=f"j-{i}") for i in range(3)]
    assert seal_holdout(journeys) == seal_holdout(list(reversed(journeys)))


def test_holdout_indisponibil_e_fail_nu_skip():
    manifest = HoldoutManifest(
        suite_id="suite-a", journey_count=3, adversarial_count=0, content_sha256="a" * 64
    )
    check = verify_holdout(manifest, None)
    assert not check.ok and "nu e disponibil" in check.reason


def test_manifest_fara_hash_nu_e_sigilat():
    """`journey_count: 40` fără hash e o afirmație pe care nimeni nu o poate verifica."""
    assert not HoldoutManifest(suite_id="suite-a", journey_count=40, adversarial_count=0).sealed
    unsealed = HoldoutManifest(suite_id="suite-a", journey_count=40, adversarial_count=0)
    assert not verify_holdout(unsealed, []).ok


def test_manifestul_refuza_numere_care_nu_se_potrivesc():
    with pytest.raises(ValidationError, match="însumează"):
        HoldoutManifest(
            suite_id="suite-a", journey_count=10, adversarial_count=0, by_family={"no_results": 3}
        )
    with pytest.raises(ValidationError, match="necunoscute"):
        HoldoutManifest(
            suite_id="suite-a", journey_count=1, adversarial_count=0, by_family={"inventata": 1}
        )


# ── Pairwise orb ────────────────────────────────────────────────────────────────────────────


def _scores(**over) -> WebRubricScores:
    base = dict.fromkeys(RUBRIC_DIMENSIONS, 4.5)
    base.update(over)
    return WebRubricScores(**base)


def _cases(n: int):
    return [(f"j-{i}", "elliptical_followup", (f"champ {i}",), (f"cand {i}",)) for i in range(n)]


def test_pachetul_nu_poate_purta_etichete_de_release():
    packet = JourneyPacket(
        pair_id="p1",
        journey_id="j-1",
        family="correction",
        transcript_a=("răspuns candidate v2",),
        transcript_b=("răspuns",),
    )
    with pytest.raises(BlindLeak):
        assert_blind(packet)


def test_randomizarea_laturii_e_determinista():
    a, keys_a = build_packets(_cases(8), seed="s1")
    b, keys_b = build_packets(_cases(8), seed="s1")
    assert [k.candidate_side for k in keys_a] == [k.candidate_side for k in keys_b]
    assert [p.pair_id for p in a] == [p.pair_id for p in b]
    # Alt seed ⇒ altă repartiție (altfel „randomizarea" ar fi o constantă).
    _, keys_c = build_packets(_cases(8), seed="s2")
    assert [k.candidate_side for k in keys_c] != [k.candidate_side for k in keys_a]


def test_win_score_urmeaza_formula_din_card():
    assert win_score("A", "A") == 1.0
    assert win_score("B", "A") == 0.0
    assert win_score("tie", "A") == 0.5


def test_dezacordul_de_castigator_cere_adjudecare():
    r1 = Rating(
        pair_id="p",
        evaluator_id="e1",
        winner="A",
        reason="more_natural",
        scores_a=_scores(),
        scores_b=_scores(),
    )
    r2 = r1.model_copy(update={"evaluator_id": "e2", "winner": "B"})
    adj = needs_adjudication([r1, r2])
    assert adj is not None and adj.reason == "winner_disagreement"


def test_diferenta_mare_pe_o_dimensiune_cere_adjudecare():
    """Chiar cu același câștigător: dacă nu văd același lucru, mediile lor nu se combină."""
    r1 = Rating(
        pair_id="p",
        evaluator_id="e1",
        winner="A",
        reason="more_helpful",
        scores_a=_scores(naturalness=5.0),
        scores_b=_scores(),
    )
    r2 = r1.model_copy(update={"evaluator_id": "e2", "scores_a": _scores(naturalness=3.0)})
    adj = needs_adjudication([r1, r2])
    assert adj is not None and adj.reason.startswith("spread:")


def test_un_singur_evaluator_nu_e_de_ajuns():
    r = Rating(
        pair_id="p",
        evaluator_id="e1",
        winner="tie",
        reason="no_difference",
        scores_a=_scores(),
        scores_b=_scores(),
    )
    assert needs_adjudication([r]) is not None


def test_perechea_in_dezacord_nu_intra_in_scor():
    """A o include cu media a doi oameni care nu sunt de acord ar FABRICA o observație."""
    packets, keys = build_packets(_cases(2), seed="s")
    ratings = []
    for i, p in enumerate(packets):
        base = Rating(
            pair_id=p.pair_id,
            evaluator_id="e1",
            winner="A",
            reason="more_natural",
            scores_a=_scores(),
            scores_b=_scores(),
        )
        ratings.append(base)
        ratings.append(
            base.model_copy(update={"evaluator_id": "e2", "winner": "B" if i == 0 else "A"})
        )
    result = aggregate(ratings, list(keys), {f"j-{i}": "elliptical_followup" for i in range(2)})
    assert result.n == 1, "perechea în dezacord a intrat în scor"
    assert result.disagreement_rate == 0.5
    assert len(result.adjudicated) == 1


def test_order_bias_e_raportat_intotdeauna():
    """Dacă „A" câștigă mereu, rezultatul măsoară poziția, nu calitatea."""
    packets, keys = build_packets(_cases(6), seed="s")
    ratings = []
    for p in packets:
        r = Rating(
            pair_id=p.pair_id,
            evaluator_id="e1",
            winner="A",
            reason="more_natural",
            scores_a=_scores(),
            scores_b=_scores(),
        )
        ratings.append(r)
        ratings.append(r.model_copy(update={"evaluator_id": "e2"}))
    result = aggregate(ratings, list(keys), {f"j-{i}": "correction" for i in range(6)})
    assert result.order_bias == 1.0, "bias-ul de ordine n-a fost detectat"


# ── Poarta ──────────────────────────────────────────────────────────────────────────────────


def _ok_coverage(holdout: bool):
    journeys = [
        _journey(journey_id=f"j-{i}", family=fam, adversarial=i % 2 == 0)
        for i, fam in enumerate(sorted(FAMILIES) * 8)
    ]
    return coverage(journeys, holdout=holdout)


def _sealed():
    journeys = [_journey(journey_id=f"h-{i}") for i in range(3)]
    manifest = HoldoutManifest(
        suite_id="suite-a",
        journey_count=3,
        adversarial_count=0,
        content_sha256=seal_holdout(journeys),
    )
    return verify_holdout(manifest, journeys)


def _good_pairwise(n=50, **over):
    from src.evals.pairwise import PairwiseResult

    result = PairwiseResult(
        n=n,
        wins=int(n * 0.6),
        ties=0,
        losses=int(n * 0.4),
        score=0.60,
        ci_low=0.52,
        ci_high=0.68,
        order_bias=0.5,
        disagreement_rate=0.1,
        rubric_means=dict.fromkeys(RUBRIC_DIMENSIONS, 4.3),
    )
    for k, v in over.items():
        setattr(result, k, v)
    return result


def _gate(**over):
    kw = dict(
        policy=GatePolicy(),
        seal=_sealed(),
        dev_coverage=_ok_coverage(False),
        holdout_coverage=_ok_coverage(True),
        deterministic=DeterministicResult(turns_checked=120),
        pairwise=_good_pairwise(),
    )
    kw.update(over)
    return evaluate_gate(**kw)


def test_drumul_fericit_da_pass():
    assert _gate().verdict == VERDICT_PASS


def test_fara_holdout_verdictul_e_not_ready_nu_pass():
    """Cel mai important test din fișier: un gate care trece pe lipsa datelor e fail-open."""
    report = _gate(seal=None, holdout_coverage=None)
    assert report.verdict == VERDICT_NOT_READY
    assert report.verdict != VERDICT_PASS


def test_not_ready_e_distinct_de_fail():
    """`FAIL` ar sugera că am măsurat și candidateul a pierdut. N-am măsurat."""
    report = _gate(dev_coverage=coverage([_journey()], holdout=False))
    assert report.verdict == VERDICT_NOT_READY
    assert any("10/60" in r or "/60" in r for r in report.reasons)


def test_o_halucinatie_nu_poate_fi_compensata_de_scoruri_perfecte():
    """Cerința centrală a cardului, impusă de cod: determinist ÎNAINTE de stil."""
    det = DeterministicResult(turns_checked=120)
    det.record("grounding", "j-3")
    report = _gate(
        deterministic=det,
        pairwise=_good_pairwise(rubric_means=dict.fromkeys(RUBRIC_DIMENSIONS, 5.0), score=0.95),
    )
    assert report.verdict == VERDICT_FAIL
    assert any("grounding" in r for r in report.reasons)


def test_o_verificare_determinista_necunoscuta_e_refuzata():
    with pytest.raises(ValueError, match="necunoscută"):
        DeterministicResult().record("vibe_check", "j-1")


def test_esantion_mic_da_insufficient_nu_fail():
    report = _gate(pairwise=_good_pairwise(n=12))
    assert report.verdict == VERDICT_INSUFFICIENT


def test_pragurile_de_rubrica_si_pairwise_blocheaza():
    assert _gate(pairwise=_good_pairwise(score=0.52)).verdict == VERDICT_FAIL
    assert _gate(pairwise=_good_pairwise(ci_low=0.48)).verdict == VERDICT_FAIL
    low = _good_pairwise(rubric_means={**dict.fromkeys(RUBRIC_DIMENSIONS, 4.3), "trust": 3.5})
    report = _gate(pairwise=low)
    assert report.verdict == VERDICT_FAIL
    assert any("trust" in r for r in report.reasons)


def test_un_cohort_prabusit_blocheaza_desi_media_e_buna():
    pairwise = _good_pairwise(
        rubric_by_family={"no_results": dict.fromkeys(RUBRIC_DIMENSIONS, 3.0)}
    )
    report = _gate(pairwise=pairwise)
    assert report.verdict == VERDICT_FAIL
    assert any("no_results" in r for r in report.reasons)


def test_order_bias_mare_invalideaza_masuratoarea():
    report = _gate(pairwise=_good_pairwise(order_bias=0.9))
    assert report.verdict == VERDICT_FAIL
    assert any("order bias" in r for r in report.reasons)


def test_overtalk_sever_peste_prag_blocheaza():
    report = _gate(severe_overtalk_ratio=0.12)
    assert report.verdict == VERDICT_FAIL
    assert any("overtalk" in r for r in report.reasons)


def test_regresia_pe_cohort_blocheaza():
    pairwise = _good_pairwise()
    pairwise.by_family = {"correction": {"n": 10, "pairwise_score": 0.50}}
    report = _gate(pairwise=pairwise, champion_by_family={"correction": 0.70})
    assert report.verdict == VERDICT_FAIL
    assert any("regresie" in r for r in report.reasons)


def test_amprenta_de_policy_intra_in_raport():
    """Pragurile se schimbă doar prin PR de policy — amprenta face schimbarea vizibilă."""
    report = _gate()
    assert report.policy_fingerprint == GatePolicy().fingerprint
    assert GatePolicy().fingerprint != GatePolicy(min_pairwise_score=0.9).fingerprint


def test_raportul_nu_contine_transcripturi():
    """Cardul: „runnerul nu printează transcriptul în CI artifacts"."""
    payload = json.dumps(_gate().as_dict(), ensure_ascii=False)
    assert "transcript" not in payload
    assert "caut o cremă" not in payload


def test_toate_verificarile_deterministe_sunt_documentate():
    assert len(DETERMINISTIC_CHECKS) == len(set(DETERMINISTIC_CHECKS))
    assert "grounding" in DETERMINISTIC_CHECKS and "no_pii" in DETERMINISTIC_CHECKS
