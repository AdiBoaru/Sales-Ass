"""Warm driver server pentru testarea pre-producție a conversațiilor (NU producție).

Expune pipeline-ul REAL (`handle_turn`, DEFAULT_STAGES, LLM real, DB real) peste HTTP,
ca un harness de simulare să poată purta conversații lungi multi-tur fără să reconecteze
la fiecare mesaj. Un singur proces cald: pool asyncpg + client OpenAI inițializate o dată.

  POST /turn  {sender, text, ...}     → rulează un tur prin pipeline-ul real → {reply, route, ...}
  GET  /trace/{conversation_id}        → adevărul din DB: transcript + timeline de evenimente
  GET  /health                         → starea driverului (channel/biz)
  GET  /substrate                      → ce „materie primă" are botul (produse/embeddings/url/faq)

Rulează (din rădăcina proiectului):  python scripts/sim/server.py
Folosește DEMO_BIZ (Sole Demo). Datele de simulare sunt marcate cu sender_external_id
care începe cu `sim:` → curățabile cu scripts/sim/cleanup.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

# rulat ca script (python scripts/sim/server.py) → adăugăm rădăcina la sys.path pt `import src`.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from src.agent.llm import get_llm  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.db.queries.channels import upsert_channel  # noqa: E402
from src.worker.processor import handle_turn  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
SIM_PROVIDER = "SIM-DRIVER"

_state: dict = {}

# Pooler-ul Supabase (session mode) e capat la 15 clienți. Sub concurență mare (multe
# persona), 2 conexiuni/request × N depășeau capul → EMAXCONNSESSION (HTTP 500). Limităm
# munca DB concurentă; combinat cu deținerea unei SINGURE conexiuni lungi pe /turn (admin
# doar în pâlpâiri scurte), rămânem sub cap.
_db_sem = asyncio.Semaphore(6)


def _jsonb(v):
    """asyncpg întoarce jsonb ca str → îl parsăm; dict/None trec direct."""
    if isinstance(v, str):
        try:
            return json.loads(v or "{}")
        except ValueError:
            return {}
    return v or {}


def _jsonb_row(row):
    """asyncpg Record → dict simplu (sau None) pentru serializare JSON."""
    return dict(row) if row is not None else None


def _merge_breakdown(dst: dict, src: dict) -> None:
    """Sumă aditivă a unei defalcări {cheie: {calls, tokens_in/out, cached_tokens, cost_usd,
    latency_ms?}}. Adună TOATE câmpurile numerice prezente (inclusiv `latency_ms` de la by_stage,
    care altfel s-ar pierde) → by_model rămâne fără latency, by_stage o păstrează."""
    for key, row in src.items():
        into = dst.setdefault(key, {})
        for k, v in row.items():
            if isinstance(v, (int, float)):
                into[k] = round(into.get(k, 0) + v, 6)


def _turn_usage(events: list[dict]) -> dict | None:
    """Agregă consumul LLM al ACESTUI tur din event-urile `llm_usage` (cost-obs NX-103).

    Pot exista două: phase='turn' (reply-ul, cu defalcare pe stagiu) și phase='post_turn'
    (summarizer/profil/cache embed). Le însumăm pentru costul total al mesajului, dar păstrăm
    `reply_cost_usd` (doar reply) separat de `total_cost_usd` (reply + fundal). None dacă turul
    n-a atins LLM-ul (free-layer/cache/welcome) → afișăm „$0 (fără LLM)"."""
    rows = [e["props"] for e in events if e["type"] == "llm_usage"]
    if not rows:
        return None
    agg = {
        "tokens_in": 0,
        "tokens_out": 0,
        "cached_tokens": 0,
        "total_cost_usd": 0.0,
        "reply_cost_usd": 0.0,
        "savings_usd": 0.0,
        "llm_calls": 0,
        "by_stage": {},
        "by_model": {},
    }
    for p in rows:
        agg["tokens_in"] += int(p.get("tokens_in") or 0)
        agg["tokens_out"] += int(p.get("tokens_out") or 0)
        agg["cached_tokens"] += int(p.get("cached_tokens") or 0)
        agg["total_cost_usd"] += float(p.get("cost_usd") or 0.0)
        agg["savings_usd"] += float(p.get("savings_usd") or 0.0)
        agg["llm_calls"] += int(p.get("llm_calls") or 0)
        if p.get("phase") == "turn":
            agg["reply_cost_usd"] += float(p.get("cost_usd") or 0.0)
        # Merge ADITIV pe stagiu + model → același agregator merge și pe un singur tur (un
        # singur event cu stagii) și pe toată conversația (sumă pe tururi).
        _merge_breakdown(agg["by_stage"], p.get("by_stage") or {})
        _merge_breakdown(agg["by_model"], p.get("by_model") or {})
    agg["total_cost_usd"] = round(agg["total_cost_usd"], 6)
    agg["reply_cost_usd"] = round(agg["reply_cost_usd"], 6)
    agg["savings_usd"] = round(agg["savings_usd"], 6)
    return agg


