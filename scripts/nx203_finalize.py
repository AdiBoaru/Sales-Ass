"""NX-203 — din judecăți umane în corpus validat, împărțit în felii sigilate.

Ultimul pas al lanțului: `extract_families` → `build_pools` → `label` → **`finalize`**. Aici
etichetele devin un `QrelsSet` pe care îl consumă harnessul care exista deja
(`src/evals/retrieval/` — metrici, split-uri H1/H2/H3 single-use, validare de integritate).
Scriptul ăsta nu re-implementează nimic din el: convertește, apoi îi cere verdictul.

Trei reguli de includere, toate din același principiu — **într-un instrument de măsură, degradarea
grațioasă e o minciună**:

  • **Familia închisă cu `s` sau `k` NU intră.** Ai declarat că amestecă cereri diferite sau că nu e
    o cerere validă; judecățile puse pe ea s-ar sprijini pe un contract inexistent, dar ar arăta în
    fișier exact ca datele bune.
  • **Familia etichetată PARȚIAL nu intră.** Recall-ul are la numitor produsele relevante
    CUNOSCUTE; dacă jumătate din pool n-a fost judecată, numitorul e arbitrar, iar metrica iese
    optimistă exact cât de leneș a fost evaluatorul.
  • **Familia fără niciun produs relevant nu intră în corpusul principal.** Nu e o eroare — e un
    caz de ABSTENȚIE („n-am așa ceva"), un contract diferit, care se măsoară cu alte metrici. Se
    raportează separat, nu se aruncă.

`provenance` e `merchant_content`, nu `real_sanitized`. Frazele sunt scrise de comerciant despre ce
caută clienții, nu observate în trafic. Contate ca „reale", ar trece gate-ul care există tocmai ca
să prevină un corpus în care niciun client n-a scris nimic.

    python scripts/nx203_finalize.py                        # raport + verdict, nu scrie
    python scripts/nx203_finalize.py --business <uuid>      # + verifică produsele în catalog
    python scripts/nx203_finalize.py --write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from src.evals.retrieval.schema import (  # noqa: E402
    HardConstraint,
    Provenance,
    QrelJudgment,
    QrelsQuery,
    QrelsSet,
    Relevance,
)
from src.evals.retrieval.splits import partition  # noqa: E402
from src.evals.retrieval.validation import validate_integrity  # noqa: E402

DATA_DIR = ROOT / "tests" / "golden" / "nx203"
FAMILIES = DATA_DIR / "families_draft.json"
POOLS = DATA_DIR / "pools.json"
JUDGMENTS = DATA_DIR / "judgments.json"
QRELS = DATA_DIR / "qrels_sole.json"
ABSTENTION = DATA_DIR / "abstention_families.json"
REPORT = ROOT / "reports" / "nx203-corpus.md"

#: Ținta de corpus din card: familii DISTINCTE, integral verificate uman. Parafrazele nu o ridică —
#: metrica agregă pe familie, deci rezoluția crește doar cu contracte de adevăr noi.
TARGET_FAMILIES = 100

#: Relevanța de la care un produs „răspunde" cererii. Sub ea (marginal) contează la nDCG, dar nu
#: face familia utilizabilă: o familie ale cărei singure potriviri sunt marginale nu poate deosebi
#: un motor bun de unul prost.
_USEFUL_RELEVANCE = int(Relevance.relevant)


def _closed(notes: dict, fid: str) -> str | None:
    note = notes.get(fid)
    return (
        note["action"]
        if note and note["action"] in ("skip_family", "next_family", "split")
        else None
    )


def build_qrels(families: dict, pools: dict, state: dict) -> tuple[QrelsSet, dict, list[dict]]:
    """`(corpus, statistici, familii de abstenție)`. Pur: nicio scriere, nicio conexiune."""
    by_id = {f["family_id"]: f for f in families["families"]}
    judgments = state.get("judgments", {})
    notes = state.get("family_notes", {})
    rationales = state.get("rationales", {})
    stats: Counter = Counter()
    excluded: dict[str, list[str]] = defaultdict(list)
    abstention: list[dict] = []
    queries: list[QrelsQuery] = []

    for fid, pool in pools["pools"].items():
        fam = by_id.get(fid)
        if fam is None:
            stats["fara_familie"] += 1
            continue
        judged = judgments.get(fid, {})
        if not judged:
            stats["neetichetate"] += 1
            continue
        if (action := _closed(notes, fid)) is not None:
            stats[f"inchisa_{action}"] += 1
            excluded[action].append(fid)
            continue
        if len(judged) < len(pool):
            stats["partiale"] += 1
            excluded["partial"].append(fid)
            continue

        graded = [(pid, v) for pid, v in judged.items() if v != "forbidden"]
        forbidden = sorted(pid for pid, v in judged.items() if v == "forbidden")
        if not any(int(v) >= _USEFUL_RELEVANCE for _pid, v in graded):
            stats["abstentie"] += 1
            abstention.append(
                {
                    "family_id": fid,
                    "queries": fam["queries"],
                    "product_type": fam["product_type"],
                    "reason": "niciun produs peste relevanța `marginal`",
                }
            )
            continue

        constraints = [
            HardConstraint(facet=c["facet"], op=c["op"], value=c["value"])
            for c in fam["hard_constraints"]
        ]
        for i, text in enumerate(fam["queries"]):
            queries.append(
                QrelsQuery(
                    id=f"{fid}-q{i:02d}",
                    query=text,
                    locale=fam.get("locale", "ro"),
                    provenance=Provenance.merchant_content,
                    # `category` alimentează stratificarea feliilor. Tipul de produs e
                    # granularitatea
                    # corectă: pe categoria de catalog (933 de produse sub `ingrijirea-tenului`)
                    # stratificarea n-ar echilibra nimic.
                    category=fam["product_type"],
                    human_verified=True,
                    # Toate formulările unei familii primesc ACELAȘI grup, deci cad în ACEEAȘI
                    # felie.
                    # Fără asta, o frază și parafraza ei ajung în tuning și în holdout, iar gate-ul
                    # măsoară ce a văzut deja — contaminare pe care nicio verificare pe text n-o
                    # prinde, fiindcă textele chiar diferă.
                    family_id=fid,
                    split_group_id=fid,
                    catalog_version=fam["catalog_version"],
                    judgments=[
                        # Relevanța 0 se PĂSTREAZĂ: „judecat nerelevant" nu e „nejudecat". Ștearsă,
                        # produsul ar arăta ca unul pe care nimeni nu l-a văzut, iar acoperirea
                        # pool-ului ar fi imposibil de reconstituit mai târziu.
                        QrelJudgment(product_id=pid, relevance=Relevance(int(v)))
                        for pid, v in sorted(graded)
                    ],
                    forbidden_products=forbidden,
                    # Motivul e OBLIGATORIU în schemă: o excepție nejustificată e indistinctă de un
                    # fals-pozitiv cules din retrieval, iar corpusul ar consfinți greșeala.
                    forbidden_rationale={
                        pid: rationales.get(fid, {}).get(pid, "") for pid in forbidden
                    },
                    hard_constraints=constraints,
                )
            )
        stats["familii_incluse"] += 1

    qset = QrelsSet(business_id=pools["business_id"], queries=queries)
    stats["query_uri"] = len(queries)
    return qset, {"counts": stats, "excluded": dict(excluded)}, abstention


async def _catalog_ids(business_id: str) -> set[str] | None:
    """Id-urile active ale tenantului, pentru verificarea „judecăm produse care există".

    Opțional deliberat: fără DB, validatorul raportează verificarea ca INDISPONIBILĂ, nu ca trecută.
    Un corpus care judecă produse inexistente produce metrici care arată perfect."""
    from src.db.connection import admin_conn, close_pool, get_pool  # noqa: PLC0415

    try:
        pool = await get_pool()
        async with admin_conn(pool) as conn:
            rows = await conn.fetch(
                "select id::text as id from products where business_id = $1 and status = 'active'",
                business_id,
            )
        await close_pool()
        return {r["id"] for r in rows}
    except Exception as e:  # noqa: BLE001 — lipsa DB-ului nu e o eroare de corpus
        print(f"  (catalog inaccesibil: {type(e).__name__} — verificarea rămâne indisponibilă)")
        return None


def _render(qset: QrelsSet, info: dict, abstention: list[dict], report, splits: dict) -> str:
    c = info["counts"]
    lines = [
        "# NX-203 — corpus SOLE, stare curentă",
        "",
        f"- familii incluse: **{c['familii_incluse']}** (ținta: {TARGET_FAMILIES})",
        f"- query-uri (formulări): **{c['query_uri']}**",
        "",
        "## Ce a rămas afară, și de ce",
        "",
        "| motiv | familii |",
        "| --- | ---: |",
        f"| neetichetate încă | {c['neetichetate']} |",
        f"| etichetate parțial | {c['partiale']} |",
        f"| închise: amestecă cereri (`s`) | {c['inchisa_split']} |",
        f"| închise: nu e cerere validă (`k`) | {c['inchisa_skip_family']} |",
        f"| închise: destul (`n`) | {c['inchisa_next_family']} |",
        f"| abstenție (niciun produs relevant) | {c['abstentie']} |",
        "",
        "## Felii",
        "",
        "| felie | query-uri |",
        "| --- | ---: |",
    ]
    lines += [f"| {s.value} | {len(qs)} |" for s, qs in splits.items()]
    lines += ["", "## Verdict de integritate", ""]
    lines.append(f"- blocante: **{len(report.blocking)}**")
    for issue in report.blocking:
        lines.append(f"  - {issue}")
    lines.append(f"- verificări indisponibile: **{len(report.unavailable)}**")
    for issue in report.unavailable:
        lines.append(f"  - {issue}")
    if abstention:
        lines += ["", f"## Familii de abstenție: {len(abstention)}", ""]
        lines += [f"- „{a['queries'][0]}”" for a in abstention[:10]]
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--business", help="verifică și că produsele judecate există în catalog")
    ap.add_argument("--min-families", type=int, default=TARGET_FAMILIES)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    for path in (FAMILIES, POOLS):
        if not path.exists():
            print(f"Lipsește {path.relative_to(ROOT)}.")
            return 1
    if not JUDGMENTS.exists():
        print("Nicio judecată încă. Rulează `python scripts/nx203_label.py`.")
        return 1

    families = json.loads(FAMILIES.read_text(encoding="utf-8"))
    pools = json.loads(POOLS.read_text(encoding="utf-8"))
    state = json.loads(JUDGMENTS.read_text(encoding="utf-8"))
    if state.get("catalog_version") != pools.get("catalog_version"):
        print("Etichetele sunt pe alt catalog decât pool-urile. Refuz: exact defectul care a")
        print("expirat corpusul demo. Re-rulează extract_families + build_pools.")
        return 1

    qset, info, abstention = build_qrels(families, pools, state)
    catalog_ids = await _catalog_ids(args.business) if args.business else None

    report = validate_integrity(
        qset,
        min_queries=1,
        min_families=args.min_families,
        require_human_verified=True,
        require_real_per_category=False,  # corpusul e `merchant_content`; vezi docstring
        require_split_sizes=True,
        catalog_product_ids=catalog_ids,
    )
    splits = partition(qset)
    text = _render(qset, info, abstention, report, splits)
    print(text)

    if args.write:
        QRELS.write_text(
            json.dumps(qset.model_dump(mode="json"), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        ABSTENTION.write_text(
            json.dumps({"families": abstention}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        print(f"\nScris: {QRELS.relative_to(ROOT)} · {ABSTENTION.relative_to(ROOT)}")
    else:
        print("\n(dry-run — nimic scris; adaugă --write)")

    # Verdictul e al validatorului, nu al scriptului: ieșirea non-zero înseamnă „corpusul nu e
    # gata", nu „scriptul a crăpat". Feliile de holdout rămân sigilate până la gate-ul lor
    # (NX-207/209/210).
    return 0 if report.is_clean() else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
