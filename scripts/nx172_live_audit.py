"""NX-172 felia 2 — audit LIVE pe pipeline real (retrieval + embeddings OpenAI + DB live).

Închide bucla de validare a catalogului v3 pe cele 150: verifică pe DATE REALE (nu ScriptedLLM ca
gate-ul golden din tests/test_golden.py) că:
  1. calea semantică e activă (`has_embeddings=True`);
  2. REGULA 7 — un query de makeup NU întoarce produse de PĂR pe calea reală (CU filtru de categorie
     din triaj). NB: pe semantic BRUT (fără categorie) un query ambiguu („mascara pentru volum")
     POATE scurge un șampon volumizant — filtrul de categorie e cel care impune regula 7, nu stratul
     semantic. Auditul raportează AMBELE ca dovadă;
  3. filtrul `published` (NX-171c) e coerent — zero produse active ne-published.

Rulează ca ADMIN (retrieval read pe catalog + embeddings OpenAI). Necesită OPENAI_API_KEY + DB demo
seedat (`seed_catalog_v2.py` + `backfill_content_status` + `embed_products`).

    python scripts/nx172_live_audit.py            # exit 0 = PASS, 2 = leak/regresie
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.llm import get_llm  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool, register_vector_codec  # noqa: E402
from src.db.queries.catalog import has_embeddings, search_products_semantic  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
# categorii de PĂR (regula 7: un query de makeup NU trebuie să le întoarcă).
HAIR = {
    "par",
    "ingrijirea-parului",
    "sampoane",
    "sampon-uscat",
    "masti-de-par",
    "balsamuri-de-par",
    "uleiuri-pentru-par",
    "accesorii-pentru-par",
    "aparate-ingrijire",
    "ingrijire-fara-clatire",
}
# (query natural, category_key pe care l-ar seta triajul)
MAKEUP_QUERIES = [
    ("fond de ten cu acoperire medie", "fond-de-ten"),
    ("ruj mat", "rujuri"),
    ("mascara pentru volum", "mascara"),
    ("anticearcan", "anticearcan"),
]


async def main() -> int:
    llm = get_llm()
    if llm is None:
        print("OPENAI_API_KEY lipsă — nu pot rula retrieval semantic")
        return 1
    pool = await get_pool()
    failures: list = []
    async with admin_conn(pool) as conn:
        await register_vector_codec(conn)
        rows = await conn.fetch(
            "select p.id::text id, c.slug cat from products p "
            "left join categories c on c.id = p.primary_category_id where p.business_id=$1",
            DEMO_BIZ,
        )
        cat_of = {r["id"]: r["cat"] for r in rows}

        print("has_embeddings:", await has_embeddings(conn, DEMO_BIZ))

        print("\n-- RAW semantic (fără filtru categorie — informativ) --")
        for q, _cat in MAKEUP_QUERIES:
            vec = (await llm.embed([q]))[0]
            prods = await search_products_semantic(conn, DEMO_BIZ, vec, limit=6)
            cats = [cat_of.get(p["id"]) for p in prods]
            hair = [(p["name"], c) for p, c in zip(prods, cats, strict=True) if c in HAIR]
            print(f"  [{q}] cat={cats} -> {'OK' if not hair else f'leak semantic {hair}'}")

        print("\n-- Pipeline real (CU filtru categorie din triaj) = REGULA 7 --")
        for q, cat in MAKEUP_QUERIES:
            vec = (await llm.embed([q]))[0]
            prods = await search_products_semantic(conn, DEMO_BIZ, vec, category=cat, limit=6)
            cats = [cat_of.get(p["id"]) for p in prods]
            hair = [(p["name"], c) for p, c in zip(prods, cats, strict=True) if c in HAIR]
            print(
                f"  [{q}|cat={cat}] -> {len(prods)} produse, cat={cats} -> "
                f"{'OK' if not hair else f'FAIL {hair}'}"
            )
            if hair:
                failures.append((q, hair))

        draft_active = await conn.fetchval(
            "select count(*) from products where business_id=$1 and status='active' "
            "and content_status is distinct from 'published'",
            DEMO_BIZ,
        )
        print("\nproduse active ne-published (regula NX-171c, ar trebui 0):", draft_active)
        if draft_active != 0:
            failures.append(("published-filter", draft_active))

    await close_pool()
    print("\nVERDICT:", "PASS" if not failures else f"FAIL {failures}")
    return 0 if not failures else 2


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    raise SystemExit(asyncio.run(main()))