@asynccontextmanager
async def lifespan(app: FastAPI):
    # canalul de simulare (idempotent) — creat cu rol ADMIN (channels e read-only pt bot).
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ch = await upsert_channel(
            conn, DEMO_BIZ, "whatsapp", SIM_PROVIDER, display_name="Sim Driver"
        )
    _state["channel_id"] = ch["id"]
    # config de business + verificarea cheii OpenAI (altfel pipeline-ul degradează la fallback).
    async with tenant_conn(DEMO_BIZ) as conn:
        _state["biz"] = await load_business(conn, DEMO_BIZ)
    _state["llm_ready"] = get_llm() is not None
    yield
    await close_pool()


app = FastAPI(lifespan=lifespan, title="nativx sim driver")


class TurnIn(BaseModel):
    sender: str
    text: str | None = None
    content_type: str = "text"
    media_id: str | None = None
    sender_name: str | None = "Client"


@app.get("/health")
async def health():
    biz = _state.get("biz")
    return {
        "ok": True,
        "channel_id": _state.get("channel_id"),
        "biz": biz.slug if biz else None,
        "vertical": biz.vertical if biz else None,
        "llm_ready": _state.get("llm_ready"),
    }


@app.get("/substrate")
async def substrate():
    """Ce are botul de lucru: dacă embeddings/url/faq lipsesc, recomandările vor suferi —
    ne ajută să separăm gaurile de DATE de bug-urile de COD/arhitectură. Inspecție =
    admin_conn (privilegiat), scoped pe DEMO_BIZ (bot_runtime n-ar putea citi analytics)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:

        async def q(sql):
            return await conn.fetchval(sql, DEMO_BIZ)

        return {
            "products": await q("select count(*) from products where business_id=$1"),
            "products_active": await q(
                "select count(*) from products where business_id=$1 and status='active'"
            ),
            "products_with_url": await q(
                "select count(*) from products where business_id=$1 and product_url is not null"
            ),
            "products_with_price": await q(
                "select count(*) from products where business_id=$1 and price is not null"
            ),
            "products_in_stock": await q(
                "select count(*) from products where business_id=$1 "
                "and (availability='in_stock' or stock_total > 0)"
            ),
            "product_embeddings": await q(
                "select count(*) from product_embeddings where business_id=$1"
            ),
            "categories": await q("select count(*) from categories where business_id=$1"),
            "faqs": await q("select count(*) from faqs where business_id=$1"),
            "intent_aliases_approved": await q(
                "select count(*) from intent_aliases where business_id=$1 and status='approved'"
            ),
            "semantic_cache": await q("select count(*) from semantic_cache where business_id=$1"),
            "wa_templates_approved": await q(
                "select count(*) from wa_templates where business_id=$1 and status='approved'"
            ),
            "orders": await q("select count(*) from orders where business_id=$1"),
        }


@app.post("/turn")
async def turn(inp: TurnIn):
    event = {
        "channel_kind": "whatsapp",
        "channel_account_id": SIM_PROVIDER,
        "sender_external_id": inp.sender,
        "provider_msg_id": f"sim.{uuid.uuid4().hex}",
        "content_type": inp.content_type,
        "body": inp.text,
        "media_id": inp.media_id,
        "sender_name": inp.sender_name,
    }
    t0 = time.perf_counter()
    route = None
    events: list[dict] = []
    usage_total: dict | None = None
    pool = await get_pool()
    async with _db_sem:
        # ts0 într-o pâlpâire scurtă de admin (NU ținem admin pe durata turului).
        async with admin_conn(pool) as actx:
            ts0 = await actx.fetchval("select now()")
        # turul REAL pe calea de producție (bot_runtime + RLS) — o SINGURĂ conexiune lungă.
        async with tenant_conn(DEMO_BIZ) as conn:
            result = await handle_turn(conn, _state["biz"], _state["channel_id"], event, redis=None)
        # inspecția evenimentelor pe admin (bot_runtime are DOAR insert pe analytics_events).
        if result.conversation_id:
            async with admin_conn(pool) as actx:
                rows = await actx.fetch(
                    "select event_type, properties from analytics_events "
                    "where conversation_id=$1 and created_at >= $2 order by id",
                    result.conversation_id,
                    ts0,
                )
                # Cumulat pe TOATĂ conversația (cost-obs NX-103): sumă din event-urile llm_usage.
                usage_total = _jsonb_row(
                    await actx.fetchrow(
                        "select coalesce(sum(tokens_in),0)::bigint as tokens_in, "
                        "coalesce(sum(tokens_out),0)::bigint as tokens_out, "
                        "coalesce(sum((properties->>'cached_tokens')::bigint),0)::bigint "
                        "  as cached_tokens, "
                        "coalesce(sum(cost_usd),0)::float8 as cost_usd, "
                        "coalesce(sum((properties->>'savings_usd')::numeric),0)::float8 "
                        "  as savings_usd, "
                        "coalesce(sum((properties->>'llm_calls')::int),0)::int as llm_calls "
                        "from analytics_events "
                        "where conversation_id=$1 and event_type='llm_usage'",
                        result.conversation_id,
                    )
                )
            for r in rows:
                props = _jsonb(r["properties"])
                events.append({"type": r["event_type"], "props": props})
                if r["event_type"] == "intent_detected" and props.get("route"):
                    route = props["route"]
    return {
        "reply": result.reply_text,
        "route": route,
        "conversation_id": result.conversation_id,
        "deduped": result.deduped,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "events": [e["type"] for e in events],
        "event_detail": events,
        # NX-103: consumul ACESTUI mesaj (reply + fundal) + cumulat pe conversație.
        "usage": _turn_usage(events),
        "usage_total": usage_total,
    }


@app.get("/trace/{conversation_id}")
async def trace(conversation_id: str):
    # adevărul din DB pt judecători — inspecție pe admin (analytics_events e select-denied pt bot).
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        msgs = await conn.fetch(
            "select direction, author, body, content_type, status, created_at "
            "from messages where conversation_id=$1 order by created_at, id",
            conversation_id,
        )
        evs = await conn.fetch(
            "select event_type, properties, created_at from analytics_events "
            "where conversation_id=$1 order by created_at, id",
            conversation_id,
        )
        conv = await conn.fetchrow(
            "select status, bot_active, handoff_until, risk_flags, state "
            "from conversations where id=$1",
            conversation_id,
        )
    ev_list = [{"type": e["event_type"], "props": _jsonb(e["properties"])} for e in evs]
    return {
        "conversation_id": conversation_id,
        # NX-103: cost cumulat pe conversație + defalcare pe stagiu/model (cost-obs).
        "usage_total": _turn_usage(ev_list),
        "conversation": {
            "status": conv["status"] if conv else None,
            "bot_active": conv["bot_active"] if conv else None,
            "handoff_until": conv["handoff_until"].isoformat()
            if conv and conv["handoff_until"]
            else None,
            "risk_flags": list(conv["risk_flags"]) if conv and conv["risk_flags"] else [],
            "state": _jsonb(conv["state"]) if conv else {},
        }
        if conv
        else None,
        "messages": [
            {
                "direction": m["direction"],
                "author": m["author"],
                "body": m["body"],
                "content_type": m["content_type"],
                "status": m["status"],
                "at": m["created_at"].isoformat() if m["created_at"] else None,
            }
            for m in msgs
        ],
        "events": [
            {
                "type": e["event_type"],
                "props": _jsonb(e["properties"]),
                "at": e["created_at"].isoformat() if e["created_at"] else None,
            }
            for e in evs
        ],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8099, log_level="warning")
