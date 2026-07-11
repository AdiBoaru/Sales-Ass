"""Seed DEMO pentru raportul de cerere (NX-164) — date SINTETICE de sample, NU live.

Populează `analytics_events` pe TENANTUL DEMO cu evenimente de cerere realiste (unmet_query,
product_search, agent_recommended, cart_updated, checkout_link_created) ca raportul read-side
(`src/db/queries/demand.py`) să fie DEMO-abil imediat, fără a aștepta trafic real.

GĂRZI (respectă regulile de onestitate):
  • DOAR pe business-ul demo (`DEMO_BIZ`) — niciodată amestecat cu clienți reali;
  • fiecare event poartă `properties.demo = true` → identificabil + curățabil;
  • banner zgomotos la rulare: astea NU sunt date live;
  • `--reset` șterge întâi seed-ul demo anterior (idempotent), altfel doar adaugă.

    python scripts/seed_demo_demand.py           # adaugă seed demo
    python scripts/seed_demo_demand.py --reset   # curăță seed-ul demo anterior, apoi seedează
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_settings  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Distribuția de cerere sintetică (skincare) — genul de semnale reale pe care le-ar produce botul:
# branduri NEcatalogate cerute des (adu-le), produse numite lipsă, branduri catalogate căutate,
# produse recomandate/adăugate/checkout. product_id-urile sunt fictive (ref-uri de demo).
_UNMET_NO_RESULT = [
    ("Bioderma", "creme-fata", 7),
    ("Avène", "protectie-solara", 4),
    ("SVR", None, 2),
]
_UNMET_NAMED = [("Bioderma", "Sebium Hydra"), ("La Roche-Posay", "Cicaplast Baume B5 40ml")]
_SEARCH_BRANDS = [
    ("CeraVe", "creme-fata", 9),
    ("La Roche-Posay", "protectie-solara", 6),
    ("Vichy", "seruri", 3),
]
_RECOMMENDED = [("prod-cerave-pm", 12), ("prod-lrp-anthelios", 8), ("prod-vichy-mineral89", 5)]
_CART = [("prod-cerave-pm", 5), ("prod-lrp-anthelios", 3)]
_CHECKOUT = [("prod-cerave-pm", 3), ("prod-lrp-anthelios", 2)]


def _demo(**props: object) -> str:
    """Properties JSON cu markerul `demo=true` (identificabil + curățabil). NU prezentat ca live."""
    return json.dumps({**props, "demo": True})


async def _connect() -> asyncpg.Connection:
    """Conexiune la Supabase (IPv4 + SSL relaxat pentru pooler), ca celelalte seed-uri."""
    p = urlparse(get_settings().supabase_db_url)
    ip = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(
        host=ip,
        port=p.port or 5432,
        user=unquote(p.username or ""),
        password=unquote(p.password or ""),
        database=(p.path or "/postgres").lstrip("/"),
        ssl=ctx,
    )


async def _emit(conn: asyncpg.Connection, event_type: str, props_json: str) -> None:
    """Un event de demo pe tenantul DEMO, cu un conversation_id sintetic (drilldown coerent)."""
    await conn.execute(
        """
        insert into analytics_events (business_id, conversation_id, event_type, properties)
        values ($1, gen_random_uuid(), $2, $3::jsonb)
        """,
        DEMO_BIZ,
        event_type,
        props_json,
    )


async def seed(conn: asyncpg.Connection) -> int:
    n = 0
    for brand, cat, times in _UNMET_NO_RESULT:
        for _ in range(times):
            await _emit(
                conn, "unmet_query", _demo(reason="no_result", brand=brand, category_key=cat)
            )
            n += 1
    for brand, _product in _UNMET_NAMED:
        await _emit(conn, "unmet_query", _demo(reason="named_not_found", brand=brand))
        n += 1
    for brand, cat, times in _SEARCH_BRANDS:
        for _ in range(times):
            await _emit(conn, "product_search", _demo(brand=brand, category_key=cat, count=6))
            n += 1
    for pid, times in _RECOMMENDED:
        for _ in range(times):
            await _emit(conn, "agent_recommended", _demo(n=1, product_ids=[pid]))
            n += 1
    for pid, times in _CART:
        for _ in range(times):
            await _emit(conn, "cart_updated", _demo(lines=1, value=99.0, product_ids=[pid]))
            n += 1
    for pid, times in _CHECKOUT:
        for _ in range(times):
            await _emit(
                conn, "checkout_link_created", _demo(items=1, value=99.0, product_ids=[pid])
            )
            n += 1
    return n


async def reset(conn: asyncpg.Connection) -> int:
    """Șterge DOAR evenimentele de demo (marker `demo=true`) ale tenantului DEMO. Idempotent."""
    res = await conn.execute(
        "delete from analytics_events where business_id = $1 and properties->>'demo' = 'true'",
        DEMO_BIZ,
    )
    return int(res.split()[-1]) if res.startswith("DELETE") else 0


async def main() -> None:
    # Consola Windows (cp1252) nu poate encoda diacriticele din banner → forțează UTF-8 (degradare
    # grațioasă unde nu se poate). Doar output; nu atinge logica.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Seed DEMO — cerere sintetică (NX-164). NU date live.")
    ap.add_argument("--reset", action="store_true", help="șterge seed-ul demo anterior înainte")
    args = ap.parse_args()

    print("=" * 72)
    print("  SEED DEMO — date SINTETICE de sample pentru raportul de cerere (NX-164).")
    print("  NU sunt date live. DOAR tenantul demo. Marker: properties.demo = true.")
    print("=" * 72)

    conn = await _connect()
    try:
        if args.reset:
            deleted = await reset(conn)
            print(f"reset: {deleted} evenimente demo șterse")
        inserted = await seed(conn)
        print(f"seed: {inserted} evenimente demo inserate pe {DEMO_BIZ}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
