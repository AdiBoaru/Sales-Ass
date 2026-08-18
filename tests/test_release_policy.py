"""NX-249 — policy-ul: validare, amprentă, CAS, cache mărginit, audit.

Un policy prost validat e mai periculos decât unul absent, fiindcă „aproape" funcționează: un
`percent` scris `precent` ar trece tăcut cu 0, un candidate egal cu control ar produce un raport
care compară ceva cu el însuși și arată PASS, iar un policy de staging citit de producție ar
promova trafic real pe baza unei aprobări date pentru altceva.

Pe partea de scriere, testul care contează e CAS-ul: doi operatori simultani nu au voie să creadă
amândoi că au aplicat. Al doilea trebuie să PIARDĂ explicit — dacă primul tocmai a apăsat
kill-switchul, o suprascriere tăcută ar reporni canaryul în mijlocul incidentului.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.db.queries.release import PolicyRow, PolicyStoreUnavailable
from src.release import policy_store
from src.release.models import (
    MODE_CANARY,
    MODE_FORCE_CONTROL,
    MODE_OBSERVE,
    POLICY_SCHEMA_VERSION,
    STAGES,
    PolicyError,
    ReleasePolicy,
    stage_for,
)

T0 = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
BID = "6098812a-50fc-44bd-a1ba-bc77e6399158"


def payload(**over) -> dict:
    base = {
        "policy_id": "nx249-pilot",
        "revision": 0,
        "environment": "test",
        "created_at": (T0 - timedelta(hours=2)).isoformat(),
        "not_before": (T0 - timedelta(hours=1)).isoformat(),
        "expires_at": (T0 + timedelta(days=7)).isoformat(),
        "control_release_sha": "c0ntr0l1234567",
        "control_pipeline_version": "web-chat.v1",
        "candidate_release_sha": "cand1date7654321",
        "candidate_pipeline_version": "web-view.v2",
        "mode": MODE_CANARY,
        "percent": 5,
        "stage": 3,
        "eligible_business_ids": [BID],
        "internal_business_ids": [],
        "stable_salt_id": "salt-1",
        "quality_packet_hash": "sha256:q",
        "e2e_packet_hash": "sha256:e",
        "deploy_manifest_hash": "sha256:d",
        "slo_policy_version": "slo_policy.v1",
        "quality_policy_version": "nx246-gate-v1",
        "approved_by": "adi",
        "approved_at": T0.isoformat(),
        "change_ticket": "NX-249",
    }
    base.update(over)
    return base


# ── Validare ────────────────────────────────────────────────────────────────────────────────
def test_policy_valid_se_construieste_si_are_amprenta_stabila():
    p = ReleasePolicy.from_payload(payload())
    again = ReleasePolicy.from_payload(payload())
    assert p.fingerprint == again.fingerprint
    assert p.fingerprint.startswith("sha256:")
    assert p.schema_version == POLICY_SCHEMA_VERSION


def test_amprenta_se_schimba_la_orice_modificare_de_continut():
    base = ReleasePolicy.from_payload(payload()).fingerprint
    assert ReleasePolicy.from_payload(payload(percent=20, stage=4)).fingerprint != base
    assert ReleasePolicy.from_payload(payload(policy_id="altul")).fingerprint != base


def test_ordinea_allowlistului_nu_schimba_amprenta():
    """Forma canonică sortează: două liste cu aceiași tenanți sunt același policy."""
    a = ReleasePolicy.from_payload(payload(eligible_business_ids=[BID, "zzz"], percent=5))
    b = ReleasePolicy.from_payload(payload(eligible_business_ids=["zzz", BID], percent=5))
    assert a.fingerprint == b.fingerprint


def test_camp_necunoscut_e_respins_nu_ignorat():
    """`precent=20` nu are voie să treacă tăcut cu procentul la default."""
    with pytest.raises(PolicyError):
        ReleasePolicy.from_payload(payload(precent=20))


def test_candidate_egal_cu_control_e_respins():
    with pytest.raises(PolicyError, match="difere"):
        ReleasePolicy.from_payload(payload(candidate_release_sha="c0ntr0l1234567"))


def test_timestamps_ne_monotone_sunt_respinse():
    with pytest.raises(PolicyError, match="monotone"):
        ReleasePolicy.from_payload(payload(expires_at=(T0 - timedelta(days=2)).isoformat()))


def test_timestamp_fara_fus_orar_e_respins():
    """Un naiv interpretat local ar decala ferestrele de observare cu ore."""
    with pytest.raises(PolicyError, match="fus orar"):
        ReleasePolicy.from_payload(payload(not_before="2026-08-18T12:00:00"))


def test_modurile_care_livreaza_candidate_cer_dovezi():
    with pytest.raises(PolicyError, match="quality_packet_hash"):
        ReleasePolicy.from_payload(payload(quality_packet_hash="", e2e_packet_hash=""))


def test_observe_nu_cere_dovezi():
    """Un `observe` la 0% nu livrează nimic, deci n-are ce dovedi."""
    p = ReleasePolicy.from_payload(
        payload(
            mode=MODE_OBSERVE,
            percent=0,
            stage=0,
            quality_packet_hash="",
            e2e_packet_hash="",
            deploy_manifest_hash="",
            slo_policy_version="",
            quality_policy_version="",
            approved_by="",
            change_ticket="",
        )
    )
    assert p.mode == MODE_OBSERVE


def test_canary_la_zero_la_suta_e_respins():
    """Un canary la 0% ar apărea în rapoarte ca etapă activă fără să livreze nimic."""
    with pytest.raises(PolicyError, match="percent > 0"):
        ReleasePolicy.from_payload(payload(percent=0))


def test_procent_in_afara_intervalului_e_respins():
    with pytest.raises(PolicyError):
        ReleasePolicy.from_payload(payload(percent=101))


def test_allowlistul_nu_poate_fi_un_string():
    """`"uuid"` ar deveni o listă de caractere, iar allowlistul ar fi tăcut gol."""
    with pytest.raises(PolicyError, match="listă"):
        ReleasePolicy.from_payload(payload(eligible_business_ids=BID))


def test_contract_de_schema_necunoscut_e_respins():
    with pytest.raises(PolicyError):
        ReleasePolicy.from_payload(payload(schema_version="release-policy.v9"))


def test_saltul_nu_apare_niciodata_in_policy():
    """Policy-ul poartă `stable_salt_id`, nu valoarea. Un policy exfiltrat nu dă bucketuri."""
    p = ReleasePolicy.from_payload(payload())
    text = json.dumps(p.to_payload())
    assert "stable_salt_id" in text
    assert "salt-de-test" not in text
    assert not any("salt" in k and k != "stable_salt_id" for k in p.to_payload())


# ── Etape ───────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "percent,stage,expected",
    [(5, 3, "3-pilot"), (20, 4, "4-expand"), (50, 5, "5-majority"), (100, 6, "6-default")],
)
def test_etapa_declarata_se_citeste_din_policy(percent, stage, expected):
    p = ReleasePolicy.from_payload(payload(percent=percent, stage=stage))
    assert stage_for(p).label == expected


def test_etapele_2_si_6_au_aceleasi_cifre_dar_praguri_diferite():
    """De ce etapa e DECLARATĂ, nu dedusă: „demo 100%" și „default 100%" sunt indistinctibile
    din (mod, procent), dar cer 24h/100 de ture vs 14 zile/2.000. O deducere ar fi nimerit-o pe
    prima și ar fi slăbit poarta exact la ultima etapă."""
    demo = ReleasePolicy.from_payload(payload(percent=100, stage=2))
    default = ReleasePolicy.from_payload(payload(percent=100, stage=6))
    assert (demo.mode, demo.percent) == (default.mode, default.percent)
    assert stage_for(demo).min_candidate_turns < stage_for(default).min_candidate_turns


def test_un_procent_in_afara_etapei_declarate_e_respins():
    """7% nu e „aproape etapa 3": e o etapă neaprobată, iar policy-ul o refuză la construcție."""
    with pytest.raises(PolicyError, match="percent=5"):
        ReleasePolicy.from_payload(payload(percent=7, stage=3))


