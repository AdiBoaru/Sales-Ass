"""NX-366 — rulează un set de conversații de test pe API-ul de producție, ca widgetul.

    PYTHONPATH=. python scripts/sim/prod_set_run.py --set tests/golden/prod_sets/<set>.json
    PYTHONPATH=. python scripts/sim/prod_set_run.py --set … --yes      # rularea reală (credite!)

Fără `--yes` e dry-run: numără conversațiile și mesajele și nu trimite nimic. Cu `--yes`, fiecare
conversație primește o sesiune nouă (`/web/bootstrap`), iar mesajele merg pe `/web/chat`, exact ca
din widget; conversațiile rulează câte `--parallel` deodată, mesajele unei conversații în ordine.
Turele ajung în `conversation_traces` cu captura NX-366 (dacă e aprinsă pe producție), deci după
rulare se rejoacă cu `scripts/trace_replay.py --conversation <id>` la 0 $.

Costul îl plătește cheia de producție: ~0,001-0,002 $ pe tur pe `gpt-6-luna` (măsurat pe setul din
2026-10-01). Rularea o pornește Adi.

Ieșirea (`reports/nx366/set-<versiune>-<stamp>.json`, locală): pentru fiecare conversație,
`visitor_id`, `conversation_id` (rezolvat din DB după rulare) și fiecare tur cu statusul HTTP,
secundele și răspunsul widgetului. Fereastra rulării e în raport, ca turele să poată fi excluse din
raportul canary și din detectorii NX-363.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "reports" / "nx366"
BASE = "https://bot.nativextech.com"  # API-ul; `demo.*` e vitrina și răspunde HTML la orice cale
ORIGIN = "https://demo.nativextech.com"
#: Tokenul PUBLIC al widgetului `sole-ro` (`data-token`, nu e secret: e în pagina magazinului).
TOKEN = "pub_b738dd1aa2ff2e0535b491792cc789d9"


def load_set(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    for conv in doc["conversations"]:
        if not conv.get("id") or not conv.get("turns"):
            raise SystemExit(f"conversație fără id sau fără ture în {path}")
    return doc


#: Câte reluări primește o cerere oprită la proxy și cât se așteaptă între ele. Traefik scoate
#: singurul backend din rotație după o probă `/health/ready` picată, până la proba următoare
#: (10 s), și răspunde atunci „Service Unavailable" ca text simplu (2026-10-09: două rulări au
#: pierdut 11 și 18 conversații așa). O astfel de cerere n-a ajuns la aplicație, deci reluarea nu
#: dublează nimic.
PROXY_RETRIES = 3
PROXY_WAIT_S = 12.0


def _proxy_unavailable(r: Any) -> bool:
    """503 de la proxy (text simplu), nu de la aplicație (care răspunde JSON)."""
    return r.status_code == 503 and not r.headers.get("content-type", "").startswith(
        "application/json"
    )


def _send(fn: Any, *args: Any, **kwargs: Any) -> Any:
    for attempt in range(PROXY_RETRIES + 1):
        r = fn(*args, **kwargs)
        if not _proxy_unavailable(r) or attempt == PROXY_RETRIES:
            return r
        print(f"  proxy 503, reiau în {PROXY_WAIT_S:.0f} s", flush=True)
        time.sleep(PROXY_WAIT_S)
    return r


def _run_conversation(base: str, token: str, conv: dict[str, Any]) -> dict[str, Any]:
    import httpx  # noqa: PLC0415 — doar rularea reală are nevoie de rețea

    headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
    turns: list[dict[str, Any]] = []
    with httpx.Client(timeout=200) as c:
        try:
            s = _send(
                c.get, f"{base}/web/bootstrap", params={"token": token}, headers=headers
            ).json()
            s["token"], s["visitor_id"], s["sig"]  # noqa: B018 — forma sesiunii, verificată aici
        except Exception as e:  # noqa: BLE001 — conversația pică, setul continuă și se raportează
            return {"visitor_id": None, "error": f"bootstrap: {type(e).__name__}", "turns": []}
        for t in conv["turns"]:
            started = datetime.now(UTC).isoformat()
            t0 = time.monotonic()
            try:
                r = _send(
                    c.post,
                    f"{base}/web/chat",
                    headers=headers,
                    json={
                        "token": s["token"],
                        "visitor_id": s["visitor_id"],
                        "sig": s["sig"],
                        "message": t["message"],
                    },
                )
                status = r.status_code
                try:
                    body = r.json()
                except ValueError:
                    body = {"raw": r.text[:2000]}
            except Exception as e:  # noqa: BLE001 — un tur picat se raportează, nu oprește setul
                status, body = None, {"error": type(e).__name__}
            turns.append(
                {
                    "message": t["message"],
                    "started": started,
                    "status": status,
                    "seconds": round(time.monotonic() - t0, 1),
                    "response": body,
                }
            )
            print(f"{conv['id']} | {t['message']!r} -> {status}", flush=True)
    return {"visitor_id": s["visitor_id"], "turns": turns}


async def _resolve_conversations(business: str, visitors: dict[str, str]) -> dict[str, str]:
    """`visitor_id` → `conversation_id`, pe conexiunea de operator (read-only, `business_id`)."""
    from src.db.connection import admin_conn, close_pool, get_pool  # noqa: PLC0415

    pool = await get_pool()
    out: dict[str, str] = {}
    try:
        async with admin_conn(pool) as conn:
            business_id = await conn.fetchval(
                "select id::text from businesses where slug = $1 or id::text = $1", business
            )
            for conv_id, vid in visitors.items():
                cid = await conn.fetchval(
                    """
                    select cv.id::text from channel_identities ci
                      join conversations cv
                        on cv.contact_id = ci.contact_id and cv.business_id = ci.business_id
                     where ci.business_id = $1::uuid and ci.external_id = $2
                     order by cv.created_at desc limit 1
                    """,
                    business_id,
                    vid,
                )
                if cid:
                    out[conv_id] = cid
    finally:
        await close_pool()
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set", required=True, type=Path)
    p.add_argument("--business", default="sole-ro")
    p.add_argument("--base", default=BASE)
    p.add_argument("--token", default=TOKEN)
    p.add_argument("--parallel", type=int, default=3)
    p.add_argument(
        "--only",
        default="",
        help="id-uri de conversație separate prin virgulă (reluarea celor picate dintr-o rulare)",
    )
    p.add_argument("--yes", action="store_true", help="rulează pe producție (consumă credite)")
    p.add_argument(
        "--final",
        action="store_true",
        help="permite rularea unui set nevăzut (heldout-*), o singură dată, pentru verdictul final",
    )
    args = p.parse_args(argv)
    if args.set.name.startswith("heldout-") and not args.final:
        p.error(
            f"{args.set.name} e setul nevăzut: se rulează o singură dată, la verdictul final "
            "(--final), nu în timpul reparațiilor"
        )
    doc = load_set(args.set)
    convs = doc["conversations"]
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        unknown = wanted - {c["id"] for c in convs}
        if unknown:
            p.error(f"id-uri necunoscute în set: {sorted(unknown)}")
        convs = [c for c in convs if c["id"] in wanted]
    n_turns = sum(len(c["turns"]) for c in convs)
    if not args.yes:
        print(f"dry-run: {doc.get('version')} · {len(convs)} conversații · {n_turns} ture")
        print("rularea reală: adaugă --yes (o pornește Adi, consumă credite pe producție)")
        return 0
    started = datetime.now(UTC).isoformat()
    results: dict[str, Any] = {}
    lock = threading.Lock()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"set-{doc.get('version')}-{stamp}.json"
    report: dict[str, Any] = {
        "set": doc.get("version"),
        "base": args.base,
        "window": {"started": started, "ended": None},
        "conversations": results,
    }

    def save() -> None:
        # Scris după FIECARE conversație: o rulare întreruptă păstrează ce s-a plătit deja.
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )

    def one(conv: dict[str, Any]) -> None:
        try:
            res = _run_conversation(args.base, args.token, conv)
        except Exception as e:  # noqa: BLE001 — o conversație picată nu oprește setul
            res = {"visitor_id": None, "error": type(e).__name__, "turns": []}
        with lock:
            results[conv["id"]] = res
            save()

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
        list(ex.map(one, convs))
    ended = datetime.now(UTC).isoformat()
    report["window"]["ended"] = ended
    visitors = {k: v["visitor_id"] for k, v in results.items() if v.get("visitor_id")}
    for conv_id, cid in asyncio.run(_resolve_conversations(args.business, visitors)).items():
        results[conv_id]["conversation_id"] = cid
    save()
    print(f"fereastra: {started} → {ended}")
    print(f"raport local: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
