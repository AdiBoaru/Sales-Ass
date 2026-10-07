"""Sonda de efort a interpretării: aceeași cerere de interpretare, pe `none` și pe `low`.

    PYTHONPATH=. python scripts/sim/interpret_effort_probe.py --set-report <raport>.json
    PYTHONPATH=. python scripts/sim/interpret_effort_probe.py --set-report <raport>.json --yes

Întrebarea: câte etichete greșite ale interpretării (act, referință, schimbare) le repară doar
raționamentul? Cererea NU se reconstruiește de mână. Fiecare tur din setul rulat pe producție se
rejoacă local cu `trace_replay.replay_turn` (0 $, DB read-only, aceeași funcție ca NX-366), iar
clientul fals capturează `kwargs`-urile apelului `turn_interpretation`, construite de codul de
producție din starea REALĂ de dinaintea turului. Deci fiecare tur e izolat: o greșeală la turul 2
nu se propagă în cererea turului 3 (starea vine din înregistrare).

Fără `--yes`: doar captura și numărătoarea (zero apeluri de model). Cu `--yes` (consumă credite, o
pornește Adi), fiecare cerere pleacă de două ori prin `LLMClient.complete_schema_raw`, aceeași cale
ca adaptorul, deci regulile lui `_sampling` pentru temperatură și raționament se aplică la fel:
brațul `none` (al doilea eșantion al efortului de azi, ca să se vadă zgomotul față de înregistrare)
și brațul `low`, cu ordinea amestecată per tur. Raportul local
(`reports/nx366/interpret-effort-<stamp>.json`) are interpretarea brută a celor trei: înregistrată,
`none`, `low`. Verdictul „corect/greșit" nu e al scriptului: se judecă pe `expect`-ul setului.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.evals import trace_replay as tr  # noqa: E402

OUT_DIR = ROOT / "reports" / "nx366"
SCHEMA_NAME = "turn_interpretation"
ROWS_SQL = """
select turn_id::text as turn_id, conversation_id::text as conversation_id,
       business_id::text as business_id, created_at, client_text, diagnostics
