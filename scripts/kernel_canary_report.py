"""NX-353 — raportul kernelului pe trafic real (dark, apoi canary) și regula GO dark → canary.

    PYTHONPATH=. python scripts/kernel_canary_report.py --business sole-ro --since 2026-10-01
    [--prompt-version interpret.v4.1]

READ-ONLY, zero apeluri de model. Citește, pe fereastră și pe tenant (`business_id = $1`):
`analytics_events` (`kernel_turn`, `turn_interpretation`, `kernel_dark`, `llm_usage`,
`conversation_state_shadow_diff`) și `conversation_traces` (`diagnostics.kernel_dark` = ce ar fi
servit kernelul, `recommended` = ce a servit v1), apoi hidratează produsele ambelor seturi într-un
singur query. Regula GO e PRE-ÎNREGISTRATĂ în `tasks/stage1/NX-353.md` §4; pragurile de mai jos sunt
copia ei și nu se mută după rulare.

Metricile de potrivire (G3/G4) sunt PROXY: adevărul e starea kernelului (tipul spus sau umbrela,
nevoile active validate din cuvintele clientului), deci favorizează kernelul (declarat). Etichetele
reale rămân revizuirea manuală (G6): raportul alege 20 de ture dark cu seed fix, iar
`scripts/kernel_trace.py` le deschide.

În consolă doar agregate și id-uri de tur; raportul complet e LOCAL (`reports/nx353/`, ignorat de
git), fără text de client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "reports" / "nx353"

# --- regula GO, pre-înregistrată (NX-353 §4, 2026-09-29) ----------------------------------------
MIN_TURNS = 60
MIN_DAYS = 5
MAX_ERROR_RATE = 0.05
EMPTY_FLOOR, EMPTY_RATE = 2, 0.05
MEAN_MARGIN = 0.05
MAX_INTERPRET_P90_MS = 4000.0
MANUAL_SAMPLE = 20
SEED = 353
EPS = 1e-9

#: Fallback-urile de EROARE (G1). `dark` nu e o eroare: e modul.
ERROR_REASONS = frozenset(
    {
        "provider_error",
        "refused",
        "truncated",
        "invalid_json",
        "schema_violation",
        "internal_error",
        "exception",
        "snapshot_error",
        "vocabulary_unavailable",
        # NX-353 (recenzia, adăugat înaintea oricărei rulări): plafonul NOSTRU pe interpretarea dark
        "dark_timeout",
    }
)
#: Dimensiunea cuvintelor clientului fără cheie de catalog: niciun card n-o poartă, deci ar dilua
#: G4 cu perechi (0, 0) (recenzia NX-353).
UNMAPPED = "unmapped"
#: Un apel tăiat de plafonul dark n-are rând în `per_call` (apelul a fost anulat): intră în G5
#: CENZURAT, peste prag, altfel p90 ar ieși mai bun exact când furnizorul e blocat.
CENSORED_MS = MAX_INTERPRET_P90_MS + 1
EVENT_TYPES = (
    "kernel_turn",
    "turn_interpretation",
    "kernel_dark",
    "llm_usage",
    "conversation_state_shadow_diff",
)
TYPE_DIM = "product_type"


# --- intrarea (formă neutră, construită din DB sau din teste) ------------------------------------


@dataclass(frozen=True)
class Event:
    turn_id: str | None
    type: str
    properties: Mapping[str, Any]
    day: str


@dataclass(frozen=True)
class DarkTurn:
    """Un tur cu `diagnostics.kernel_dark`: ce ar fi servit kernelul lângă ce a servit v1."""

    turn_id: str
    day: str
    record: Mapping[str, Any]
    v1_ids: tuple[str, ...]


@dataclass(frozen=True)
class Card:
    """Faptele unui produs servit, hidratate din catalog (tipul și atributele)."""

    product_type: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


# --- metricile (pure) ----------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float | None:
    """Percentila `q` (0-1) cu interpolare liniară; `None` pe o listă goală."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _carries(card: Card, dimension: str, value: str) -> bool:
    raw = card.attributes.get(dimension)
    values: Iterable[Any] = raw if isinstance(raw, list | tuple) else (raw,)
    want = value.strip().casefold()
    return any(v is not None and str(v).strip().casefold() == want for v in values)


