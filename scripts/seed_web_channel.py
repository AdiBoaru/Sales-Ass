"""Seed-ul canalului WEB (widget de chat) pentru business-ul demo (NX-20a).

Creează rândul `channels(kind='webchat')` cu un `public_token` (= provider_account_id) și un
`session_secret` (în `settings`). IDEMPOTENT: la re-rulare reutilizează tokenul + secretul
existente (altfel s-ar invalida sigurile vizitatorilor deja emise). Rulează ca ADMIN
(`channels` e read-only pentru bot).

Rulează (local sau pe VPS, cu .env populat):
    python scripts/seed_web_channel.py                    # tenantul din SEED_BUSINESS_SLUG
    python scripts/seed_web_channel.py --business sole-ro # explicit (slug sau uuid)

Afișează `public_token`-ul de pus în widget (`data-token`).
"""

import argparse
import asyncio
import os
import secrets
import socket
import ssl
import sys
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

# Rădăcina proiectului pe sys.path când scriptul e rulat direct (python scripts/...).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.queries.channels import upsert_channel  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


async def _resolve_business(conn: asyncpg.Connection, ref: str) -> str:
    """`ref` = uuid SAU slug → business_id. Slugul e ce scrie omul; uuid-ul e ce vrea schema.

    Un slug inexistent oprește scriptul: altfel ar crea un canal orfan, iar
    `resolve_web_session` (JOIN pe `businesses`) l-ar respinge tăcut la prima cerere.
    """
    row = await conn.fetchrow(
        "select id::text as id from businesses where slug = $1 or id::text = $1", ref
    )
    if row is None:
        sys.exit(f"Business inexistent: {ref!r} (nici slug, nici uuid)")
    return row["id"]


async def _connect() -> asyncpg.Connection:
    p = urlparse(DSN)
    ip = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(
        host=ip,
        port=p.port or 5432,
        user=unquote(p.username),
        password=unquote(p.password),
        database=(p.path or "/postgres").lstrip("/"),
        ssl=ctx,
    )


async def main() -> None:
    if not DSN:
        sys.exit("SUPABASE_DB_URL lipsește în .env")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--business",
        default=os.environ.get("SEED_BUSINESS_SLUG") or DEMO_BIZ,
        help="slug sau uuid; implicit SEED_BUSINESS_SLUG din .env",
    )
    args = ap.parse_args()

    conn = await _connect()
    try:
        business_id = await _resolve_business(conn, args.business)
        # Reutilizează tokenul + secretul unui canal webchat existent (stabilitate sigur vizitator).
        existing = await conn.fetchrow(
            """
            select provider_account_id, settings->>'session_secret' as secret
            from channels
            where business_id = $1 and kind = 'webchat' and status = 'active'
            limit 1
            """,
            business_id,
        )
        if existing and existing["secret"]:
            public_token, secret = existing["provider_account_id"], existing["secret"]
            print("Canal webchat existent → reutilizez tokenul/secretul.")
        else:
            public_token = "pub_" + secrets.token_hex(16)
            secret = secrets.token_hex(32)

        result = await upsert_channel(
            conn,
            business_id,
            "webchat",
            public_token,
            display_name=f"Web widget ({args.business})",
            settings={"public_token": public_token, "session_secret": secret},
        )
        verb = "creat" if result["created"] else "exista (reactivat)"
        print(f"Canal webchat {verb}: channel_id={result['id']} business={business_id}")
        print(f"\npublic_token (data-token în widget): {public_token}")
        print("session_secret: <ascuns în channels.settings>")
    finally:
        await conn.close()

    print("\nGata. Endpointurile /web/* devin live după NX-20b/20c (WEB_ENABLED=true).")


if __name__ == "__main__":
    asyncio.run(main())