from conversation_traces
where business_id = $1::uuid and conversation_id = any($2::uuid[])
order by conversation_id, created_at
"""
BUSINESS_SQL = "select id::text from businesses where slug = $1 or id::text = $1"


class _Capturing(tr.RecordedModel):
    """Clientul replay-ului, plus captura cererii de interpretare (prima pe tur)."""

    captured: list[dict[str, Any]] = []

    def next(self, endpoint: str, kwargs: dict[str, Any]) -> Any:
        rf = kwargs.get("response_format") or {}
        if (
            endpoint == "chat.completions"
            and (rf.get("json_schema") or {}).get("name") == SCHEMA_NAME
        ):
            rec = self._calls[self.cursor] if self.cursor < len(self._calls) else None
            content = None
            if rec and rec.get("ok"):
                content = rec["response"]["choices"][0]["message"]["content"]
            _Capturing.captured.append({"kwargs": kwargs, "recorded": content})
        return super().next(endpoint, kwargs)


def summarize(content: str | None) -> str:
    """Interpretarea ca o linie: fir, acte, referințe, schimbări. PUR."""
    if not content:
        return "∅"
    try:
        d = json.loads(content)
    except ValueError:
        return "invalid_json"
    acts = ", ".join(
        f"{a.get('kind')}{'[' + '/'.join(a.get('targets') or []) + ']' if a.get('targets') else ''}"
        for a in d.get("acts") or []
    )
    refs = ", ".join(
        f"{r.get('id')}={r.get('kind')}:{r.get('name') or r.get('ordinal') or r.get('text')}"
        for r in d.get("references") or []
    )
    changes = ", ".join(
        f"{c.get('op')} {c.get('dimension')} {c.get('relation') or ''} "
        f"{c.get('value') if c.get('value') is not None else c.get('number')}"
        f"{' vs ' + c['relative_to'] if c.get('relative_to') else ''}".strip()
        for c in d.get("changes") or []
    )
    return f"{d.get('thread')} | acts: {acts} | refs: {refs or '—'} | changes: {changes or '—'}"


async def capture(report: dict[str, Any], business: str) -> list[dict[str, Any]]:
    """Rejoacă fiecare tur al setului (0 $) și întoarce cererea de interpretare a fiecăruia."""
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415

    labels = {v["conversation_id"]: k for k, v in report["conversations"].items()}
    pool = await get_pool()
    out: list[dict[str, Any]] = []
    tr.RecordedModel = _Capturing  # type: ignore[misc] — replay_turn își construiește clientul
    try:
        async with admin_conn(pool) as conn:
            business_id = await conn.fetchval(BUSINESS_SQL, business)
        async with tenant_conn(business_id) as conn:
            rows = [dict(r) for r in await conn.fetch(ROWS_SQL, business_id, list(labels))]
        index: dict[str, int] = {}
        for row in rows:
            label = labels[row["conversation_id"]]
            index[label] = index.get(label, 0) + 1
            _Capturing.captured = []
            res = await tr.replay_turn(row)
            cap = _Capturing.captured[0] if _Capturing.captured else None
            out.append(
                {
                    "label": f"{label}:T{index[label]}",
                    "turn_id": row["turn_id"],
                    "client": row["client_text"],
                    "replay": res.status,
                    "kwargs": cap["kwargs"] if cap else None,
                    "recorded": cap["recorded"] if cap else None,
                }
            )
            print(
                f"{out[-1]['label']:<28} replay={res.status:<10} capturat={'da' if cap else 'NU'}"
            )
    finally:
        await close_pool()
    return out


async def run_arms(turns: list[dict[str, Any]], efforts: list[str], seed: int) -> None:
    from src.agent.llm import get_llm  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415

    llm = get_llm()
    temp = get_settings().llm_temperature_interpret
    rng = random.Random(seed)
    for t in turns:
        kw = t["kwargs"]
        if kw is None:
            continue
        msgs = {m["role"]: m["content"] for m in kw["messages"]}
        order = list(efforts)
        rng.shuffle(order)  # al doilea apel prinde cache-ul primului (tiparul NX-312 felia 5)
        t["arms"] = {}
        for effort in order:
            t0 = time.monotonic()
            try:
                reply = await llm.complete_schema_raw(
                    msgs["system"],
                    msgs["user"],
                    kw["response_format"]["json_schema"],
                    model=kw.get("model"),
                    reasoning_effort=effort,
                    temperature=temp,
                )
                t["arms"][effort] = {
                    "content": reply.content,
                    "finish": reply.finish_reason,
                    "ms": round((time.monotonic() - t0) * 1000),
                }
            except Exception as e:  # noqa: BLE001 — un braț picat se raportează, setul continuă
                t["arms"][effort] = {"error": type(e).__name__, "ms": None}
        print(f"\n{t['label']}  „{t['client']}”")
        print(f"   înregistrat: {summarize(t['recorded'])}")
        for effort in efforts:
            arm = t["arms"][effort]
            body = summarize(arm.get("content")) if "error" not in arm else f"EROARE {arm['error']}"
            print(f"   {effort:<11} ({arm['ms']} ms): {body}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set-report", required=True, type=Path, help="raportul `prod_set_run.py`")
    p.add_argument("--business", default="sole-ro")
    p.add_argument("--efforts", default="none,low")
    p.add_argument(
        "--only", default="", help="etichete separate prin virgulă, ex. f3_nume_exact:T2"
    )
    p.add_argument("--seed", type=int, default=366)
    p.add_argument("--yes", action="store_true", help="trimite cererile (consumă credite)")
    args = p.parse_args(argv)

    report = json.loads(args.set_report.read_text(encoding="utf-8"))
    turns = asyncio.run(capture(report, args.business))
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        turns = [t for t in turns if t["label"] in wanted]
    ok = [t for t in turns if t["kwargs"] is not None]
    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    chars = sum(len(m["content"]) for t in ok for m in t["kwargs"]["messages"])
    print(
        f"\n{len(ok)} cereri de interpretare capturate din {len(turns)} ture · "
        f"{len(efforts)} brațe = {len(ok) * len(efforts)} apeluri · ~{chars // 4:,} tokeni de "
        f"intrare pe braț (în mare parte din cache)"
    )
    if not args.yes:
        print("dry-run: niciun apel de model. Rularea reală: adaugă --yes (o pornește Adi).")
        return 0
    asyncio.run(run_arms(ok, efforts, args.seed))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"interpret-effort-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    for t in turns:
        t.pop("kwargs", None)  # promptul întreg nu intră în raport, doar interpretările
    out.write_text(json.dumps(turns, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nraport local: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
