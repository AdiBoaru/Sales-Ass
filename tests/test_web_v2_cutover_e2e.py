"""NX-249 — controllerul de release pe Postgres REAL: captură, epoch, CAS, drenare, cutover.

Exclus din CI fast (`-m "not integration"`). Acoperă exact garanțiile care nu se pot dovedi fără
DB, fiindcă sunt garanții ale SCHEMEI, nu ale codului:

  • captura de asignare e scrisă în ACEEAȘI operație cu rândul de ledger (nu există fereastră în
    care un turn să existe fără cohort);
  • sticky-ul se re-derivă din ledger — deci supraviețuiește unui Redis pierdut și unui restart;
  • CAS-ul pe `(environment, revision)` e impus de un unique index, nu de o secvență
    citește-apoi-scrie care are o fereastră de race între cele două;
  • CHECK-ul de vocabular respinge un track inventat (un cohort fantomă în raport se citește ca
    „candidate e mai bun");
  • checkerul de cutover refuză închiderea cât timp există ture active fără captură (= v1).

Cere migrările 040 + 044 aplicate — altfel modulul se SKIP-uie cu mesaj explicit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from scripts.cutover_check import evaluate_closure
from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries import web_turns as wt
from src.db.queries.release import (
    count_active_turns,
    current_policy,
    insert_policy_revision,
    latest_capture,
    load_cohort_facts,
    policy_history,
)
from src.release import policy_store
from src.release.assignment import ReleaseContext
from src.release.models import (
    DECISION_CANDIDATE,
    DECISION_CONTROL,
    MODE_CANARY,
    TRACK_CANDIDATE,
    TRACK_CHAMPION,
    ReleasePolicy,
)
from src.web import turn_service as ts
from tests.test_release_assignment import SALT

pytestmark = [pytest.mark.integration]

T0 = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
VIEW = {"content": "Salut! Uite serul potrivit.", "products": [{"name": "Ser X"}]}


def _policy(bid: str, *, percent: int, stage: int, revision: int, env: str) -> ReleasePolicy:
    return ReleasePolicy(
        policy_id=f"nx249-e2e-{env}",
        revision=revision,
        environment=env,
        created_at=(T0 - timedelta(hours=2)).isoformat(),
        not_before=(T0 - timedelta(hours=1)).isoformat(),
        expires_at=(T0 + timedelta(days=30)).isoformat(),
        control_release_sha="c0ntr0l1234567",
        control_pipeline_version="web-chat.v1",
        candidate_release_sha="cand1date7654321",
        candidate_pipeline_version="web-view.v2",
        mode=MODE_CANARY,
        percent=percent,
        stage=stage,
        eligible_business_ids=(bid,),
        stable_salt_id="salt-e2e",
        quality_packet_hash="sha256:q",
        e2e_packet_hash="sha256:e",
        deploy_manifest_hash="sha256:d",
        slo_policy_version="slo_policy.v1",
        quality_policy_version="nx246-gate-v1",
        approved_by="adi",
        approved_at=T0.isoformat(),
        change_ticket="NX-249",
    )


async def _make_business(conn) -> str:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-249 release', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx249-{uuid4().hex[:8]}",
    )
    return bid


async def _make_scope(conn, bid: str) -> tuple[str, str, str]:
    channel_id = str(uuid4())
    await conn.execute(
        "insert into channels (id, business_id, kind, provider_account_id) "
        "values ($1, $2, 'webchat', $3)",
        channel_id,
        bid,
        f"tok-{uuid4().hex[:10]}",
    )
    contact_id = await conn.fetchval(
        "insert into contacts (business_id) values ($1) returning id", bid
    )
    conversation_id = await conn.fetchval(
        "insert into conversations (business_id, contact_id, channel_id) "
        "values ($1, $2, $3) returning id",
        bid,
        str(contact_id),
        channel_id,
    )
    return channel_id, str(contact_id), str(conversation_id)


@pytest.fixture(autouse=True)
async def _require_migrations():
    """Function-scoped deliberat (ca la NX-232): un fixture async module-scoped ar crea poolul în
    alt event loop decât testele → InterfaceError."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        has_ledger = await conn.fetchval("select to_regclass('public.web_turns') is not null")
        has_release = await conn.fetchval(
            "select to_regclass('public.release_policies') is not null"
        )
        has_columns = await conn.fetchval(
            "select count(*) from information_schema.columns "
            "where table_name = 'web_turns' and column_name = 'release_track'"
        )
    if not has_ledger:
        pytest.skip("migrarea 040_web_turns nu e aplicată (rulează scripts/migrate.py)")
    if not has_release or not has_columns:
        pytest.skip("migrarea 044_release_policy nu e aplicată (rulează scripts/migrate.py)")
    policy_store.reset_cache()


