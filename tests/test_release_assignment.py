"""NX-249 — asignarea: determinism, epoch, kill-switch, fail-closed.

Testul central nu e „bucketul e uniform", ci: **procentul nu poate muta o conversație existentă.**
Un canary care re-decide la fiecare tur schimbă pipeline-ul în mijlocul unui dialog — clientul
vede altă memorie, alte referințe ordinale și alt coș, iar raportul compară două cohorturi în care
aceleași conversații apar de ambele părți.

Restul acoperă exact modurile în care o asignare „stabilă" se strică tăcut: două procese care
ajung la concluzii diferite, un store căzut interpretat ca „merge înainte", un policy expirat care
curge la nesfârșit, și un `force_control` care convertește o conversație candidate la control fără
să fi dovedit compatibilitatea.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.release.assignment import (
    BUCKETS,
    ReleaseContext,
    chi_square_uniformity,
    distribution,
    resolve,
    stable_bucket,
)
from src.release.models import (
    DECISION_CANDIDATE,
    DECISION_CONTROL,
    DECISION_DRAIN,
    MODE_CANARY,
    MODE_CLOSED,
    MODE_FORCE_CONTROL,
    MODE_INTERNAL,
    MODE_OBSERVE,
    REASON_BUCKET_OUT,
    REASON_CONTROLLER_OFF,
    REASON_DRAIN_INCOMPATIBLE,
    REASON_FORCE_CONTROL,
    REASON_INTERNAL,
    REASON_OUTSIDE_ADMISSION,
    REASON_POLICY_EXPIRED,
    REASON_POLICY_MISSING,
    REASON_STICKY,
    REASON_STORE_UNAVAILABLE,
    REASON_TENANT_NOT_ELIGIBLE,
    TRACK_CANDIDATE,
    TRACK_CHAMPION,
    CapturedExecution,
    ReleasePolicy,
)

#: Etapa declarată implicit, per mod/procent (tabelul `STAGES`). Testele care vizează etapa
#: o trec explicit; restul nu trebuie să se ocupe de ea.
#: `force_control` nu e o etapă: păstrează etapa din care s-a oprit (aici, pilotul).
_DEFAULT_STAGE = {MODE_OBSERVE: 0, MODE_INTERNAL: 1, MODE_FORCE_CONTROL: 3, MODE_CLOSED: 7}
_CANARY_STAGE = {5: 3, 20: 4, 50: 5, 100: 6}

T0 = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
BID = "6098812a-50fc-44bd-a1ba-bc77e6399158"
OTHER_BID = "7098812a-50fc-44bd-a1ba-bc77e6399158"
SALT = "salt-de-test-nx249"


def policy(
    *,
    mode: str = MODE_CANARY,
    percent: int = 5,
    eligible: tuple[str, ...] = (BID,),
    internal: tuple[str, ...] = (),
    rollback_compatible: bool = False,
    revision: int = 1,
    not_before: datetime = T0 - timedelta(hours=1),
    expires_at: datetime = T0 + timedelta(days=7),
    admission_from: str = "",
    admission_to: str = "",
    salt_id: str = "salt-1",
    stage: int | None = None,
) -> ReleasePolicy:
    """Un policy valid, cu dovezile completate — testele de VALIDARE stau în test_release_policy."""
    needs_evidence = mode in (MODE_INTERNAL, MODE_CANARY, MODE_CLOSED)
    if stage is None:
        stage = _DEFAULT_STAGE[mode] if mode != MODE_CANARY else _CANARY_STAGE[percent]
    return ReleasePolicy(
        policy_id="nx249-test",
        revision=revision,
        environment="test",
        created_at=(not_before - timedelta(hours=1)).isoformat(),
        not_before=not_before.isoformat(),
        expires_at=expires_at.isoformat(),
        admission_from=admission_from,
        admission_to=admission_to,
        control_release_sha="c0ntr0l1234567",
        control_pipeline_version="web-chat.v1",
        candidate_release_sha="cand1date7654321",
        candidate_pipeline_version="web-view.v2",
        mode=mode,
        percent=percent,
        stage=stage,
        eligible_business_ids=eligible,
        internal_business_ids=internal,
        stable_salt_id=salt_id,
        quality_packet_hash="sha256:q" if needs_evidence else "",
        e2e_packet_hash="sha256:e" if needs_evidence else "",
        deploy_manifest_hash="sha256:d" if needs_evidence else "",
        slo_policy_version="slo_policy.v1" if needs_evidence else "",
        quality_policy_version="nx246-gate-v1" if needs_evidence else "",
        rollback_compatible=rollback_compatible,
        approved_by="adi" if needs_evidence else "",
        approved_at=T0.isoformat(),
        change_ticket="NX-249" if needs_evidence else "",
    )


def decide(p, *, conv="conv-1", prior=None, now=T0, business=BID, **kw):
    return resolve(
        p,
        business_id=business,
        conversation_id=conv,
        prior=prior,
        now=now,
        salt=SALT,
        **kw,
    )


# ── Bucketing ───────────────────────────────────────────────────────────────────────────────
def test_bucketul_e_determinist_intre_apeluri_si_procese():
    """Aceleași intrări, același bucket. Golden vector: dacă derivarea se schimbă, testul pică.

    Valoarea e „magică" DELIBERAT: e amprenta contractului de bucketing. Un refactor care schimbă
    ordinea câmpurilor în mesajul HMAC ar reasigna tot traficul în tăcere — aici nu poate.
    """
    first = stable_bucket(BID, "conv-1", salt=SALT, salt_id="salt-1")
    again = stable_bucket(BID, "conv-1", salt=SALT, salt_id="salt-1")
    assert first == again
    assert 0 <= first < BUCKETS
    # Golden: reproductibil pe orice mașină/versiune de Python (hmac-sha256, nu `hash()`).
    assert first == stable_bucket(BID, "conv-1", salt=SALT, salt_id="salt-1")
    assert first == 71


def test_saltul_si_salt_id_schimba_bucketul():
    """Rotirea saltului e o REASIGNARE, nu o continuare — și trebuie să se vadă în derivare."""
    base = stable_bucket(BID, "conv-1", salt=SALT, salt_id="salt-1")
    assert stable_bucket(BID, "conv-1", salt="alt-salt", salt_id="salt-1") != base
    assert stable_bucket(BID, "conv-1", salt=SALT, salt_id="salt-2") != base


def test_tenanti_diferiti_nu_impart_bucketul():
    """Un `conversation_id` identic la doi tenanți nu trebuie să cadă în același bucket."""
    assert stable_bucket(BID, "conv-1", salt=SALT, salt_id="s") != stable_bucket(
        OTHER_BID, "conv-1", salt=SALT, salt_id="s"
    )


def test_distributia_e_aproximativ_uniforma_pe_10000_de_iduri():
    """Un hash strâmb se vede aici, nu peste două zile într-un raport de canary."""
    p = policy(percent=5)
    pairs = [(BID, f"conv-{i}") for i in range(10_000)]
    hist = distribution(p, pairs, salt=SALT)
    assert len(hist) == BUCKETS, "toate bucketurile trebuie atinse la 10k ID-uri"
    chi2 = chi_square_uniformity(hist)
    # 99 grade de libertate: pragul 5% e ~123,2, cel de 0,1% e ~148,2. Folosim limita relaxată:
    # testul trebuie să prindă un hash rupt, nu să pice o dată la douăzeci de rulări.
    assert chi2 < 148.0, f"distribuție suspect de neuniformă (χ²={chi2:.1f})"
    in_rollout = sum(n for b, n in hist.items() if b < 5)
    assert 400 <= in_rollout <= 600, f"5% din 10k ar trebui ~500, e {in_rollout}"


@pytest.mark.parametrize("percent", [0, 5, 20, 50, 100])
def test_procentul_se_respecta_aproximativ_pe_esantion_mare(percent):
    p = policy(mode=MODE_CANARY, percent=percent) if percent else policy(mode=MODE_OBSERVE)
    hits = sum(
        1 for i in range(5_000) if decide(p, conv=f"conv-{i}").decision == DECISION_CANDIDATE
    )
    expected = 5_000 * percent / 100
    assert abs(hits - expected) < 250, f"{percent}%: {hits} vs ~{expected}"


# ── Epoch: procentul atinge doar conversațiile NOI ──────────────────────────────────────────
def test_cresterea_procentului_nu_muta_conversatiile_existente():
    """5→20 nu are voie să mute în candidate o conversație deja servită de control.

    Cazul e ales ca să fie CONCLUDENT: alegem o conversație al cărei bucket e sub 20 dar peste 5,
    adică exact una pe care noul procent ar prinde-o dacă asignarea s-ar recalcula.
    """
    p5 = policy(percent=5)
    conv = next(
        c
        for c in (f"conv-{i}" for i in range(500))
        if 5 <= stable_bucket(BID, c, salt=SALT, salt_id="salt-1") < 20
    )
    first = decide(p5, conv=conv)
    assert first.decision == DECISION_CONTROL
    assert first.reason == REASON_BUCKET_OUT

    p20 = policy(percent=20, revision=2)
    # Fără captură, noul procent ar prinde-o (dovedim că e cazul relevant):
    assert decide(p20, conv=conv).decision == DECISION_CANDIDATE
    # Cu captura de la turul anterior, rămâne control.
    prior = CapturedExecution(track=TRACK_CHAMPION, policy_id="nx249-test", policy_revision=1)
    sticky = decide(p20, conv=conv, prior=prior)
    assert sticky.decision == DECISION_CONTROL
    assert sticky.reason == REASON_STICKY


def test_o_conversatie_candidate_ramane_candidate_cand_procentul_scade():
    """Simetria: scăderea 20→5 nu evacuează o conversație aflată deja pe candidate."""
    prior = CapturedExecution(track=TRACK_CANDIDATE, policy_id="nx249-test", policy_revision=1)
    out = decide(policy(percent=5, revision=2), conv="conv-oricare", prior=prior)
    assert out.decision == DECISION_CANDIDATE
    assert out.reason == REASON_STICKY


def test_doua_taburi_si_doua_procese_dau_aceeasi_asignare():
    """Nimic din decizie nu depinde de proces, de ceas fin sau de ordinea requesturilor."""
    p = policy(percent=50)
    a = decide(p, conv="conv-tab")
    b = decide(p, conv="conv-tab", now=T0 + timedelta(minutes=17))
    assert (a.decision, a.track, a.bucket) == (b.decision, b.track, b.bucket)


# ── Moduri ──────────────────────────────────────────────────────────────────────────────────
def test_observe_nu_livreaza_candidate():
    out = decide(policy(mode=MODE_OBSERVE, percent=0))
    assert out.decision == DECISION_CONTROL


def test_internal_trece_peste_procent_dar_doar_pentru_allowlist():
    p = policy(mode=MODE_INTERNAL, percent=0, eligible=(), internal=(BID,))
    assert decide(p).reason == REASON_INTERNAL
    assert decide(p).decision == DECISION_CANDIDATE
    assert decide(p, business=OTHER_BID).reason == REASON_TENANT_NOT_ELIGIBLE


def test_canary_refuza_tenantii_din_afara_allowlistului():
    out = decide(policy(percent=100, eligible=(OTHER_BID,)))
    assert out.decision == DECISION_CONTROL
    assert out.reason == REASON_TENANT_NOT_ELIGIBLE


def test_allowlist_gol_nu_inseamna_toti():
    """Fail-closed pe allowlist: „gol" e zero tenanți, nu „fără restricții"."""
    with pytest.raises(ValueError, match="allowlist"):
        policy(percent=100, eligible=())


def test_fereastra_de_admisie_inchisa_opreste_conversatiile_noi_dar_nu_pe_cele_vechi():
    p = policy(
        percent=100,
        admission_from=(T0 - timedelta(hours=1)).isoformat(),
        admission_to=(T0 - timedelta(minutes=1)).isoformat(),
    )
    fresh = decide(p, conv="conv-nou")
    assert fresh.decision == DECISION_CONTROL
    assert fresh.reason == REASON_OUTSIDE_ADMISSION
    prior = CapturedExecution(track=TRACK_CANDIDATE, policy_id="nx249-test", policy_revision=1)
    assert decide(p, conv="conv-vechi", prior=prior).decision == DECISION_CANDIDATE


def test_closed_livreaza_candidate_tuturor():
    """Etapa 7: v1 e închis, deci allowlistul nu mai poate lăsa pe cineva pe o rută inexistentă."""
    out = decide(policy(mode=MODE_CLOSED, percent=100, eligible=(OTHER_BID,)))
    assert out.decision == DECISION_CANDIDATE


# ── Kill-switch ─────────────────────────────────────────────────────────────────────────────
def test_force_control_opreste_accepturile_noi():
    out = decide(policy(mode=MODE_FORCE_CONTROL, percent=0, eligible=(BID,)))
    assert out.decision == DECISION_CONTROL
    assert out.reason == REASON_FORCE_CONTROL


def test_force_control_dreneaza_o_conversatie_candidate_fara_compatibilitate_dovedita():
    """Nu convertim tăcut: starea și referințele conversației vin din candidate."""
    prior = CapturedExecution(track=TRACK_CANDIDATE, policy_id="nx249-test", policy_revision=1)
    out = decide(policy(mode=MODE_FORCE_CONTROL, percent=0), prior=prior)
    assert out.decision == DECISION_DRAIN
    assert out.reason == REASON_DRAIN_INCOMPATIBLE
    assert out.track is None, "un turn drenat nu aparține niciunui cohort"


def test_force_control_muta_pe_control_cand_compatibilitatea_e_dovedita():
    prior = CapturedExecution(track=TRACK_CANDIDATE, policy_id="nx249-test", policy_revision=1)
    out = decide(policy(mode=MODE_FORCE_CONTROL, percent=0, rollback_compatible=True), prior=prior)
    assert out.decision == DECISION_CONTROL
    assert out.reason == REASON_FORCE_CONTROL


def test_force_control_bate_sticky_ul_pentru_conversatiile_control():
    prior = CapturedExecution(track=TRACK_CHAMPION, policy_id="nx249-test", policy_revision=1)
    assert decide(policy(mode=MODE_FORCE_CONTROL, percent=0), prior=prior).reason == (
        REASON_FORCE_CONTROL
    )


# ── Fail-closed ─────────────────────────────────────────────────────────────────────────────
def test_fara_policy_totul_e_control():
    out = decide(None)
    assert out.decision == DECISION_CONTROL
    assert out.reason == REASON_POLICY_MISSING


def test_store_indisponibil_e_distinct_de_policy_absent():
    """Amândouă duc la control, dar cer acțiuni diferite — deci trebuie să se poată deosebi."""
    out = decide(None, store_available=False)
    assert out.reason == REASON_STORE_UNAVAILABLE


def test_policy_expirat_nu_se_prelungeste_singur():
    p = policy(percent=100, expires_at=T0 - timedelta(minutes=1))
    out = decide(p)
    assert out.decision == DECISION_CONTROL
    assert out.reason == REASON_POLICY_EXPIRED


def test_policy_inainte_de_not_before_nu_livreaza():
    p = policy(percent=100, not_before=T0 + timedelta(hours=1))
    assert decide(p).reason == REASON_POLICY_EXPIRED


def test_controllerul_stins_nu_atinge_nimic():
    out = decide(policy(percent=100), controller_enabled=False)
    assert out.decision == DECISION_CONTROL
    assert out.reason == REASON_CONTROLLER_OFF
    assert out.policy_id == "", "cu controllerul stins nu se capturează niciun policy"


def test_o_conversatie_drenata_nu_produce_captura_de_cohort():
    """`as_props` trebuie să spună `unknown`, nu să inventeze un track pentru un turn refuzat."""
    prior = CapturedExecution(track=TRACK_CANDIDATE, policy_id="p", policy_revision=1)
    props = decide(policy(mode=MODE_FORCE_CONTROL, percent=0), prior=prior).as_props()
    assert props["decision"] == DECISION_DRAIN
    assert props["track"] == "unknown"


# ── ReleaseContext ──────────────────────────────────────────────────────────────────────────
def test_contextul_captureaza_ceasul_o_singura_data():
    """Două decizii din același context folosesc ACELAȘI `now` — altfel granița de expirare ar
    face asignarea nedeterministă exact în clipa în care contează."""
    p = policy(percent=100, expires_at=T0 + timedelta(seconds=1))
    ctx = ReleaseContext(policy=p, available=True, salt=SALT, now=T0)
    assert ctx.decide(BID, "conv-a", None).decision == DECISION_CANDIDATE
    assert ctx.decide(BID, "conv-b", None).decision == DECISION_CANDIDATE
    assert ctx.mode == MODE_CANARY


def test_contextul_fara_policy_raporteaza_modul_observe():
    ctx = ReleaseContext(policy=None, available=False, salt=SALT, now=T0)
    assert ctx.mode == MODE_OBSERVE
    assert ctx.decide(BID, "conv", None).reason == REASON_STORE_UNAVAILABLE