def subject_share(types: Sequence[str], cards: Sequence[Card]) -> float | None:
    """G3: proporția cardurilor de un tip din subiectul kernelului (tipul spus sau umbrela).
    `None` fără tip; un set gol are proporția 0."""
    if not types:
        return None
    if not cards:
        return 0.0
    wanted = set(types)
    return sum(1 for c in cards if c.product_type in wanted) / len(cards)


def need_share(needs: Sequence[Sequence[str]], cards: Sequence[Card]) -> float | None:
    """G4: media pe nevoi a proporției cardurilor care poartă valoarea. `None` fără nevoi."""
    pairs = [(str(n[0]), str(n[1])) for n in needs if len(n) >= 2 and n[0] != UNMAPPED]
    if not pairs:
        return None
    if not cards:
        return 0.0
    shares = [sum(1 for c in cards if _carries(c, d, v)) / len(cards) for d, v in pairs]
    return sum(shares) / len(shares)


def mean_rule(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """Media kernelului ≥ media v1 − 0,05. Fără perechi: nemăsurat (`passed=None`)."""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "kernel": None, "v1": None, "passed": None}
    kernel = sum(k for k, _ in pairs) / n
    v1 = sum(v for _, v in pairs) / n
    return {
        "n": n,
        "kernel": round(kernel, 3),
        "v1": round(v1, 3),
        "passed": kernel >= v1 - MEAN_MARGIN - EPS,
    }