@pytest.fixture
async def shop():
    """Un business throwaway + mediu de release izolat per test (numele mediului e unic)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        bid = await _make_business(conn)
    env = f"e2e-{uuid4().hex[:8]}"
    try:
        yield bid, env
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from businesses where id = $1", bid)
            await conn.execute("delete from release_policies where environment = $1", env)
            await conn.execute(
                "delete from audit_log where action = 'release_policy_apply' "
                "and details->>'environment' = $1",
                env,
            )
        await close_pool()
        policy_store.reset_cache()


async def _window(conn) -> tuple[datetime, datetime]:
    """Fereastra raportului, ancorată în ceasul DB-ului.

    `accepted_at` are `default now()`, deci îl pune SERVERUL. O fereastră calculată cu ceasul
    mașinii de test poate rata rândurile proaspete când cele două ceasuri diferă cu câteva
    secunde — exact genul de test „instabil pe CI" care se rezolvă prin dezactivare.
    """
    db_now = await conn.fetchval("select now()")
    return db_now - timedelta(days=1), db_now + timedelta(days=1)


async def _accept(conn, bid, conv, contact, ctx: ReleaseContext | None, *, client_id=None):
    """Acceptul, cu asignarea rezolvată exact ca în `turn_service.accept_web_turn`."""
    prior = await latest_capture(conn, bid, conv)
    assignment = ctx.decide(bid, conv, prior) if ctx else None
    row = await wt.insert_turn(
        conn,
        bid,
        conv,
        contact,
        client_id or str(uuid4()),
        f"fp-{uuid4().hex[:8]}",
        session_ref_hash="h",
        conversation_revision=0,
        pipeline_version=ts.RESPONSE_CONTRACT_SYNC_V1,
        release_track=assignment.track if assignment else None,
        release_policy_id=(assignment.policy_id or None) if assignment else None,
        release_policy_revision=assignment.policy_revision if assignment else None,
    )
    return row, assignment


# ── Captura pe ledger ───────────────────────────────────────────────────────────────────────
async def test_asignarea_se_captureaza_in_acelasi_insert_cu_randul_de_ledger(shop):
    """Nu există fereastră în care un turn să existe fără cohort."""
    bid, env = shop
    pool = await get_pool()
    ctx = ReleaseContext(
        policy=_policy(bid, percent=100, stage=6, revision=0, env=env),
        available=True,
        salt=SALT,
        now=T0,
    )
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        row, assignment = await _accept(conn, bid, conv, contact, ctx)
        stored = await wt.get_turn_by_id(conn, bid, row.id)
    assert assignment.decision == DECISION_CANDIDATE
    assert stored.release_track == TRACK_CANDIDATE
    assert stored.release_policy_id == f"nx249-e2e-{env}"
    assert stored.release_policy_revision == 0


async def test_un_track_inventat_e_respins_de_schema(shop):
    """Un cohort fantomă în raport se citește ca „candidate e mai bun" — deci CHECK în DB."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        with pytest.raises(asyncpg.CheckViolationError):
            await wt.insert_turn(
                conn,
                bid,
                conv,
                contact,
                str(uuid4()),
                "fp",
                release_track="canary",  # nu e un pipeline, e un MOD de rollout
            )


