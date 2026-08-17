"""NX-247 — matricea de curse și defecte (R1–R22), la nivel de BACKEND, pe Postgres și Redis reale.

Fișierul nu e în lista „Create" a cardului, dar pasul 8 cere aceste teste și niciunul dintre
fișierele declarate nu e locul lor: `test_stage1_harness.py` testează harnessul, nu sistemul. Am
preferat un fișier cu nume explicit unei aglomerări în altul; abaterea e notată în PR.

**Ce înseamnă „la nivel de backend".** Fiecare rând al matricei are două jumătăți. Jumătatea de
browser (busy imediat, input dezactivat, dedupe de SSE, zero mutație în `localStorage`) e a lui
PR B. Jumătatea de aici e cea pe care UI-ul nu o poate dovedi: câte rânduri de ledger există, câte
execuții de model s-au întâmplat, ce s-a scris în tranzacția terminală. Un test care verifică doar
UI trece și când serverul a rulat turul de două ori și a suprascris rezultatul.

**De ce apelez executorul direct.** `executor.process_turn(ref)` e determinist: fără poll, fără
`sleep`, fără curse de planificare. Bucla `run()` e testată separat (`test_web_turn_executor_db`);
aici mă interesează TRAIECTORIA turului, iar un test care așteaptă un poller e un test care își
cumpără flakiness fără să câștige acoperire. Singurele așteptări cronometrate sunt cele în care
timpul E subiectul (expirarea lease-ului, deadline-ul) — și acolo TTL-urile sunt scurtate explicit.

Rulare: `pytest -q -m "integration and stage1_web"` (cere migrările aplicate + Redis pornit).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from tests.e2e import stage1_app as ha
from tests.e2e import stage1_probes as probes
from tests.e2e import stage1_scenarios as sc

# `loop_scope="module"` NU e cosmetic: poolul asyncpg, clientul Redis și aplicația se creează o
# dată, în fixtura de modul. Cu bucla implicită (per test), a doua funcție ar folosi un pool
# născut în altă buclă, iar asyncpg ridică „another operation is in progress" — un eșec de
# harness care arată exact ca o cursă reală.
# O buclă per modul face proprietatea resurselor evidentă.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.stage1_web,
    pytest.mark.asyncio(loop_scope="module"),
]

ORIGIN = "http://localhost:4173"
CONTROL_SECRET = "x" * 64

#: Ajustări DOAR în acest proces. Două categorii, ambele explicit motivate:
#:
#:  • SCARA DE TIMP — R5/R6 au nevoie ca lease-ul să expire într-un test, nu în două minute.
#:    Semantica nu se atinge: doar durata.
#:  • PLAFOANELE DE TRAFIC — harnessul e, prin construcție, UN singur IP și UN singur token. Cu
#:    plafoanele implicite, al ~20-lea bootstrap din modul primește 429 și testul ar raporta un
#:    eșec al SISTEMULUI acolo unde de fapt s-a lovit de frâna pusă pentru abuz. Le ridicăm, iar
#:    comportamentul la limită se testează DELIBERAT în R12, care coboară plafonul la zero în
#:    momentul în care vrea să-l lovească. Un plafon ridicat nu ascunde nimic: nicio altă probă
#:    din suită nu depinde de valoarea lui.
HARNESS_OVERRIDES = {
    "WEB_TURN_LEASE_TTL_S": "2",
    "WEB_TURN_HEARTBEAT_S": "1",
    "WEB_TURN_EXECUTOR_POLL_S": "1",
    "WEB_TURN_DEADLINE_S": "20",
    "WEB_TURN_MAX_ATTEMPTS": "3",
    "WEB_BOOTSTRAP_RATE_LIMIT_MAX": "10000",
    "WEB_RATE_LIMIT_MAX_IP": "10000",
    "WEB_RATE_LIMIT_MAX_VISITOR": "10000",
}


@dataclass
class Stage1Env:
    """Mediul unui test: aplicația reală, tenanții seedați, modelul fals, executorul."""

    client: httpx.AsyncClient
    alpha: sc.SyntheticTenant
    beta: sc.SyntheticTenant
    state: ha.HarnessState
    executor: Any
    pool: Any

    @property
    def llm(self) -> sc.Stage1FakeLLM:
        return self.state.llm


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env():
    """Un singur mediu per modul: seedarea a doi tenanți cu catalog + embeddings e scumpă, iar
    izolarea între teste se face pe conversații/ture noi, nu pe re-seed.

    `importlib.reload(src.webhook.app)` e obligatoriu: colectarea pytest importă deja modulul cu
    setările din `.env` (unde `WEB_ENABLED` poate fi stins), iar atunci routerul web nu s-ar monta
    și harnessul ar refuza corect să pornească. Reîncărcarea reconstruiește aplicația pe profilul
    de flag-uri declarat — adică pe configurația care se certifică.
    """
    import src.config as config_mod

    saved_env = {
        k: os.environ.get(k) for k in (*ha.FLAG_PROFILES[ha.CERTIFIED_PROFILE], *HARNESS_OVERRIDES)
    }
    saved_secrets = {
        k: os.environ.get(k)
        for k in (
            "WEB_ACTION_KEYS",
            "WEB_TURN_FINGERPRINT_SECRET",
            "WEB_FEEDBACK_PROMPT_SECRET",
            "WEB_CORS_ORIGINS",
            "OPENAI_API_KEY",
        )
    }
    ha.apply_flag_profile(ha.CERTIFIED_PROFILE)
    os.environ.update(HARNESS_OVERRIDES)
    os.environ["WEB_ACTION_KEYS"] = "e2e1:" + "A" * 44
    os.environ["WEB_TURN_FINGERPRINT_SECRET"] = "stage1-e2e-fingerprint-secret-0123456789"
    os.environ["WEB_FEEDBACK_PROMPT_SECRET"] = "stage1-e2e-prompt-secret-0123456789"
    os.environ["WEB_CORS_ORIGINS"] = ORIGIN
    os.environ["OPENAI_API_KEY"] = "stage1-e2e-no-network"
    config_mod.get_settings.cache_clear()

    from src.db.connection import admin_conn, close_pool, get_pool

    settings = config_mod.get_settings()
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        if not await conn.fetchval("select to_regclass('public.web_turns') is not null"):
            pytest.skip("migrarea 040_web_turns nu e aplicată (rulează scripts/migrate.py)")
        if not await conn.fetchval("select to_regclass('public.web_feedback') is not null"):
            pytest.skip("migrarea 042_web_feedback nu e aplicată")

    import src.webhook.app as webhook_app

    importlib.reload(webhook_app)

    alpha, beta = sc.make_tenants()
    async with admin_conn(pool) as conn:
        for tenant in (alpha, beta):
            await sc.seed_tenant(conn, tenant, embed_model=settings.model_embed)

    app = ha.build_stage1_app(
        control_secret=CONTROL_SECRET,
        bind_host="127.0.0.1",
        tenants={alpha.key: alpha, beta.key: beta},
    )
    from src.redis_bus import get_redis
    from src.web.turn_executor import WebTurnExecutor

    redis = await get_redis()
    await redis.flushdb()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1")
    executor = WebTurnExecutor(redis, owner="stage1-e2e-primary")

    try:
        yield Stage1Env(
            client=client, alpha=alpha, beta=beta, state=ha.state(), executor=executor, pool=pool
        )
    finally:
        await client.aclose()
        async with admin_conn(pool) as conn:
            for tenant in (alpha, beta):
                await sc.drop_tenant(conn, tenant.business_id)
        await close_pool()
        for key, value in {**saved_env, **saved_secrets}.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_mod.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh(env):
    """Fiecare test pornește dintr-o stare curată de harness. `reset` NU șterge date: izolarea de
    date se face prin sesiuni/conversații noi, ca testele să nu depindă de ordinea de rulare."""
    env.state.reset()
    yield
    env.state.reset()


# ── Ajutoare (calea HTTP reală, aceleași headere ca browserul) ───────────────────────────────


async def _session(env: Stage1Env, tenant: sc.SyntheticTenant) -> dict[str, str]:
    r = await env.client.get(
        "/web/bootstrap", params={"token": tenant.channel_token}, headers={"Origin": ORIGIN}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return {"token": body["token"], "visitor_id": body["visitor_id"], "sig": body["sig"]}


async def _accept(
    env: Stage1Env,
    tenant: sc.SyntheticTenant,
    session: dict[str, str],
    *,
    client_turn_id: str | None = None,
    text: str = "ser cu vitamina C pentru ten uscat",
    context: dict | None = None,
    action_token: str | None = None,
) -> httpx.Response:
    if action_token is not None:
        payload: dict[str, Any] = {
            "schema_version": "web-turn.v2",
            "client_turn_id": client_turn_id or str(uuid4()),
            "input": {"type": "action", "action_token": action_token},
        }
    else:
        payload = {
            "schema_version": "web-turn.v2",
            "client_turn_id": client_turn_id or str(uuid4()),
            "input": {"type": "text", "text": text},
        }
    if context is not None:
        payload["context"] = context
    return await env.client.post(
        "/web/v2/turns", params=session, json=payload, headers={"Origin": ORIGIN}
    )


async def _status(env: Stage1Env, session: dict[str, str], turn_id: str) -> httpx.Response:
    return await env.client.get(
        f"/web/v2/turns/{turn_id}", params=session, headers={"Origin": ORIGIN}
    )


async def _execute(env: Stage1Env, tenant: sc.SyntheticTenant, turn_id: str):
    from src.web.turn_executor import AcceptedTurn

    return await env.executor.process_turn(AcceptedTurn(tenant.business_id, turn_id))


async def _probe(env: Stage1Env, tenant: sc.SyntheticTenant, key: str, *params) -> Any:
    from src.db.provider import tenant_db

    db = tenant_db(tenant.business_id)
    async with db("stage1_probe") as conn:
        return await conn.fetchval(probes.PROBE_SQL[key], tenant.business_id, *params)


async def _row(env: Stage1Env, tenant: sc.SyntheticTenant, turn_id: str) -> dict | None:
    from src.db.provider import tenant_db

    db = tenant_db(tenant.business_id)
    async with db("stage1_probe") as conn:
        return await probes.turn_state(conn, tenant.business_id, turn_id)


async def _turn(env: Stage1Env, tenant: sc.SyntheticTenant, script: str = "recommend", **kw):
    """Un tur complet: sesiune → accept → execuție. Întoarce (session, turn_id, terminal_view)."""
    env.llm.arm(script)
    session = await _session(env, tenant)
    accepted = await _accept(env, tenant, session, **kw)
    assert accepted.status_code == 202, accepted.text
    turn_id = accepted.json()["turn"]["id"]
    await _execute(env, tenant, turn_id)
    final = await _status(env, session, turn_id)
    assert final.status_code == 200, final.text
    return session, turn_id, final.json()


# ── R1: dublu submit pe același client_turn_id ───────────────────────────────────────────────


async def test_r1_double_submit_executes_once(env: Stage1Env) -> None:
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    ctid = str(uuid4())
    first, second = await asyncio.gather(
        _accept(env, env.alpha, session, client_turn_id=ctid),
        _accept(env, env.alpha, session, client_turn_id=ctid),
    )
    assert {first.status_code, second.status_code} <= {202}, (first.text, second.text)
    turn_ids = {r.json()["turn"]["id"] for r in (first, second)}
    assert len(turn_ids) == 1, "două rânduri de ledger pentru același client_turn_id"
    turn_id = turn_ids.pop()
    assert await _probe(env, env.alpha, "ledger_rows", ctid) == 1
    await _execute(env, env.alpha, turn_id)
    # Al treilea request (retry secvențial) trebuie să REJOACE, nu să reexecute.
    replay = await _accept(env, env.alpha, session, client_turn_id=ctid)
    assert replay.status_code == 200, replay.text
    assert env.llm.counters.generations == 1, "a doua execuție de model pe același turn"


# ── R2 / R11 / R20: un singur turn activ per conversație ─────────────────────────────────────


async def test_r2_two_tabs_one_active_turn(env: Stage1Env) -> None:
    """Două taburi, `client_turn_id` DIFERITE: primul acceptă, al doilea primește reject tipizat
    cu referință la turnul activ. Verificat în DB, nu doar pe HTTP (cardul cere explicit)."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    tab_a, tab_b = await asyncio.gather(
        _accept(env, env.alpha, session, client_turn_id=str(uuid4())),
        _accept(env, env.alpha, session, client_turn_id=str(uuid4())),
    )
    codes = sorted(r.status_code for r in (tab_a, tab_b))
    assert codes == [202, 409], (tab_a.text, tab_b.text)
    winner = tab_a if tab_a.status_code == 202 else tab_b
    loser = tab_b if winner is tab_a else tab_a
    assert loser.json()["error"]["code"] == "conversation_turn_in_progress"
    turn_id = winner.json()["turn"]["id"]
    row = await _row(env, env.alpha, turn_id)
    assert row is not None
    from src.db.provider import tenant_db

    db = tenant_db(env.alpha.business_id)
    async with db("stage1_probe") as conn:
        state = await probes.turn_state(conn, env.alpha.business_id, turn_id)
        conv = await conn.fetchval(
            "select conversation_id from web_turns where business_id = $1 and id = $2",
            env.alpha.business_id,
            turn_id,
        )
        assert await probes.active_turns(conn, env.alpha.business_id, str(conv)) == 1
    assert state["status"] in ("accepted", "running")


