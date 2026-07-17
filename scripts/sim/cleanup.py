"""Curăță datele de SIMULARE din tenantul demo (identități de canal `sim:%`).

De ce există: `scripts/sim/server.py` + `nx172_smoke.py` conduc conversații REALE prin pipeline pe
DB-ul live. Fiecare rulare lasă contacte, conversații, mesaje și rânduri de `outbox` commit-uite.
Se adună — și nu doar ca gunoi: testele de integrare care revendicau coada (`claim_due`) picau cu
«assert 2 == 1» din cauza lor, iar eșecul se raporta ca regresie de dispatcher (NX-177).

Scriptul era documentat ca prerechizit în `server.py` și `nx172_smoke.py`, dar NU era în repo —
exista doar ca fișier local, netrackat, pe mașina lui Adi. Adică: toți ceilalți (CI, Codex, orice
mașină nouă) n-aveau cum să curețe.

GARANȚII:
  - **dry-run by default** — arată ce ar șterge; scrie DOAR cu `--apply`;
  - **scoped**: un singur `--business` (P7) + DOAR identități `sim:%` → datele reale nu se ating;
  - **tranzacție all-or-nothing** — un eșec la mijloc nu lasă contacte fără conversații;
  - rol ADMIN (mentenanță cross-tabel, ca `jobs/cleanup_dedupe`).

Rulare:
    PYTHONPATH=. python scripts/sim/cleanup.py                    # dry-run
    PYTHONPATH=. python scripts/sim/cleanup.py --apply            # șterge
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Marcajul datelor de simulare. `server.py` scrie `sender` ca `sim:<run>:<persona>` →
# `channel_identities.external_id`. Orice contact care NU are o identitate `sim:%` e REAL.
_SIM_PREFIX = "sim:%"
_SIM_DEDUPE_PREFIX = "sim.%"


async def _collect(conn, business_id: str) -> tuple[list, list]:
    """`(contact_ids, conversation_ids)` ale simulării. Sursa de adevăr = identitatea de canal."""
    cids = [
        r["contact_id"]
        for r in await conn.fetch(
            "select contact_id from channel_identities "
            "where business_id = $1 and external_id like $2",
            business_id,
            _SIM_PREFIX,
        )
    ]
    if not cids:
        return [], []
    convs = [
        r["id"]
        for r in await conn.fetch(
            "select id from conversations where business_id = $1 and contact_id = any($2::uuid[])",
            business_id,
            cids,
        )
    ]
    return cids, convs


async def _counts(conn, business_id: str, cids: list, convs: list) -> dict[str, int]:
    """Ce s-ar șterge (dry-run) — aceleași predicate ca ștergerea, doar cu `count(*)`."""
    out: dict[str, int] = {}
    by_conv = (
        "messages",
        "outbox",
        "conversation_summaries",
        "analytics_events",
        "checkout_links",
        "proactive_jobs",
    )
    for t in by_conv:
        out[t] = (
            await conn.fetchval(
                f"select count(*) from {t} where conversation_id = any($1::uuid[])",  # noqa: S608
                convs,
            )
            if convs
            else 0
        )
    out["back_in_stock_subscriptions"] = await conn.fetchval(
        "select count(*) from back_in_stock_subscriptions where contact_id = any($1::uuid[])", cids
    )
    out["conversations"] = len(convs)
    out["channel_identities"] = await conn.fetchval(
        "select count(*) from channel_identities where contact_id = any($1::uuid[])", cids
    )
    out["contacts"] = len(cids)
    out["inbound_dedupe"] = await conn.fetchval(
        "select count(*) from inbound_dedupe where business_id = $1 and provider_msg_id like $2",
        business_id,
        _SIM_DEDUPE_PREFIX,
    )
    return out


async def _purge(conn, business_id: str, cids: list, convs: list) -> dict[str, int]:
    """Șterge în ordinea dependențelor (copii → părinți). Tot în TX-ul caller-ului."""

    async def d(sql: str, *args) -> int:
        res = await conn.execute(sql, *args)
        return int(res.split()[-1]) if res else 0

    out: dict[str, int] = {}
    if convs:
        # `proactive_jobs` ÎNAINTE de conversations (FK) — un job de sim ar trimite altfel un
        # mesaj proactiv pentru o conversație ștearsă.
        for t in (
            "messages",
            "outbox",
            "conversation_summaries",
            "analytics_events",
            "checkout_links",
            "proactive_jobs",
        ):
            out[t] = await d(
                f"delete from {t} where conversation_id = any($1::uuid[])",  # noqa: S608
                convs,
            )
    out["back_in_stock_subscriptions"] = await d(
        "delete from back_in_stock_subscriptions where contact_id = any($1::uuid[])", cids
    )
    out["conversations"] = await d(
        "delete from conversations where business_id = $1 and contact_id = any($2::uuid[])",
        business_id,
        cids,
    )
    out["channel_identities"] = await d(
        "delete from channel_identities where business_id = $1 and contact_id = any($2::uuid[])",
        business_id,
        cids,
    )
    out["contacts"] = await d(
        "delete from contacts where business_id = $1 and id = any($2::uuid[])", business_id, cids
    )
    out["inbound_dedupe"] = await d(
        "delete from inbound_dedupe where business_id = $1 and provider_msg_id like $2",
        business_id,
        _SIM_DEDUPE_PREFIX,
    )
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="Purjă a datelor de simulare (sim:*)")
    ap.add_argument("--business", default=DEMO_BIZ, help=f"business_id (default: {DEMO_BIZ})")
    ap.add_argument("--apply", action="store_true", help="ȘTERGE (fără el: dry-run)")
    args = ap.parse_args()

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        async with conn.transaction():
            cids, convs = await _collect(conn, args.business)
            if not cids:
                print("Nimic de curățat (0 contacte `sim:`).")
                return 0
            stats = (
                await _purge(conn, args.business, cids, convs)
                if args.apply
                else await _counts(conn, args.business, cids, convs)
            )
            if not args.apply:  # dry-run → nu lăsăm nimic în urmă nici măcar accidental
                raise _DryRun(stats, len(cids), len(convs))
    _report(stats, len(cids), len(convs), applied=True)
    return 0


class _DryRun(Exception):
    """Rollback intenționat al TX-ului de dry-run (nimic nu se scrie)."""

    def __init__(self, stats: dict[str, int], n_contacts: int, n_convs: int):
        self.stats, self.n_contacts, self.n_convs = stats, n_contacts, n_convs


def _report(stats: dict[str, int], n_contacts: int, n_convs: int, *, applied: bool) -> None:
    head = "ȘTERS" if applied else "DRY-RUN — s-ar șterge"
    print(f"{head}: {n_contacts} contacte `sim:` + {n_convs} conversații")
    for k, v in stats.items():
        print(f"  {k:<28} {v}")
    if not applied:
        print("\nNimic nu s-a scris. Rulează cu --apply ca să ștergi.")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    async def _run() -> int:
        try:
            return await main()
        except _DryRun as dr:
            _report(dr.stats, dr.n_contacts, dr.n_convs, applied=False)
            return 0
        finally:
            await close_pool()

    raise SystemExit(asyncio.run(_run()))