async def test_randurile_fara_captura_raman_null_nu_champion(shop):
    """Expand-only: imaginea precedentă scrie fără coloane, iar NULL rămâne `unknown` în raport."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        row, assignment = await _accept(conn, bid, conv, contact, None)
        stored = await wt.get_turn_by_id(conn, bid, row.id)
        w_from, w_to = await _window(conn)
        facts = await load_cohort_facts(conn, bid, window_from=w_from, window_to=w_to)
    assert assignment is None
    assert stored.release_track is None
    assert [f.track for f in facts.facts] == ["unknown"]


# ── Epoch: sticky din ledger ────────────────────────────────────────────────────────────────
async def test_sticky_ul_se_rederiva_din_ledger_dupa_restart(shop):
    """Nu din Redis, nu din cookie: un FLUSHALL nu are voie să reasigneze o conversație."""
    bid, env = shop
    pool = await get_pool()
    p100 = _policy(bid, percent=100, stage=6, revision=0, env=env)
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        first, a1 = await _accept(conn, bid, conv, contact, ReleaseContext(p100, True, SALT, T0))
        await wt.claim_turn(conn, bid, first.id, owner="w1", lease_ttl_s=60)
        await wt.complete_turn(conn, bid, first.id, lease_epoch=1, response_json=VIEW)
        # Proces NOU, cache gol, alt policy (canary la 5%) — sticky-ul vine din DB.
        p5 = _policy(bid, percent=5, stage=3, revision=1, env=env)
        capture = await latest_capture(conn, bid, conv)
        second = ReleaseContext(p5, True, SALT, T0).decide(bid, conv, capture)
    assert a1.decision == DECISION_CANDIDATE
    assert capture.track == TRACK_CANDIDATE
    assert second.decision == DECISION_CANDIDATE
    assert second.reason == "sticky_epoch"


async def test_cresterea_procentului_nu_muta_conversatia_pe_date_reale(shop):
    bid, env = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        # Etapa „observe" nu livrează: prima tură iese control.
        observe = _policy(bid, percent=5, stage=3, revision=0, env=env).model_copy(
            update={"mode": "observe", "percent": 0, "stage": 0}
        )
        first, a1 = await _accept(conn, bid, conv, contact, ReleaseContext(observe, True, SALT, T0))
        await wt.claim_turn(conn, bid, first.id, owner="w", lease_ttl_s=60)
        await wt.complete_turn(conn, bid, first.id, lease_epoch=1, response_json=VIEW)
        # Ridicăm la 100%: conversația EXISTENTĂ rămâne control.
        p100 = _policy(bid, percent=100, stage=6, revision=1, env=env)
        second, a2 = await _accept(conn, bid, conv, contact, ReleaseContext(p100, True, SALT, T0))
        stored = await wt.get_turn_by_id(conn, bid, second.id)
        # ...dar o conversație NOUĂ intră în epochul nou.
        _, contact2, conv2 = await _make_scope(conn, bid)
        _, a3 = await _accept(conn, bid, conv2, contact2, ReleaseContext(p100, True, SALT, T0))
    assert a1.decision == DECISION_CONTROL
    assert a2.decision == DECISION_CONTROL
    assert a2.reason == "sticky_epoch"
    assert stored.release_track == TRACK_CHAMPION
    assert a3.decision == DECISION_CANDIDATE


async def test_reclaim_ul_nu_atinge_captura(shop):
    """Failure matrix: „retry/reclaim după deploy → același pipeline"."""
    bid, env = shop
    pool = await get_pool()
    ctx = ReleaseContext(_policy(bid, percent=100, stage=6, revision=0, env=env), True, SALT, T0)
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        row, _ = await _accept(conn, bid, conv, contact, ctx)
        await wt.claim_turn(conn, bid, row.id, owner="w1", lease_ttl_s=60)
        await conn.execute(
            "update web_turns set lease_expires_at = now() - interval '1 minute' where id = $1",
            row.id,
        )
        reclaimed = await wt.claim_turn(conn, bid, row.id, owner="w2", lease_ttl_s=60)
        after = await wt.get_turn_by_id(conn, bid, row.id)
    assert reclaimed.reclaimed is True
    assert after.attempt == 2
    assert after.release_track == TRACK_CANDIDATE
    assert after.release_policy_revision == 0


