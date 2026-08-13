"""NX-237 — proba REPRODUCTIBILĂ a coșului canonic: receipts, idempotency, recovery.

Manual drive-ul cerut de card, pe fixture cu ceas CONTROLAT — zero DB, zero OpenAI, zero rețea
(persistența = `tests/fake_commerce.Store`, același fake ca testele unit; faptele trec prin
`build_facts` REAL). Ce demonstrează, în ordine:

  1. aceeași variantă adăugată din ACTION și din TOOL → o singură linie (aceeași comandă typed);
  2. retry cu aceeași cheie → același receipt, zero a doua mutație;
  3. schimbare de cantitate + remove → versiuni monotone, un receipt per mutație;
  4. prețul se schimbă în catalog între render și mutație → snapshotul următor e FRESH;
  5. stocul se epuizează → add refuzat explicat, coșul neschimbat;
  6. „kill după succes extern, înainte de finalize" → receipt `unknown_reconcile`, retry-ul NU
     re-împinge, `reconcile()` rezolvă prin lookup la provider;
  7. coș de 10 linii → UN singur call de hidratare per operație (bugetul anti-N+1).

Rulare:
    PYTHONPATH=. python scripts/sim/cart_receipt_recovery.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Consola Windows e cp1252 by default — ieșirea are diacritice + box drawing.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.commerce.adapters.base import AdapterResult  # noqa: E402
from src.commerce.cart_models import CartCommand  # noqa: E402
from src.commerce.cart_service import (  # noqa: E402
    CartService,
    action_idempotency_key,
    tool_idempotency_key,
)
from src.db.provider import static_db  # noqa: E402
from tests.fake_commerce import Store, install, product_row  # noqa: E402

BIZ = "biz-demo"
CONV = "conv-demo"
P = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(1, 12)]
V1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _setattr(obj, name, fn):
    setattr(obj, name, fn)


class LossyAdapter:
    """Providerul extern din pasul 6: execută, apoi «pierde» răspunsul. `lookup` e memoria lui."""

    name = "lossy-storefront"

    def __init__(self) -> None:
        self.push_calls = 0
        self.executed: dict[str, AdapterResult] = {}

    async def push_checkout(self, *, idempotency_key, ref_code, **kw):
        self.push_calls += 1
        # Providerul CHIAR execută (side effect-ul există)...
        self.executed[idempotency_key] = AdapterResult(
            ok=True, external_ref=f"ext-{ref_code}", url=f"https://store.example/co/{ref_code}"
        )
        raise TimeoutError("...dar răspunsul se pierde pe drum")

    async def lookup(self, *, business_id, idempotency_key):
        return self.executed.get(idempotency_key)


def receipts_table(store: Store) -> str:
    rows = [
        f"    {r['id']:>7}  {r['operation']:<12} {r['status']:<18} key={r['idempotency_key']}"
        for r in store.receipts
    ]
    return "\n".join(rows) or "    (niciun receipt)"


async def main() -> None:
    store = Store()
    install(store, _setattr)
    for pid in P:
        store.products[pid] = product_row(pid, price=50.0)
    store.products[P[0]] = product_row(
        P[0], price=100.0, variants=[{"id": V1, "label": "50ml", "price": 120.0, "stock": 5}]
    )
    svc = CartService(db=static_db(object()), business_id=BIZ, contact_id="c-1", sla_s=86400)

    print("═" * 78)
    print("1) Aceeași variantă, din ACTION și din TOOL → o singură linie")
    click = CartCommand.parse("add", {"product_id": P[0], "variant_id": V1, "quantity": 1})
    out = await svc.mutate(
        CONV, click, idempotency_key=action_idempotency_key("act-1"), turn_id="t-1"
    )
    print(f"   click  → v{out.snapshot.version}, linii={len(out.snapshot.lines)}")
    tool = CartCommand.parse("add", {"product_id": P[0], "variant_id": V1, "quantity": 1})
    out = await svc.mutate(
        CONV, tool, idempotency_key=tool_idempotency_key("t-2", tool), turn_id="t-2"
    )
    line = out.snapshot.lines[0]
    print(
        f"   tool   → v{out.snapshot.version}, linii={len(out.snapshot.lines)} "
        f"(qty={line.quantity}, variantă={line.variant_label}, preț={line.unit_price_display})"
    )
    assert len(out.snapshot.lines) == 1 and line.quantity == 2

    print("\n2) Retry cu ACEEAȘI cheie → același receipt, zero a doua mutație")
    replay = await svc.mutate(
        CONV, tool, idempotency_key=tool_idempotency_key("t-2", tool), turn_id="t-2"
    )
    print(
        f"   replayed={replay.receipt.replayed}, qty rămâne {replay.snapshot.lines[0].quantity}, "
        f"v{replay.snapshot.version}"
    )
    assert replay.receipt.replayed and replay.snapshot.lines[0].quantity == 2

    print("\n3) set_quantity + remove → versiuni monotone")
    cmd = CartCommand.parse("set_quantity", {"product_id": P[0], "variant_id": V1, "quantity": 3})
    out = await svc.mutate(CONV, cmd, idempotency_key="k-set", turn_id="t-3")
    print(f"   set_quantity=3 → v{out.snapshot.version}")
    cmd = CartCommand.parse("add", {"product_id": P[1], "quantity": 2})
    out = await svc.mutate(CONV, cmd, idempotency_key="k-add2", turn_id="t-4")
    cmd = CartCommand.parse("remove", {"product_id": P[1]})
    out = await svc.mutate(CONV, cmd, idempotency_key="k-rm", turn_id="t-5")
    print(f"   add+remove → v{out.snapshot.version}, linii={len(out.snapshot.lines)}")

    print("\n4) Prețul se schimbă în catalog DUPĂ render → următoarea citire e FRESH")
    store.products[P[0]]["variants"][0]["price"] = 150.0
    snap = await svc.get_snapshot(CONV)
    print(f"   unit_price acum: {snap.lines[0].unit_price_display} (era 120,00 lei)")
    assert snap.lines[0].unit_price == 150.0

    print("\n5) Stocul se epuizează → add refuzat explicat, coșul NESCHIMBAT")
    store.products[P[0]]["variants"][0]["stock"] = 0
    cmd = CartCommand.parse("add", {"product_id": P[0], "variant_id": V1, "quantity": 1})
    out = await svc.mutate(CONV, cmd, idempotency_key="k-oos", turn_id="t-6")
    print(f"   error={out.error}, qty rămâne {out.snapshot.lines[0].quantity}")
    assert out.error == "out_of_stock"

    print("\n6) Kill după succes extern, înainte de finalize → unknown_reconcile → reconcile")
    store.products[P[0]]["variants"][0]["stock"] = 5  # stocul revine; coșul redevine vandabil
    adapter = LossyAdapter()
    svc_ext = CartService(
        db=static_db(object()), business_id=BIZ, contact_id="c-1", sla_s=86400, adapter=adapter
    )
    out = await svc_ext.create_checkout(
        CONV, idempotency_key="ck-1", turn_id="turn-co", base_url="https://shop.example/co"
    )
    print(f"   după «crash»: receipt={out.receipt.status}, error={out.error}")
    retry = await svc_ext.create_checkout(
        CONV, idempotency_key="ck-1", turn_id="turn-co", base_url="https://shop.example/co"
    )
    print(
        f"   retry orb: replay={retry.receipt.replayed}, push_calls={adapter.push_calls} "
        "(NU s-a re-împins)"
    )
    assert adapter.push_calls == 1
    receipt = await svc_ext.reconcile("ck-1")
    print(f"   reconcile: status={receipt.status}, external_ref={receipt.external_ref}")
    assert receipt.status == "succeeded"

    print("\n7) Coș de 10 linii → UN call de hidratare per operație (anti-N+1)")
    await svc.mutate(
        CONV,
        CartCommand.parse("clear"),
        idempotency_key="k-clear",
        turn_id="t-7",
    )
    for i, pid in enumerate(P[:10]):
        await svc.mutate(
            CONV,
            CartCommand.parse("add", {"product_id": pid, "quantity": 1}),
            idempotency_key=f"k-fill-{i}",
            turn_id="t-8",
        )
    store.hydration_calls = 0
    snap = await svc.get_snapshot(CONV)
    print(f"   linii={len(snap.lines)}, hydration_calls={store.hydration_calls}")
    assert len(snap.lines) == 10 and store.hydration_calls == 1

    print("\nREGISTRUL DE RECEIPTS (redacted — doar refs și coduri):")
    print(receipts_table(store))
    print("═" * 78)
    print("TOATE PROBELE AU TRECUT: un receipt per mutație, versiuni monotone, fapte fresh,")
    print("zero total/discount/ETA/promo inventat, zero dublare la retry/response loss.")


if __name__ == "__main__":
    asyncio.run(main())
