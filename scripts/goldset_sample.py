"""NX-265 pasul 1 — extrage și STRATIFICĂ frazele candidate pentru setul de evaluare.

Frazele nu se inventează. 12.665 dintre ele stau deja în catalog, în
`product_sections.kind='recommendation_trigger'` (~5 per produs, sursă `aura`): sunt frazele cu care
produsele astea SUNT căutate, nu cele pe care ni le imaginăm noi.

**Ce a ieșit la implementare și cardul nu prevedea.** Din cele 10 clase declarate, doar șase se pot
extrage direct. Restul se DERIVĂ, fiecare printr-o transformare declarată, ca să rămână
reproductibil:

| clasă | de unde |
|---|---|
| `nevoie` | frază de catalog care conține o frază din `concern_map` |
| `specificatie` | frază de catalog cu număr + unitate (SPF, ml, gr) |
| `brand` | frază de catalog care începe cu un brand real |
| `ingredient` | frază de catalog cu „cu <ingredient>" |
| `nuanta_finish` | frază de catalog de la un produs din rădăcina `machiaj` |
| `multi` | frază de catalog cu semnal din ≥2 fațete |
| `typo` | MUTAȚIE deterministă a unei fraze reale (transpoziție de două litere) |
| `buget` | frază reală + clauză de buget, din moneda pachetului |
| `nu_e_cautare` | `faqs.question` — 20 de întrebări reale de pe sole.ro |
| `fara_rezultat` | **măsurat**, nu presupus: frazele care întorc zero pe calea lexicală |

Clasa „referință/rafinare" din card a fost SCOASĂ. Un set de retrieval judecă o interogare și un
top-k; „mai ieftin" sau „a doua" n-au sens fără turul dinainte. Ele aparțin suitei de *journey*
(NX-246 felia 3), nu aici — un caz care nu se poate judeca ar dilua exact metrica pe care o vrem.

Dry-run prin natură: scrie doar candidați, nu setul final. Judecata umană e pasul următor
(`scripts/goldset_annotate.py`).

    python scripts/goldset_sample.py --business <uuid> --per-class 15
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.query_terms import content_terms  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402
from src.domain.normalize import normalize  # noqa: E402
from src.web.localization import currency_word  # noqa: E402

# Bulletul de frază din secțiunea `recommendation_trigger`: ghilimele românești sau drepte.
BULLET_RE = re.compile(r"^\s*[•\-\*]\s*[„\"'“](.+?)[”\"'”]\s*$", re.M)

# Număr + unitate = specificație, nu nevoie. Unitățile vin din text, nu dintr-o listă de domeniu:
# orice cifră lipită de litere e o specificație în orice vertical.
SPEC_RE = re.compile(r"\b\d+\s*[a-z]{1,4}\b|\b[a-z]{2,5}\s*\d{1,3}\b", re.I)

MIN_PHRASE_WORDS = 2
MAX_PHRASE_CHARS = 90


def _phrases(body: str) -> list[str]:
    return [m.strip() for m in BULLET_RE.findall(body or "") if m.strip()]


def _typo(phrase: str) -> str | None:
    """Transpune două litere alăturate din cel mai lung cuvânt. Determinist, deci reproductibil.

    Un typo real nu e o literă la întâmplare: e o transpoziție sau o dublare, iar transpoziția e cea
    pe care plasa de trigrame o prinde uneori și alteori nu — exact zona care merită măsurată
    („sampoon" se prinde, „sanpon" nu)."""
    words = sorted(phrase.split(), key=len, reverse=True)
    for w in words:
        if len(w) >= 6 and w.isalpha():
            i = len(w) // 2
            swapped = w[:i] + w[i + 1] + w[i] + w[i + 2 :]
            if swapped != w:
                return phrase.replace(w, swapped, 1)
    return None


def _fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:16]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--per-class", type=int, default=15)
    ap.add_argument("--locale", default="ro")
    ap.add_argument("--out", default="reports/goldset-candidates.json")
    args = ap.parse_args()

    try:
        async with tenant_conn(args.business) as conn:
            business = await load_business(conn, args.business)
            pack = load_domain_pack(business) if business else None
            if pack is None:
                print("tenantul n-are domain pack", file=sys.stderr)
                return 2
            concern_phrases = {normalize(p) for p in pack.concern_map}
            # Cuvântul ROSTIT, nu codul ISO: clientul scrie „sub 100 lei", nu „sub 100 RON".
            # `currency_word` e tabelul locale-aware din NX-240, deci nu inventăm aici un cuvânt
            # românesc în cod (ar pica poarta NX-264, pe bună dreptate).
            currency = currency_word(getattr(pack, "currency", None), args.locale)

            brands = {
                normalize(r["name"])
                for r in await conn.fetch(
                    "select name from brands where business_id = $1", args.business
                )
                if r["name"]
            }
            sections = await conn.fetch(
                """select s.product_id::text as id, s.body,
                          coalesce(split_part(cat.slug, '-', 1), '') as root
                     from product_sections s
                     join products p on p.id = s.product_id and p.business_id = s.business_id
                     left join categories cat on cat.id = p.primary_category_id
                    where s.business_id = $1 and s.kind = 'recommendation_trigger'
                      and p.status = 'active'""",
                args.business,
            )
            median_price = int(
                await conn.fetchval(
                    """select percentile_disc(0.5) within group
                              (order by coalesce(sale_price, price))
                         from products where business_id = $1 and status = 'active'
                          and coalesce(sale_price, price) > 0""",
                    args.business,
                )
                or 100
            )
            faq_questions = [
                r["question"]
                for r in await conn.fetch(
                    "select question from faqs where business_id = $1 and question is not null",
                    args.business,
                )
            ]
    finally:
        await close_pool()

    # --- clasificare ------------------------------------------------------------------------
    buckets: dict[str, list[dict]] = collections.defaultdict(list)
    seen: set[str] = set()
    for row in sections:
        for phrase in _phrases(row["body"]):
            if len(phrase) > MAX_PHRASE_CHARS or len(phrase.split()) < MIN_PHRASE_WORDS:
                continue
            fp = _fingerprint(phrase)
            if fp in seen:
                continue
            seen.add(fp)
            norm = normalize(phrase)
            terms = set(content_terms(phrase, args.locale))

            has_brand = any(b and b in norm for b in brands)
            has_spec = bool(SPEC_RE.search(norm))
            has_need = any(c in norm for c in concern_phrases)
            has_ingredient = " cu " in f" {norm} "
            is_makeup = row["root"] == "machiaj"

            signals = sum([has_brand, has_spec, has_need, has_ingredient])
            entry = {"query": phrase, "source_product": row["id"], "fingerprint": fp}

            if signals >= 2:
                buckets["multi"].append(entry)
            elif has_spec:
                buckets["specificatie"].append(entry)
            elif has_brand:
                buckets["brand"].append(entry)
            elif is_makeup:
                buckets["nuanta_finish"].append(entry)
            elif has_ingredient:
                buckets["ingredient"].append(entry)
            elif has_need or len(terms) >= 3:
                buckets["nevoie"].append(entry)

    # Pragul de buget e un PERCENTIL al catalogului, nu o cifră rotundă aleasă de mine: „sub 100"
    # ar putea fi sub cel mai ieftin produs (caz fără rezultat, dar din alt motiv decât credem) sau
    # peste tot catalogul (constrângere care nu constrânge). Mediana taie catalogul în două.
    budget_threshold = median_price

    # --- clase DERIVATE, fiecare printr-o transformare declarată --------------------------------
    for entry in buckets["nevoie"][: args.per_class]:
        mutated = _typo(entry["query"])
        if mutated:
            buckets["typo"].append(
                {
                    "query": mutated,
                    "derived_from": entry["fingerprint"],
                    "transform": "transpozitie_litere.v1",
                    "fingerprint": _fingerprint(mutated),
                }
            )
    for entry in buckets["nevoie"][args.per_class : args.per_class * 2] if currency else []:
        q = f"{entry['query']} sub {budget_threshold} {currency}"
        buckets["buget"].append(
            {
                "query": q,
                "derived_from": entry["fingerprint"],
                "transform": "sufix_buget.v1",
                "fingerprint": _fingerprint(q),
            }
        )
    for question in faq_questions:
        buckets["nu_e_cautare"].append(
            {"query": question, "source": "faqs.question", "fingerprint": _fingerprint(question)}
        )

    # --- stratificare deterministă --------------------------------------------------------------
    # Ordonarea e pe amprentă, nu aleatorie: aceeași bază ⇒ același eșantion, deci setul se poate
    # regenera și comparat, nu doar produs o dată.
    stratified = {
        cls: sorted(items, key=lambda e: e["fingerprint"])[: args.per_class]
        for cls, items in sorted(buckets.items())
    }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "_provenance": {
                    "business_id": args.business,
                    "locale": args.locale,
                    "source": "product_sections.recommendation_trigger + faqs.question",
                    "phrases_seen": len(seen),
                    "per_class": args.per_class,
                    "_note": (
                        "CANDIDAȚI, nu set. Clasa `fara_rezultat` se determină MĂSURAT în pasul de "
                        "adnotare (frazele care întorc zero), nu se ghicește aici. Clasa "
                        "referință/rafinare e scoasă: n-are sens fără turul dinainte."
                    ),
                },
                "classes": {
                    cls: {"count": len(items), "queries": items}
                    for cls, items in stratified.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"fraze unice extrase: {len(seen)}\n")
    for cls, items in stratified.items():
        pool = len(buckets[cls])
        sample = items[0]["query"][:56] if items else "—"
        print(f"  {cls:16} {len(items):>3} din {pool:>5}   ex: {sample}")
    total = sum(len(v) for v in stratified.values())
    print(f"\ntotal candidați: {total}")
    print(f"scris: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