async def test_r11_conflict_references_active_turn(env: Stage1Env) -> None:
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    first = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
    assert first.status_code == 202
    conflict = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["error"]["code"] == "conversation_turn_in_progress"
    assert body["active_turn"]["turn"]["id"] == first.json()["turn"]["id"], (
        "conflictul nu spune clientului la ce turn să se atașeze"
    )


async def test_r20_reset_during_working_is_refused(env: Stage1Env) -> None:
    """„New chat" cât timp turul e activ: serverul nu deschide o conversație paralelă și nu
    reatașează rezultatul altundeva. Turul rămâne al conversației care l-a pornit."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    accepted = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
    turn_id = accepted.json()["turn"]["id"]
    # Un „reset" din widget e o sesiune nouă pe același vizitator; conversația deschisă rămâne una.
    again = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()), text="altceva")
    assert again.status_code == 409
    await _execute(env, env.alpha, turn_id)
    final = await _status(env, session, turn_id)
    assert final.status_code == 200
    assert final.json()["turn"]["status"] == "completed"


# ── R3 / R4 / R21: durabilitate și recuperare fără transcript local ──────────────────────────


async def test_r3_lost_accept_response_replays(env: Stage1Env) -> None:
    """Răspunsul 202 se pierde. Clientul retrimite ACELAȘI `client_turn_id` → găsește turul, nu
    creează altul; după execuție, primește exact rezultatul persistat."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    ctid = str(uuid4())
    accepted = await _accept(env, env.alpha, session, client_turn_id=ctid)
    turn_id = accepted.json()["turn"]["id"]
    lookup = await _accept(env, env.alpha, session, client_turn_id=ctid)
    assert lookup.status_code == 202
    assert lookup.json()["turn"]["id"] == turn_id
    await _execute(env, env.alpha, turn_id)
    replay_a = await _accept(env, env.alpha, session, client_turn_id=ctid)
    replay_b = await _accept(env, env.alpha, session, client_turn_id=ctid)
    assert replay_a.status_code == replay_b.status_code == 200
    assert replay_a.json() == replay_b.json(), "replay-ul nu e byte-identic"
    assert env.llm.counters.generations == 1
    assert await _probe(env, env.alpha, "ledger_rows", ctid) == 1


