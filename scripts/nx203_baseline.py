"""NX-203 baseline — de unde pleacă retrievalul, măsurat pe corpusul tenantului.

Rulează calea REALĂ de retrieval peste qrels, în două regimuri (text brut / cu constrângerile
familiei aplicate), și scrie un raport machine-readable. Diferența dintre cele două spune cât ține
de retrieval și cât de înțelegerea cererii.

**Felia. Implicit rulează DOAR pe `tuning`.** Feliile de holdout sunt single-use: se deschid o
singură dată, la gate-ul lor, iar după aceea nu mai validează nimic. Un baseline care le-ar atinge
din obișnuință le-ar arde înainte să existe ceva de validat, iar pierderea e tăcută — cifrele arată
la fel. De aceea deschiderea cere `--open-holdout <gate>`, cu numele gate-ului scris explicit.

**Modelul nu e chemat dacă producția nu-l cheamă.** Cu `SEARCH_SEMANTIC_ENABLED=false` (implicit
azi), brațul vector nu există pe calea reală, deci baseline-ul rulează lexical-only și costă zero.
Un baseline măsurat pe o configurație care nu rulează nicăieri nu e un punct de plecare, e o
ficțiune.

**Cine a etichetat intră în raport.** Un corpus etichetat de model produce cifre care arată exact ca
cele ratificate; `labeler` în `_meta` e singurul lucru care le deosebește peste șase luni.

    python scripts/nx203_baseline.py
    python scripts/nx203_baseline.py --qrels tests/golden/nx203/qrels_sole.json
    python scripts/nx203_baseline.py --open-holdout NX-209    # ARDE felia H2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from src.agent.llm import get_llm  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.evals.retrieval.adaptor import retrieve_products  # noqa: E402
from src.evals.retrieval.catalog import assert_catalog_unchanged, load_catalog  # noqa: E402
from src.evals.retrieval.harness import RunConfig, run_benchmark  # noqa: E402
from src.evals.retrieval.schema import QrelsSet  # noqa: E402
from src.evals.retrieval.splits import Split, holdout_slice_for_gate, partition  # noqa: E402

DEFAULT_QRELS = ROOT / "tests" / "golden" / "nx203" / "qrels_sole.json"


def _load(path: pathlib.Path) -> QrelsSet:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return QrelsSet(**{k: v for k, v in raw.items() if not k.startswith("_")})


def _select(qset: QrelsSet, gate: str | None) -> tuple[QrelsSet, Split]:
    """Felia pe care se rulează. Fără gate → `tuning`; cu gate → felia LUI, și numai a lui."""
    target = holdout_slice_for_gate(gate) if gate else Split.tuning
    if target is None:
        raise SystemExit(f"Gate necunoscut: {gate!r}. Așteptat NX-207 / NX-209 / NX-210.")
    queries = partition(qset)[target]
    if not queries:
        raise SystemExit(f"Felia {target.value} e goală — nu are ce măsura.")
    return QrelsSet(business_id=qset.business_id, queries=queries), target


async def _prefetch(qset: QrelsSet, llm, *, apply_constraints: bool) -> dict[str, list[str]]:
    """Retrievalul real pentru fiecare query, o dată per regim. `llm=None` ⇒ lexical-only."""
    out: dict[str, list[str]] = {}
    async with tenant_conn(qset.business_id) as conn:
        for q in qset.queries:
            out[q.query] = await retrieve_products(
                conn,
                llm,
                qset.business_id,
                q.query,
                hard_constraints=[hc.model_dump() for hc in q.hard_constraints],
                apply_constraints=apply_constraints,
            )
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--qrels", type=pathlib.Path, default=DEFAULT_QRELS)
    ap.add_argument(
        "--open-holdout",
        metavar="GATE",
        help="deschide felia gate-ului (NX-207/209/210). O ARDE: după rulare nu mai validează",
    )
    ap.add_argument("--out", type=pathlib.Path, help="unde se scrie raportul (implicit: reports/)")
    args = ap.parse_args()

    if not args.qrels.exists():
        print(f"Lipsește {args.qrels}. Rulează întâi `python scripts/nx203_finalize.py --write`.")
        return 1

    full = _load(args.qrels)
    qset, split = _select(full, args.open_holdout)
    raw_meta = json.loads(args.qrels.read_text(encoding="utf-8"))
    verified = Counter(q.human_verified for q in qset.queries)

    if args.open_holdout:
        print(f"⚠  DESCHID felia {split.value} pentru {args.open_holdout}. Se ARDE: după rularea")
        print("   asta nu mai poate valida nimic. Consemnează deschiderea în cardul gate-ului.\n")

    print(f"corpus : {len(full.queries)} query-uri · felia {split.value}: {len(qset.queries)}")
    print(f"etichete: human_verified={verified[True]} · model={verified[False]}")
    if verified[False]:
        print("   (cifrele de mai jos sunt ORIENTATIVE — etichetele nu sunt verificate uman)")

    settings = get_settings()
    # Baseline-ul măsoară configurația care RULEAZĂ. Dacă brațul semantic e stins în producție,
    # e stins și aici — altfel raportul descrie un sistem care nu există.
    llm = get_llm() if settings.search_semantic_enabled else None
    print(f"semantic: {'ON' if llm is not None else 'OFF (lexical-only, zero cost)'}\n")

    async with tenant_conn(qset.business_id) as conn:
        catalog = await load_catalog(conn, qset.business_id)
    print(f"catalog: {len(catalog.products)} produse — {catalog.fingerprint}\n")

    reports: dict[str, dict] = {}
    for label, apply_c in (("raw", False), ("with_constraints", True)):
        fetched = await _prefetch(qset, llm, apply_constraints=apply_c)
        report = run_benchmark(
            qset,
            lambda query, f=fetched: f.get(query, []),
            RunConfig(
                label=label,
                embedding_model="text-embedding-3-small" if llm is not None else None,
                reranker="none",
                split=split.value,
            ),
            catalog,
        )
        reports[label] = report.model_dump()
        print(f"=== {label}")
        print(f"  Recall@20   : {report.recall_at_20:.3f}")
        print(f"  nDCG@6      : {report.ndcg_at_6:.3f}")
        print(f"  Top-6 hit   : {report.top_6_hit_rate:.3f}")
        print(f"  MRR         : {report.mrr:.3f}")
        print(f"  Forbidden@6 : {report.forbidden_violation_rate:.3f}")
        print()

    async with tenant_conn(qset.business_id) as conn:
        await assert_catalog_unchanged(conn, qset.business_id, catalog)
    await close_pool()

    out = args.out or ROOT / "reports" / f"nx203-baseline-{split.value}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "_meta": {
                    "source_qrels": str(args.qrels.relative_to(ROOT)),
                    "catalog": catalog.fingerprint,
                    "catalog_version": raw_meta.get("catalog_version"),
                    "split": split.value,
                    "n_queries": len(qset.queries),
                    # Fără asta, un corpus etichetat de model produce cifre indistincte de cele
                    # ratificate. `human_verified` e afirmația pe care se sprijină orice gate.
                    "human_verified": verified[True],
                    "model_labeled": verified[False],
                    "semantic_arm": llm is not None,
                    "note": "raw = retrieval pe text brut; with_constraints = constrângerile "
                    "familiei aplicate (înțelegere perfectă a cererii). Diferența = cât ține de "
                    "retrieval vs de rezolvarea cererii.",
                },
                "configs": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"raport: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