def _props(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def interpret_latencies(events: Iterable[Event]) -> list[float]:
    """Durata apelurilor de interpretare (`llm_usage.per_call[].purpose == "interpret"`)."""
    out: list[float] = []
    for e in events:
        if e.type != "llm_usage":
            continue
        for row in e.properties.get("per_call") or ():
            if isinstance(row, Mapping) and row.get("purpose") == "interpret":
                ms = row.get("ms")
                if isinstance(ms, int | float):
                    out.append(float(ms))
    return out


def _first_per_turn(items: Iterable[Any]) -> Iterable[Any]:
    """Primul rând pe `turn_id` (un retry de aftercare sau un eveniment dublat nu contează de două
    ori); rândurile fără `turn_id` rămân, fiecare."""
    seen: set[str] = set()
    for item in items:
        tid = getattr(item, "turn_id", None)
        if tid:
            if tid in seen:
                continue
            seen.add(tid)
        yield item


def sample_turns(turn_ids: Iterable[str], k: int = MANUAL_SAMPLE, seed: int = SEED) -> list[str]:
    """G6: `k` ture alese aleator cu seed FIX, din id-urile SORTATE (reproductibil)."""
    pool = sorted(set(turn_ids))
    return sorted(random.Random(seed).sample(pool, min(k, len(pool))))


def prompt_versions(events: Iterable[Event]) -> dict[str, int]:
    """NX-356: câte ture per `prompt_version` (din `turn_interpretation`). PUR. Vizibil în fiecare
    raport, ca o fereastră care amestecă două versiuni să se vadă și fără filtru."""
    firsts = _first_per_turn(e for e in events if e.type == "turn_interpretation")
    return dict(Counter(str(e.properties.get("prompt_version")) for e in firsts))


def only_prompt_version(
    events: Sequence[Event], dark_turns: Sequence[DarkTurn], version: str
) -> tuple[list[Event], list[DarkTurn]]:
    """NX-356: doar turele interpretate cu `version`. PUR. Vederea interpretării s-a schimbat la
    `interpret.v4.1` (replica botului netăiată), iar o fereastră care cuprinde deploy-ul ar amesteca
    două măsurători în G1-G5."""
    keep = {
        e.turn_id
        for e in events
        if e.type == "turn_interpretation" and e.properties.get("prompt_version") == version
    }
    return (
        [e for e in events if e.turn_id in keep],
        [t for t in dark_turns if t.turn_id in keep],
    )


def summarize(
    events: Sequence[Event],
    dark_turns: Sequence[DarkTurn],
    cards: Mapping[str, Card],
) -> dict[str, Any]:
    """Volum, umbra stării, interpretarea, comparația kernel/v1 și verdictul regulii GO. PUR.

    Populația regulii GO sunt turele DARK (`kernel_turn.mode == "dark"`), una pe `turn_id`: turele
    servite de canary (și fallback-urile lor) nu umflă nici numărul, nici zilele (recenzia)."""
    kernel_turns = list(_first_per_turn(e for e in events if e.type == "kernel_turn"))
    served = [e for e in kernel_turns if e.properties.get("served") is True]
    dark = [e for e in kernel_turns if e.properties.get("mode") == "dark"]
    reasons = Counter(str(e.properties.get("fallback_reason")) for e in dark)
    # o căutare dark care aruncă nu schimbă `fallback_reason` (turul rămâne `dark`): se numără
    # din `kernel_dark.error`, pe aceleași ture
    search_errors = {
        e.turn_id
        for e in events
        if e.type == "kernel_dark" and e.properties.get("error") and e.turn_id
    }
    errors = sum(
        1
        for e in dark
        if str(e.properties.get("fallback_reason")) in ERROR_REASONS or e.turn_id in search_errors
    )
    days = sorted({e.day for e in dark})
    dark_turns = list(_first_per_turn(dark_turns))
    outcomes = Counter(
        str(e.properties.get("outcome")) for e in events if e.type == "turn_interpretation"
    )
    shadow = [e for e in events if e.type == "conversation_state_shadow_diff"]
    shadow_fields: Counter[str] = Counter()
    for e in shadow:
        if e.properties.get("differs"):
            shadow_fields.update(str(f) for f in e.properties.get("fields") or ())
    latencies = interpret_latencies(events) + [
        CENSORED_MS for _ in range(reasons.get("dark_timeout", 0))
    ]
    p90 = percentile(latencies, 0.9)

    # G2-G4: turele dark cu plan `search` unde v1 a servit ≥ 1 produs (aceeași populație)
    compared, empty, m_subject, m_needs = [], 0, [], []
    for turn in dark_turns:
        record = turn.record
        if record.get("executor") != "search" or not turn.v1_ids:
            continue
        kernel_ids = list(record.get("kernel_ids") or ()) if record.get("searched") else []
        compared.append(turn.turn_id)
        empty += not kernel_ids
        kernel_cards = [cards[i] for i in kernel_ids if i in cards]
        v1_cards = [cards[i] for i in turn.v1_ids if i in cards]
        truth = record.get("truth") or {}
        # un tip DEDUS de cod din setul arătat de v1 nu e al clientului: nu e adevăr (declarat)
        types = [] if truth.get("type_learned") else list(truth.get("types") or ())
        k, v = subject_share(types, kernel_cards), subject_share(types, v1_cards)
        if k is not None and v is not None:
            m_subject.append((k, v))
        k, v = (
            need_share(truth.get("needs") or (), kernel_cards),
            need_share(truth.get("needs") or (), v1_cards),
        )
        if k is not None and v is not None:
            m_needs.append((k, v))

    n_unserved = len(dark)
    empty_cap = max(EMPTY_FLOOR, EMPTY_RATE * len(compared))
    gates = {
        "G1_errors": {
            "errors": errors,
            "turns": n_unserved,
            "rate": round(errors / n_unserved, 3) if n_unserved else None,
            "passed": (errors <= MAX_ERROR_RATE * n_unserved + EPS) if n_unserved else None,
        },
        "G2_empty": {
            "empty": empty,
            "turns": len(compared),
            "cap": round(empty_cap, 2),
            "passed": (empty <= empty_cap + EPS) if compared else None,
        },
        "G3_subject": mean_rule(m_subject),
        "G4_needs": mean_rule(m_needs),
        "G5_latency": {
            "calls": len(latencies),
            "p50_ms": round(percentile(latencies, 0.5) or 0, 1) if latencies else None,
            "p90_ms": round(p90, 1) if p90 is not None else None,
            "passed": (p90 <= MAX_INTERPRET_P90_MS + EPS) if p90 is not None else None,
        },
    }
    enough = n_unserved >= MIN_TURNS and len(days) >= MIN_DAYS
    failed = [name for name, g in gates.items() if g["passed"] is False]
    unmeasured = [name for name, g in gates.items() if g["passed"] is None]
    if failed:
        verdict = "NO-GO"
    elif not enough or unmeasured:
        verdict = "INSUFFICIENT"
    else:
        verdict = "PENDING-MANUAL"  # G1-G5 trec; G6 e revizuirea lui Adi
    dark_ids = [t.turn_id for t in dark_turns]
    return {
        "volume": {
            "branch_turns": len(kernel_turns),
            "served": len(served),
            "dark": n_unserved,
            "dark_search_errors": len(search_errors),
            "days": len(days),
            "fallback_reasons": dict(sorted(reasons.items())),
            "dark_records": len(dark_turns),
            "min_turns": MIN_TURNS,
            "min_days": MIN_DAYS,
        },
        "interpretation_outcomes": dict(sorted(outcomes.items())),
        "state_shadow": {
            "turns": len(shadow),
            "differs": sum(1 for e in shadow if e.properties.get("differs")),
            "fields": dict(shadow_fields.most_common()),
        },
        "gates": gates,
        "verdict": verdict,
        "failed": failed,
        "unmeasured": unmeasured,
        "G6_manual_sample": sample_turns(dark_ids),
        "compared_turns": compared,
    }


# --- citirea (DB read-only) ----------------------------------------------------------------------

Opener = Callable[..., AbstractAsyncContextManager[Any]]

BUSINESS_SQL = "select id::text from businesses where slug = $1 or id::text = $1"
EVENTS_SQL = """
select turn_id::text as turn_id, event_type, properties, created_at
from analytics_events
where business_id = $1::uuid and created_at >= $2 and created_at < $3
  and event_type = any($4::text[])
"""
TRACES_SQL = """
select turn_id::text as turn_id, created_at, recommended, diagnostics -> 'kernel_dark' as dark
from conversation_traces
where business_id = $1::uuid and created_at >= $2 and created_at < $3
  and diagnostics ? 'kernel_dark'
"""
HYDRATE_SQL = """
select p.id::text as id, p.attributes
from products p
where p.business_id = $1::uuid and p.id = any($2::uuid[])
"""


def _ids(raw: Any) -> tuple[str, ...]:
    rows = json.loads(raw) if isinstance(raw, str) else (raw or [])
    out = []
    for r in rows:
        pid = (r.get("product_id") or r.get("id")) if isinstance(r, Mapping) else None
        if pid:
            out.append(str(pid))
    return tuple(out)


def _day(created_at: Any) -> str:
    return created_at.date().isoformat() if hasattr(created_at, "date") else str(created_at)[:10]


async def load(
    business: str, since: datetime, until: datetime, *, admin: Opener, tenant: Opener
) -> tuple[str, list[Event], list[DarkTurn], dict[str, Card]]:
    """Evenimentele, turele dark și produsele hidratate. `admin()` / `tenant(business_id)` deschid
    conexiunile (injectate, ca testul să ruleze pe o conexiune falsă)."""
    async with admin() as conn:
        business_id = await conn.fetchval(BUSINESS_SQL, business)
        if business_id is None:
            raise SystemExit(f"tenant necunoscut: {business!r}")
        # `analytics_events` e append-only pentru `bot_runtime` (doar INSERT): citirea e pe
        # conexiunea de operator, cu `business_id` explicit (P7), ca la celelalte sonde (NX-316).
        event_rows = await conn.fetch(EVENTS_SQL, business_id, since, until, list(EVENT_TYPES))
    async with tenant(business_id) as conn:
        trace_rows = await conn.fetch(TRACES_SQL, business_id, since, until)
    events = [
        Event(r["turn_id"], r["event_type"], _props(r["properties"]), _day(r["created_at"]))
        for r in event_rows
    ]
    dark_turns = [
        DarkTurn(r["turn_id"], _day(r["created_at"]), _props(r["dark"]), _ids(r["recommended"]))
        for r in trace_rows
    ]
    wanted = {i for t in dark_turns for i in (*t.v1_ids, *(t.record.get("kernel_ids") or ()))}
    cards: dict[str, Card] = {}
    if wanted:
        async with tenant(business_id) as conn:
            rows = await conn.fetch(HYDRATE_SQL, business_id, sorted(wanted))
        for r in rows:
            attrs = _props(r["attributes"])
            ptype = attrs.get(TYPE_DIM)
            cards[r["id"]] = Card(product_type=str(ptype) if ptype else None, attributes=attrs)
    return business_id, events, dark_turns, cards


def render_console(report: Mapping[str, Any]) -> str:
    """Agregatele și id-urile de tur, fără text de client. PUR."""
    lines = [
        f"verdict: {report['verdict']}  (eșuate: {report['failed'] or '-'}; "
        f"nemăsurate: {report['unmeasured'] or '-'})",
        f"volum: {json.dumps(report['volume'], ensure_ascii=False)}",
        f"interpretare: {json.dumps(report['interpretation_outcomes'], ensure_ascii=False)}",
        f"versiuni de prompt: {json.dumps(report.get('prompt_versions', {}), ensure_ascii=False)}",
        f"umbra stării: {json.dumps(report['state_shadow'], ensure_ascii=False)}",
    ]
    for name, gate in report["gates"].items():
        lines.append(f"{name}: {json.dumps(gate, ensure_ascii=False)}")
    lines.append("G6 (revizuire manuală, `scripts/kernel_trace.py --business <slug> <turn_id>`):")
    lines.extend(f"  {t}" for t in report["G6_manual_sample"])
    return "\n".join(lines)


async def _main(
    business: str, since: datetime, until: datetime, prompt_version: str | None = None
) -> dict[str, Any]:
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415

    try:
        pool = await get_pool()
        _, events, dark_turns, cards = await load(
            business, since, until, admin=lambda: admin_conn(pool), tenant=tenant_conn
        )
    finally:
        await close_pool()
    versions = prompt_versions(events)
    if prompt_version:
        events, dark_turns = only_prompt_version(events, dark_turns, prompt_version)
    report = summarize(events, dark_turns, cards)
    report["prompt_versions"] = versions
    report["window"] = {
        "business": business,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "prompt_version": prompt_version,
    }
    return report


def _date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--business", required=True, help="slug-ul sau id-ul tenantului")
    parser.add_argument("--since", type=_date, help="început (ISO), implicit acum − 7 zile")
    parser.add_argument("--until", type=_date, help="sfârșit (ISO), implicit acum")
    parser.add_argument(
        "--prompt-version",
        help="doar turele interpretate cu versiunea asta (ex. interpret.v4.1, NX-356)",
    )
    args = parser.parse_args(argv)
    until = args.until or datetime.now(UTC)
    since = args.since or until - timedelta(days=7)
    report = asyncio.run(_main(args.business, since, until, args.prompt_version))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"report-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(render_console(report))
    print(f"raport local: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
