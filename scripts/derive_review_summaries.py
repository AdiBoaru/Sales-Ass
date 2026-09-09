"""Derivă `product_review_summaries` din recenziile REALE ale tenantului. Dry-run implicit.

De ce există: `reviews` are 183.003 de recenzii pe 2.743 din 2.758 de produse SOLE, iar
`product_review_summaries` are 0 rânduri — deci `top_pros`/`review_pro` ies NULL pe orice card și
„ce spun clienții" e o întrebare la care botul răspunde onest că n-are date. Singurul producător
existent (`scripts/archive/summarize_reviews_demo.py`) nu citea recenziile: inventa cu model și
rescria `products.rating`. Pe date reale ar fi fost falsificare, nu rezumat.

Ce face în loc: FRECVENȚĂ verificabilă, zero model. Temele vin din vocabularul tenantului
(`domain_pack.review_themes`), se potrivesc cu matcherul NX-268 pe fiecare recenzie, se numără
recenzii DISTINCTE per temă, iar rândul e compus din șablon localizat. Regula, pragurile și porțile
sunt în `src/catalog/review_themes.py` (pur, testabil fără DB) — aici e doar I/O și raport.

Ce NU face, pe măsurătoare (vezi docstringul modulului): nu deduce „minusuri" din rating (4★ e
identic cu 5★ ca text), nu numără reacții adverse (imposibil determinist cu precizie utilă), nu
pune cifre în proză (`grounding_guard` le-ar confrunta cu un `review_count` care poate diverge).

Jobul e AUTORITATEA tabelei pentru tenant: cu `--apply` rescrie rândurile produselor eligibile și
șterge rândurile produselor care nu mai sunt (recenzii sub prag, nicio temă). Idempotent: a doua
rulare pe aceleași date nu schimbă niciun rând și nu avansează `built_at`.

    BIZ=99fe1292-f9ed-469e-8183-f994ea5b59c0  PACK=db/seed/domain_pack_sole_ro.json
    python scripts/derive_review_summaries.py --business $BIZ                  # dry-run + raport
    python scripts/derive_review_summaries.py --business $BIZ --pack $PACK     # teme din fișier
    python scripts/derive_review_summaries.py --business $BIZ --propose-themes # n-grame din corpus
    python scripts/derive_review_summaries.py --business $BIZ --sample 8       # rânduri cu citate
    python scripts/derive_review_summaries.py --business $BIZ --audit tingling # precizie, de mână
    python scripts/derive_review_summaries.py --business $BIZ --apply          # scrie (confirmă)
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import random
import sys
from collections.abc import Mapping, Sequence
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.derivation import match_keys, phrase_spans, tokens  # noqa: E402
from src.catalog.query_terms import stopwords  # noqa: E402
from src.catalog.review_themes import (  # noqa: E402
    MIN_REVIEWS_PER_PRODUCT,
    Composition,
    ReviewDigest,
    ReviewRow,
    ThemeVocabulary,
    build_theme_matchers,
    compose,
    digest,
    evidence,
    load_vocabulary,
    rule_id,
)
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.domain.normalize import normalize  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# O temă prezentă pe atâta din produse nu discriminează nimic (testul de lift din NX-264, aplicat
# invers): e vocea magazinului sau a formularului, nu a produsului. Raportul o marchează; decizia de
# a o scoate din pachet rămâne a omului.
UNIVERSAL_SHARE = 0.90

_REVIEWS_SQL = """
select r.id::text as id, r.product_id::text as product_id, r.rating, r.body
  from reviews r
 where r.business_id = $1 and r.body is not null
 order by r.product_id, r.id
"""

_PRODUCTS_SQL = "select id::text as id, name from products where business_id = $1"

_COLUMNS_SQL = """
select column_name from information_schema.columns
 where table_schema = 'public' and table_name = 'product_review_summaries'
"""

_EXISTING_SQL = """
select product_id::text as product_id, summary, top_pros, top_cons, review_count_at_build,
       rule_id, evidence::text as evidence, locale
  from product_review_summaries where business_id = $1
