"""Seed-ul canalului Telegram pentru business-ul demo (NX-63).

Validează tokenul cu getMe, derivă bot id-ul și inserează rândul `channels`
(idempotent). Rulează ca ADMIN (channels e read-only pentru bot). DB = Supabase
remote; tokenul din .env.

Rulează (local sau pe VPS, cu .env populat):
    python scripts/seed_telegram_channel.py
"""

import asyncio
import os
import socket
import ssl
import sys
from urllib.parse import unquote, urlparse

import asyncpg
import httpx
from dotenv import load_dotenv

# Rădăcina proiectului pe sys.path când scriptul e rulat direct (python scripts/...),
# nu doar ca modul. Trebuie ÎNAINTE de importul `src`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.queries.channels import upsert_channel  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


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


async def _get_bot(token: str) -> dict:
    """getMe → {id, username}. Validează tokenul (typo → eroare clară, nu rând greșit)."""
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(f"https://api.telegram.org/bot{token}/getMe")
        resp.raise_for_status()
        data = resp.json()
    if not data.get("ok"):
        sys.exit(f"Token Telegram invalid (getMe): {data}")
    return data["result"]


async def main() -> None:
    if not DSN:
        sys.exit("SUPABASE_DB_URL lipsește în .env")
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN lipsește în .env (vezi TODO-MANUAL: TG-TEST)")

    bot = await _get_bot(TOKEN)
    bot_id = str(bot["id"])
    username = bot.get("username")
    print(f"Bot validat: @{username} (id={bot_id})")

    conn = await _connect()
    try:
        result = await upsert_channel(
            conn, DEMO_BIZ, "telegram", bot_id, display_name=f"@{username}"
        )
        verb = "creat" if result["created"] else "exista (reactivat)"
        print(f"Canal telegram {verb}: channel_id={result['id']} business={DEMO_BIZ}")
    finally:
        await conn.close()

    print("\nGata. Pe VPS: `docker compose up -d` apoi scrie botului pe Telegram.")


if __name__ == "__main__":
    asyncio.run(main())