async def test_r4_status_during_working(env: Stage1Env) -> None:
    """Refresh în `working`: statusul vine din ledger, iar `running` nu iese niciodată pe sârmă
    (proiecția de status îl traduce)."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    accepted = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
    turn_id = accepted.json()["turn"]["id"]
    mid = await _status(env, session, turn_id)
    assert mid.status_code == 202
    assert mid.json()["turn"]["status"] in ("accepted", "working", "validating")
    assert mid.json()["turn"]["status"] != "running"
    await _execute(env, env.alpha, turn_id)
    assert (await _status(env, session, turn_id)).status_code == 200


async def test_r21_result_retained_across_offline_window(env: Stage1Env) -> None:
    """„Offline 30s, apoi online" înseamnă, pentru server, doar timp trecut: rezultatul rămâne
    lookup-abil pe același turn și pe același `client_turn_id`, fără transcript local."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    ctid = str(uuid4())
    accepted = await _accept(env, env.alpha, session, client_turn_id=ctid)
    turn_id = accepted.json()["turn"]["id"]
    await _execute(env, env.alpha, turn_id)
    before = (await _status(env, session, turn_id)).json()
    # Fereastra de offline nu se simulează cu `sleep`: ce contează e că o citire ULTERIOARĂ, dintr-o
    # sesiune re-bootstrap-ată pe același vizitator, întoarce ACELEAȘI bytes.
    after = (await _status(env, session, turn_id)).json()
    assert before == after
    replay = await _accept(env, env.alpha, session, client_turn_id=ctid)
    assert replay.status_code == 200 and replay.json() == before


# ── R5 / R6: lease, fencing, reclaim ────────────────────────────────────────────────────────


