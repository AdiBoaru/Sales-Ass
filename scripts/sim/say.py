"""CLI subțire pentru harness-ul de conversație: trimite UN mesaj de client către driverul
warm (scripts/sim/server.py) și printează răspunsul botului + metadate, ca JSON compact.

Starea conversației persistă în DB pe `--sender` (același sender = aceeași conversație, istoric
inclus). Folosit de agenții-persona ca să poarte conversații multi-tur fără să se lupte cu
quoting-ul curl/JSON.

Exemple:
  python scripts/sim/say.py --sender "sim:run:ten-gras" --text "buna, aveti creme de fata?"
  python scripts/sim/say.py --sender "sim:run:foo" --text "si mai ieftin?" --pretty
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8099/turn"


def _fmt_usd(v: float) -> str:
    """Cost în USD cu suficiente zecimale ca să nu apară $0.00 pentru un mesaj ieftin.
    (Aceeași convenție ca scripts/sim/cost_report.py: 0 → $0.0000, mic → 6 zecimale.)"""
    return f"${v:.6f}" if 0 < v < 0.01 else f"${v:.4f}"


def _cost_line(data: dict) -> str | None:
    """Linia de consum afișată la FIECARE mesaj (cost-obs NX-103): tokeni in/out, cached + economia,
    apeluri, cost reply vs cost tur (cu fundal), cumulat pe conversație, latență, defalcare/stagiu.
    None dacă turul n-a folosit LLM (free-layer/cache/welcome) → afișăm că a fost gratis."""
    u = data.get("usage")
    tot = data.get("usage_total") or {}
    conv_cost = float(tot.get("cost_usd") or 0.0)
    if not u:
        suffix = f" · Σconv {_fmt_usd(conv_cost)}" if conv_cost else ""
        return f"💸 fără LLM (0 tokeni, $0) — servit din cache/free-layer{suffix}"
    cached = int(u.get("cached_tokens") or 0)
    tin = int(u.get("tokens_in") or 0)
    # rata de cache (cât din input a venit din cache), NU o reducere de cost — economia în $ e
    # afișată separat ca „economie cache" (tokenii cached costă ~10% din input, nu 0).
    pct = f" = {round(100 * cached / tin)}% din input" if cached and tin else ""
    cache_part = f" (cached {cached:,}{pct})" if cached else ""
    parts = [
        f"💸 in {tin:,}{cache_part}",
        f"out {int(u.get('tokens_out') or 0):,}",
        f"{int(u.get('llm_calls') or 0)} apeluri",
        f"reply {_fmt_usd(float(u.get('reply_cost_usd') or 0.0))}",
        f"tur {_fmt_usd(float(u.get('total_cost_usd') or 0.0))}",
        f"Σconv {_fmt_usd(conv_cost)}",
        f"{data.get('latency_ms', 0)}ms",
    ]
    if float(u.get("savings_usd") or 0.0) > 0:
        parts.insert(1, f"economie cache {_fmt_usd(float(u['savings_usd']))}")
    line = "💸 " + " · ".join(p.replace("💸 ", "") for p in parts)
    by_stage = u.get("by_stage") or {}
    if by_stage:
        stages = " · ".join(
            f"{name}:{_fmt_usd(float(row.get('cost_usd') or 0.0))}"
            for name, row in sorted(by_stage.items(), key=lambda kv: -(kv[1].get("cost_usd") or 0))
        )
        line += f"\n   ├ stagii: {stages}"
    return line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sender", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--content-type", default="text")
    ap.add_argument("--pretty", action="store_true")
    a = ap.parse_args()

    body = json.dumps({"sender": a.sender, "text": a.text, "content_type": a.content_type}).encode(
        "utf-8"
    )
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 — localhost cunoscut
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1

    out = {
        "reply": data.get("reply"),
        "route": data.get("route"),
        "conversation_id": data.get("conversation_id"),
        "latency_ms": data.get("latency_ms"),
        "events": data.get("events"),
        "deduped": data.get("deduped"),
        "usage": data.get("usage"),  # cost-obs NX-103: consumul acestui mesaj
        "usage_total": data.get("usage_total"),  # cumulat pe conversație
    }
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    # Linia de consum pe STDERR (vizibilă în terminal, fără să strice JSON-ul de pe stdout pe
    # care îl parsează agenții-persona). „Foarte bine afișat la fiecare mesaj" — exact aici.
    cost = _cost_line(data)
    if cost:
        print(cost, file=sys.stderr)
    print(json.dumps(out, ensure_ascii=False, indent=2 if a.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
