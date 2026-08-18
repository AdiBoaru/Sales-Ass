"""NX-247 — launcherul harnessului E2E Stage 1. Loopback, `ENV=test`, secret per proces.

Ce face, în ordine (ordinea NU e cosmetică):

  1. scrie profilul de flag-uri în `os.environ` ÎNAINTE de orice import de `src.*` — `Settings` e
     cache-uit, deci un flag pus mai târziu nu s-ar aplica și harnessul ar rula pe altă
     configurație decât cea pe care o declară în handshake;
  2. generează secretele efemere de care are nevoie configurația v2 (inel de chei de acțiuni,
     secrete de fingerprint/prompt) — random per proces, niciodată dintr-un `.env` de producție;
  3. instalează garda de rețea: numai loopback + DB/Redis. Orice altceva e refuzat și NUMĂRAT;
  4. seedează doi tenanți sintetici cu ID-uri vecine (vezi `sibling_business_ids`);
  5. construiește aplicația de PRODUCȚIE + routerul de control, pornește executorul și sweeperul;
  6. scrie fișierul de handshake pe care îl citește suportul Playwright (PR B);
  7. la ieșire, șterge tenanții — inclusiv pe calea de excepție.

Rulare:
    ENV=test python scripts/stage1_e2e_server.py --port 8099 \\
        --handshake .stage1-e2e/handshake.json --origin http://localhost:4173

Handshake-ul conține secretul de control și tokenurile publice ale tenanților: e efemer,
gitignorat, șters la oprire, și NU are ce căuta într-un artefact de CI.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import secrets
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _ephemeral_secrets(origin: str) -> None:
    """Secrete random per proces. `setdefault` NU se folosește pentru cele criptografice: dacă un
    `.env` local le are setate, harnessul trebuie să le SUPRASCRIE — un test care semnează cu
    cheia de producție e un test care poate emite tokenuri valide în producție."""
    os.environ["WEB_ACTION_KEYS"] = f"e2e1:{base64.b64encode(secrets.token_bytes(32)).decode()}"
    os.environ["WEB_TURN_FINGERPRINT_SECRET"] = secrets.token_urlsafe(32)
    os.environ["WEB_FEEDBACK_PROMPT_SECRET"] = secrets.token_urlsafe(32)
    os.environ["OPENAI_API_KEY"] = "stage1-e2e-no-network"
    os.environ["WEB_CORS_ORIGINS"] = origin
    os.environ["WEB_DEMO_ACCESS_ENABLED"] = "false"
    os.environ.setdefault("LOG_LEVEL", "WARNING")


async def _serve(args: argparse.Namespace) -> int:
    # Importurile de `src.*` se fac AICI, nu la nivel de modul: `main()` a scris deja profilul de
    # flag-uri în `os.environ`, iar `Settings` e cache-uit la primul `get_settings()`.
    from src.config import get_settings
    from src.db.connection import admin_conn, close_pool, get_pool
    from tests.e2e.stage1_app import deny_outbound_network
    from tests.e2e.stage1_scenarios import (
        drop_tenant,
        make_tenants,
        purge_synthetic_tenants,
        seed_tenant,
    )

    guard = deny_outbound_network()
    settings = get_settings()
    alpha, beta = make_tenants()
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        exists = await conn.fetchval("select to_regclass('public.web_turns') is not null")
    if not exists:
        print(
            "migrarea 040_web_turns lipsește pe DB-ul țintă — rulează `python scripts/migrate.py`",
            file=sys.stderr,
        )
        return 3
    # Purjă la PORNIRE, nu doar la oprire: un `kill -9` (sau Stop-Process pe Windows) sare peste
    # `finally`, iar fără asta tenanții sintetici s-ar acumula la fiecare crash și „stack efemer" ar
    # deveni o afirmație falsă. Self-healing, ca `_purge_audit` din scripts/sim/web_audit.py.
    async with admin_conn(pool) as conn:
        leftovers = await purge_synthetic_tenants(conn)
    if leftovers:
        print(f"purjat {len(leftovers)} tenanți sintetici rămași de la o rulare anterioară")
    if args.purge_only:
        await close_pool()
        guard.uninstall()
        return 0

    # De aici încolo TOTUL e în `try`, iar `finally` șterge tenanții: orice cale de eroare dintre
    # „am scris date" și „am pornit" trebuie să curețe (prima rulare a lăsat două businessuri în DB
    # fiindcă un `print` a crăpat exact între seed și `serve()`).
    async with admin_conn(pool) as conn:
        for tenant in (alpha, beta):
            await seed_tenant(conn, tenant, embed_model=settings.model_embed)
    try:
        return await _run(args, alpha, beta, pool, guard)
    finally:
        async with admin_conn(pool) as conn:
            for tenant in (alpha, beta):
                await drop_tenant(conn, tenant.business_id)
        await close_pool()
        guard.uninstall()
        if guard.attempts:
            print(f"ATENȚIE: {len(guard.attempts)} tentative de rețea refuzate: {guard.attempts}")


async def _run(args: argparse.Namespace, alpha, beta, pool, guard) -> int:
    """Partea care servește. Separată de seed/cleanup ca `finally`-ul de mai sus să acopere tot."""
    import uvicorn

    from tests.e2e.stage1_app import CERTIFIED_PROFILE, build_stage1_app

    control_secret = secrets.token_urlsafe(48)
    tenants = {alpha.key: alpha, beta.key: beta}
    app = build_stage1_app(control_secret=control_secret, bind_host=args.host, tenants=tenants)

    handshake = {
        "_note": "EFEMER. Conține secretul de control — nu se comite, nu se urcă ca artifact.",
        "base_url": f"http://{args.host}:{args.port}",
        "control_header": "X-Stage1-Control",
        "control_secret": control_secret,
        "flag_profile": args.profile,
        "certified_profile": CERTIFIED_PROFILE,
        "allowed_origin": args.origin,
        "tenants": {
            key: {"token": t.channel_token, "locale": t.locale} for key, t in tenants.items()
        },
    }
    handshake_path = Path(args.handshake)
    handshake_path.parent.mkdir(parents=True, exist_ok=True)
    # Suprascriere, nu append: un handshake rămas de la un proces omorât poartă un secret care nu
    # mai e onorat de nimeni, iar suportul Playwright l-ar citi și ar primi 404 fără explicație.
    handshake_path.write_text(json.dumps(handshake, indent=2) + "\n", encoding="utf-8")
    print(f"handshake: {handshake_path} (secret redactat: {control_secret[:4]}…)")
    print(f"tenanți: alpha={alpha.channel_token} beta={beta.channel_token}")

    from src.redis_bus import get_redis
    from src.web.turn_executor import WebTurnExecutor
    from src.web.turn_recovery import run_recovery_loop

    redis = await get_redis()
    executor = WebTurnExecutor(redis, owner="stage1-e2e")
    background = [
        asyncio.create_task(executor.run()),
        asyncio.create_task(run_recovery_loop(redis)),
    ]

    config = uvicorn.Config(
        app, host=args.host, port=args.port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    stop = asyncio.Event()

    def _request_stop(*_a) -> None:
        stop.set()
        server.should_exit = True
        executor.request_stop()

    with contextlib.suppress(NotImplementedError, ValueError):
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)

    try:
        await server.serve()
    finally:
        executor.request_stop()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        # Handshake-ul dispare ODATĂ cu procesul: secretul de control n-are de ce să supraviețuiască
        # serverului care îl onora. Ștergerea tenanților e în `_serve`, ca să acopere și căderile
        # de dinainte de `serve()`.
        with contextlib.suppress(OSError):
            handshake_path.unlink()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Harness E2E Stage 1 (NX-247), test-only, loopback")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--profile", default="v2_transport", help="profil de flag-uri (stage1_app)")
    ap.add_argument("--origin", default="http://localhost:4173", help="originul allowlistat")
    ap.add_argument("--handshake", default=".stage1-e2e/handshake.json")
    ap.add_argument(
        "--purge-only",
        action="store_true",
        help="șterge doar tenanții sintetici rămași dintr-o rulare crăpată, fără a porni serverul",
    )
    args = ap.parse_args()

    if args.host not in LOOPBACK:
        print(
            f"refuz: harnessul se leagă DOAR la loopback (cerut {args.host!r}).",
            file=sys.stderr,
        )
        return 2
    if os.environ.get("ENV") not in (None, "", "test"):
        print(
            f"refuz: ENV={os.environ['ENV']!r} — harnessul rulează doar cu ENV=test",
            file=sys.stderr,
        )
        return 2

    from tests.e2e.stage1_app import apply_flag_profile  # noqa: PLC0415 — înainte de src.config

    apply_flag_profile(args.profile)
    _ephemeral_secrets(args.origin)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        # Consola Windows e cp1252: un `print` cu diacritice ridică UnicodeEncodeError și omoară
        # launcherul DUPĂ ce a seedat datele. Același tipar ca în `scripts/migrate.py`.
        sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(_serve(args))


if __name__ == "__main__":
    raise SystemExit(main())