# ── Storeul de policy ───────────────────────────────────────────────────────────────────────
async def test_cas_ul_e_impus_de_unique_nu_de_cod(shop):
    """Doi operatori simultani: al doilea PIERDE, determinist, la nivel de schemă."""
    bid, env = shop
    pool = await get_pool()
    p = _policy(bid, percent=5, stage=3, revision=0, env=env)
    async with admin_conn(pool) as conn:
        first = await insert_policy_revision(
            conn,
            environment=env,
            revision=0,
            policy_id=p.policy_id,
            policy=p.to_payload(),
            actor="adi",
            reason="etapa 3",
        )
        second = await insert_policy_revision(
            conn,
            environment=env,
            revision=0,
            policy_id=p.policy_id,
            policy=p.to_payload(),
            actor="altcineva",
            reason="tot etapa 3",
        )
        current = await current_policy(conn, env)
    assert first is True
    assert second is False, "a doua inserție pe aceeași revizie trebuie să piardă"
    assert current.actor == "adi"


async def test_istoricul_e_append_only_si_reproductibil(shop):
    bid, env = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        for rev, pct, stage in ((0, 5, 3), (1, 20, 4), (2, 50, 5)):
            p = _policy(bid, percent=pct, stage=stage, revision=rev, env=env)
            await policy_store.apply(
                conn,
                p,
                expected_revision=None if rev == 0 else rev - 1,
                actor="adi",
                reason=f"etapa {stage}",
                environment=env,
            )
        history = await policy_history(conn, env)
        current = await current_policy(conn, env)
    assert [r.revision for r in history] == [2, 1, 0]
    assert current.policy["percent"] == 50
    # Reviziile vechi rămân CITIBILE: „ce policy era atunci" e un SELECT, nu o presupunere.
    assert history[-1].policy["percent"] == 5


async def test_apply_ul_lasa_urma_in_audit_log(shop):
    bid, env = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await policy_store.apply(
            conn,
            _policy(bid, percent=5, stage=3, revision=0, env=env),
            expected_revision=None,
            actor="oncall",
            reason="pilot",
            environment=env,
        )
        rows = await conn.fetch(
            "select actor, action, entity, details from audit_log "
            "where action = 'release_policy_apply' and details->>'environment' = $1",
            env,
        )
    assert len(rows) == 1
    assert rows[0]["actor"] == "oncall"
    assert rows[0]["entity"] == "release_policy"
    # Auditul numără tenanții, nu-i enumeră.
    assert bid not in rows[0]["details"]


async def test_policy_ul_nu_e_lizibil_de_pe_conexiunea_de_tenant(shop):
    """Rândul poartă allowlistul: un tenant n-are voie să afle cine altcineva e în canary.

    Migrarea 044 nu dă grant lui `bot_runtime`. Pe deploymenturile în care poolul de bot rulează
    în mod COMPAT (logat ca admin, fără `DATABASE_URL_BOT` — vezi `src/db/connection.py`), poarta
    nu se poate observa, deci testul se sare explicit în loc să dea o falsă siguranță.
    """
    from src.db import connection as dbconn

    bid, env = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await policy_store.apply(
            conn,
            _policy(bid, percent=5, stage=3, revision=0, env=env),
            expected_revision=None,
            actor="adi",
            reason="pilot",
            environment=env,
        )
    await dbconn.get_bot_pool()
    if not dbconn._bot_login_mode:
        pytest.skip("bot_pool în mod compat (logat ca admin) — grantul nu se poate observa")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with dbconn.tenant_conn(bid) as conn:
            await conn.fetch("select * from release_policies")


# ── Drenare + cutover ───────────────────────────────────────────────────────────────────────
async def test_turele_active_se_numara_pe_cohort(shop):
    bid, env = shop
    pool = await get_pool()
    ctx = ReleaseContext(_policy(bid, percent=100, stage=6, revision=0, env=env), True, SALT, T0)
    async with admin_conn(pool) as conn:
        _, c1, conv1 = await _make_scope(conn, bid)
        _, c2, conv2 = await _make_scope(conn, bid)
        _, c3, conv3 = await _make_scope(conn, bid)
        await _accept(conn, bid, conv1, c1, ctx)  # candidate, activ
        await _accept(conn, bid, conv2, c2, None)  # fără captură = v1 in-flight
        done, _ = await _accept(conn, bid, conv3, c3, ctx)
        await wt.claim_turn(conn, bid, done.id, owner="w", lease_ttl_s=60)
        await wt.complete_turn(conn, bid, done.id, lease_epoch=1, response_json=VIEW)
        active = await count_active_turns(conn, bid)
    assert active.get(TRACK_CANDIDATE) == 1
    assert active.get("unknown") == 1
    assert TRACK_CHAMPION not in active


