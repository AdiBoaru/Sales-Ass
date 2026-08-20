"""Derivă overlay-ul de limbă (frază RO → cheie de catalog) DIN CATALOG, prin co-ocurență.

De ce există: cheile pe care le filtrează catalogul sunt tehnice și adesea englezești (`dry`,
`oily`), în timp ce clientul scrie „ten uscat". Puntea dintre ele a fost până acum o hartă scrisă
de mână — și exact ea a driftat: după re-seedul catalogului din iulie, `concern_map` trimitea spre
valori românești care nu mai existau nicăieri, deci FIECARE filtru pe nevoi întorcea zero, tăcut,
timp de cinci săptămâni.

Soluția nu e „scriem harta mai atent", ci **s-o derivăm din date**. Catalogul conține deja text
românesc (`best_for`, `key_benefit`, `ai_summary`) scris lângă cheile tehnice ale acelorași
produse. Dacă expresia „ten uscat" apare la produse care poartă `dry`, atunci „ten uscat" ÎNSEAMNĂ
`dry` — nu pentru că știm noi românește, ci pentru că datele o spun.

Cum se decide o mapare (toate pragurile sunt relative, nu ghicite pe un catalog anume):

  * **suport** — expresia apare la cel puțin `MIN_SUPPORT` produse (o coincidență nu e vocabular);
  * **puritate** — `P(cheie | expresie)`: cât de constant apar împreună;
  * **lift** — `P(cheie | expresie) / P(cheie)`: cât de SPECIFICĂ e legătura. Fără lift, o cheie
    foarte frecventă ar părea „explicația" oricărei expresii, doar pentru că e peste tot;
  * **margine** — câștigătoarea trebuie să bată următoarea clasată; altfel expresia e ambiguă și
    nu producem nicio mapare. Mai bine niciun sinonim decât unul greșit: un sinonim greșit devine
    filtru, iar un filtru greșit golește rezultatul.

Ținta oricărei mapări e, prin construcție, o cheie din vocabularul REAL (`load_vocabulary`), deci
scriptul nu POATE produce o hartă moartă ca cea de dinainte.

Dry-run implicit; `--apply` scrie în `businesses.settings.domain_pack.concern_map`.

    python scripts/derive_concern_overlay.py --business <uuid>
    python scripts/derive_concern_overlay.py --business <uuid> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass

from src.catalog.vocabulary import CATEGORY_DIMENSION, CatalogVocabulary, load_vocabulary
from src.db.connection import admin_conn, close_pool, get_pool
from src.domain.normalize import normalize

# Praguri — deliberat conservatoare: overlay-ul devine FILTRU, deci o mapare greșită golește un
# rezultat. Preferăm să ratăm sinonime (clientul reformulează) decât să inventăm unul.
MIN_SUPPORT = 5  # la câte produse trebuie să apară expresia
MIN_PURITY = 0.80  # P(cheie | expresie)
MIN_LIFT = 1.5  # de câte ori mai probabilă decât la întâmplare
MIN_MARGIN = 1.5  # cât trebuie să bată câștigătoarea următoarea clasată (pe lift)
MAX_NGRAM = 3  # „ten uscat" da; fraze lungi sunt proză, nu vocabular

# Textul liber de unde învățăm limba clientului. NU e o listă de chei de catalog: sunt câmpurile
# care conțin PROZĂ, adică exact cele pe care descoperirea de vocabular le RESPINGE (media
# cuvintelor prea mare). Ce e inutil ca filtru e util ca dicționar.
PROSE_SQL = """
select p.id,
       concat_ws(' ',
                 case when jsonb_typeof(p.attributes->'best_for') = 'string'
                      then p.attributes->>'best_for' end,
                 case when jsonb_typeof(p.attributes->'key_benefit') = 'string'
                      then p.attributes->>'key_benefit' end,
                 p.ai_summary) as prose,
       p.attributes
  from products p
 where p.business_id = $1
   and p.status = 'active'