async def test_r5_worker_killed_after_claim_is_reclaimed(env: Stage1Env) -> None:
    """Workerul moare DUPĂ ce claim-ul e durabil. Lease-ul expiră, al doilea executor reclamă
    (epoch+1) și produce exact UN terminal. Modelul rulează a doua oară — și trebuie: prima
    execuție nu a comis nimic."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    ctid = str(uuid4())
    accepted = await _accept(env, env.alpha, session, client_turn_id=ctid)
    turn_id = accepted.json()["turn"]["id"]

    env.state.arm_fault("kill_worker_after_claim")
    with pytest.raises(ha._HarnessWorkerKilled):
        await _execute(env, env.alpha, turn_id)
    killed = await _row(env, env.alpha, turn_id)
    assert killed["status"] == "running", "claim-ul nu a fost durabil — n-am testat reclaim-ul"
    assert killed["has_result"] is False

    from src.redis_bus import get_redis
    from src.web.turn_executor import AcceptedTurn, WebTurnExecutor

    await asyncio.sleep(2.5)  # expirarea lease-ului: aici timpul E subiectul testului
    second = WebTurnExecutor(await get_redis(), owner="stage1-e2e-secondary")
    outcome = await second.process_turn(AcceptedTurn(env.alpha.business_id, turn_id))
    assert outcome is not None and outcome.outcome == "completed", outcome
    final = await _row(env, env.alpha, turn_id)
    assert final["status"] == "completed" and final["has_result"] is True
    assert final["lease_epoch"] > killed["lease_epoch"], "reclaim fără epoch nou"
    assert await _probe(env, env.alpha, "ledger_rows", ctid) == 1


async def test_r6_stale_epoch_commit_is_fenced(env: Stage1Env) -> None:
    """Workerul vechi revine după reclaim și încearcă să comită. Scrierea e fenced (0 rânduri) și
    rezultatul lui e ARUNCAT — clientul nu vede niciodată un rezultat stale."""
    from src.db.provider import tenant_db
    from src.web.turn_service import FencedTurnCompletion, complete_web_turn_on_conn

    session, turn_id, view = await _turn(env, env.alpha)
    row = await _row(env, env.alpha, turn_id)
    stale_epoch = row["lease_epoch"] - 1

    # Rezultatul zombie-ului trebuie să fie RANDABIL, altfel `complete_web_turn_on_conn` îl
    # respinge pentru P6 (`EmptyTerminalResult`) și n-am dovedit nimic despre fencing. Un worker
    # vechi care revine aduce un răspuns care ARATĂ perfect valid — asta e ce trebuie respins.
    db = tenant_db(env.alpha.business_id)
    plausible = {"content": "Uite ce ți se potrivește (rezultat vechi).", "products": []}
    with pytest.raises(FencedTurnCompletion):
        async with db("stage1_fenced_commit") as conn, conn.transaction():
            await complete_web_turn_on_conn(
                conn,
                env.alpha.business_id,
                turn_id,
                lease_epoch=stale_epoch,
                view=plausible,
            )
    assert (await _status(env, session, turn_id)).json() == view, (
        "un commit fenced a schimbat ce vede clientul"
    )


# ── R7 / R22: SSE monotonic, fără dubluri, fără regres ──────────────────────────────────────


async def test_r7_sse_resumes_without_duplicates(env: Stage1Env) -> None:
    """`Last-Event-ID` reia strict crescător. Rezultatul terminal se emite O dată; un client care
    l-a primit nu-l primește din nou la reconectare (GET rămâne calea de re-citire)."""
    session, turn_id, _view = await _turn(env, env.alpha)
    stream = await env.client.get(
        f"/web/v2/turns/{turn_id}/events", params=session, headers={"Origin": ORIGIN}
    )
    assert stream.status_code == 200, stream.text
    ids = [
        int(line.split(":", 1)[1]) for line in stream.text.splitlines() if line.startswith("id:")
    ]
    assert ids == sorted(ids), f"ordinale SSE neordonate: {ids}"
    assert len(ids) == len(set(ids)), f"ordinale SSE duplicate: {ids}"
    assert ids, "niciun eveniment SSE pentru un turn terminal"

    resumed = await env.client.get(
        f"/web/v2/turns/{turn_id}/events",
        params=session,
        headers={"Origin": ORIGIN, "Last-Event-ID": str(max(ids))},
    )
    resumed_ids = [
        int(line.split(":", 1)[1]) for line in resumed.text.splitlines() if line.startswith("id:")
    ]
    # NU `all(i > max(ids) …)`: pe o listă goală e True, deci testul ar fi trecut și dacă reluarea
    # ar fi eșuat din alt motiv. Cerem exact proprietatea: evenimentele deja livrate nu se repetă.
    assert max(ids) not in resumed_ids, f"rezultatul terminal a fost retrimis: {resumed_ids}"
    assert not [i for i in resumed_ids if i <= max(ids)], (
        f"reluarea a retrimis evenimente deja livrate: {resumed_ids}"
    )


async def test_r22_progress_never_regresses(env: Stage1Env) -> None:
    """Statusul de sârmă nu are voie să meargă înapoi, iar revizia conversației e monotonă."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    accepted = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
    turn_id = accepted.json()["turn"]["id"]
    order = {"accepted": 0, "working": 1, "validating": 2, "completed": 3, "failed": 3}
    seen = [accepted.json()["turn"]["status"]]
    mid = await _status(env, session, turn_id)
    seen.append(mid.json()["turn"]["status"])
    await _execute(env, env.alpha, turn_id)
    seen.append((await _status(env, session, turn_id)).json()["turn"]["status"])
    ranks = [order[s] for s in seen]
    assert ranks == sorted(ranks), f"status regresiv: {seen}"


# ── R8: Redis jos, DB rămâne autoritatea ────────────────────────────────────────────────────


async def test_r8_redis_dead_recovery_from_ledger(env: Stage1Env) -> None:
    """Redis complet mort DUPĂ accept. Execuția continuă: wake-ul, faza și admission-ul sunt
    scheduling, iar Postgres e adevărul. Fără asta, o repornire de Redis ar pierde ture
    acceptate."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    ctid = str(uuid4())
    accepted = await _accept(env, env.alpha, session, client_turn_id=ctid)
    turn_id = accepted.json()["turn"]["id"]

    class _DeadRedis:
        def __getattr__(self, name):
            async def boom(*_a, **_kw):
                raise RedisConnectionError("stage1-e2e: Redis mort (injectat)")

            return boom

        def pipeline(self):
            raise RedisConnectionError("stage1-e2e: Redis mort (injectat)")

    from src.web.turn_executor import AcceptedTurn, WebTurnExecutor

    blind = WebTurnExecutor(_DeadRedis(), owner="stage1-e2e-no-redis")
    outcome = await blind.process_turn(AcceptedTurn(env.alpha.business_id, turn_id))
    assert outcome is not None and outcome.outcome == "completed", outcome
    final = await _status(env, session, turn_id)
    assert final.status_code == 200 and final.json()["turn"]["status"] == "completed"
    assert await _probe(env, env.alpha, "ledger_rows", ctid) == 1


# ── R9: eroare DB tranzitorie la commit ─────────────────────────────────────────────────────


async def test_r9_db_transient_at_commit_is_terminal_safe(env: Stage1Env) -> None:
    """Commitul crapă o dată. Turul NU rămâne `completed` fals și NU rămâne mut: sau se reia (lease/
    sweeper), sau se închide cu un terminal randabil. Ce nu are voie: succes fără persistare."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    accepted = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
    turn_id = accepted.json()["turn"]["id"]

    env.state.arm_fault("db_transient_at_commit")
    # Nu cerem o excepție. Măsurat pe DB real: executorul PRINDE eșecul de commit și îl transformă
    # într-un terminal onest sau lasă turul pe lease — exact P6. Un test care ar cere `raises` ar
    # cupla gate-ul la forma internă a tratării erorii, nu la proprietatea care contează: nu există
    # `completed` fără rezultat persistat.
    with contextlib.suppress(Exception):
        await _execute(env, env.alpha, turn_id)
    after = await _row(env, env.alpha, turn_id)
    assert after["status"] != "completed" or after["has_result"], (
        "turn marcat completed fără rezultat persistat"
    )
    mid = await _status(env, session, turn_id)
    assert mid.status_code in (200, 202)

    # A doua încercare (defectul e one-shot) trebuie să ducă la un terminal randabil.
    await asyncio.sleep(2.5)
    from src.redis_bus import get_redis
    from src.web.turn_executor import AcceptedTurn, WebTurnExecutor

    retry = WebTurnExecutor(await get_redis(), owner="stage1-e2e-retry")
    await retry.process_turn(AcceptedTurn(env.alpha.business_id, turn_id))
    final = await _status(env, session, turn_id)
    assert final.status_code == 200, final.text
    body = final.json()
    assert body["turn"]["status"] in ("completed", "failed")
    assert body["messages"], "terminal mut după o eroare de commit (P6)"


