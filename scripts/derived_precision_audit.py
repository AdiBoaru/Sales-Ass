"""NX-268 — poarta de PRECIZIE a faptelor derivate. Un om verifică, codul numără.

Nicio fațetă nu intră în enforcement pe baza acoperirii. Acoperirea spune câte produse poartă o
valoare; precizia spune câte dintre ele o poartă pe bună dreptate, iar diferența dintre cele două e
tot ce contează atunci când fațeta ajunge să EXCLUDĂ produse (NX-271). Măsurat deja o dată în
proiect: derivarea de nevoi a dat categoriei „Buze" 92% acoperire cu valori corecte și complet
irelevante — o cifră mare care nu însemna nimic.

**Pragurile sunt PREÎNREGISTRATE** (`tests/derived_precision_policy.json`, amprentat), ca la NX-246
felia 3. Motivul e prozaic: după ce vezi rezultatul, orice prag pare rezonabil. Amprenta politicii
intră în raport, deci o schimbare de prag după audit se vede.

Pragurile diferă după ce PROMITE fațeta, nu după cât de greu e s-o derivi:

* `fragrance_free`, `spf` — promisiuni. Clientul cumpără pe baza lor și un fals pozitiv e o
  minciună comercială. Prag înalt.
* `concerns`, `skin_type`, `texture` — semnale de rang. Un fals pozitiv coboară calitatea unei
  recomandări, nu produce o afirmație falsă. Prag mai jos.

Verdictul se dă pe LIMITA DE JOS Wilson, nu pe proporția brută: 100 de eșantioane cu 95 de „da" nu
înseamnă „precizie 95%", înseamnă „precizie între ~89% și ~98%". A raporta 95% ar fi să confunzi
măsurătoarea cu adevărul — aceeași regulă ca la feedback (NX-246 felia 2).

    python scripts/derived_precision_audit.py --business <uuid> --facet concerns   # adnotează
    python scripts/derived_precision_audit.py --business <uuid> --report           # verdicte

Progresul se salvează după FIECARE verdict: auditul se poate întrerupe și relua oricând.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import math
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.derivation import (  # noqa: E402
    build_matchers,
    match_keys,
    tokens,
)
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402
from src.domain.normalize import normalize  # noqa: E402

POLICY_PATH = ROOT / "tests" / "derived_precision_policy.json"
AUDIT_DIR = ROOT / "reports" / "derived_precision"

# Secțiunile din care derivă nevoile — aceleași ca în jobul de derivare. Duplicarea e deliberată:
# auditul trebuie să citească EXACT ce a citit derivarea, iar un import din script ar lega două
# programe care rulează în momente diferite.
POSITIVE_SECTIONS = (
    "fit",
    "problem",
    "purpose",
    "recommendation_trigger",
    "summary",
    "questions",
    "editorial",
)

VERDICTS = {"y": "correct", "n": "wrong", "?": "unsure"}


def _wilson_lower(successes: int, n: int, z: float = 1.96) -> float:
    """Limita de JOS a intervalului Wilson. Nu Wald: pe 10/10, Wald spune „între 100% și 100%",
    ceea ce e o afirmație pe care niciun eșantion de 10 n-o poate susține."""
    if n == 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


def _policy() -> dict:
    if not POLICY_PATH.exists():
        raise SystemExit(f"lipsește politica preînregistrată: {POLICY_PATH}")
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def _policy_fingerprint(policy: dict) -> str:
    """Amprenta politicii, peste conținutul CANONIC (fără note). O schimbare de prag după audit
    schimbă amprenta, deci se vede în raport — nu se poate rescrie istoria tăcut."""
    canonical = json.dumps(policy.get("facets", {}), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _audit_path(business_id: str, facet: str) -> pathlib.Path:
    return AUDIT_DIR / f"{business_id[:8]}-{facet}.json"


def _load_audit(business_id: str, facet: str) -> dict:
    path = _audit_path(business_id, facet)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"business_id": business_id, "facet": facet, "verdicts": {}}


def _save_audit(data: dict) -> None:
    path = _audit_path(data["business_id"], data["facet"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


async def _derive_all(business_id: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """(derivate per produs, context per produs). Rulează ACEEAȘI potrivire ca jobul de derivare —
    dacă ar rula alta, auditul ar măsura alt sistem decât cel care scrie în catalog."""
    async with tenant_conn(business_id) as conn:
        business = await load_business(conn, business_id)
        if business is None:
            raise SystemExit("business inexistent")
        pack = load_domain_pack(business)
        if pack is None:
            raise SystemExit("tenantul n-are domain pack")
        raw_pack = (business.settings or {}).get("domain_pack") or {}
        stems_cfg = raw_pack.get("concern_stems") or {}
        matchers = build_matchers(
            dict(pack.concern_map),
            stems={k: v.get("stems") or [] for k, v in stems_cfg.items() if isinstance(v, dict)},
            excludes={
                k: v.get("excludes") or [] for k, v in stems_cfg.items() if isinstance(v, dict)
            },
            normalize=normalize,
        )
        facet_values = {f.key: set(f.values or ()) for f in pack.facets}
        products = await conn.fetch(
            "select id::text as id, name from products "
            "where business_id = $1 and status = 'active'",
            business_id,
        )
        sections = await conn.fetch(
            "select product_id::text as id, kind, coalesce(body,'') as body "
            "from product_sections where business_id = $1",
            business_id,
        )
    await close_pool()

    by_product: dict[str, dict[str, list[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for s in sections:
        by_product[s["id"]][s["kind"]].append(s["body"])

    derived: dict[str, dict] = {}
    context: dict[str, dict] = {}
    for p in products:
        pid = p["id"]
        secs = by_product.get(pid, {})
        hits: dict[str, list[str]] = collections.defaultdict(list)
        evidence: dict[str, list[str]] = collections.defaultdict(list)
        for kind in POSITIVE_SECTIONS:
            for body in secs.get(kind, []):
                for key, hit in match_keys(tokens(normalize(body)), matchers).items():
                    hits[key].append(kind)
                    evidence[key].extend(hit.evidence)
        per_facet: dict[str, list[str]] = {}
        for facet, allowed in facet_values.items():
            values = sorted(k for k in hits if k in allowed)
            if values:
                per_facet[facet] = values
        if per_facet:
            derived[pid] = per_facet
            context[pid] = {
                "name": p["name"],
                "evidence": {k: sorted(set(v))[:4] for k, v in evidence.items()},
                "sections": {
                    kind: " ".join(secs.get(kind, []))[:600]
                    for kind in POSITIVE_SECTIONS
                    if secs.get(kind)
                },
            }
    return derived, context


def _sample(derived: dict[str, dict], facet: str, size: int, seed: str) -> list[str]:
    """Eșantion ALEATOR, dar reproductibil: sămânța e `business_id + fațetă`, deci două rulări
    aleg aceleași produse și auditul se poate relua. Un eșantion care se schimbă la fiecare rulare
    n-ar fi reluabil, iar unul ordonat (primele 100) ar măsura ordinea catalogului, nu fațeta."""
    eligible = sorted(pid for pid, facets in derived.items() if facet in facets)
    rng = random.Random(seed)
    rng.shuffle(eligible)
    return eligible[:size]


async def _annotate(args) -> int:
    policy = _policy()
    spec = policy["facets"].get(args.facet)
    if spec is None:
        raise SystemExit(f"fațeta {args.facet!r} nu e în politica preînregistrată")
    derived, context = await _derive_all(args.business)
    sample = _sample(derived, args.facet, spec["sample_size"], f"{args.business}:{args.facet}")
    if not sample:
        print(f"niciun produs cu `{args.facet}` derivat — nu e nimic de auditat")
        return 1

    data = _load_audit(args.business, args.facet)
    todo = [pid for pid in sample if pid not in data["verdicts"]]
    print(
        f"fațeta `{args.facet}` · eșantion {len(sample)} · rămase {len(todo)}\n"
        f"prag preînregistrat: {spec['min_precision']:.0%} (limita de jos Wilson)\n"
        "y = valoarea e susținută de text · n = nu e · ? = nu pot decide · q = ieși\n"
    )
    for i, pid in enumerate(todo, 1):
        info = context[pid]
        values = derived[pid][args.facet]
        print(f"\n[{i}/{len(todo)}] {info['name'][:100]}")
        print(f"  derivat: {args.facet} = {values}")
        for key in values:
            if ev := info["evidence"].get(key):
                print(f"    {key:20} ← {ev}")
        for kind, body in list(info["sections"].items())[:3]:
            print(f"    [{kind}] {body[:220]}")
        answer = input("  verdict (y/n/?/q): ").strip().lower()
        if answer == "q":
            break
        if answer not in VERDICTS:
            print("  (răspuns necunoscut — sărit, îl vei revedea la reluare)")
            continue
        data["verdicts"][pid] = {"verdict": VERDICTS[answer], "values": values}
        _save_audit(data)
    done = len(data["verdicts"])
    print(f"\nsalvat: {done}/{len(sample)} verdicte în {_audit_path(args.business, args.facet)}")
    return 0


def _report(args) -> int:
    policy = _policy()
    fingerprint = _policy_fingerprint(policy)
    print(f"politică preînregistrată: {fingerprint}\n")
    print(f"{'fațetă':18}{'n':>5}{'corecte':>9}{'precizie':>10}{'Wilson↓':>9}{'prag':>7}  verdict")
    rows = {}
    for facet, spec in sorted(policy["facets"].items()):
        data = _load_audit(args.business, facet)
        verdicts = data["verdicts"]
        # `unsure` NU intră în numitor: o valoare pe care auditorul n-a putut-o decide nu e nici
        # dovadă pentru, nici împotrivă. A o număra ca greșeală ar penaliza ambiguitatea sursei;
        # ca reușită, ar ascunde-o. Se raportează separat.
        decided = [v for v in verdicts.values() if v["verdict"] in ("correct", "wrong")]
        correct = sum(1 for v in decided if v["verdict"] == "correct")
        n = len(decided)
        lower = _wilson_lower(correct, n)
        if n < spec["min_sample"]:
            verdict = "INSUFFICIENT"
        elif lower >= spec["min_precision"]:
            verdict = "ENFORCE_READY"
        else:
            verdict = "NOT_READY"
        precision = correct / n if n else 0
        print(
            f"{facet:18}{n:>5}{correct:>9}{precision:>9.0%}{lower:>9.0%}"
            f"{spec['min_precision']:>7.0%}  {verdict}"
        )
        rows[facet] = {
            "n_decided": n,
            "correct": correct,
            "unsure": len(verdicts) - n,
            "precision": round(precision, 4),
            "wilson_lower": round(lower, 4),
            "min_precision": spec["min_precision"],
            "min_sample": spec["min_sample"],
            "verdict": verdict,
        }
    out = AUDIT_DIR / f"{args.business[:8]}-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "business_id": args.business,
                "policy_fingerprint": fingerprint,
                "facets": rows,
                # Cine e `ENFORCE_READY` e o LISTĂ, nu o propoziție: NX-271 o citește ca atare și
                # aprinde o fațetă pe rând.
                "enforce_ready": sorted(
                    f for f, r in rows.items() if r["verdict"] == "ENFORCE_READY"
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nraport: {out}")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--facet", help="fațeta de auditat (cere adnotare interactivă)")
    ap.add_argument("--report", action="store_true", help="verdicte din ce s-a adnotat deja")
    args = ap.parse_args()
    if args.report:
        return _report(args)
    if not args.facet:
        raise SystemExit("dă --facet <nume> ca să adnotezi, sau --report ca să vezi verdictele")
    return await _annotate(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