def test_modul_trebuie_sa_se_potriveasca_etapei_declarate():
    with pytest.raises(PolicyError, match="declarată cu modul"):
        ReleasePolicy.from_payload(payload(mode=MODE_OBSERVE, percent=0, stage=3))


def test_etapele_au_praguri_crescatoare():
    """Un tabel în care o etapă cere mai puțin decât precedenta ar permite regresii de rigoare."""
    canary = [s for s in STAGES if s.min_candidate_turns]
    for prev, nxt in zip(canary, canary[1:], strict=False):
        assert nxt.min_candidate_turns >= prev.min_candidate_turns
        assert nxt.min_hours >= prev.min_hours


# ── Storeul (fake conn) ─────────────────────────────────────────────────────────────────────
class FakeConn:
    """Conexiune minimală: `current_policy` + `insert_policy_revision` + `write_release_audit`.

    Deliberat NU e un mock de asyncpg: testele de aici verifică REGULILE storeului (CAS, validare,
    cache), nu SQL-ul. SQL-ul se verifică pe Postgres real în `test_web_v2_cutover_e2e.py`.
    """

    def __init__(self, rows=None, *, table_exists=True):
        self.rows: list[PolicyRow] = list(rows or [])
        self.table_exists = table_exists
        self.audit: list[dict] = []

    async def fetchval(self, sql, *args):
        if "release_policies" in sql:
            return self.table_exists
        return None

    async def fetchrow(self, sql, *args):
        env = args[0]
        rows = sorted(
            (r for r in self.rows if r.environment == env), key=lambda r: r.revision, reverse=True
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "environment": r.environment,
            "revision": r.revision,
            "policy_id": r.policy_id,
            "policy": r.policy,
            "actor": r.actor,
            "reason": r.reason,
            "change_ticket": r.change_ticket,
            "applied_at": r.applied_at,
        }

    async def execute(self, sql, *args):
        if "audit_log" in sql:
            self.audit.append({"actor": args[0], "action": args[1], "details": json.loads(args[3])})
            return "INSERT 0 1"
        # insert into release_policies
        env, revision = args[0], args[1]
        if any(r.environment == env and r.revision == revision for r in self.rows):
            import asyncpg  # noqa: PLC0415

            raise asyncpg.UniqueViolationError("duplicate revision")
        self.rows.append(
            PolicyRow(
                environment=env,
                revision=revision,
                policy_id=args[2],
                policy=json.loads(args[3]),
                actor=args[4],
                reason=args[5],
                change_ticket=args[6],
                applied_at=T0,
            )
        )
        return "INSERT 0 1"


