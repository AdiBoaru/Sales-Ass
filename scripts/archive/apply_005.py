# DEPRECAT (NX-123): foloseste `python scripts/migrate.py` (runner ordonat + tracking).
# Pastrat doar pentru istoric — NU mai rula manual apply_0NN.py.
"""Aplică NX-50: face `bot_runtime` rol de LOGIN cu parolă (din vault) + verifică.

Rulează (parola NU se comite — vine din environment):
    BOT_RUNTIME_PASSWORD='...' python scripts/apply_005.py

Se conectează ca ADMIN (SUPABASE_DB_URL = postgres). Parola se citează
server-side cu format(%L) → zero injection (DDL nu acceptă bind params).
Vezi docs/005_bot_runtime_login.sql pentru context + varianta manuală.
"""

import asyncio
import os
import socket
import ssl
import sys
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
BOT_PASSWORD = os.environ.get("BOT_RUNTIME_PASSWORD")


async def connect():
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


def check(label, got, expected):
    ok = got == expected
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got} (aștept {expected})")
    return ok


async def main():
    if not DSN:
        print("EROARE: SUPABASE_DB_URL lipsește din .env")
        sys.exit(2)
    if not BOT_PASSWORD:
        print("EROARE: setează BOT_RUNTIME_PASSWORD în environment (din vault)")
        sys.exit(2)

    conn = await connect()
    results = []
    try:
        exists = await conn.fetchval(
            "select exists(select 1 from pg_roles where rolname='bot_runtime')"
        )
        if not exists:
            print("EROARE: bot_runtime lipsește — rulează întâi scripts/apply_003.py")
            sys.exit(1)

        print("1. ALTER ROLE bot_runtime LOGIN PASSWORD ... ")
        # %L citează parola ca literal SQL (escape sigur). Construim DDL-ul server-side.
        ddl = await conn.fetchval(
            "select format('alter role bot_runtime login password %L', $1::text)",
            BOT_PASSWORD,
        )
        await conn.execute(ddl)
        print("   aplicat fără erori.")

        print("\n2. Verific atributele rolului:")
        row = await conn.fetchrow(
            "select rolcanlogin, rolbypassrls, rolsuper from pg_roles where rolname='bot_runtime'"
        )
        results.append(check("LOGIN activat", row["rolcanlogin"], True))
        results.append(check("FĂRĂ bypassrls (RLS rămâne plasă)", row["rolbypassrls"], False))
        results.append(check("NU e superuser", row["rolsuper"], False))
    finally:
        await conn.close()

    ok_msg = "PROVISIONING OK ✓ — setează DATABASE_URL_BOT în .env"
    print("\n" + (ok_msg if all(results) else "EXISTĂ VERIFICĂRI PICATE ✗"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
