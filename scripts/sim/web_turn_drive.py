"""NX-232 — manual drive pentru ledgerul de ture web (cardul, secțiunea „Manual drive").

Pașii (identici în ambele moduri):
  1. sesiune web → trimite ACELAȘI request de DOUĂ ori CONCURENT, cu același `client_msg_id`;
  2. retrimite a treia oară, secvențial (refresh / răspuns HTTP pierdut);
  3. retrimite cu ACELAȘI ID dar ALT text → așteaptă 409 `idempotency_conflict`.

Așteptat (cu `WEB_TURN_LEDGER_ENABLED=true`): UN singur apel de pipeline, toate răspunsurile
terminale BYTE-IDENTICE (replay din `web_turns.response_json`), conflictul → 409.

Moduri:
  • IN-PROCESS (default) — harness-ul web_audit (NX-179): fakeredis + `/web/chat` GENUIN,
    DB live + OpenAI real, flagul de ledger pornit DOAR în acest proces, apelurile de pipeline
    NUMĂRATE prin wrapper pe `handle_turn`, vizitatorul de drive auto-curățat la final.
        python scripts/sim/web_turn_drive.py
  • HTTP — același drive contra unui backend pornit (VPS/local cu flagul ON):
        python scripts/sim/web_turn_drive.py --base http://127.0.0.1:8090 --token <public_token>
    Pentru crash-point-ul „commit reușit, răspuns pierdut": kill imediat după logul
    `tur procesat:`, repornește, apoi `--replay-id <client_msg_id>` → același ViewModel, zero LLM.

NB: consumă credite LLM la PRIMUL tur (unul singur) — de rulat de om, nu din CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

# Flagul TREBUIE în env înainte ca `get_settings()` să fie apelat prima dată (cache LRU);
# load_dotenv NU suprascrie variabilele deja setate, deci rămâne al nostru.
os.environ.setdefault("WEB_TURN_LEDGER_ENABLED", "true")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

DRIVE_TEXT = "Bună, căutați-mi un șampon pentru păr gras"


def _canon(body: object) -> str:
    return json.dumps(body, sort_keys=True, ensure_ascii=False)


def _report(results: list[tuple[str, int, object]]) -> int:
    """Verdictul comun: câte răspunsuri terminale DISTINCTE există (așteptat: exact 1)."""
    for label, code, body in results:
        print(f"[{label}] {code}: {_canon(body)[:400]}")
    terminals = {
        _canon(b) for _, c, b in results if c == 200 and isinstance(b, dict) and "turn" not in b
    }
    print(f"\nrăspunsuri terminale distincte (așteptat 1): {len(terminals)}")
    return 0 if len(terminals) == 1 else 2


# ── Modul IN-PROCESS (calea /web/chat reală, fakeredis, LLM real) ───────────────────────────


async def _drive_inprocess() -> int:
    from scripts.sim.web_audit import (
        _FakeRequest,
        _install_fake_redis,
        _purge_audit,
        _session,
    )

    _install_fake_redis()  # ÎNAINTE de orice import care capturează get_redis

    from fastapi import HTTPException

    import src.web.app as wa
    from src.db.connection import admin_conn, close_pool, get_pool
    from src.web.app import WebChatIn

    # Numărăm apelurile REALE de pipeline — dovada „fără al doilea apel LLM" cerută de card.
    calls = {"pipeline": 0}
    real_handle = wa.handle_turn

    async def counting_handle(*a, **kw):
        calls["pipeline"] += 1
        return await real_handle(*a, **kw)

    wa.handle_turn = counting_handle

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "select provider_account_id, business_id::text as business_id "
            "from channels where kind='webchat' and status='active' limit 1"
        )
    if row is None:
        print("Niciun canal webchat activ în DB.")
        return 1
    token, biz_id = row["provider_account_id"], row["business_id"]
    vid, sig = await _session(token, "nx232_drive")
    cid = str(uuid4())
    print(f"canal webchat: {token[:12]}…  visitor: {vid[:24]}…  client_msg_id: {cid}")

    async def chat(text: str, client_msg_id: str) -> tuple[int, object]:
        payload = {
            "token": token,
            "visitor_id": vid,
            "sig": sig,
            "message": text,
            "client_msg_id": client_msg_id,
        }
        try:
            return 200, await wa.web_chat(WebChatIn(**payload), _FakeRequest(payload))
        except HTTPException as e:
            return e.status_code, {"detail": e.detail}

    try:
        (c1, b1), (c2, b2) = await asyncio.gather(chat(DRIVE_TEXT, cid), chat(DRIVE_TEXT, cid))
        c3, b3 = await chat(DRIVE_TEXT, cid)  # refresh/retry → replay terminal identic
        code = _report([("concurent A", c1, b1), ("concurent B", c2, b2), ("retry", c3, b3)])
        print(f"apeluri de pipeline (așteptat 1): {calls['pipeline']}")
        if calls["pipeline"] != 1:
            code = 2

        c4, b4 = await chat("cu totul alt mesaj", cid)
        print(f"[alt input, același ID] {c4} (așteptat 409): {b4}")
        if c4 != 409:
            code = 2

        async with admin_conn(pool) as conn:
            ledger = await conn.fetchrow(
                "select status, attempt, lease_epoch, response_json is not null as has_view "
                "from web_turns where business_id = $1 and client_turn_id = $2",
                biz_id,
                cid,
            )
        print(f"rând ledger: {dict(ledger) if ledger else None}")
        return code
    finally:
        wa.handle_turn = real_handle
        try:
            async with admin_conn(pool) as conn:
                purged = await _purge_audit(conn, biz_id)
            if purged:
                print(f"auto-curățat {purged} vizitator(i) de drive (`web_audit_%`).")
        except Exception as e:  # noqa: BLE001 — curățarea nu maschează verdictul
            print(f"⚠ auto-curățarea a eșuat ({type(e).__name__}) — rulează web_audit/purge.")
        await close_pool()


# ── Modul HTTP (backend pornit separat, cu flagul ON) ───────────────────────────────────────


async def _drive_http(base: str, token: str, text: str, replay_id: str | None) -> int:
    import httpx

    async def chat(client, sess, msg, cid):
        r = await client.post(
            f"{base}/web/chat",
            json={
                "token": sess["token"],
                "visitor_id": sess["visitor_id"],
                "sig": sess["sig"],
                "message": msg,
                "client_msg_id": cid,
            },
            timeout=90,
        )
        is_json = r.headers.get("content-type", "").startswith("application/json")
        return r.status_code, (r.json() if is_json else r.text)

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{base}/web/bootstrap", params={"token": token})
        r.raise_for_status()
        sess = r.json()
        cid = replay_id or str(uuid4())
        print(f"client_msg_id = {cid}")

        if replay_id:
            code, body = await chat(client, sess, text, cid)
            print(f"[replay după restart] {code}: {_canon(body)[:400]}")
            return 0

        (c1, b1), (c2, b2) = await asyncio.gather(
            chat(client, sess, text, cid), chat(client, sess, text, cid)
        )
        c3, b3 = await chat(client, sess, text, cid)
        code = _report([("concurent A", c1, b1), ("concurent B", c2, b2), ("retry", c3, b3)])
        c4, b4 = await chat(client, sess, "cu totul alt mesaj", cid)
        print(f"[alt input, același ID] {c4} (așteptat 409): {b4}")
    return code


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="URL backend (fără el: modul in-process)")
    ap.add_argument("--token", default=None, help="public token webchat (cerut în modul HTTP)")
    ap.add_argument("--text", default=DRIVE_TEXT)
    ap.add_argument("--replay-id", default=None, help="client_msg_id de rejucat după restart")
    args = ap.parse_args()

    if args.base:
        if not args.token:
            print("Modul HTTP cere --token.")
            return 2
        return await _drive_http(args.base, args.token, args.text, args.replay_id)
    return await _drive_inprocess()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    raise SystemExit(asyncio.run(main()))
