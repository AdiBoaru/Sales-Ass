"""NX-311 — proba de latență: unde se duce timpul unui tur și cât din el e ceasul NOSTRU.

Read-only: citește `analytics_events` (`turn_latency`, `llm_usage`) și `conversation_traces` pe
tenant. Nu apelează niciun model, nu scrie nimic, nu consumă credite.

Răspunde la cele patru întrebări pe care s-a construit cardul:

  1. cât durează un tur, și cât din el e apelul de model;
  2. ce separă turele de 100-120s de cele de 20s (răspunsul: `llm_retry`, nu „modelul e lent");
  3. care e CAUZA retry-ului — după fix, codurile `llm_retry_{timeout,status,connection}` o spun
     direct; înainte de fix se deduce din `conversation_traces.diagnostics.rich_error`;
  4. cât de CENZURATĂ e măsurătoarea: sub vechiul perete de 30s, maximul observat al apelului de
     compunere era exact 30,1s — nu coada distribuției, ci zidul. `llm_call_over_30s` e semnalul
     care ridică cenzura, deci raportul îl afișează separat ca „acoperire" a recalibrării.

    python scripts/llm_call_budget_probe.py [--business sole-ro] [--days 30]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from typing import Any

# NX-312: inițializarea procesului (`.env`, politica de event loop, encodingul consolei) stă sub
# `__main__`, nu la import. `per_call_summary` e testat din suită, iar un `load_dotenv()` la import
# ar aprinde `.env`-ul local peste toate testele care rulează după el.
from src.db.connection import admin_conn, get_pool

#: Cheile pe care `conversation_traces.diagnostics` le poartă când turul a ÎNCERCAT compunerea
#: bogată. Prezența oricăreia = apelul care raționează a rulat; `rich_error` = a murit de tot.
_RICH_KEYS = {"rich_raw", "rich_from_facts", "rich_downgraded", "rich_error"}

#: Vechiul perete. Pragul de cenzură, nu un prag de configurație — vezi `llm._SLOW_CALL_AFTER_MS`.
_OLD_WALL_MS = 30_000


def _props(value: Any) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * q)))
    return ordered[idx]


def _fmt_s(ms: float) -> str:
    return f"{ms / 1000:.1f}s"


def _phase(props: dict, name: str) -> dict:
    return (props.get("phases") or {}).get(name) or {}


def per_call_summary(rows: list[dict]) -> dict[tuple[str, bool], dict[str, float]]:
    """NX-312 — rândurile `llm_usage.per_call`, grupate pe (formă, raționează). PUR.

    Doar apelurile REUȘITE intră în percentile: un apel mort după 90s de retry nu e durata unui
    apel, e durata unui eșec, iar amestecate ar muta p90 fără să spună de ce. Eșecurile se
    numără separat (`failed`)."""
    groups: dict[tuple[str, bool], list[dict]] = {}
    failed: Counter = Counter()
    for r in rows:
        key = (str(r.get("shape") or "text"), bool(r.get("reasoning")))
        if not r.get("ok", True):
            failed[key] += 1
            continue
        groups.setdefault(key, []).append(r)
    out: dict[tuple[str, bool], dict[str, float]] = {}
    for key in set(groups) | set(failed):
        g = groups.get(key, [])
        uncached = [max(0, (r.get("tokens_in") or 0) - (r.get("cached") or 0)) for r in g]
        out[key] = {
            "n": len(g),
            "failed": failed[key],
            "ms_p50": _pct([r.get("ms") or 0 for r in g], 0.5),
            "ms_p90": _pct([r.get("ms") or 0 for r in g], 0.9),
            "in_p50": _pct([r.get("tokens_in") or 0 for r in g], 0.5),
            "uncached_p50": _pct(uncached, 0.5),
            "out_p50": _pct([r.get("tokens_out") or 0 for r in g], 0.5),
            "reasoning_p50": _pct([r.get("reasoning_tokens") or 0 for r in g], 0.5),
            "ms_per_1k_uncached": _slope(uncached, [r.get("ms") or 0 for r in g]),
        }
    return out


def _slope(xs: list[float], ys: list[float]) -> float | None:
    """Panta celor mai mici pătrate, în ms per 1.000 de tokeni NECACHE-UIȚI. `None` sub 5 puncte
    sau fără variație pe x: o pantă pe trei apeluri e zgomot care arată ca o cifră.

    Răspunde la întrebarea pe care stă felia 3 a NX-312: cât cumpără, în timp, tăierea inputului
    unei runde de buclă. Dacă panta e mică, input mai mic nu înseamnă apel mai rapid."""
    n = len(xs)
    if n < 5:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return 1000.0 * cov / var


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    # `bot_runtime` are DOAR INSERT pe `analytics_events` (append-only, 003) → proba (tool de
    # SUPORT/ops, nu runtime) citește pe `admin_conn`, exact ca `scripts/turn_replay.py`. Izolarea
    # rămâne în COD: fiecare query filtrează explicit `business_id` (P7). Zero PII în output —
    # se citesc doar durate și coduri de degradare, niciun text de client.
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        row = await conn.fetchrow("select id from businesses where slug = $1", args.business)
        if row is None:
            print(f"tenant necunoscut: {args.business}")
            return 2
        business_id = str(row["id"])
        lat_rows = await conn.fetch(
            """
            select turn_id, properties
              from analytics_events
             where business_id = $1
               and event_type = 'turn_latency'
               and created_at > now() - ($2::int || ' days')::interval
            """,
            business_id,
            args.days,
        )
        diag_rows = await conn.fetch(
            """
            select turn_id, diagnostics
              from conversation_traces
             where business_id = $1
               and created_at > now() - ($2::int || ' days')::interval
            """,
            business_id,
            args.days,
        )
        usage_rows = await conn.fetch(
            """
            select properties
              from analytics_events
             where business_id = $1
               and event_type = 'llm_usage'
               and created_at > now() - ($2::int || ' days')::interval
            """,
            business_id,
            args.days,
        )

    # Evenimentul de aftercare (`phase=post_turn`) NU e turul pe care îl așteaptă clientul.
    live: dict[Any, dict] = {}
    for r in lat_rows:
        props = _props(r["properties"])
        if props.get("phase") != "post_turn":
            live[r["turn_id"]] = props
    diags = {r["turn_id"]: _props(r["diagnostics"]) for r in diag_rows}

    if not live:
        print(f"zero ture live pe {args.business} în ultimele {args.days} zile — nimic de măsurat")
        return 0

    print(f"tenant {args.business} · {args.days} zile · {len(live)} ture live\n")

    # ── 1. Turul, și cât din el e modelul ─────────────────────────────────────────────────────
    e2e = [p.get("e2e_ms") or 0 for p in live.values()]
    model_ms = [_phase(p, "model").get("ms") or 0 for p in live.values()]
    print("TURUL")
    print(
        f"  e2e            p50 {_fmt_s(_pct(e2e, 0.5)):>7} · p90 {_fmt_s(_pct(e2e, 0.9)):>7}"
        f" · max {_fmt_s(max(e2e)):>7}"
    )
    print(f"  model, agregat {100 * sum(model_ms) / max(1, sum(e2e)):.1f}% din e2e")

    # ── 2. Ce separă turele lente ─────────────────────────────────────────────────────────────
    def _retried(p: dict) -> bool:
        return bool((p.get("degradations") or {}).get("llm_retry"))

    with_retry = [p.get("e2e_ms") or 0 for p in live.values() if _retried(p)]
    without = [p.get("e2e_ms") or 0 for p in live.values() if not _retried(p)]
    print("\nCE SEPARA TURELE LENTE: retry-ul, nu viteza modelului")
    for label, vals in (("CU retry", with_retry), ("FĂRĂ retry", without)):
        if vals:
            print(
                f"  {label:<11} n={len(vals):>3} · p50 {_fmt_s(_pct(vals, 0.5)):>7}"
                f" · p90 {_fmt_s(_pct(vals, 0.9)):>7} · max {_fmt_s(max(vals)):>7}"
            )

    # ── 3. Cauza ──────────────────────────────────────────────────────────────────────────────
    causes: Counter = Counter()
    for p in live.values():
        for code, n in (p.get("degradations") or {}).items():
            if code.startswith("llm_retry_") and code != "llm_retry_no_budget":
                causes[code] += n
    print("\nCAUZA RETRY-ULUI")
    if causes:
        for code, n in causes.most_common():
            print(f"  {code:<24} {n}")
    else:
        print("  codurile de cauză nu apar încă (trafic dinainte de NX-311).")
        rich_errors = Counter(
            str(d.get("rich_error"))[:60] for d in diags.values() if d.get("rich_error")
        )
        if rich_errors:
            print("  dedus din `diagnostics.rich_error`:")
            for name, n in rich_errors.most_common():
                print(f"    {n}x  {name}")

    # ── 4. Cenzura ────────────────────────────────────────────────────────────────────────────
    attempted = [t for t, d in diags.items() if _RICH_KEYS & set(d) and t in live]
    died = [t for t in attempted if (diags.get(t) or {}).get("rich_error")]
    retried = [t for t in attempted if _retried(live[t])]
    over_wall = sum(
        (p.get("degradations") or {}).get("llm_call_over_30s", 0) for p in live.values()
    )

    print("\nAPELUL CARE RAȚIONEAZĂ (compunerea răspunsului)")
    if attempted:
        print(f"  ture care l-au încercat:            {len(attempted)}")
        print(
            f"  cu cel puțin un retry:              {len(retried)}"
            f"  ({100 * len(retried) / len(attempted):.1f}%)"
        )
        print(
            f"  moarte de tot (răspuns degradat):   {len(died)}"
            f"  ({100 * len(died) / len(attempted):.1f}%)"
        )

    # Estimarea duratei: model total − celelalte runde la p50-ul turelor fără compunere bogată.
    plain_per_call = []
    for turn_id, p in live.items():
        if _RICH_KEYS & set(diags.get(turn_id) or {}):
            continue
        m = _phase(p, "model")
        if m.get("n"):
            plain_per_call.append((m.get("ms") or 0) / m["n"])
    baseline = _pct(plain_per_call, 0.5) if plain_per_call else 0.0
    est = []
    for turn_id in attempted:
        if turn_id in retried:
            continue  # cu retry, „durata" ar include încercările tăiate
        m = _phase(live[turn_id], "model")
        if m.get("n"):
            est.append(max(0.0, (m.get("ms") or 0) - baseline * (m["n"] - 1)))
    if est:
        print(
            f"\n  durata estimată (rundă simplă = {_fmt_s(baseline)}):"
            f"  p50 {_fmt_s(_pct(est, 0.5))} · p75 {_fmt_s(_pct(est, 0.75))}"
            f" · p90 {_fmt_s(_pct(est, 0.9))} · max {_fmt_s(max(est))}"
        )
        if max(est) <= _OLD_WALL_MS * 1.05 and over_wall == 0:
            print(
                "  ⚠ CENZURAT: maximul se oprește la vechiul perete de 30s, iar\n"
                "    `llm_call_over_30s` nu s-a emis încă. Coada reală e NEMĂSURATĂ:\n"
                "    pragul rămâne o primă calibrare, nu un adevăr."
            )

    print(f"\n  apeluri peste vechiul perete (`llm_call_over_30s`): {over_wall}")
    if over_wall:
        print("    ↑ cifra care ridică cenzura: atât ar fi fost tăiat de pragul de dinainte.")

    # ── 5. Pe apel (NX-312) ───────────────────────────────────────────────────────────────────
    # Totalul pe tur nu spune CARE apel a costat. Rândurile `per_call` o spun direct, fără scădere.
    rows: list[dict] = []
    for r in usage_rows:
        props = _props(r["properties"])
        if props.get("phase") == "turn":
            rows.extend(props.get("per_call") or [])
    print("\nPE APEL (NX-312, doar turul, fără aftercare)")
    if not rows:
        print("  niciun rând `per_call` încă: trafic dinainte de NX-312.")
        return 0
    summary = per_call_summary(rows)
    print("  formă    raționează    n  eșuate    ms p50    ms p90   in p50  necache p50  out p50")
    for (shape, reasoning), s in sorted(summary.items()):
        print(
            f"  {shape:<8} {('da' if reasoning else 'nu'):<10} {s['n']:>4} {s['failed']:>7}"
            f" {s['ms_p50']:>9.0f} {s['ms_p90']:>9.0f} {s['in_p50']:>8.0f}"
            f" {s['uncached_p50']:>12.0f} {s['out_p50']:>8.0f}"
        )
    tools = summary.get(("tools", False))
    if tools is not None:
        slope = tools["ms_per_1k_uncached"]
        if slope is None:
            print("\n  rundă de buclă: prea puține apeluri pentru o pantă (sub 5).")
        else:
            print(
                f"\n  rundă de buclă: {slope:+.0f} ms per 1.000 de tokeni necache-uiți"
                " (cât cumpără tăierea inputului, NX-312 felia 3)"
            )
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    raise SystemExit(asyncio.run(main()))