"""


@dataclass(frozen=True)
class Candidate:
    phrase: str
    dimension: str
    key: str
    support: int
    purity: float
    lift: float
    margin: float


def _ngrams(text: str, max_n: int = MAX_NGRAM) -> set[str]:
    """Expresiile candidate dintr-un text, normalizate.

    Două reguli structurale, niciuna o listă de cuvinte:

    * **n-gramele nu traversează punctuația.** „Textură cremă. Ingrediente-cheie: ..." nu conține
      expresia „crema ingrediente-cheie" — sunt două propoziții. Fără segmentare, jumătate din
      candidați sunt fragmente care nu există ca limbă și care se potrivesc din întâmplare.
    * **fără fragmente de o literă.** „vitamina E." producea și candidatul „e", care coreează
      perfect cu `vitamina E` dar nu e un cuvânt pe care l-ar scrie cineva.

    Fără stopword-uri: o expresie fără putere de discriminare cade oricum la lift/margine, iar o
    listă de cuvinte de oprit ar fi încă un artefact scris de mână — adică exact ce eliminăm.
    """
    out: set[str] = set()
    for segment in re.split(r"[.;:!?,()\[\]/|]+", text or ""):
        words = [w for w in normalize(segment).split() if len(w) > 1]
        for n in range(1, max_n + 1):
            for i in range(len(words) - n + 1):
                out.add(" ".join(words[i : i + n]))
    return out


def _product_keys(attributes: dict, vocab: CatalogVocabulary) -> set[tuple[str, str]]:
    """Perechile `(dimensiune, cheie)` pe care le poartă un produs, filtrate la vocabularul REAL —
    ca să nu învățăm sinonime pentru valori care oricum nu sunt adresabile."""
    known = {
        (dim, e.key)
        for dim in vocab.dimensions
        if dim != CATEGORY_DIMENSION
        for e in vocab.entries(dim)
    }
    out: set[tuple[str, str]] = set()
    for dim, value in (attributes or {}).items():
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, str) and (dim, v) in known:
                out.add((dim, v))
    return out


def derive(rows: list, vocab: CatalogVocabulary) -> list[Candidate]:
    """Co-ocurență expresie ↔ cheie, filtrată prin suport/puritate/lift/margine."""
    total = len(rows)
    key_totals: Counter[tuple[str, str]] = Counter()
    phrase_totals: Counter[str] = Counter()
    joint: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)

    for r in rows:
        attrs = r["attributes"]
        if isinstance(attrs, str):
            attrs = json.loads(attrs)
        keys = _product_keys(attrs or {}, vocab)
        phrases = _ngrams(r["prose"] or "")
        for k in keys:
            key_totals[k] += 1
        for p in phrases:
            phrase_totals[p] += 1
            for k in keys:
                joint[p][k] += 1

    out: list[Candidate] = []
    for phrase, n_phrase in phrase_totals.items():
        if n_phrase < MIN_SUPPORT:
            continue
        scored = []
        for key, n_both in joint[phrase].items():
            purity = n_both / n_phrase
            base = key_totals[key] / total if total else 0.0
            lift = (purity / base) if base else 0.0
            scored.append((lift, purity, key))
        if not scored:
            continue
        scored.sort(reverse=True)
        lift, purity, (dim, key) = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        margin = (lift / runner_up) if runner_up else float("inf")
        if purity < MIN_PURITY or lift < MIN_LIFT or margin < MIN_MARGIN:
            continue
        # O expresie care E deja cheia nu are nevoie de traducere.
        if normalize(key) == phrase:
            continue
        out.append(
            Candidate(
                phrase=phrase,
                dimension=dim,
                key=key,
                support=n_phrase,
                purity=purity,
                lift=lift,
                margin=margin,
            )
        )
    out.sort(key=lambda c: (-c.support, c.phrase))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", required=True)
    ap.add_argument("--apply", action="store_true", help="scrie overlay-ul (implicit: dry-run)")
    ap.add_argument("--out", help="scrie candidații într-un fișier JSON, pentru revizuire umană")
    args = ap.parse_args()

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            vocab = await load_vocabulary(conn, args.business)
            rows = await conn.fetch(PROSE_SQL, args.business)
            candidates = derive(list(rows), vocab)

            print(f"produse: {len(rows)} | dimensiuni: {len(vocab.dimensions)}")
            print(f"expresii derivate: {len(candidates)}\n")
            print(f"{'expresie':<28} {'→ cheie':<26} {'sup':>4} {'pur':>6} {'lift':>6}")
            print("-" * 76)
            for c in candidates:
                print(
                    f"{c.phrase:<28} {c.dimension + '=' + c.key:<26} "
                    f"{c.support:>4} {c.purity:>6.0%} {c.lift:>6.1f}"
                )

            overlay = {c.phrase: c.key for c in candidates}
            # Invarianta, verificată explicit: nicio țintă în afara vocabularului. Prin construcție
            # e adevărată; o afirmăm oricum, fiindcă exact asta n-a verificat nimeni în iulie.
            live = {e.key for dim in vocab.dimensions for e in vocab.entries(dim)}
            dead = sorted(t for t in overlay.values() if t not in live)
            if dead:
                print(f"\nEROARE: ținte inexistente în catalog: {dead}", file=sys.stderr)
                return 2
            print(f"\ntoate cele {len(set(overlay.values()))} ținte există în catalog ✓")

            if not args.apply:
                print("\n(dry-run — rulează cu --apply ca să scrii)")
                return 0

            raw = await conn.fetchval(
                "select settings from businesses where id = $1", args.business
            )
            settings = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
            pack = settings.get("domain_pack") or {}
            previous = pack.get("concern_map") or {}
            pack["concern_map"] = overlay
            settings["domain_pack"] = pack
            await conn.execute(
                "update businesses set settings = $2::jsonb where id = $1",
                args.business,
                json.dumps(settings, ensure_ascii=False),
            )
            print(f"\nscris: {len(previous)} intrări vechi → {len(overlay)} derivate")
            return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
