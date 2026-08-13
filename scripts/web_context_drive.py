"""NX-234 — manual drive: ACELAȘI `product_id` pe doi tenanți, patru tururi, un formatter safe.

Ce dovedește, pe Postgres REAL (fără OpenAI, fără scrieri în tenanții reali — creează două
businessuri throwaway și le șterge la final):

  1. PDP valid            → context `resolved`, fapte din CATALOG (nume/preț/URL), ancoră canonică;
  2. variantă din ALT produs → context `invalid`, ZERO preț și ZERO stoc folosite;
  3. produs nepublicat    → fără evidence, aceeași semantică externă ca „inexistent";
  4. ID de la ALT tenant  → fără evidence, indistinct de inexistent (zero existence leak);
  5. preț schimbat ÎNTRE accept și execuție → rehidratare la execuție + freshness marcat.

Și, la final, întrebarea din card: „ce părere ai despre acesta?" se ancorează pe produsul paginii
FĂRĂ ca browserul să fi trimis numele sau prețul — se vede în `resolve` (sursa `page`) și în
ID-urile pe care le-ar cere tool-urile.

    python scripts/web_context_drive.py
    python scripts/web_context_drive.py --keep     # nu curăța tenanții (debug)

Downstream-ul rulat aici e cel DETERMINIST (ancoră + intenții pre-loop). Răspunsul de model nu e
inclus deliberat: ar consuma credite fără să adauge nimic la ce trebuie dovedit — că faptele intră
din catalog și că un context invalid nu produce niciunul.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Consola Windows e cp1252 by default: fara asta, un „ș" din raport
# omoara rularea cu UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from src.agent.reference_resolver import (  # noqa: E402
    PageAnchor,
    resolve_product_reference,
)
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402
from src.db.provider import static_db  # noqa: E402
from src.models import BusinessConfig, ConversationState  # noqa: E402
from src.privacy import apply_boundary  # noqa: E402
from src.web.context import from_payload, normalize_context, to_payload  # noqa: E402
from src.web.contracts_v2 import PageContextClaim  # noqa: E402
from src.worker.turn_snapshot import (  # noqa: E402
    build_turn_snapshot,
    snapshot_events,
    snapshot_safety_violations,
)

SHARED_KEY = "SHOP-PRODUCT-4471"  # aceeași cheie de platformă la AMBII tenanți


async def _seed(conn, *, label: str, price: float) -> dict:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1,$2,$3,'beauty_salon','active','ro')",
        bid,
        f"nx234drive-{uuid4().hex[:8]}",
        f"NX-234 drive {label}",
    )
    cat = await conn.fetchval(
        "insert into categories (business_id, slug, name, path) "
        "values ($1,'seruri','Seruri','ingrijire/seruri') returning id",
        bid,
    )
    pid = await conn.fetchval(
        "insert into products (business_id, primary_category_id, external_id, slug, name, price,"
        " currency, availability, stock_total, rating, review_count, status, content_status,"
        " product_url) values ($1,$2,$3,$4,$5,$6,'RON','in_stock',7,0,0,'active','published',$7)"
        " returning id",
        bid,
        cat,
        SHARED_KEY,
        f"ser-{uuid4().hex[:6]}",
        f"Ser Niacinamidă ({label})",
        price,
        f"https://{label.lower()}.example.com/p/ser",
    )
    other = await conn.fetchval(
        "insert into products (business_id, primary_category_id, slug, name, price, status)"
        " values ($1,$2,$3,'Alt produs',30,'active') returning id",
        bid,
        cat,
        f"alt-{uuid4().hex[:6]}",
    )
    foreign_variant = await conn.fetchval(
        "insert into product_variants (business_id, product_id, label, sku, price, stock)"
        " values ($1,$2,'50 ml',$3,30,2) returning id",
        bid,
        other,
        f"SKU-{uuid4().hex[:8]}",
    )
    draft = await conn.fetchval(
        "insert into products (business_id, primary_category_id, slug, name, price, status)"
        " values ($1,$2,$3,'Produs nepublicat',10,'draft') returning id",
        bid,
        cat,
        f"draft-{uuid4().hex[:6]}",
    )
    return {
        "business_id": bid,
        "product_id": str(pid),
        "foreign_variant_id": str(foreign_variant),
        "draft_id": str(draft),
    }


async def _cleanup(conn, bid: str) -> None:
    await conn.execute("delete from product_variants where business_id=$1", bid)
    await conn.execute("delete from products where business_id=$1", bid)
    await conn.execute("delete from categories where business_id=$1", bid)
    await conn.execute("delete from businesses where id=$1", bid)


async def _snapshot(conn, business_id: str, claim: PageContextClaim, *, text: str):
    """Drumul REAL: normalizare → persistare (payload) → recitire → rehidratare → snapshot.

    Trecerea prin `to_payload`/`from_payload` nu e decor: exact așa ajunge contextul la executorul
    async (persistat la accept, recitit la execuție), deci drive-ul trebuie să meargă pe el."""
    normalized = from_payload(to_payload(normalize_context(claim)))
    raw, safe = apply_boundary(text)
    business = BusinessConfig(id=business_id, slug="drive", name="Drive", default_locale="ro")
    return await build_turn_snapshot(
        static_db(conn),
        turn_id=str(uuid4()),
        business=business,
        contact_id=str(uuid4()),
        conversation_id=str(uuid4()),
        conversation_revision=1,
        state=ConversationState(),
        raw_inbound=raw,
        safe_inbound=safe,
        context=normalized,
        channel_kind="webchat",
    )


def _report(title: str, snapshot, *, question: str) -> dict:
    safe = snapshot.to_safe_dict()
    anchor = (
        PageAnchor(
            snapshot.surface.product.product_id,
            snapshot.surface.product.name,
            snapshot.surface.product.price,
        )
        if snapshot.surface.product
        else None
    )
    resolution = resolve_product_reference(question, [], page=anchor)
    violations = snapshot_safety_violations(snapshot)
    print(f"\n=== {title}")
    print(json.dumps(safe["surface"], indent=2, ensure_ascii=False, sort_keys=True))
    print(f"  referinta '{question}': source={resolution.source} outcome={resolution.outcome}")
    print(f"  ancoră: {resolution.product_id or '(niciuna)'}")
    print(f"  evenimente: {[t for t, _ in snapshot_events(snapshot).items]}")
    print(f"  violări de siguranță în snapshot: {violations or 'NICIUNA'}")
    return {
        "title": title,
        "status": snapshot.surface.status,
        "anchor": resolution.product_id,
        "source": resolution.source,
        "has_evidence": snapshot.surface.has_evidence,
        "violations": violations,
    }


async def drive(*, keep: bool) -> int:
    question = "ce părere ai despre acesta?"
    pool = await get_pool()
    results: list[dict] = []
    async with admin_conn(pool) as conn:
        a = await _seed(conn, label="A", price=89.0)
        b = await _seed(conn, label="B", price=999.0)
    try:
        async with admin_conn(pool) as conn:
            bid = a["business_id"]
            results.append(
                _report(
                    "1. PDP valid (cheia platformei, aceeași la ambii tenanți)",
                    await _snapshot(
                        conn,
                        bid,
                        PageContextClaim(surface="product", product_id=SHARED_KEY),
                        text=question,
                    ),
                    question=question,
                )
            )
            results.append(
                _report(
                    "2. Variantă din ALT produs (tamper)",
                    await _snapshot(
                        conn,
                        bid,
                        PageContextClaim(
                            surface="product",
                            product_id=a["product_id"],
                            variant_id=a["foreign_variant_id"],
                        ),
                        text=question,
                    ),
                    question=question,
                )
            )
            results.append(
                _report(
                    "3. Produs nepublicat",
                    await _snapshot(
                        conn,
                        bid,
                        PageContextClaim(surface="product", product_id=a["draft_id"]),
                        text=question,
                    ),
                    question=question,
                )
            )
            results.append(
                _report(
                    "4. ID valid, dar al ALTUI tenant",
                    await _snapshot(
                        conn,
                        bid,
                        PageContextClaim(surface="product", product_id=b["product_id"]),
                        text=question,
                    ),
                    question=question,
                )
            )
            # 5. Prețul se schimbă ÎNTRE accept și execuție: contextul persistat e ID-only, deci
            # execuția vede prețul NOU (nu unul înghețat în request).
            before = await _snapshot(
                conn, bid, PageContextClaim(surface="product", product_id=SHARED_KEY), text=question
            )
            await conn.execute(
                "update products set price = 59 where business_id=$1 and external_id=$2",
                bid,
                SHARED_KEY,
            )
            after = await _snapshot(
                conn, bid, PageContextClaim(surface="product", product_id=SHARED_KEY), text=question
            )
            print("\n=== 5. Preț schimbat între accept și execuție")
            print(f"  la accept:  {before.surface.product.price}")
            print(f"  la execuție:{after.surface.product.price}  (rehidratat, nu înghețat)")
            results.append(
                {
                    "title": "5. Preț schimbat între accept și execuție",
                    "price_at_accept": before.surface.product.price,
                    "price_at_execution": after.surface.product.price,
                }
            )
    finally:
        if not keep:
            async with admin_conn(pool) as conn:
                await _cleanup(conn, a["business_id"])
                await _cleanup(conn, b["business_id"])
        await close_pool()

    ok = (
        results[0]["status"] in {"resolved", "stale"}
        and results[0]["has_evidence"]
        and results[0]["source"] == "page"
        and results[1]["status"] == "invalid"
        and not results[1]["has_evidence"]
        and not results[2]["has_evidence"]
        and not results[3]["has_evidence"]
        and all(not r.get("violations") for r in results[:4])
        and results[4]["price_at_accept"] != results[4]["price_at_execution"]
    )
    print("\n" + ("PASS — doar PDP-ul valid are fapte." if ok else "FAIL — vezi mai sus."))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Manual drive NX-234 (Postgres real, fără OpenAI)")
    ap.add_argument("--keep", action="store_true", help="nu șterge tenanții throwaway")
    args = ap.parse_args()
    return asyncio.run(drive(keep=args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