"""

# `where ... is distinct from` pe conflict: rândul se rescrie DOAR dacă s-a schimbat ceva, deci
# `built_at` avansează doar la conținut nou. Fără asta, a doua rulare ar arăta 2.700 de „scrieri"
# și nimeni n-ar mai putea deosebi o regenerare reală de un no-op.
_UPSERT_SQL = """
insert into product_review_summaries
       (product_id, business_id, summary, sentiment, top_pros, top_cons,
        review_count_at_build, built_at, rule_id, evidence, locale)
values ($1, $2, $3, $4, $5, $6, $7, now(), $8, $9::jsonb, $10)
on conflict (product_id) do update
   set summary = excluded.summary,
       sentiment = excluded.sentiment,
       top_pros = excluded.top_pros,
       top_cons = excluded.top_cons,
       review_count_at_build = excluded.review_count_at_build,
       built_at = now(),
       rule_id = excluded.rule_id,
       evidence = excluded.evidence,
       locale = excluded.locale
 where product_review_summaries.business_id = excluded.business_id
   and (product_review_summaries.summary, product_review_summaries.top_pros,
        product_review_summaries.top_cons, product_review_summaries.review_count_at_build,
        product_review_summaries.rule_id, product_review_summaries.evidence,
        product_review_summaries.locale)
       is distinct from
       (excluded.summary, excluded.top_pros, excluded.top_cons, excluded.review_count_at_build,
        excluded.rule_id, excluded.evidence, excluded.locale)
"""

_DELETE_STALE_SQL = """
delete from product_review_summaries
 where business_id = $1 and not (product_id = any($2::uuid[]))
