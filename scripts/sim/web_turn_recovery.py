"""NX-233 — manual drive REPRODUCTIBIL pentru executorul async + recovery (cardul, „Manual drive").

Rulează CAP-COADĂ pe DB-ul real (tenant throwaway, auto-curățat), cu pipeline STUB CONTORIZAT
(zero OpenAI, zero credite) și DOUĂ instanțe de executor cu owneri diferiți. Scenariile:

  1. același `client_turn_id` din două „taburi" SIMULTAN → un singur rând, o singură execuție,
     GET (autorizat pe sesiune) întoarce EXACT view-ul persistat;
  2. worker OMORÂT după model, înainte de commit → lease-ul expiră → AL DOILEA executor
     reclamă (epoch+1), termină; zombie-ul cu epoch vechi scrie 0 rânduri;
  3. crash DUPĂ commit, înainte de „publish"/răspuns → rezultatul rămâne completed, GET îl
     rejoacă identic (fail-ul post-commit face 0 rânduri — fencing pe status);
  4. Redis complet MORT după accept → sweeperul + scanul executorului recuperează turul
     EXCLUSIV din Postgres (DB = autoritatea).

Nu tipărește body-uri brute, prompturi sau tokenuri: doar ID-uri TRUNCHIATE, statusuri și
contoare (claim/model/commit). Lease scurt (2s) + heartbeat 1s, DOAR în acest proces.

Rulare:  PYTHONPATH=. python scripts/sim/web_turn_recovery.py
Exit: 0 = toate scenariile au verdictul așteptat; 2 = cel puțin unul a picat.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

# Parametrii de drive TREBUIE în env înainte de primul get_settings() (cache LRU).
os.environ.setdefault("WEB_TURN_LEASE_TTL_S", "2")
os.environ.setdefault("WEB_TURN_HEARTBEAT_S", "1")
os.environ.setdefault("WEB_TURN_EXECUTOR_POLL_S", "1")
os.environ.setdefault("ADMISSION_ENABLED", "false")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def _tid(value: str) -> str:
    """ID trunchiat pentru timeline (anonimizat — nu tipărim UUID-uri întregi)."""
    return f"{value[:8]}…"


class _MiniRedis:
    """Strictul necesar pentru wake (lpush/ltrim/brpop) + phase (set/get), în memorie."""

    def __init__(self):
        self._lists: dict[str, list[str]] = {}
        self._kv: dict[str, str] = {}

    def pipeline(self):
        outer = self

        class _Pipe:
            def __init__(self):
                self._ops = []

            def lpush(self, key, value):
                self._ops.append(("lpush", key, value))

            def ltrim(self, key, a, b):
                self._ops.append(("ltrim", key, a, b))

            async def execute(self):
                for op in self._ops:
                    if op[0] == "lpush":
                        outer._lists.setdefault(op[1], []).insert(0, op[2])
                    else:  # ("ltrim", key, start, stop)
                        outer._lists[op[1]] = outer._lists.get(op[1], [])[: op[3] + 1]
                return [1] * len(self._ops)

        return _Pipe()

    async def brpop(self, key, timeout=None):
        items = self._lists.get(key) or []
        if items:
            return key, items.pop()
        await asyncio.sleep(min(0.05, timeout or 0.05))
        return None

    async def set(self, key, value, ex=None):
        self._kv[key] = value

    async def get(self, key):
        return self._kv.get(key)


class _DeadRedis:
    """Redis complet indisponibil: ORICE operație aruncă (scenariul 4)."""

    def __getattr__(self, name):
        def _boom(*a, **k):
            raise ConnectionError("redis mort")

        return _boom


async def main() -> int:
    from src.config import get_settings
    from src.db.connection import admin_conn, close_pool, get_pool
    from src.db.provider import static_db
    from src.db.queries import web_turns as wt
    from src.models import Reply
    from src.web import turn_executor as te
    from src.web import turn_recovery as tr
    from src.web import turn_service as ts
    from src.worker.processor import TurnResult

    settings = get_settings()
    pool = await get_pool()
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        mark = "OK " if ok else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    # ── tenant throwaway ────────────────────────────────────────────────────
    bid = str(uuid4())
    channel_id = str(uuid4())
    token = f"tok-{uuid4().hex[:10]}"
    async with admin_conn(pool) as conn:
        await conn.execute(
            "insert into businesses (id, slug, name, vertical, status, default_locale) "
            "values ($1, $2, 'NX-233 drive', 'beauty_salon', 'active', 'ro')",
            bid,
            f"nx233drv-{uuid4().hex[:8]}",
        )
        await conn.execute(
            "insert into channels (id, business_id, kind, provider_account_id) "
            "values ($1, $2, 'webchat', $3)",
            channel_id,
            bid,
            token,
        )
    print(f"tenant throwaway: {_tid(bid)}  canal: {_tid(channel_id)}")

    # Toate scrierile tenant-scoped merg pe poolul admin în drive (fără bot pool).
    def drive_db(business_id):
        def provider(op=None):
            return admin_conn(pool)

        return provider

    te.tenant_db = drive_db
    tr.tenant_db = drive_db

    counters = {"model": 0, "commit": 0, "claim": 0}
    mode = {"value": "ok"}  # ok | hang | crash_after_commit
    hang = asyncio.Event()

    async def stub_handle(db, business, chan_id, event, **kw):
        """Pipeline STUB contorizat: „modelul" e un contor, commit-ul e REAL (hook-ul NX-232
        în tranzacție). Zero OpenAI, zero tool-uri."""
        counters["model"] += 1
        if mode["value"] == "hang":
            await hang.wait()
        reply = Reply(text="Răspuns de drive (stub, fără LLM).")
        async with db("turn_commit") as conn:
            async with conn.transaction():
                await kw["commit_hook"](conn, reply, "ro")
        counters["commit"] += 1
        if mode["value"] == "crash_after_commit":
            raise RuntimeError("crash simulat DUPĂ commit, înainte de publish")
        return TurnResult(
            "c", "ct", kw.get("turn_id"), reply.text, None, reply=reply, language="ro"
        )

    async def no_aftercare(db, redis, work):
        return 0.0

    async def no_events(db, *a, **k):
        return None

    te.handle_turn = stub_handle
    te.run_aftercare = no_aftercare
    te.persist_events = no_events

    real_claim = te.claim_web_turn

    async def counting_claim(*a, **kw):
        out = await real_claim(*a, **kw)
        if out is not None:
            counters["claim"] += 1
        return out

    te.claim_web_turn = counting_claim

    redis = _MiniRedis()
    ex1 = te.WebTurnExecutor(redis, owner="drive-exec-1")
    ex2 = te.WebTurnExecutor(redis, owner="drive-exec-2")

    async def accept(visitor: str, text: str, client_id: str | None = None):
        fp = ts.request_fingerprint("sek", business_id=bid, channel_token=token, text=text)
        async with admin_conn(pool) as conn:
            return await ts.accept_web_turn(
                static_db(conn),
                business_id=bid,
                channel_id=channel_id,
                channel_kind="webchat",
                channel_token=token,
                sender_external_id=visitor,
                client_turn_id=client_id or str(uuid4()),
                fingerprint=fp,
                deadline_at=datetime.now(UTC) + timedelta(seconds=60),
                session_ref=ts.session_ref_hash(token, visitor),
                persist_inbound=True,
                safe_body=text,
            )

    async def row_of(turn_id: str):
        async with admin_conn(pool) as conn:
            return await wt.get_turn_by_id(conn, bid, turn_id)

    try:
        # ── 1. același ID din două „taburi" simultan ────────────────────────
        print("\n[1] același client_turn_id din două taburi, simultan")
        visitor = f"web_drive_{uuid4().hex[:8]}"
        cid = str(uuid4())
        out_a, out_b = await asyncio.gather(
            accept(visitor, "vreau un ser", cid), accept(visitor, "vreau un ser", cid)
        )
        kinds = sorted(type(o).__name__ for o in (out_a, out_b))
        check("un singur rând nou", kinds == ["Accepted", "ExistingInProgress"], str(kinds))
        accepted = out_a if isinstance(out_a, ts.Accepted) else out_b
        t1 = accepted.row.id
        print(f"  turn: {_tid(t1)}")
        before = counters["model"]
        out = await ex1.process_turn(te.AcceptedTurn(bid, t1))
        check(
            "execuție unică, completed",
            out.outcome == "completed" and counters["model"] == before + 1,
            f"model={counters['model'] - before}",
        )
        async with admin_conn(pool) as conn:
            mine = await ts.get_turn_for_session(
                static_db(conn),
                business_id=bid,
                turn_id=t1,
                channel_token=token,
                visitor_id=visitor,
            )
        check(
            "GET autorizat rejoacă view-ul persistat",
            mine is not None and ts.renderable(mine.response_json),
        )
        # retry-ul aceluiași ID după terminal → replay, zero model nou
        before = counters["model"]
        replay = await accept(visitor, "vreau un ser", cid)
        check(
            "retry după terminal = replay, zero model",
            isinstance(replay, ts.ExistingCompleted) and counters["model"] == before,
        )

        # ── 2. kill după model, înainte de commit → reclaim cu epoch nou ────
        print("\n[2] worker omorât după model, înainte de commit → reclaim")
        visitor2 = f"web_drive_{uuid4().hex[:8]}"
        out2 = await accept(visitor2, "vreau un șampon")
        t2 = out2.row.id
        print(f"  turn: {_tid(t2)}")
        mode["value"] = "hang"
        model_base = counters["model"]
        task = asyncio.create_task(ex1.process_turn(te.AcceptedTurn(bid, t2)))
        while counters["model"] <= model_base:  # așteptăm să intre „în model"
            await asyncio.sleep(0.02)
        task.cancel()  # ⚡ kill: procesul moare cu lease-ul în mână
        await asyncio.gather(task, return_exceptions=True)
        r2 = await row_of(t2)
        epoch_zombie = r2.lease_epoch
        check(
            "turul rămâne running cu lease (nimic fals completed)",
            r2.status == "running" and r2.response_json is None,
        )
        print(f"  aștept expirarea lease-ului ({settings.web_turn_lease_ttl_s}s)…")
        await asyncio.sleep(settings.web_turn_lease_ttl_s + 0.5)
        mode["value"] = "ok"
        out = await ex2.process_turn(te.AcceptedTurn(bid, t2))
        r2 = await row_of(t2)
        check(
            "al doilea executor reclamă cu epoch+1 și termină",
            out.outcome == "completed"
            and r2.status == "completed"
            and r2.lease_epoch == epoch_zombie + 1,
            f"epoch {epoch_zombie}→{r2.lease_epoch}",
        )
        async with admin_conn(pool) as conn:
            zombie_wrote = await wt.complete_turn(
                conn,
                bid,
                t2,
                lease_epoch=epoch_zombie,
                response_json={"content": "rezultat zombie"},
            )
        r2 = await row_of(t2)
        check(
            "zombie-ul cu epoch vechi scrie 0 rânduri",
            not zombie_wrote and "zombie" not in str(r2.response_json),
        )

        # ── 3. crash DUPĂ commit, înainte de publish → GET rejoacă ──────────
        print("\n[3] crash după commit, înainte de publish/răspuns")
        visitor3 = f"web_drive_{uuid4().hex[:8]}"
        out3 = await accept(visitor3, "aveți crema de zi?")
        t3 = out3.row.id
        print(f"  turn: {_tid(t3)}")
        mode["value"] = "crash_after_commit"
        commits_before = counters["commit"]
        await ex1.process_turn(te.AcceptedTurn(bid, t3))
        mode["value"] = "ok"
        r3 = await row_of(t3)
        check(
            "rezultatul comis SUPRAVIEȚUIEȘTE crash-ului post-commit",
            r3.status == "completed"
            and ts.renderable(r3.response_json)
            and counters["commit"] == commits_before + 1,
        )
        before = counters["model"]
        replay3 = await accept(visitor3, "aveți crema de zi?", r3.client_turn_id)
        check(
            "GET/retry livrează EXACT rezultatul comis, zero model",
            isinstance(replay3, ts.ExistingCompleted)
            and replay3.row.response_json == r3.response_json
            and counters["model"] == before,
        )

        # ── 4. Redis complet mort după accept → recovery din DB ─────────────
        print("\n[4] Redis mort după accept → sweeper + scan din Postgres")
        visitor4 = f"web_drive_{uuid4().hex[:8]}"
        out4 = await accept(visitor4, "ce ingrediente are serul?")
        t4 = out4.row.id
        print(f"  turn: {_tid(t4)}")
        dead = _DeadRedis()
        report = await tr.sweep_once(dead)  # wake-ul pică; scanul tot îl vede
        check("sweeperul vede turul fără Redis", report.scanned >= 1, f"scanned={report.scanned}")
        ex_dead = te.WebTurnExecutor(dead, owner="drive-exec-dead")
        refs = await ex_dead._next_refs()
        ours = [r for r in refs if r.business_id == bid and r.turn_id == t4]
        check("scanul executorului îl găsește fără Redis", len(ours) == 1)
        out = await ex_dead.process_turn(te.AcceptedTurn(bid, t4))
        r4 = await row_of(t4)
        check(
            "turul se termină recuperat integral din DB",
            out.outcome == "completed" and r4.status == "completed",
        )

        print(
            f"\ncontoare: claim={counters['claim']} model={counters['model']} "
            f"commit={counters['commit']}"
        )
        print(f"verdict: {'PASS' if not failures else 'FAIL: ' + ', '.join(failures)}")
        return 0 if not failures else 2
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from businesses where id = $1", bid)
        print(f"auto-curățat tenantul de drive {_tid(bid)}.")
        await close_pool()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    raise SystemExit(asyncio.run(main()))
