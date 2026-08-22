"""Calibrează rezoluția semantică pe termenii REALI ceruți de clienți.

De ce e nevoie de rulare separată: centroizii se construiesc din embeddinguri deja stocate (gratis),
dar ca să afli dacă „ten uscat" chiar aterizează lângă `dry` trebuie embedat termenul. Costul e
neglijabil — termenii sunt scurți și puțini (~2-4 tokeni fiecare, o singură cerere batch) — dar e
un apel la furnizor, deci rulează explicit, nu pe ascuns dintr-un test.

Termenii NU sunt inventați: se citesc din `analytics_events`, din ce a cerut efectiv modelul în
conversații reale. Un banc de test scris de mână ar măsura cât de bine ghicesc eu, nu cât de bine
înțelege sistemul clienții.

Ce raportează, pentru fiecare termen: cheia câștigătoare, similaritatea, marginea față de a doua, și
verdictul la pragurile curente. La final, distribuția — de acolo se aleg pragurile, dintr-un
histogram real, nu dintr-o intuiție.

    PYTHONPATH=. python scripts/verify_semantic_resolution.py --business <uuid>
    PYTHONPATH=. python scripts/verify_semantic_resolution.py --business <uuid> --limit 40
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from src.agent.llm import OpenAIClient
from src.catalog.semantic_vocabulary import (
    MIN_MARGIN,
    MIN_SIMILARITY,
    build_centroids,
    resolve_semantic,
)
from src.catalog.vocabulary import CATEGORY_DIMENSION, ResolutionStatus, load_vocabulary
from src.db.connection import admin_conn, close_pool, get_pool

# Termenii pe care modelul i-a trimis efectiv ca nevoi, cu frecvența lor. `properties->'args'` e
# scris de tool runner; citim doar vocabular, niciodată textul clientului (P12).
TERMS_SQL = """
select lower(t.term) as term, count(*) as n
  from analytics_events e,
       lateral jsonb_array_elements_text(
           coalesce(e.properties->'args'->'concerns', '[]'::jsonb)) t(term)
 where e.business_id = $1
   and e.event_type = 'tool_call'
   and e.properties->>'name' = 'search_products'
 group by 1
 order by n desc
 limit $2
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", required=True)
    ap.add_argument("--limit", type=int, default=30, help="câți termeni distincți (după frecvență)")
    args = ap.parse_args()

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            vocab = await load_vocabulary(conn, args.business)
            centroids = await build_centroids(conn, args.business, vocab)
            rows = await conn.fetch(TERMS_SQL, args.business, args.limit)

        terms = [r["term"] for r in rows]
        freq = {r["term"]: int(r["n"]) for r in rows}
        if not terms:
            print("niciun termen în analytics — nimic de calibrat")
            return 1

        print(f"vocabular: {len(vocab.dimensions)} dimensiuni | centroizi: {len(centroids.items)}")
        print(f"termeni reali de calibrat: {len(terms)}")
        print(f"praguri curente: similaritate ≥ {MIN_SIMILARITY}, margine ≥ {MIN_MARGIN}\n")

        client = OpenAIClient()
        vectors = await client.embed(terms)  # UN singur batch

        facets = tuple(n for n in vocab.dimensions if n != CATEGORY_DIMENSION)
        verdicts: Counter[str] = Counter()

        print(f"{'termen':<26} {'n':>4}  {'→ rezoluție':<34} {'verdict':<11}")
        print("-" * 88)
        for term, vec in zip(terms, vectors, strict=False):
            r = resolve_semantic(centroids, term, vec, vocab, dimensions=facets)
            # similaritatea brută, ca să vedem distribuția chiar și sub prag
            raw = resolve_semantic(
                centroids, term, vec, vocab, dimensions=facets, min_similarity=-1.0, min_margin=0.0
            )
            best = f"{raw.dimension}={raw.key}" if raw.key else "—"
            verdicts[r.status.value] += 1
            flag = {
                ResolutionStatus.KNOWN: "KNOWN",
                ResolutionStatus.AMBIGUOUS: "AMBIGUU",
                ResolutionStatus.UNKNOWN: f"—({r.reason})",
            }[r.status]
            print(f"{term[:26]:<26} {freq[term]:>4}  {best:<34} {flag:<11}")

        print(f"\nverdicte: {dict(verdicts)}")
        print(
            "\nCum se citește: un termen care iese KNOWN pe o cheie EVIDENT greșită înseamnă prag "
            "prea mic;\nunul care iese UNKNOWN deși cheia corectă e vizibil prima înseamnă prag "
            "prea mare.\nPragurile se strâng abia după ce lista de mai sus arată rezonabil — "
            "un filtru greșit\ngolește rezultatul, iar asta e exact clasa de defect pe care o "
            "închidem."
        )
        print(f"\n(termeni embedați: {len(terms)} — cost sub $0.0001)")
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
