"""NX-351 — sonda pe catalogul real: planul kernelului executat pe turele SOLE, înaintea aprinderii.

Refolosește interpretările unei rulări A (`nx335_interpret_replay`, `results.json`), deci ZERO
apeluri de model. Pe fiecare conversație a instantaneului local, turele trec în ordine prin lanțul
kernelului (`kernel_turn`: aceleași funcții ale producției, cu faptele și perechile citite din DB),
cu ecranul sintetizat din ce a servit v1. Un plan `search` se execută cu `run_planned_search` pe
catalogul real (read-only), iar setul lui se compară cu ce a servit v1 pe același tur, pe adevărul
din etichetele A (subiect și nevoi). Regula GO e pre-înregistrată în `tasks/stage1/NX-351.md`.

    PYTHONPATH=. python scripts/nx351_kernel_catalog_probe.py \
        --dir "<instantaneul NX-335>" --run "<run-.../results.json>"

Raportul complet (id-uri de tur și de produs) e LOCAL (`reports/nx351/`, ignorat de git); în consolă
doar agregate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from scripts import nx335_interpret_replay as rp  # noqa: E402

OUT_DIR = ROOT / "reports" / "nx351"
TYPE_DIM = "product_type"
CATEGORY_DIM = "category"
#: Dimensiunile etichetate care NU sunt nevoi de fațetă (subiectul, prețul, cuvintele nemapate).
NOT_NEEDS = frozenset({TYPE_DIM, CATEGORY_DIM, "price", "unmapped"})
#: Regula GO, pre-înregistrată (NX-351): marja pe medie, cât de mult poate pierde un tur, câte ture.
MEAN_MARGIN = 0.05
TURN_LOSS = 0.5
MAX_LOSING_TURNS = 2
MAX_EMPTY_TURNS = 2
EPS = 1e-9


# --- adevărul din etichete (pur) -----------------------------------------------------------------


@dataclass(frozen=True)
class Truth:
    """Ce cumpără clientul până la tur inclusiv, după etichete: tipul, raftul, nevoile de fațetă."""

    product_type: str | None = None
    category: str | None = None
    needs: tuple[tuple[str, str], ...] = ()

    @property
    def known(self) -> bool:
        return bool(self.product_type or self.category or self.needs)


def next_truth(truth: Truth, label: Mapping[str, Any] | None) -> Truth:
    """Adevărul după un tur etichetat. O etichetă lipsă sau `uncertain` nu schimbă nimic. Un subiect
    nou (alt raft sau alt tip) golește nevoile de dinainte: sunt ale subiectului vechi (aproximare
    declarată în card: o reluare a unui subiect parcat nu se reconstruiește)."""
    if not label or label.get("uncertain"):
        return truth
    ptype, category, needs = truth.product_type, truth.category, list(truth.needs)
    subject_moved = False
    for change in label.get("changes") or []:
        op, dimension, value, relation = (list(change) + [None] * 4)[:4]
        if value is None or relation == "avoid":
            continue
        value = str(value)
        if dimension == TYPE_DIM:
            subject_moved = subject_moved or (ptype is not None and ptype != value)
            ptype = value
        elif dimension == CATEGORY_DIM:
            if category is not None and category != value:
                subject_moved = True
                ptype = None if not _sets(label, TYPE_DIM) else ptype
            category = value
        elif dimension not in NOT_NEEDS:
            if op == "set":
                needs = [n for n in needs if n[0] != dimension]
            if (dimension, value) not in needs:
                needs.append((dimension, value))
    if subject_moved:
        fresh = [(d, v) for d, v in needs if (d, v) not in truth.needs]
        needs = fresh
    return Truth(ptype, category, tuple(needs))


def _sets(label: Mapping[str, Any], dimension: str) -> bool:
    return any(len(c) > 1 and c[1] == dimension for c in label.get("changes") or [])


# --- metricile (pure) ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    """Faptele unui produs servit, hidratate din catalog."""

    product_type: str | None = None
    category_path: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


def _carries(card: Card, dimension: str, value: str) -> bool:
    raw = card.attributes.get(dimension)
    values: Iterable[Any] = raw if isinstance(raw, list | tuple) else (raw,)
    want = value.strip().casefold()
    for v in values:
        if isinstance(v, bool):
            if str(v).casefold() == want:
                return True
        elif v is not None and str(v).strip().casefold() == want:
            return True
    return False


def _on_shelf(card: Card, path: str | None) -> bool:
    here = card.category_path or ""
    return bool(path) and (here == path or here.startswith(f"{path}/"))


def subject_share(truth: Truth, cards: Sequence[Card], shelf_path: str | None) -> float | None:
    """M1: proporția cardurilor de tipul adevărat; fără tip, de pe raftul adevărat. `None` fără
    subiect adevărat. Un set gol are proporția 0 (golul se numără și la M3)."""
    if truth.product_type:
        hits = sum(1 for c in cards if c.product_type == truth.product_type)
    elif truth.category and shelf_path:
        hits = sum(1 for c in cards if _on_shelf(c, shelf_path))
    else:
        return None
    return hits / len(cards) if cards else 0.0


def need_share(truth: Truth, cards: Sequence[Card]) -> float | None:
    """M2: media pe nevoi a proporției cardurilor care poartă valoarea. `None` fără nevoi."""
    if not truth.needs:
        return None
    if not cards:
        return 0.0
    shares = [sum(1 for c in cards if _carries(c, d, v)) / len(cards) for d, v in truth.needs]
    return sum(shares) / len(shares)


def pair_rule(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """Regula pre-înregistrată pe o metrică: media kernelului ≥ media v1 − 0,05 ȘI cel mult 2 ture
    unde kernelul e sub v1 cu > 0,5. Fără perechi: nemăsurat (`passed=None`)."""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "kernel": None, "v1": None, "losing_turns": 0, "passed": None}
    kernel = sum(k for k, _ in pairs) / n
    v1 = sum(v for _, v in pairs) / n
    losing = sum(1 for k, v in pairs if k < v - TURN_LOSS - EPS)
    passed = kernel >= v1 - MEAN_MARGIN - EPS and losing <= MAX_LOSING_TURNS
    return {
        "n": n,
        "kernel": round(kernel, 3),
        "v1": round(v1, 3),
        "losing_turns": losing,
        "passed": passed,
    }


def verdict(
    m1: Sequence[tuple[float, float]],
    m2: Sequence[tuple[float, float]],
    empty_turns: int,
    errors: int,
) -> dict[str, Any]:
    rules = {
        "M1_subject": pair_rule(m1),
        "M2_needs": pair_rule(m2),
        "M3_empty": {"turns": empty_turns, "passed": empty_turns <= MAX_EMPTY_TURNS},
        "M4_errors": {"count": errors, "passed": errors == 0},
    }
    failed = [name for name, r in rules.items() if r["passed"] is False]
    unmeasured = [name for name, r in rules.items() if r["passed"] is None]
    return {
        **rules,
        "verdict": "NO-GO" if failed else ("NOT-MEASURED" if unmeasured else "GO"),
        "failed": failed,
        "unmeasured": unmeasured,
    }


def jaccard(a: Sequence[str], b: Sequence[str]) -> float | None:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else None


# --- rularea (DB read-only, zero apeluri de model) ----------------------------------------------


@dataclass
class TurnRecord:
    turn_id: str
    executor: str
    labelled: bool
    truth: Truth
    v1_ids: list[str]
    kernel_ids: list[str] | None = None
    lexical_step: str | None = None
    ms: float | None = None
    error: str | None = None
    versus_v1: Mapping[str, Any] = field(default_factory=dict)
    search_args: Mapping[str, Any] | None = None


def _load_run(path: Path) -> dict[str, Mapping[str, Any]]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    return {r["turn_id"]: r for r in rows if r.get("arm", "none") == "none"}


def _interpretation(row: Mapping[str, Any] | None) -> Any:
    from src.conversation.interpretation import TurnInterpretation  # noqa: PLC0415

    if not row or row.get("outcome") != "ok" or not row.get("interpretation"):
        return None
    return TurnInterpretation.model_validate(row["interpretation"])


class _Provider:
    """`deps.db(op)` = checkout tenant-scoped, ca în sondele NX-298/NX-313."""

    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, _operation: str = "unlabeled"):
        from src.db.connection import tenant_conn  # noqa: PLC0415

        return tenant_conn(self.business_id)


def _product_id(p: Mapping[str, Any]) -> str | None:
    pid = p.get("product_id") or p.get("id")
    return str(pid) if pid else None


async def _planned_search(
    snap: Any, deps: Any, turn: Any, args: Any, slots: int
) -> tuple[list[str], str | None, float]:
    from src.models import Contact, InboundMessage, TurnContext  # noqa: PLC0415
    from src.tools.catalog_tools import run_planned_search  # noqa: PLC0415

    biz = snap.business
    ctx = TurnContext(
        turn_id=f"nx351-{turn.turn_id}",
        business=biz,
        contact=Contact(id="nx351-probe", business_id=biz.id),
        message=InboundMessage(
            provider_msg_id=f"nx351-{turn.turn_id}", body=turn.client_text, channel_kind="webchat"
        ),
        conversation_id=f"nx351-{turn.conversation_id}",
        language=turn.language or biz.default_locale or "ro",
    )
    started = perf_counter()
    result = await run_planned_search(ctx, deps, args)
    ms = (perf_counter() - started) * 1000
    ids = [pid for p in (result.products or []) if (pid := _product_id(p))][:slots]
    event = next((e for e in ctx.events if e.type == "product_search"), None)
    step = (event.properties or {}).get("lexical_step") if event else None
    return ids, step, ms


_HYDRATE_SQL = (
    "select p.id::text as id, p.attributes, c.path as category_path "
    "from products p left join categories c on c.id = p.primary_category_id "
    "and c.business_id = p.business_id "
    "where p.business_id = $1::uuid and p.id = any($2::uuid[])"
)


async def _hydrate(business_id: str, ids: set[str]) -> dict[str, Card]:
    from src.db.connection import tenant_conn  # noqa: PLC0415

    if not ids:
        return {}
    async with tenant_conn(business_id) as conn:
        rows = await conn.fetch(_HYDRATE_SQL, business_id, sorted(ids))
    out: dict[str, Card] = {}
    for r in rows:
        attrs = r["attributes"]
        attrs = json.loads(attrs) if isinstance(attrs, str) else dict(attrs or {})
        ptype = attrs.get(TYPE_DIM)
        out[r["id"]] = Card(
            product_type=str(ptype) if ptype else None,
            category_path=r["category_path"],
            attributes=attrs,
        )
    return out


async def probe(snap: Any, run_rows: Mapping[str, Mapping[str, Any]]) -> list[TurnRecord]:
    import src.tools.catalog_tools  # noqa: F401, PLC0415 — înregistrează uneltele
    from src.catalog.reference_facts import fetch_reference_facts  # noqa: PLC0415
    from src.catalog.subject_pairs import subject_type_pairs  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415
    from src.conversation.state_v2 import ConversationStateV2  # noqa: PLC0415
    from src.worker.runner import PipelineDeps  # noqa: PLC0415

    bid = snap.business.id
    labels = rp.load_labels(snap.business.slug)
    tenant = rp._TenantDeps(bid)
    deps = PipelineDeps(llm=None, db=_Provider(bid))
    slots = get_settings().card_slots

    async def facts(lookup: Any) -> Any:
        return await fetch_reference_facts(tenant, bid, lookup)

    async with tenant.db("kernel_subject_pairs") as conn:
        pairs = await subject_type_pairs(conn, bid)

    records: list[TurnRecord] = []
    for conversation in snap.conversations():
        state = ConversationStateV2()
        truth = Truth()
        for index, turn in enumerate(conversation):
            label = labels.get(turn.turn_id)
            truth = next_truth(truth, label)
            interp = _interpretation(run_rows.get(turn.turn_id))
            inp = rp.make_input(snap, state, conversation[:index], turn)
            record = TurnRecord(
                turn_id=turn.turn_id,
                executor="no_interpretation" if interp is None else "no_plan",
                labelled=bool(label) and not label.get("uncertain"),
                truth=truth,
                v1_ids=[pid for r in turn.recommended if (pid := _product_id(r))],
            )
            try:
                kernel = await rp.kernel_turn(
                    state,
                    interp,
                    words=rp.user_words(inp),
                    pack=snap.pack,
                    vocab=snap.vocab,
                    locale=inp.locale,
                    facts=facts,
                    recommended=turn.recommended,
                    pairs=pairs,
                    turn_id=turn.turn_id,
                )
            except Exception as exc:  # noqa: BLE001 — M4 numără, rularea continuă
                record.error = f"chain:{type(exc).__name__}"
                records.append(record)
                continue
            planned = kernel.planned
            plan = planned.plans[planned.primary] if planned and planned.plans else None
            if plan is not None:
                record.executor = str(plan.executor)
            record.versus_v1 = rp.versus_v1(turn, kernel)
            if plan is not None and plan.executor == "search" and plan.search_args is not None:
                record.search_args = plan.search_args.model_dump(mode="json", exclude_none=True)
                try:
                    ids, step, ms = await _planned_search(snap, deps, turn, plan.search_args, slots)
                    record.kernel_ids, record.lexical_step, record.ms = ids, step, ms
                except Exception as exc:  # noqa: BLE001
                    record.error = f"search:{type(exc).__name__}"
            state = kernel.state_after
            records.append(record)
    return records


def _shelf_paths(vocab: Any) -> dict[str, str]:
    return {e.key: e.path for e in vocab.categories if e.path}


def summarize(records: Sequence[TurnRecord], cards: Mapping[str, Card], vocab: Any) -> dict:
    paths = _shelf_paths(vocab)
    m1: list[tuple[float, float]] = []
    m2: list[tuple[float, float]] = []
    empty = 0
    population = 0
    overlap: list[float] = []
    only_kernel = only_v1 = 0
    versus = Counter()
    per_turn: list[dict[str, Any]] = []
    for r in records:
        searched = r.kernel_ids is not None
        if searched and not r.v1_ids:
            only_kernel += 1
        if not searched and r.v1_ids and r.executor == "search":
            only_v1 += 1
        if not (r.labelled and searched and r.v1_ids and r.truth.known):
            continue
        population += 1
        kernel_cards = [cards[i] for i in r.kernel_ids or [] if i in cards]
        v1_cards = [cards[i] for i in r.v1_ids if i in cards]
        if not r.kernel_ids:
            empty += 1
        shelf = paths.get(r.truth.category or "")
        row: dict[str, Any] = {"turn_id": r.turn_id, "step": r.lexical_step}
        s_k, s_v = (
            subject_share(r.truth, kernel_cards, shelf),
            subject_share(r.truth, v1_cards, shelf),
        )
        if s_k is not None and s_v is not None:
            m1.append((s_k, s_v))
            row["m1"] = [round(s_k, 3), round(s_v, 3)]
        n_k, n_v = need_share(r.truth, kernel_cards), need_share(r.truth, v1_cards)
        if n_k is not None and n_v is not None:
            m2.append((n_k, n_v))
            row["m2"] = [round(n_k, 3), round(n_v, 3)]
        j = jaccard(r.kernel_ids or [], r.v1_ids)
        if j is not None:
            overlap.append(j)
        for flag in ("v1_shelf_dropped", "need_lost"):
            if r.versus_v1.get(flag):
                better = "m1" in row and row["m1"][0] >= row["m1"][1]
                versus[f"{flag}:{'kernel_ok' if better else 'kernel_worse_or_unmeasured'}"] += 1
        per_turn.append(row)
    ms = sorted(r.ms for r in records if r.ms is not None)
    errors = sum(1 for r in records if r.error)
    return {
        "turns": len(records),
        "executors": dict(Counter(r.executor for r in records)),
        "population": population,
        "only_kernel_searched": only_kernel,
        "only_v1_served": only_v1,
        "rules": verdict(m1, m2, empty, errors),
        "errors": dict(Counter(r.error for r in records if r.error)),
        "jaccard_mean": round(sum(overlap) / len(overlap), 3) if overlap else None,
        "versus_v1": dict(versus),
        "lexical_steps": dict(Counter(r.lexical_step or "?" for r in records if r.kernel_ids)),
        "search_ms_p50": round(ms[len(ms) // 2], 1) if ms else None,
        "search_ms_p90": round(ms[int(len(ms) * 0.9)], 1) if ms else None,
        "per_turn": per_turn,
    }


async def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dir", type=Path, required=True, help="instantaneul local NX-335")
    ap.add_argument("--run", type=Path, required=True, help="results.json al rulării A")
    args = ap.parse_args(argv)

    snap = rp.load_snapshot(args.dir)
    run_rows = _load_run(args.run)
    try:
        records = await probe(snap, run_rows)
        ids = {i for r in records for i in (*r.v1_ids, *(r.kernel_ids or []))}
        cards = await _hydrate(snap.business.id, ids)
    finally:
        from src.db.connection import close_pool  # noqa: PLC0415

        await close_pool()
    summary = summarize(records, cards, snap.vocab)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"run-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    detail = [
        {
            "turn_id": r.turn_id,
            "executor": r.executor,
            "labelled": r.labelled,
            "truth": asdict(r.truth),
            "v1_ids": r.v1_ids,
            "kernel_ids": r.kernel_ids,
            "lexical_step": r.lexical_step,
            "ms": r.ms,
            "error": r.error,
            "versus_v1": dict(r.versus_v1),
            "search_args": r.search_args,
        }
        for r in records
    ]
    (out / "results.json").write_text(
        json.dumps(
            {"run": str(args.run), "summary": summary, "turns": detail},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    public = {k: v for k, v in summary.items() if k != "per_turn"}
    print(json.dumps(public, ensure_ascii=False, indent=2))
    print(f"\nraport local: {out}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(_main()))
