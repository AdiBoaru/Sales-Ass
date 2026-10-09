"""NX-396 — vorbești cu asistentul pe calea WEB REALĂ (`/web/chat`, in-process), tur cu tur.

    PYTHONPATH=. python scripts/sim/agent_chat.py             # interactiv, pe SEED_BUSINESS_SLUG
    PYTHONPATH=. python scripts/sim/agent_chat.py --business sole-ro --keep

Fiecare mesaj trece prin pipeline-ul real (porți, straturi, agentul unic, plasa), cu modelul REAL
și DB-ul real, deci CONSUMĂ CREDITE: îl pornește Adi. După fiecare răspuns se tipăresc textul,
cardurile, sugestiile și cine a servit turul (`assistant_turn` = agentul, `assistant_fallback` =
plasa, cu motivul), din evenimentele turului.

Comenzi în prompt: `/nou` = conversație nouă (alt vizitator), `/iesi` = ieșire.
Vizitatorii sunt marcați `web_audit_agent_chat_*` și se șterg la ieșire (ca la `web_audit.py`),
în afară de `--keep`, când rămân în DB ca să-i poți citi (`conversation_traces`).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _who_served(business_id: str, since: float) -> str:
    """Cine a servit ultimul tur, din `analytics_events` (citire scurtă pe conexiunea de admin)."""
    from datetime import UTC, datetime

    from src.db.connection import admin_conn, get_pool

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        row = await conn.fetchrow(
            """
            select event_type, properties from analytics_events
             where business_id = $1 and created_at >= $2
               and event_type in ('assistant_turn', 'assistant_fallback')
             order by created_at desc limit 1
            """,
            business_id,
            datetime.fromtimestamp(since, UTC),
        )
    if row is None:
        return "fără eveniment de agent (agentul n-a rulat: flag stins, creier unic sau alt strat)"
    props = row["properties"] if isinstance(row["properties"], dict) else {}
    if row["event_type"] == "assistant_turn":
        return (
            f"AGENT · {props.get('rounds')} runde · unelte {props.get('tools')} · "
            f"reîncercări {props.get('retries')} · {props.get('ms')} ms"
        )
    return f"PLASA (agentul a căzut: {props.get('reason')}) · {props.get('ms')} ms"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--business", default=None, help="slug; implicit SEED_BUSINESS_SLUG din .env")
    ap.add_argument("--keep", action="store_true", help="nu șterge conversațiile la ieșire")
    args = ap.parse_args()

    from scripts.sim import web_audit as wa

    wa._install_fake_redis()  # noqa: SLF001 — aceeași cale ca auditul (Redis-ul Docker nu e pe host)
    from src.config import get_settings
    from src.db.connection import admin_conn, close_pool, get_pool

    s = get_settings()
    print(
        f"agent: {'PORNIT' if s.assistant_agent_enabled else 'STINS'} "
        f"({s.assistant_canary_percent}%) · creier unic: {s.single_brain_enabled} · "
        f"model: {s.model_agent} · efort: {s.llm_reasoning_effort_assistant}"
    )
    slug = args.business or os.environ.get("SEED_BUSINESS_SLUG") or "sole-ro"
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "select c.provider_account_id, c.business_id::text as business_id "
            "from channels c join businesses b on b.id = c.business_id "
            "where b.slug = $1 and c.kind = 'webchat' and c.status = 'active' limit 1",
            slug,
        )
    if row is None:
        print(f"Niciun canal webchat activ pe {slug!r}.")
        return 1
    token, business_id = row["provider_account_id"], row["business_id"]

    async def new_client() -> wa.WebClient:
        vid, sig = await wa._session(token, "agent_chat")  # noqa: SLF001
        return wa.WebClient(token, vid, sig, "agent_chat")

    client = await new_client()
    print(f"tenant {slug} · scrie mesajul (/nou = conversație nouă, /iesi = ieșire)\n")
    try:
        while True:
            try:
                text = input("tu> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text == "/iesi":
                break
            if text == "/nou":
                client = await new_client()
                print("— conversație nouă —\n")
                continue
            started = time.time()
            t = await client.say(text)
            print(f"\nbot> {t.content}")
            for p in t.products:
                reason = f" — {p.get('reason')}" if p.get("reason") else ""
                print(f"  • {p.get('name')} · {p.get('price')} lei{reason}")
            if t.suggestions:
                print("  sugestii: " + " | ".join(t.suggestions))
            print(f"  [{await _who_served(business_id, started - 1)}]\n")
    finally:
        if not args.keep:
            async with admin_conn(pool) as conn:
                purged = await wa._purge_audit(conn, business_id)  # noqa: SLF001
            print(f"curățat {purged} vizitator(i) de test")
        await close_pool()
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
