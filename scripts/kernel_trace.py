"""NX-336 — tipărește traceul kernelului unui tur (`kernel_trace.render`), din
`conversation_traces.diagnostics["kernel"]`.

    python scripts/kernel_trace.py --business sole-ro <turn_id>

Contractul, §„Canonical turn trace": „deschizi turul și citești primul ✗". Un tur căzut pe calea
v1 are `kernel_fallback{reason, vocabulary_snapshot}` în loc de trace; se tipărește motivul.

READ-ONLY. Slug-ul se rezolvă pe `admin_conn` (operația care DERIVĂ tenantul, ca la NX-335), iar
rândul se citește pe `tenant_conn` cu `business_id = $1` (P7, RLS ca plasă). Textul clientului e
`client_text`, deja forma SAFE (NX-230); traceul e redactat înainte de stocare."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.conversation.kernel_trace import KernelTrace, render  # noqa: E402

TRACE_SQL = """
select diagnostics, client_text
from conversation_traces
where business_id = $1::uuid and turn_id = $2::uuid
"""
BUSINESS_SQL = "select id::text from businesses where slug = $1 or id::text = $1"

Opener = Callable[..., AbstractAsyncContextManager[Any]]


def format_row(diagnostics: Any, client_text: str | None) -> str:
    """Rândul `conversation_traces` ca text. PUR."""
    doc = json.loads(diagnostics) if isinstance(diagnostics, str) else dict(diagnostics or {})
    kernel = doc.get("kernel")
    if kernel:
        return render(KernelTrace.model_validate(kernel), user_text=client_text)
    fallback = doc.get("kernel_fallback")
    if fallback:
        head = (
            f"KERNEL FALLBACK    reason={fallback.get('reason')}  "
            f"vocabulary={fallback.get('vocabulary_snapshot')}"
        )
        chain = fallback.get("chain")
        if chain:
            # NX-387: lanțul care a dus la fallback (redactat), când turul a ajuns până la executori
            body = render(KernelTrace.model_validate(chain), user_text=client_text)
            return f"{head}\n\n{body}"
        return head
    return "fără trace de kernel pe turul ăsta (flag stins sau tur oprit înaintea ramurii)"


async def load(business: str, turn_id: str, *, admin: Opener, tenant: Opener) -> str:
    """Traceul turului, ca text. `admin()` / `tenant(business_id)` deschid conexiunile (injectate,
    ca testul să ruleze pe o conexiune falsă)."""
    async with admin() as conn:
        business_id = await conn.fetchval(BUSINESS_SQL, business)
    if business_id is None:
        raise SystemExit(f"tenant necunoscut: {business!r}")
    async with tenant(business_id) as conn:
        row = await conn.fetchrow(TRACE_SQL, business_id, turn_id)
    if row is None:
        return f"tur necunoscut pe {business}: {turn_id}"
    return format_row(row["diagnostics"], row["client_text"])


async def _main(business: str, turn_id: str) -> str:
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415

    try:
        pool = await get_pool()
        return await load(business, turn_id, admin=lambda: admin_conn(pool), tenant=tenant_conn)
    finally:
        await close_pool()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--business", required=True, help="slug-ul sau id-ul tenantului")
    parser.add_argument("turn_id")
    args = parser.parse_args(argv)
    print(asyncio.run(_main(args.business, args.turn_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