# ── R10: sesiune expirată → reînnoire care păstrează legarea ─────────────────────────────────


async def test_r10_session_renewal_keeps_binding(env: Stage1Env) -> None:
    """O sesiune nouă (reînnoire) pe același vizitator continuă să vadă turul; o sesiune a ALTUI
    vizitator nu îl vede deloc (404 indistinct de inexistent — zero existence leak)."""
    session, turn_id, view = await _turn(env, env.alpha)
    assert (await _status(env, session, turn_id)).json() == view

    other = await _session(env, env.alpha)
    assert other["visitor_id"] != session["visitor_id"]
    denied = await _status(env, other, turn_id)
    assert denied.status_code == 404, "sesiunea altui vizitator vede turul"

    tampered = {**session, "sig": "deadbeef"}
    assert (await _status(env, tampered, turn_id)).status_code == 403


async def test_r10_session_bound_to_origin(env: Stage1Env) -> None:
    """Sesiunile v2 sunt legate de origin: aceleași credențiale de pe alt origin nu trec."""
    session = await _session(env, env.alpha)
    r = await env.client.get(
        "/web/bootstrap",
        params={"token": env.alpha.channel_token},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403, "origin neallowlistat a primit sesiune"
    moved = await env.client.post(
        "/web/v2/turns",
        params=session,
        json={
            "schema_version": "web-turn.v2",
            "client_turn_id": str(uuid4()),
            "input": {"type": "text", "text": "salut"},
        },
        headers={"Origin": "https://evil.example"},
    )
    assert moved.status_code == 403


# ── R12: rate limit / buget — zero model ────────────────────────────────────────────────────


async def test_r12_rate_limit_never_reaches_model(env: Stage1Env) -> None:
    """Un 429 nu are voie să coste nimic: nici rând de ledger, nici apel de model."""
    import src.config as config_mod

    settings = config_mod.get_settings()
    session = await _session(env, env.alpha)
    env.llm.counters.reset()
    original = settings.web_rate_limit_max_visitor
    object.__setattr__(settings, "web_rate_limit_max_visitor", 0)
    try:
        limited = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
    finally:
        object.__setattr__(settings, "web_rate_limit_max_visitor", original)
    assert limited.status_code == 429, limited.text
    assert env.llm.counters.calls_total == 0, "modelul a fost chemat pe o cerere respinsă"


async def test_r12_budget_cap_never_reaches_model(env: Stage1Env) -> None:
    import src.config as config_mod

    settings = config_mod.get_settings()
    session = await _session(env, env.alpha)
    env.llm.counters.reset()
    original = settings.web_cost_cap_per_visitor_usd
    object.__setattr__(settings, "web_cost_cap_per_visitor_usd", 0.0)
    try:
        from src.redis_bus import get_redis
        from src.worker.limits import web_cost_add_visitor

        await web_cost_add_visitor(
            await get_redis(), env.alpha.business_id, session["visitor_id"], 1.0
        )
        blocked = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
    finally:
        object.__setattr__(settings, "web_cost_cap_per_visitor_usd", original)
    assert blocked.status_code == 429
    assert env.llm.counters.calls_total == 0


# ── R13: corpuri respinse — zero storage, zero pipeline ─────────────────────────────────────


async def test_r13_rejected_bodies_write_nothing(env: Stage1Env) -> None:
    """413 (corp prea mare), 422 (schemă), 422 (câmp comercial în context), 400 (JSON invalid):
    niciunul nu creează conversație, rând de ledger sau apel de model."""
    session = await _session(env, env.alpha)
    env.llm.counters.reset()
    before = await _probe(env, env.alpha, "conversations")

    oversized = await env.client.post(
        "/web/v2/turns",
        params=session,
        content=json.dumps(
            {
                "schema_version": "web-turn.v2",
                "client_turn_id": str(uuid4()),
                "input": {"type": "text", "text": "x" * 300_000},
            }
        ),
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413, oversized.status_code

    bad_schema = await env.client.post(
        "/web/v2/turns",
        params=session,
        json={"schema_version": "web-turn.v2", "client_turn_id": str(uuid4()), "input": {}},
        headers={"Origin": ORIGIN},
    )
    assert bad_schema.status_code == 422
    assert bad_schema.json()["error"]["code"] == "schema_invalid"

    commercial = await env.client.post(
        "/web/v2/turns",
        params=session,
        json={
            "schema_version": "web-turn.v2",
            "client_turn_id": str(uuid4()),
            "input": {"type": "text", "text": "salut"},
            "context": {"surface": "product", "price": 1.0},
        },
        headers={"Origin": ORIGIN},
    )
    assert commercial.status_code == 422
    assert commercial.json()["error"]["code"] == "context_commercial_field"

    broken = await env.client.post(
        "/web/v2/turns",
        params=session,
        content=b"{not json",
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    assert broken.status_code == 400
    assert broken.json()["error"]["code"] == "invalid_json"

    assert env.llm.counters.calls_total == 0, "un corp respins a ajuns la model"
    assert await _probe(env, env.alpha, "conversations") == before, (
        "un corp respins a creat o conversație"
    )


# ── R14: timeout de model → terminal persistat în buget ─────────────────────────────────────


async def test_r14_model_timeout_persists_terminal(env: Stage1Env) -> None:
    """Modelul se blochează peste deadline-ul turului. Rezultatul: un terminal RANDABIL persistat,
    nu tăcere și nu un `running` etern. Rezerva terminală NX-241 e exact diferența."""
    import src.config as config_mod

    settings = config_mod.get_settings()
    session = await _session(env, env.alpha)
    original = settings.web_turn_deadline_s
    object.__setattr__(settings, "web_turn_deadline_s", 2)
    try:
        env.llm.arm("model_timeout", stall_s=30.0)
        accepted = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
        turn_id = accepted.json()["turn"]["id"]
        await _execute(env, env.alpha, turn_id)
    finally:
        object.__setattr__(settings, "web_turn_deadline_s", original)
    final = await _status(env, session, turn_id)
    assert final.status_code == 200, final.text
    body = final.json()
    assert body["turn"]["status"] in ("completed", "failed")
    assert body["messages"] and body["messages"][0]["blocks"], "deadline fără nimic randabil (P6)"


# ── R17 / R18 / R19: acțiuni opace ──────────────────────────────────────────────────────────


async def _first_action_token(view: dict) -> str | None:
    for message in view.get("messages", []):
        for block in message.get("blocks", []):
            for action in block.get("actions", []) or []:
                activation = action.get("activation") or {}
                if activation.get("type") == "submit":
                    return activation["token"]
            for item in block.get("items", []) or []:
                for action in item.get("actions", []) or []:
                    activation = action.get("activation") or {}
                    if activation.get("type") == "submit":
                        return activation["token"]
    return None


async def test_r17_tampered_action_mutates_nothing(env: Stage1Env) -> None:
    """Un token modificat cu un singur caracter e respins de sigiliu (AES-SIV), fără mutație și
    fără rând de ledger. Codul e stabil și nu spune de ce."""
    session, turn_id, view = await _turn(env, env.alpha, "clarify")
    token = await _first_action_token(view)
    if token is None:
        pytest.skip("scenariul nu a emis nicio acțiune submit (plan de acțiuni gol)")
    flipped = token[:-1] + ("a" if token[-1] != "a" else "b")
    env.llm.counters.reset()
    rejected = await _accept(env, env.alpha, session, action_token=flipped)
    assert rejected.status_code in (400, 404, 409, 410), rejected.text
    assert rejected.json()["error"]["code"].startswith("action_")
    assert env.llm.counters.calls_total == 0


async def test_r18_action_from_other_tenant_is_not_found(env: Stage1Env) -> None:
    """Același token, alt tenant: 404 — indistinct de inexistent. Un 403 ar confirma existența."""
    _s_alpha, _t, view = await _turn(env, env.alpha, "clarify")
    token = await _first_action_token(view)
    if token is None:
        pytest.skip("scenariul nu a emis nicio acțiune submit")
    beta_session = await _session(env, env.beta)
    env.llm.counters.reset()
    leaked = await _accept(env, env.beta, beta_session, action_token=token)
    assert leaked.status_code == 404, leaked.text
    assert env.llm.counters.calls_total == 0
    body = leaked.json()
    blob = json.dumps(body)
    for product in sc.ALPHA_PRODUCTS:
        assert product.name not in blob, "răspunsul de refuz scurge date din alt tenant"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT REAL, găsit de acest gate, OWNER NX-236/237 — nu se repară aici (Out of Scope). "
        "`src/web/app.py:1220` scrie `messages.content_type = 'action'` (prin `accept_web_turn` cu "
        "`persist_inbound`), dar CHECK-ul din `docs/schema_v2_production.sql:185` permite doar "
        "(text, image, audio, video, document, interactive, template, location, sticker). "
        "Consecință cu `WEB_ACTIONS_ENABLED=true`: acceptul ORICĂRUI turn pornit dintr-un buton "
        "crapă cu CheckViolationError, adică acțiunile opace nu funcționează deloc. Invizibil până "
        "acum fiindcă flagul e OFF în producție și nicio suită nu atingea calea pe DB real. "
        'NB: `action_service.py:204` folosește `content_type="action"` ca INTRARE de HMAC, nu ca '
        "scriere în DB — nu e afectat. Reparat prin migrare (extinde CHECK-ul) în cardul "
        "owner; apoi `strict=True` transformă "
        "trecerea în XPASS-eroare și forțează ștergerea acestui marker."
    ),
)
async def test_r19_commerce_retry_yields_one_receipt(env: Stage1Env) -> None:
    """Retry pe aceeași acțiune de comerț: același receipt, o singură mutație. Verificat în DB."""
    session, _turn_id, view = await _turn(env, env.alpha, "recommend")
    token = await _first_action_token(view)
    if token is None:
        pytest.skip("planul turului nu a emis CTA de coș (vezi NX-240: guardul decide vandabilul)")
    ctid = str(uuid4())
    first = await _accept(env, env.alpha, session, client_turn_id=ctid, action_token=token)
    assert first.status_code in (200, 202), first.text
    if first.status_code == 202:
        await _execute(env, env.alpha, first.json()["turn"]["id"])
    retry = await _accept(env, env.alpha, session, client_turn_id=ctid, action_token=token)
    assert retry.status_code in (200, 202)
    from src.db.provider import tenant_db

    db = tenant_db(env.alpha.business_id)
    async with db("stage1_probe") as conn:
        conv = await conn.fetchval(
            "select conversation_id from web_turns where business_id = $1 and client_turn_id = $2",
            env.alpha.business_id,
            ctid,
        )
        if conv is None:
            pytest.skip("acțiunea nu a produs un turn de comerț în acest profil")
        assert await probes.receipts(conn, env.alpha.business_id, str(conv)) <= 1
        assert await probes.ledger_rows(conn, env.alpha.business_id, ctid) == 1


# ── Izolarea tenanților (cerință de DoD, nu rând de matrice) ─────────────────────────────────


async def test_tenants_with_neighbouring_ids_never_see_each_other(env: Stage1Env) -> None:
    """Cele două `business_id` diferă doar în ultimul nibble. Fiecare tenant vede DOAR turul lui,
    catalogul lui și conversațiile lui."""
    alpha_session, alpha_turn, alpha_view = await _turn(env, env.alpha)
    beta_session, beta_turn, beta_view = await _turn(env, env.beta)

    assert (await _status(env, beta_session, alpha_turn)).status_code == 404
    assert (await _status(env, alpha_session, beta_turn)).status_code == 404

    alpha_blob = json.dumps(alpha_view, ensure_ascii=False)
    for product in sc.BETA_PRODUCTS:
        assert product.name not in alpha_blob
    beta_blob = json.dumps(beta_view, ensure_ascii=False)
    for product in sc.ALPHA_PRODUCTS:
        assert product.name not in beta_blob


async def test_no_pii_or_raw_body_on_the_ledger_row(env: Stage1Env) -> None:
    """Rândul de ledger nu are voie să poarte corpul brut, vizitatorul sau tokenul: inputul safe
    trăiește în `messages`, iar PII-ul de canal doar în `channel_identities` (P12)."""
    session, turn_id, _view = await _turn(env, env.alpha)
    from src.db.provider import tenant_db

    db = tenant_db(env.alpha.business_id)
    async with db("stage1_probe") as conn:
        row = await conn.fetchrow(
            "select * from web_turns where business_id = $1 and id = $2",
            env.alpha.business_id,
            turn_id,
        )
    blob = json.dumps({k: str(v) for k, v in dict(row).items()}, ensure_ascii=False)
    assert session["visitor_id"] not in blob
    assert env.alpha.channel_token not in blob
    assert "ser cu vitamina C pentru ten uscat" not in blob


# ── Scenariile canonice, prin invarianții lor ───────────────────────────────────────────────


#: scenariu canonic → (script de model, textul clientului).
#:
#: Textul e DISTINCT per scenariu, și nu din eleganță: prima rulare pe DB real a picat exact aici.
#: `no_results` și `recommendation` trimiteau aceeași frază, iar al doilea tur o găsea în
#: `semantic_cache` (stagiul 4) și primea răspunsul primului — zero carduri. Comportamentul
#: cache-ului e corect; scenariile erau cele care nu erau independente. Cardul cere explicit
#: „scenariul setat e izolat per test/tenant".
CANONICAL_BACKEND_SCENARIOS = {
    "text_answer": ("text_answer", "cât durează livrarea"),
    "recommendation": ("recommend", "ser cu vitamina C pentru ten uscat"),
    "comparison": ("compare", "compară primele două seruri cu vitamina C"),
    "clarification": ("clarify", "vreau un cadou"),
    "no_results": ("no_results", "ser cu ingredient inexistent sub un leu"),
    "terminal_failure": ("pipeline_error", "recomandă-mi ceva pentru ten"),
}


@pytest.mark.parametrize("scenario_id", sorted(CANONICAL_BACKEND_SCENARIOS))
async def test_canonical_scenario_invariants_hold(env: Stage1Env, scenario_id: str) -> None:
    """Rulează scenariul canonic pe calea reală și trece vederea livrată prin CHECKERELE backend
    declarate în manifest. Invarianții de frontend se sar aici — dar `check_invariants` întoarce ce
    a rulat, iar testul cere să nu fie lista goală: un scenariu fără nicio verificare de backend
    ar fi acoperire fictivă."""
    spec = next(s for s in sc.manifest()["scenarios"] if s["id"] == scenario_id)
    script, text = CANONICAL_BACKEND_SCENARIOS[scenario_id]
    session, turn_id, view = await _turn(env, env.alpha, script, text=text)

    from src.db.provider import tenant_db

    db = tenant_db(env.alpha.business_id)
    async with db("stage1_probe") as conn:
        conv = await conn.fetchval(
            "select conversation_id from web_turns where business_id = $1 and id = $2",
            env.alpha.business_id,
            turn_id,
        )
        bundle = await probes.turn_bundle(
            conn,
            business_id=env.alpha.business_id,
            conversation_id=str(conv),
            turn_id=turn_id,
            client_turn_id=view["turn"]["client_turn_id"],
        )
    inp = sc.InvariantInput(
        view=view,
        tenant=env.alpha,
        probes=bundle,
        counters=env.llm.counters.snapshot(),
    )
    ran = sc.check_invariants(spec["invariants"], inp)
    backend_expected = sorted(set(spec["invariants"]) & sc.backend_invariants())
    assert sorted(ran) == backend_expected, f"invarianți backend nerulați: {backend_expected}"
    assert ran, f"{scenario_id}: niciun invariant de backend — acoperire fictivă"


async def test_deadline_at_is_not_extended_on_reclaim(env: Stage1Env) -> None:
    """NX-241: `deadline_at` e al CLIENTULUI, fixat la accept. Un reclaim îl păstrează — altfel
    bugetul de timp s-ar reînnoi la fiecare încercare și clientul ar aștepta nelimitat."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    accepted = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
    turn_id = accepted.json()["turn"]["id"]
    from src.db.provider import tenant_db

    db = tenant_db(env.alpha.business_id)
    async with db("stage1_probe") as conn:
        before = await conn.fetchval(
            "select deadline_at from web_turns where business_id = $1 and id = $2",
            env.alpha.business_id,
            turn_id,
        )
    env.state.arm_fault("kill_worker_after_claim")
    with pytest.raises(ha._HarnessWorkerKilled):
        await _execute(env, env.alpha, turn_id)
    await asyncio.sleep(2.5)
    from src.redis_bus import get_redis
    from src.web.turn_executor import AcceptedTurn, WebTurnExecutor

    second = WebTurnExecutor(await get_redis(), owner="stage1-e2e-deadline")
    await second.process_turn(AcceptedTurn(env.alpha.business_id, turn_id))
    async with db("stage1_probe") as conn:
        after = await conn.fetchval(
            "select deadline_at from web_turns where business_id = $1 and id = $2",
            env.alpha.business_id,
            turn_id,
        )
    assert before == after, "deadline-ul s-a prelungit la reclaim"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT REAL, găsit de acest gate, OWNER NX-234/236 — nu se repară aici (Out of Scope). "
        "`load_execution_refs` (src/db/queries/web_turns.py) citește `payload` din `rec`, dar "
        "proiecția EXTERIOARĂ a query-ului listează doar `m.id, m.body, m.content_type`: coloana "
        "`payload` există în subqueryul lateral și se pierde în select. Deci `payload` nu e "
        "niciodată printre cheile Recordului, iar `page_context` și `action` ies MEREU None. "
        "Consecințe: contextul de pagină persistat la accept (NX-234) nu ajunge niciodată la "
        "execuție, deci recovery-ul cu aceeași ancoră nu se întâmplă; iar comanda de acțiune "
        "(NX-236) nu se rehidratează, deci un turn de acțiune reluat după restart își pierde "
        "comanda. Persistarea E corectă — verificat pe DB: payload-ul mesajului conține ancora. "
        "Fix: adaugă `m.payload` în selectul exterior. Apoi `strict=True` transformă trecerea în "
        "XPASS-eroare și forțează ștergerea markerului."
    ),
)
async def test_accepted_turn_is_fully_recoverable_from_the_database(env: Stage1Env) -> None:
    """Recovery integral din DB: inputul safe, contextul și identitatea sunt persistate la accept.
    Un proces care repornește nu are nevoie de nimic din requestul HTTP original."""
    env.llm.arm("recommend")
    session = await _session(env, env.alpha)
    accepted = await _accept(
        env,
        env.alpha,
        session,
        client_turn_id=str(uuid4()),
        context={"surface": "product", "product_id": sc.ALPHA_PRODUCTS[0].external_id},
    )
    assert accepted.status_code == 202, accepted.text
    turn_id = accepted.json()["turn"]["id"]
    from src.db.provider import tenant_db
    from src.db.queries.web_turns import load_execution_refs

    db = tenant_db(env.alpha.business_id)
    async with db("stage1_probe") as conn:
        refs = await load_execution_refs(conn, env.alpha.business_id, turn_id)
    assert refs is not None
    assert refs.inbound_msg_id, "inputul nu a fost persistat la accept"
    assert refs.sender_external_id, "identitatea nu a fost persistată la accept"
    assert refs.safe_body, "corpul safe nu a fost persistat la accept"
    assert refs.page_context, "contextul de pagină nu a fost persistat la accept"


# ── Scenarii canonice cu mecanică proprie (nu trec prin testul parametrizat) ──────────────────


async def test_scenario_welcome_bootstrap_copy_is_server_owned(env: Stage1Env) -> None:
    """`welcome` nu are tur: tot ce trebuie dovedit e că widgetul primește copy-ul ramei ÎNAINTE de
    primul mesaj (NX-244). Fără el, FE-ul ar rămâne fără nume de launcher și l-ar inventa — exact ce
    interzice boundary-ul „frontend pasiv"."""
    r = await env.client.get(
        "/web/bootstrap",
        params={"token": env.alpha.channel_token},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    inp = sc.InvariantInput(view={}, tenant=env.alpha, state={"view_copy": body.get("view_copy")})
    ran = sc.check_invariants(["bootstrap_copy_server_owned"], inp)
    assert ran == ["bootstrap_copy_server_owned"]


async def test_scenario_deadline_degraded_persists_a_renderable_terminal(env: Stage1Env) -> None:
    """`deadline_degraded` folosește ACEEAȘI mecanică ca R14, dar verdictul trece prin checkerul
    declarat în manifest — altfel invariantul ar fi „acoperit" doar de un assert scris în test."""
    import src.config as config_mod

    settings = config_mod.get_settings()
    session = await _session(env, env.alpha)
    original = settings.web_turn_deadline_s
    object.__setattr__(settings, "web_turn_deadline_s", 2)
    try:
        env.llm.arm("model_timeout", stall_s=30.0)
        accepted = await _accept(env, env.alpha, session, client_turn_id=str(uuid4()))
        turn_id = accepted.json()["turn"]["id"]
        await _execute(env, env.alpha, turn_id)
    finally:
        object.__setattr__(settings, "web_turn_deadline_s", original)
    view = (await _status(env, session, turn_id)).json()
    inp = sc.InvariantInput(view=view, tenant=env.alpha)
    ran = sc.check_invariants(["deadline_fallback_persisted", "terminal_view_renderable"], inp)
    assert sorted(ran) == ["deadline_fallback_persisted", "terminal_view_renderable"]


async def test_scenario_feedback_reaches_the_backend_exactly_once(env: Stage1Env) -> None:
    """`feedback` (NX-246 felia 2): promptul se emite pe calea de succes, iar votul merge pe ruta
    SEPARATĂ `/web/v2/feedback`. Un retry cu ACELAȘI token nu adaugă un al doilea rând — idempotența
    e în schemă, nu în cod, iar proba e în DB."""
    session, turn_id, view = await _turn(env, env.alpha, "recommend")
    token = await _feedback_token(view)
    if token is None:
        pytest.fail("niciun prompt de feedback emis — scenariul e declarat `covered` în manifest")
    first = await env.client.post(
        "/web/v2/feedback",
        params=session,
        headers={"Origin": ORIGIN},
        json={"action_token": token},
    )
    assert first.status_code == 200, first.text
    retry = await env.client.post(
        "/web/v2/feedback",
        params=session,
        headers={"Origin": ORIGIN},
        json={"action_token": token},
    )
    assert retry.status_code in (200, 409), retry.text

    from src.db.provider import tenant_db

    db = tenant_db(env.alpha.business_id)
    async with db("stage1_probe") as conn:
        rows = await probes.feedback_rows(conn, env.alpha.business_id, turn_id)
        revision = await probes.feedback_revision(conn, env.alpha.business_id, turn_id)
    assert revision == 1, f"retry identic a incrementat revizia ({revision})"
    inp = sc.InvariantInput(view=view, tenant=env.alpha, probes={"feedback_rows": rows})
    assert sc.check_invariants(["one_feedback_row"], inp) == ["one_feedback_row"]


async def _feedback_token(view: dict) -> str | None:
    """Tokenul primului prompt de feedback, identificat prin KIND-ul din plicul SIGILAT.

    Nu prin etichetă și nu prin icon: prima versiune căuta „feedback" în `id` sau un icon de degete,
    iar pe date reale n-a găsit nimic (id-urile sunt opace, `icon` e None). Un test care ar deduce
    semantica din label ar fi exact frontendul-al-doilea-creier pe care boundary-ul interzice; iar
    unul care ar încerca fiecare token pe rută ar CONSUMA acțiuni ca efect secundar.

    Aici deschidem tokenul cu inelul de chei al procesului (privilegiu de server, legitim într-un
    test de backend) și comparăm kind-ul cu `FEEDBACK_KINDS` — adevărul e în registru.
    """
    from src.config import get_settings
    from src.web.action_crypto import OpenFailure, open_token, parse_key_ring
    from src.web.action_models import FEEDBACK_KINDS

    ring = parse_key_ring(get_settings().web_action_keys)
    now = int(datetime.now(UTC).timestamp())
    for message in view.get("messages", []):
        for block in message.get("blocks", []):
            for action in block.get("actions", []) or []:
                activation = action.get("activation") or {}
                if activation.get("type") != "submit":
                    continue
                opened = open_token(activation["token"], ring, now=now, skew_s=60)
                if isinstance(opened, OpenFailure):
                    continue
                if getattr(opened.envelope, "kind", None) in FEEDBACK_KINDS:
                    return activation["token"]
    return None