"""


def _load_pack_file(path: str) -> Mapping[str, Any] | None:
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    raw = data.get("review_themes") if isinstance(data, dict) else None
    return raw if isinstance(raw, Mapping) else None


def _snippet(body: str, phrase_tokens: Sequence[str], width: int = 70) -> str:
    """Fraza potrivită, cu context de o parte și de alta, ca auditorul să judece în propoziție."""
    words = body.split()
    norm = [normalize(w) for w in words]
    flat = tokens(" ".join(norm))
    span = next(iter(phrase_spans(flat, phrase_tokens)), None)
    if span is None:
        return body[: 2 * width]
    # `flat` poate avea alt număr de elemente decât `words` (punctuația); aproximăm poziția.
    ratio = len(words) / max(1, len(flat))
    start = max(0, int(span[0] * ratio) - 8)
    end = min(len(words), int(span[1] * ratio) + 9)
    text = " ".join(words[start:end])
    return ("…" if start else "") + text + ("…" if end < len(words) else "")


def _propose_themes(by_product: Mapping[str, list[ReviewRow]], locale: str) -> None:
    """N-gramele frecvente ale corpusului, cu răspândirea pe produse. Nu propune teme — arată
    de unde se pot lua, și marchează ce e universal (deci nu e despre produs)."""
    stop = stopwords(locale)
    n_products = len(by_product)
    for label, keep in (
        ("recenzii cu rating ≥ 4", lambda r: (r.rating or 0) >= 4),
        ("recenzii cu rating ≤ 4", lambda r: r.rating is not None and r.rating <= 4),
    ):
        for n in (2, 3):
            rev: collections.Counter[str] = collections.Counter()
            prods: dict[str, set[str]] = collections.defaultdict(set)
            for pid, rows in by_product.items():
                for r in rows:
                    if not keep(r):
                        continue
                    words = tokens(normalize(r.body))
                    seen: set[str] = set()
                    for i in range(len(words) - n + 1):
                        gram = words[i : i + n]
                        if gram[0] in stop or gram[-1] in stop:
                            continue
                        g = " ".join(gram)
                        if g in seen:
                            continue
                        seen.add(g)
                        rev[g] += 1
                        prods[g].add(pid)
            print(f"\n--- {label}: top {n}-grame (recenzii · produse · % produse) ---")
            for g, c in rev.most_common(60):
                share = len(prods[g]) / max(1, n_products)
                flag = "  ← universal (nu e despre produs)" if share >= UNIVERSAL_SHARE else ""
                print(f"{c:7} {len(prods[g]):6} {share:6.1%}  {g}{flag}")


def _audit(
    by_product: Mapping[str, list[ReviewRow]],
    vocab: ThemeVocabulary,
    key: str,
    n: int,
) -> int:
    """Eșantion aleator (seed fix) de recenzii care susțin o temă, cu fraza în context. Auditul
    e de MÂNĂ: aici doar se pune textul în fața omului, în forma în care poate decide."""
    theme = vocab.get(key)
    if theme is None:
        print(f"tema `{key}` nu e în vocabular", file=sys.stderr)
        return 2
    matchers = build_theme_matchers(vocab)
    matcher = matchers[key]
    hits: list[tuple[str, ReviewRow, tuple[str, ...]]] = []
    for pid, rows in by_product.items():
        for r in rows:
            words = tokens(normalize(r.body))
            # Potrivirea reală (cu excluderi), nu doar prezența frazei: auditul judecă exact
            # ce ar număra jobul, altfel ar valida un mecanism pe care nu-l folosește.
            if key not in match_keys(words, {key: matcher}):
                continue
            phrase = next(
                (p for p in matcher.phrases if phrase_spans(words, p)), matcher.phrases[0]
            )
            hits.append((pid, r, phrase))
    rng = random.Random(279)
    sample = rng.sample(hits, min(n, len(hits)))
    print(
        f"tema `{key}` ({theme.kind}, {theme.promise}): {len(hits)} recenzii potrivite; "
        f"eșantion {len(sample)} (seed fix). Judecă: fraza spune ce spune eticheta?"
    )
    print(f"etichetă: {dict(theme.labels)}")
    for i, (pid, r, phrase) in enumerate(sample, 1):
        print(f"\n{i:3}. [{r.rating}] prod {pid[:8]} rev {r.id[:8]}  «{' '.join(phrase)}»")
        print(f"     {_snippet(r.body, phrase)}")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--pack", default=None, help="citește `review_themes` din fișier, nu din DB")
    ap.add_argument("--locale", default=None, help="implicit: `default_locale` al tenantului")
    ap.add_argument("--sample", type=int, default=0, help="câte rânduri să afișeze cu citate")
    ap.add_argument("--audit", default=None, help="cheia temei de auditat de mână")
    ap.add_argument("--audit-n", type=int, default=40)
    ap.add_argument("--propose-themes", action="store_true", help="n-grame frecvente din corpus")
    ap.add_argument("--out", default=None, help="unde scrie raportul JSON")
    ap.add_argument("--apply", action="store_true", help="chiar scrie (fără el: doar raportează)")
    args = ap.parse_args()

    # `admin_conn`, nu `tenant_conn`: job OFFLINE care scrie în catalog, iar `bot_runtime` are
    # SELECT-only pe `product_review_summaries` prin proiectare (NU scriere în catalog din worker).
    # RLS e bypass-at, deci izolarea cade pe `where business_id = $1`, prezent în FIECARE
    # statement.
    try:
        pool = await get_pool()
        async with admin_conn(pool) as conn:
            business = await load_business(conn, args.business)
            if business is None:
                print("business inexistent", file=sys.stderr)
                return 2
            locale = args.locale or business.default_locale or "ro"
            if args.pack:
                raw_vocab = _load_pack_file(args.pack)
                vocab_source = args.pack
            else:
                raw_vocab = ((business.settings or {}).get("domain_pack") or {}).get(
                    "review_themes"
                )
                vocab_source = "businesses.settings.domain_pack.review_themes"
            vocab = load_vocabulary(raw_vocab)
            products = {r["id"]: r["name"] for r in await conn.fetch(_PRODUCTS_SQL, args.business)}
            rows = await conn.fetch(_REVIEWS_SQL, args.business)
            columns = {r["column_name"] for r in await conn.fetch(_COLUMNS_SQL)}
            existing = (
                {r["product_id"]: r for r in await conn.fetch(_EXISTING_SQL, args.business)}
                if {"rule_id", "evidence", "locale"} <= columns
                else {}
            )

        by_product: dict[str, list[ReviewRow]] = collections.defaultdict(list)
        orphans = 0
        for r in rows:
            if r["product_id"] not in products:
                orphans += 1
                continue
            by_product[r["product_id"]].append(
                ReviewRow(id=r["id"], rating=r["rating"], body=r["body"])
            )

        print(f"tenant {business.slug} · locale {locale} · vocabular din {vocab_source}")
        print(
            f"vocabular `{vocab.version or '—'}`: {len(vocab.themes)} teme "
            f"({sum(t.kind == 'praise' for t in vocab.themes)} laudă, "
            f"{sum(t.kind == 'nuance' for t in vocab.themes)} nuanță), "
            f"{len(vocab.rejected)} respinse"
        )
        for key, reason in vocab.rejected:
            print(f"   respins `{key}`: {reason}")
        print(
            f"recenzii {len(rows):,} pe {len(by_product):,} produse "
            f"(din {len(products):,}); orfane {orphans}"
        )

        if args.propose_themes:
            _propose_themes(by_product, locale)
            return 0
        if args.audit:
            return _audit(by_product, vocab, args.audit, args.audit_n)
        if not vocab.themes:
            print("vocabular gol: nimic de derivat (adaugă `review_themes` în pachet)")
            return 2

        matchers = build_theme_matchers(vocab)
        digests: dict[str, ReviewDigest] = {}
        comps: dict[str, Composition] = {}
        skipped = collections.Counter()
        withheld = collections.Counter()
        theme_products: collections.Counter[str] = collections.Counter()
        theme_reviews: collections.Counter[str] = collections.Counter()
        pro_dist: collections.Counter[str] = collections.Counter()
        con_dist: collections.Counter[str] = collections.Counter()
        # Două treceri: întâi numărătoarea (scumpă) pe tot catalogul, apoi compunerea. Rata de bază
        # a fiecărei teme (pe câte produse eligibile apare) e cunoscută abia după prima trecere și
        # decide ORDINEA pro-urilor (specificitate, vezi `review_themes.specificity`).
        for pid in sorted(by_product):
            d = digest(pid, by_product[pid], vocab, matchers)
            if d is None:
                skipped["under_min_reviews"] += 1
                continue
            digests[pid] = d
            for t in d.themes:
                theme_products[t.key] += 1
                theme_reviews[t.key] += t.reviews
        eligible = len(digests)
        base_rates = {key: n / max(1, eligible) for key, n in theme_products.items()}
        for pid, d in digests.items():
            comp = compose(d, vocab, locale, base_rates)
            if comp is None:
                skipped["no_theme_selected"] += 1
                continue
            comps[pid] = comp
            for key, reason in comp.withheld:
                withheld[f"{reason}"] += 1
            pro_dist.update(comp.pro_keys)
            con_dist.update(comp.con_keys)

        print(
            f"\nprodusele cu ≥{MIN_REVIEWS_PER_PRODUCT} recenzii: {eligible:,} · "
            f"rânduri compuse: {len(comps):,} · sărite: {dict(skipped)}"
        )
        print("\nteme (produse cu ≥1 recenzie · % din eligibile · recenzii · ca pro/contra):")
        for theme in vocab.themes:
            n_prod = theme_products[theme.key]
            share = n_prod / max(1, eligible)
            used = pro_dist[theme.key] if theme.kind == "praise" else con_dist[theme.key]
            flags = []
            if share >= UNIVERSAL_SHARE:
                flags.append("UNIVERSAL")
            if not theme.renderable:
                flags.append("neratificat, reținut")
            print(
                f"   {theme.key:16} {theme.kind:7} {n_prod:5} {share:6.1%} "
                f"{theme_reviews[theme.key]:7} {used:5}  {' '.join(flags)}"
            )
        print(f"\nreținute (motiv → produse): {dict(withheld)}")
        with_cons = sum(1 for c in comps.values() if c.top_cons)
        print(f"rânduri cu contra: {with_cons:,} din {len(comps):,}")

        # Ce s-ar schimba față de ce e în tabelă, ca `--apply` să nu fie o surpriză.
        rid = rule_id(vocab)
        changed = unchanged = new = 0
        for pid, comp in comps.items():
            old = existing.get(pid)
            if old is None:
                new += 1
            elif (
                old["summary"] == comp.summary
                and list(old["top_pros"] or []) == list(comp.top_pros)
                and list(old["top_cons"] or []) == list(comp.top_cons)
                and old["rule_id"] == rid
                and old["locale"] == locale
                and old["review_count_at_build"] == digests[pid].reviews_considered
            ):
                unchanged += 1
            else:
                changed += 1
        stale = sorted(set(existing) - set(comps))
        print(
            f"față de tabelă: noi {new:,} · schimbate {changed:,} · neschimbate {unchanged:,} · "
            f"de șters (nu mai sunt eligibile) {len(stale):,}"
        )

        if args.sample:
            print(f"\n--- {args.sample} rânduri, cu citate ---")
            by_themes = sorted(comps, key=lambda p: (-len(digests[p].themes), p))
            for pid in by_themes[: args.sample]:
                d, comp = digests[pid], comps[pid]
                print(f"\n[{pid[:8]}] {products[pid][:90]}")
                print(f"   {d.reviews_considered} recenzii · sentiment {d.sentiment}")
                print(f"   summary: {comp.summary}")
                print(f"   pros: {list(comp.top_pros)}  cons: {list(comp.top_cons)}")
                bodies = {r.id: r.body for r in by_product[pid]}
                for t in d.themes:
                    if t.key in comp.pro_keys or t.key in comp.con_keys:
                        theme = vocab.get(t.key)
                        snippet = bodies[t.sample[0]][:140].replace("\n", " ")
                        print(
                            f"   · {t.key} {t.reviews}/{d.reviews_considered} "
                            f"({t.share:.0%}) ex: {snippet!r}"
                        )
                if comp.withheld:
                    print(f"   reținute: {list(comp.withheld)}")

        out = pathlib.Path(
            args.out or ROOT / "reports" / f"review-summaries-{args.business[:8]}.json"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "business_id": args.business,
                    "locale": locale,
                    "rule_id": rid,
                    "vocabulary": {
                        "source": vocab_source,
                        "version": vocab.version,
                        "themes": len(vocab.themes),
                        "rejected": [list(r) for r in vocab.rejected],
                    },
                    "reviews": len(rows),
                    "products_with_reviews": len(by_product),
                    "eligible_products": eligible,
                    "rows_composed": len(comps),
                    "rows_with_cons": with_cons,
                    "skipped": dict(skipped),
                    "withheld": dict(withheld),
                    "themes": {
                        t.key: {
                            "kind": t.kind,
                            "promise": t.promise,
                            "ratified": t.ratified,
                            "products": theme_products[t.key],
                            "product_share": round(theme_products[t.key] / max(1, eligible), 4),
                            "reviews": theme_reviews[t.key],
                            "used": pro_dist[t.key] + con_dist[t.key],
                            "universal": theme_products[t.key] / max(1, eligible)
                            >= UNIVERSAL_SHARE,
                        }
                        for t in vocab.themes
                    },
                    "diff": {
                        "new": new,
                        "changed": changed,
                        "unchanged": unchanged,
                        "stale": len(stale),
                    },
                    "applied": bool(args.apply),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nraport: {out}")

        if not args.apply:
            print("\n(dry-run — nu s-a scris nimic; adaugă --apply)")
            return 0
        if not {"rule_id", "evidence", "locale"} <= columns:
            print(
                "migrarea 050 nu e aplicată (lipsesc rule_id/evidence/locale): refuz să scriu",
                file=sys.stderr,
            )
            return 3

        pool = await get_pool()
        async with admin_conn(pool) as conn:
            async with conn.transaction():
                written = 0
                for pid in sorted(comps):
                    d, comp = digests[pid], comps[pid]
                    status = await conn.execute(
                        _UPSERT_SQL,
                        pid,
                        args.business,
                        comp.summary,
                        d.sentiment,
                        list(comp.top_pros),
                        list(comp.top_cons),
                        d.reviews_considered,
                        rid,
                        json.dumps(
                            evidence(d, comp, vocab, base_rates), ensure_ascii=False, sort_keys=True
                        ),
                        locale,
                    )
                    written += int(status.split()[-1])
                deleted_status = await conn.execute(_DELETE_STALE_SQL, args.business, list(comps))
                deleted = int(deleted_status.split()[-1])
        print(f"scris: {written:,} rânduri (noi sau schimbate) · șterse: {deleted:,}")
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