def row_for(p: ReleasePolicy, *, revision: int | None = None, environment: str = "") -> PolicyRow:
    return PolicyRow(
        environment=environment or p.environment,
        revision=p.revision if revision is None else revision,
        policy_id=p.policy_id,
        policy=p.to_payload(),
        actor="adi",
        reason="test",
        change_ticket="NX-249",
        applied_at=T0,
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    policy_store.reset_cache()
    yield
    policy_store.reset_cache()


@pytest.mark.asyncio
async def test_citirea_intoarce_policy_validat():
    p = ReleasePolicy.from_payload(payload())
    view = await policy_store.current(FakeConn([row_for(p)]), "test")
    assert view.code == policy_store.POLICY_OK
    assert view.usable
    assert view.policy.fingerprint == p.fingerprint


@pytest.mark.asyncio
async def test_lipsa_tabelului_e_store_down_nu_policy_absent():
    view = await policy_store.current(FakeConn(table_exists=False), "test")
    assert view.code == policy_store.POLICY_STORE_DOWN
    assert not view.available
    assert view.policy is None


@pytest.mark.asyncio
async def test_document_de_alt_mediu_pe_randul_de_productie_e_respins():
    """Coloana zice `prod`, documentul zice `staging` — un policy „mutat" între medii.

    Query-ul filtrează pe `environment = $1`, deci un policy de staging nu poate fi CITIT de
    producție. Gaura rămasă e alta: cineva inserează documentul de staging pe rândul de prod, iar
    atunci trafic real s-ar promova pe o aprobare dată pentru altceva. Poarta compară documentul,
    nu doar coloana.
    """
    p = ReleasePolicy.from_payload(payload(environment="staging"))
    view = await policy_store.current(FakeConn([row_for(p, environment="prod")]), "prod")
    assert view.code == policy_store.POLICY_ENV_MISMATCH
    assert view.policy is None


@pytest.mark.asyncio
async def test_document_invalid_in_db_nu_devine_policy():
    bad = row_for(ReleasePolicy.from_payload(payload()))
    bad.policy["percent"] = 999  # editat direct în DB
    view = await policy_store.current(FakeConn([bad]), "test")
    assert view.code == policy_store.POLICY_INVALID
    assert view.policy is None


@pytest.mark.asyncio
async def test_revizia_din_document_trebuie_sa_fie_cea_din_coloana():
    """Altfel evidence packetul ar cita o revizie care nu e cea în vigoare."""
    p = ReleasePolicy.from_payload(payload(revision=3))
    view = await policy_store.current(FakeConn([row_for(p, revision=5)]), "test")
    assert view.code == policy_store.POLICY_INVALID


@pytest.mark.asyncio
async def test_o_eroare_de_db_nu_ridica_pe_calea_de_accept():
    class Broken(FakeConn):
        async def fetchrow(self, sql, *args):
            raise RuntimeError("conexiune moartă")

    view = await policy_store.current(Broken([]), "test")
    assert view.code == policy_store.POLICY_STORE_DOWN
    assert not view.available


@pytest.mark.asyncio
async def test_store_down_nu_se_memoreaza_in_cache():
    """Un incident de 30 de secunde n-are voie să țină controllerul fail-closed un TTL întreg."""
    broken = FakeConn(table_exists=False)
    assert (await policy_store.current(broken, "test")).code == policy_store.POLICY_STORE_DOWN
    broken.table_exists = True
    broken.rows.append(row_for(ReleasePolicy.from_payload(payload())))
    assert (await policy_store.current(broken, "test")).code == policy_store.POLICY_OK


@pytest.mark.asyncio
async def test_cache_ul_evita_al_doilea_query_in_TTL():
    conn = FakeConn([row_for(ReleasePolicy.from_payload(payload()))])
    calls = {"n": 0}
    original = conn.fetchrow

    async def counted(sql, *args):
        calls["n"] += 1
        return await original(sql, *args)

    conn.fetchrow = counted
    await policy_store.current(conn, "test", ttl_s=60)
    await policy_store.current(conn, "test", ttl_s=60)
    assert calls["n"] == 1
    await policy_store.current(conn, "test", ttl_s=60, force_refresh=True)
    assert calls["n"] == 2


# ── CAS + audit ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_primul_apply_cere_expected_revision_none():
    conn = FakeConn([])
    p = ReleasePolicy.from_payload(payload(revision=0))
    result = await policy_store.apply(
        conn, p, expected_revision=None, actor="adi", reason="prima etapă", environment="test"
    )
    assert result.ok
    assert result.revision == 0


@pytest.mark.asyncio
async def test_apply_ul_refuza_cand_altcineva_a_aplicat_intre_timp():
    """Cazul care contează: primul a apăsat kill-switchul, al doilea ar fi repornit canaryul."""
    conn = FakeConn([row_for(ReleasePolicy.from_payload(payload(revision=2)))])
    p = ReleasePolicy.from_payload(payload(revision=2))
    result = await policy_store.apply(
        conn, p, expected_revision=1, actor="altcineva", reason="x", environment="test"
    )
    assert not result.ok
    assert result.conflict
    assert result.revision == 2


@pytest.mark.asyncio
async def test_revizia_documentului_trebuie_sa_fie_urmatoarea():
    conn = FakeConn([row_for(ReleasePolicy.from_payload(payload(revision=2)))])
    p = ReleasePolicy.from_payload(payload(revision=7))
    result = await policy_store.apply(
        conn, p, expected_revision=2, actor="adi", reason="x", environment="test"
    )
    assert not result.ok
    assert result.reason == "policy_revision_must_be_3"


@pytest.mark.asyncio
async def test_apply_fara_actor_sau_motiv_e_refuzat():
    conn = FakeConn([])
    p = ReleasePolicy.from_payload(payload(revision=0))
    for actor, reason in (("", "motiv"), ("adi", "   ")):
        result = await policy_store.apply(
            conn, p, expected_revision=None, actor=actor, reason=reason, environment="test"
        )
        assert not result.ok
        assert result.reason == "actor_and_reason_required"


@pytest.mark.asyncio
async def test_apply_scrie_audit_fara_lista_de_tenanti():
    """Auditul e citit de mai mulți ochi decât policy-ul: numără tenanții, nu-i enumeră."""
    conn = FakeConn([])
    p = ReleasePolicy.from_payload(payload(revision=0))
    await policy_store.apply(
        conn, p, expected_revision=None, actor="adi", reason="etapa 3", environment="test"
    )
    assert len(conn.audit) == 1
    entry = conn.audit[0]
    assert entry["actor"] == "adi"
    assert entry["action"] == "release_policy_apply"
    assert entry["details"]["eligible_count"] == 1
    assert BID not in json.dumps(entry["details"])


@pytest.mark.asyncio
async def test_apply_ul_invalideaza_cache_ul():
    """Altfel operatorul ar aplica un policy și `show` i-ar arăta încă pe cel vechi."""
    conn = FakeConn([row_for(ReleasePolicy.from_payload(payload(revision=0)))])
    await policy_store.current(conn, "test", ttl_s=600)
    p = ReleasePolicy.from_payload(payload(revision=1, percent=20, stage=4))
    await policy_store.apply(
        conn, p, expected_revision=0, actor="adi", reason="etapa 4", environment="test"
    )
    view = await policy_store.current(conn, "test", ttl_s=600)
    assert view.policy.percent == 20


@pytest.mark.asyncio
async def test_apply_pe_alt_mediu_e_refuzat():
    conn = FakeConn([])
    p = ReleasePolicy.from_payload(payload(revision=0, environment="staging"))
    result = await policy_store.apply(
        conn, p, expected_revision=None, actor="adi", reason="x", environment="prod"
    )
    assert not result.ok
    assert result.reason == "environment_mismatch"


@pytest.mark.asyncio
async def test_store_indisponibil_la_apply_nu_scrie_nimic():
    conn = FakeConn([], table_exists=False)
    p = ReleasePolicy.from_payload(payload(revision=0))
    result = await policy_store.apply(
        conn, p, expected_revision=None, actor="adi", reason="x", environment="test"
    )
    assert not result.ok
    assert "store_unavailable" in result.reason
    assert conn.rows == []


# ── Kill-switch derivat ─────────────────────────────────────────────────────────────────────
def test_force_control_pastreaza_restul_policy_ului():
    """Oprirea nu rescrie ce era în canary — istoricul trebuie să arate exact ce s-a oprit."""
    p = ReleasePolicy.from_payload(payload(percent=20, stage=4, revision=3))
    killed = policy_store.force_control_from(p, now=T0, revision=4)
    assert killed.mode == MODE_FORCE_CONTROL
    assert killed.revision == 4
    assert killed.percent == 20
    assert killed.candidate_release_sha == p.candidate_release_sha
    assert killed.eligible_business_ids == p.eligible_business_ids


def test_policy_store_unavailable_e_o_exceptie_proprie():
    """Ca să nu fie prinsă din greșeală de un `except Exception` generic drept „date lipsă"."""
    assert issubclass(PolicyStoreUnavailable, RuntimeError)