async def test_inchiderea_v1_e_refuzata_cat_timp_exista_ture_fara_captura(shop):
    """Failure matrix: „v1 turn încă active la close → ruta nu se închide"."""
    bid, env = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        await _accept(conn, bid, conv, contact, None)  # v1 in-flight
        active = await count_active_turns(conn, bid)
    verdict = evaluate_closure(
        active=active, policy_mode=MODE_CANARY, policy_stage_index=6, soak_hours=400
    )
    assert verdict["can_close_v1"] is False
    assert any("v1 in-flight" in r for r in verdict["blocking_reasons"])


async def test_inchiderea_v1_trece_cand_nu_mai_e_nimic_activ(shop):
    bid, env = shop
    pool = await get_pool()
    ctx = ReleaseContext(_policy(bid, percent=100, stage=6, revision=0, env=env), True, SALT, T0)
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        row, _ = await _accept(conn, bid, conv, contact, ctx)
        await wt.claim_turn(conn, bid, row.id, owner="w", lease_ttl_s=60)
        await wt.complete_turn(conn, bid, row.id, lease_epoch=1, response_json=VIEW)
        active = await count_active_turns(conn, bid)
    verdict = evaluate_closure(
        active=active, policy_mode=MODE_CANARY, policy_stage_index=6, soak_hours=400
    )
    assert verdict["can_close_v1"] is True
    assert verdict["blocking_reasons"] == []


async def test_inchiderea_v1_e_refuzata_inainte_de_etapa_6(shop):
    verdict = evaluate_closure(
        active={}, policy_mode=MODE_CANARY, policy_stage_index=4, soak_hours=1000
    )
    assert verdict["can_close_v1"] is False
    assert any("etapa 6" in r for r in verdict["blocking_reasons"])


async def test_inchiderea_v1_e_refuzata_fara_soak(shop):
    verdict = evaluate_closure(
        active={}, policy_mode=MODE_CANARY, policy_stage_index=6, soak_hours=12
    )
    assert verdict["can_close_v1"] is False
    assert any("soak" in r for r in verdict["blocking_reasons"])


# ── Raportul de cohort ──────────────────────────────────────────────────────────────────────
async def test_faptele_de_cohort_sunt_tenant_scoped_si_fara_continut(shop):
    """P7 + P12: raportul numără, nu citește conversații."""
    bid, env = shop
    pool = await get_pool()
    ctx = ReleaseContext(_policy(bid, percent=100, stage=6, revision=0, env=env), True, SALT, T0)
    async with admin_conn(pool) as conn:
        other = await _make_business(conn)
        try:
            _, contact, conv = await _make_scope(conn, bid)
            row, _ = await _accept(conn, bid, conv, contact, ctx)
            await wt.claim_turn(conn, bid, row.id, owner="w", lease_ttl_s=60)
            await wt.complete_turn(conn, bid, row.id, lease_epoch=1, response_json=VIEW)
            _, oc, oconv = await _make_scope(conn, other)
            await _accept(conn, other, oconv, oc, ctx)
            w_from, w_to = await _window(conn)
            mine = await load_cohort_facts(conn, bid, window_from=w_from, window_to=w_to)
        finally:
            await conn.execute("delete from businesses where id = $1", other)
    assert len(mine.facts) == 1, "faptele altui tenant nu au ce căuta în raport"
    fact = mine.facts[0]
    assert fact.track == TRACK_CANDIDATE
    assert fact.renderable is True
    assert not hasattr(fact, "response_json")
    assert not hasattr(fact, "conversation_id")
