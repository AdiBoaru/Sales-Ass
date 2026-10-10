"""NX-407 — sonda A/B a efortului de raționament al agentului unic, pe calea web REALĂ.

    PYTHONPATH=. python scripts/sim/agent_effort_ab.py --set tests/golden/prod_sets/<set>.json
    PYTHONPATH=. python scripts/sim/agent_effort_ab.py --set … --efforts low,medium --yes
    PYTHONPATH=. python scripts/sim/agent_effort_ab.py --score reports/nx407/<run>/

De ce: pe toate turele agentului din 9-10 oct, 186 din 189 de apeluri au avut 0 tokeni de
raționament la `low`. Efortul NU se schimbă pe intuiție (D15): fiecare conversație a setului trece
prin `/web/chat` în proces (porți, straturi, agentul, plasa: ca `agent_chat.py`), o dată pe fiecare
efort, cu un vizitator nou. Efortul se pune pe setările procesului între rulări, deci promptul,
meniurile și uneltele sunt aceleași.

Fără `--yes` e dry-run (zero apeluri). Cu `--yes` CONSUMĂ CREDITE și ține conexiuni pe poolerul
Supabase comun cu producția (15 sesiuni): îl pornește Adi, o conversație pe rând, nu în timpul unui
deploy sau al altei rulări. Vizitatorii sunt `web_audit_nx407_*` și se șterg la final (`--keep`
îi păstrează, ca să poată fi citiți în `conversation_traces`).

Ieșirea (`reports/nx407/<stamp>/`, locală): `run.json` (tot: răspunsul, cardurile, sugestiile,
secundele, tokenii de raționament, căderea pe plasă), `blind.md` (perechile OARBE: pe fiecare tur,
răspunsurile celor două eforturi ca A/B, ordinea amestecată), `key.json` (cine e A și cine B, în
alt fișier) și `votes.json` (gol: Adi scrie `A`, `B` sau `=` pe fiecare tur). `--score <dosar>`
aplică regula GO pre-înregistrată în `tasks/stage1/NX-407.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "reports" / "nx407"
EFFORTS = ("none", "low", "medium")
#: Regula GO pre-înregistrată (NX-407): preferința oarbă, latența, căderile.
GO_PREFERENCE = 0.60
GO_P50_EXTRA_S = 3.0
GO_P90_MAX_S = 20.0
GO_EXTRA_FALLBACKS = 1


# --- părțile pure (testate) ----------------------------------------------------------------------


def load_set(path: Path) -> dict[str, Any]:
    from scripts.sim.prod_set_run import load_set as _load  # noqa: PLC0415

    return _load(path)


def percentile(values: list[float], q: float) -> float | None:
    """Percentila `q` (0-1) prin interpolare liniară; `None` pe listă goală. PUR."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pe fiecare efort: p50/p90 ale secundelor pe tur, căderile pe plasă (cu `round_timeout`
    separat), turele cu raționament și media tokenilor de raționament. PUR."""
    out: dict[str, dict[str, Any]] = {}
    for effort, convs in run["efforts"].items():
        turns = [t for c in convs.values() for t in c.get("turns", [])]
        secs = [t["seconds"] for t in turns if t.get("seconds") is not None]
        fallbacks = [t for t in turns if t.get("served_by") == "fallback"]
        reasoning = [t.get("reasoning_tokens") or 0 for t in turns if t.get("served_by") == "agent"]
        out[effort] = {
            "turns": len(turns),
            "p50_s": percentile(secs, 0.5),
            "p90_s": percentile(secs, 0.9),
            "fallbacks": len(fallbacks),
            "round_timeouts": sum(1 for t in fallbacks if t.get("reason") == "round_timeout"),
            "turns_with_reasoning": sum(1 for r in reasoning if r > 0),
            "mean_reasoning_tokens": statistics.fmean(reasoning) if reasoning else None,
        }
    return out


def _render_answer(t: dict[str, Any]) -> str:
    lines = [t.get("content") or "(fără text)"]
    for p in t.get("products") or []:
        reason = f": {p['reason']}" if p.get("reason") else ""
        lines.append(f"  - **{p.get('name')}** · {p.get('price')} lei{reason}")
    if t.get("suggestions"):
        lines.append("  _sugestii:_ " + " | ".join(t["suggestions"]))
    return "\n".join(lines)


def blind_pairs(
    run: dict[str, Any], a: str, b: str, *, seed: int
) -> tuple[str, dict[str, dict[str, str]]]:
    """Perechile oarbe pe fiecare tur comun celor două eforturi: markdown-ul de citit și cheia
    (`{tur: {"A": efort, "B": efort}}`). Ordinea A/B e amestecată per tur, reproductibil pe `seed`.
    Fără secunde și fără tokeni în markdown: doar ce vede clientul. PUR."""
    rng = random.Random(seed)
    key: dict[str, dict[str, str]] = {}
    md = [
        f"# Perechi oarbe ({run.get('set')})",
        "",
        "Pe fiecare tur: `A`, `B` sau `=` în votes.json.",
        "",
    ]
    left, right = run["efforts"].get(a, {}), run["efforts"].get(b, {})
    for conv_id in sorted(set(left) & set(right)):
        md += [f"## {conv_id}", ""]
        for i, (ta, tb) in enumerate(
            zip(left[conv_id].get("turns", []), right[conv_id].get("turns", []), strict=False), 1
        ):
            turn_key = f"{conv_id}#{i}"
            first, second = (a, b) if rng.random() < 0.5 else (b, a)
            key[turn_key] = {"A": first, "B": second}
            by = {a: ta, b: tb}
            md += [
                f"### {turn_key} · client: «{ta.get('message')}»",
                "",
                "**A**",
                "",
                _render_answer(by[first]),
                "",
                "**B**",
                "",
                _render_answer(by[second]),
                "",
            ]
    return "\n".join(md), key


def verdict(
    votes: dict[str, str],
    key: dict[str, dict[str, str]],
    stats: dict[str, dict[str, Any]],
    *,
    base: str,
    candidate: str,
) -> dict[str, Any]:
    """Regula GO pre-înregistrată (NX-407) pentru `candidate` față de `base`. PUR.

    Egalitățile (`=`) și turele fără vot nu intră în numitor. O poartă nemăsurată (niciun vot, o
    latență lipsă) dă `INSUFFICIENT`, niciodată GO."""
    wins = losses = 0
    for turn_key, vote in votes.items():
        side = key.get(turn_key)
        if side is None or vote not in ("A", "B"):
            continue
        if side[vote] == candidate:
            wins += 1
        elif side[vote] == base:
            losses += 1
    decided = wins + losses
    pref = wins / decided if decided else None
    sb, sc = stats.get(base, {}), stats.get(candidate, {})
    gates: dict[str, bool | None] = {
        "preference": None if pref is None else pref >= GO_PREFERENCE,
        "p50": None
        if sb.get("p50_s") is None or sc.get("p50_s") is None
        else sc["p50_s"] <= sb["p50_s"] + GO_P50_EXTRA_S,
        "p90": None if sc.get("p90_s") is None else sc["p90_s"] <= GO_P90_MAX_S,
        "fallbacks": None
        if "fallbacks" not in sb or "fallbacks" not in sc
        else sc["fallbacks"] <= sb["fallbacks"] + GO_EXTRA_FALLBACKS,
    }
    if any(v is None for v in gates.values()):
        result = "INSUFFICIENT"
    else:
        result = "GO" if all(gates.values()) else "NO-GO"
    return {
        "verdict": result,
        "candidate": candidate,
        "base": base,
        "preferred": wins,
        "against": losses,
        "preference": pref,
        "gates": gates,
    }


# --- rularea (credite) ---------------------------------------------------------------------------


async def _conversation_id(business_id: str, visitor_id: str) -> str | None:
    from src.db.connection import admin_conn, get_pool  # noqa: PLC0415

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        row = await conn.fetchrow(
            """
            select c.id::text as id
              from conversations c
              join channel_identities ci
                on ci.contact_id = c.contact_id and ci.business_id = c.business_id
             where c.business_id = $1 and ci.channel_kind = 'webchat' and ci.external_id = $2
             order by c.created_at desc limit 1
            """,
            business_id,
            visitor_id,
        )
    return row["id"] if row else None


async def _turn_events(business_id: str, conversation_id: str) -> list[dict[str, Any]]:
    """Pe fiecare tur al conversației (în ordine): cine l-a servit și tokenii de raționament.
    Filtrat pe conversație, deci traficul real din aceeași fereastră nu se amestecă."""
    from src.db.connection import admin_conn, get_pool  # noqa: PLC0415

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        rows = await conn.fetch(
            """
            select turn_id::text as turn_id, event_type, properties, created_at
              from analytics_events
             where business_id = $1 and conversation_id = $2
               and event_type in ('assistant_turn', 'assistant_fallback', 'turn_latency')
             order by created_at
            """,
            business_id,
            conversation_id,
        )
    turns: dict[str, dict[str, Any]] = {}
    for r in rows:
        props = (
            r["properties"] if isinstance(r["properties"], dict) else json.loads(r["properties"])
        )
        t = turns.setdefault(r["turn_id"], {"served_by": "other_layer"})
        if r["event_type"] == "assistant_turn":
            t.update(served_by="agent", reasoning_tokens=props.get("reasoning_tokens"))
        elif r["event_type"] == "assistant_fallback":
            t.update(served_by="fallback", reason=props.get("reason"))
        elif props.get("phase") != "post_turn":
            t["e2e_ms"] = props.get("e2e_ms")
    return list(turns.values())


async def _run_effort(
    effort: str, convs: list[dict[str, Any]], token: str, business_id: str
) -> dict[str, Any]:
    from scripts.sim import web_audit as wa  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    settings.llm_reasoning_effort_assistant = effort  # același proces, același prompt și meniu
    out: dict[str, Any] = {}
    for conv in convs:
        vid, sig = await wa._session(token, f"nx407_{effort}")  # noqa: SLF001
        client = wa.WebClient(token, vid, sig, f"nx407_{effort}")
        turns = []
        for t in conv["turns"]:
            started = time.monotonic()
            try:
                res = await client.say(t["message"])
                turns.append(
                    {
                        "message": t["message"],
                        "seconds": round(time.monotonic() - started, 2),
                        "content": res.content,
                        "products": [
                            {
                                "name": p.get("name"),
                                "price": p.get("price"),
                                "reason": p.get("reason"),
                            }
                            for p in res.products
                        ],
                        "suggestions": res.suggestions,
                    }
                )
            except Exception as e:  # noqa: BLE001 — un tur picat se raportează, nu oprește setul
                turns.append({"message": t["message"], "error": type(e).__name__})
        cid = await _conversation_id(business_id, vid)
        events = await _turn_events(business_id, cid) if cid else []
        aligned = len(events) == len(turns)
        for turn, ev in zip(turns, events, strict=False):
            turn.update(ev if aligned else {})
        out[conv["id"]] = {
            "visitor_id": vid,
            "conversation_id": cid,
            "aligned": aligned,
            "turns": turns,
        }
        print(f"[{effort}] {conv['id']}: {len(turns)} ture", flush=True)
    return out


async def _run(args: argparse.Namespace, doc: dict[str, Any], convs: list[dict[str, Any]]) -> Path:
    from scripts.sim import web_audit as wa  # noqa: PLC0415
    from src.db.connection import admin_conn, close_pool, get_pool  # noqa: PLC0415

    wa._install_fake_redis()  # noqa: SLF001 — ca auditul: Redis-ul Docker nu e pe host
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "select c.provider_account_id, c.business_id::text as business_id "
            "from channels c join businesses b on b.id = c.business_id "
            "where b.slug = $1 and c.kind = 'webchat' and c.status = 'active' limit 1",
            args.business,
        )
    if row is None:
        raise SystemExit(f"niciun canal webchat activ pe {args.business!r}")
    token, business_id = row["provider_account_id"], row["business_id"]
    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = OUT_DIR / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    run: dict[str, Any] = {"set": doc.get("version"), "started": stamp, "efforts": {}}
    try:
        for effort in efforts:
            run["efforts"][effort] = await _run_effort(effort, convs, token, business_id)
            (out_dir / "run.json").write_text(
                json.dumps(run, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
            )
    finally:
        if not args.keep:
            async with admin_conn(pool) as conn:
                purged = await wa._purge_audit(conn, business_id)  # noqa: SLF001
            print(f"curățat {purged} vizitator(i) de test")
        await close_pool()
    run["summary"] = summary(run)
    (out_dir / "run.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    if len(efforts) == 2:
        md, key = blind_pairs(run, efforts[0], efforts[1], seed=args.seed)
        (out_dir / "blind.md").write_text(md, encoding="utf-8")
        (out_dir / "key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
        (out_dir / "votes.json").write_text(
            json.dumps({k: "" for k in key}, indent=1), encoding="utf-8"
        )
    return out_dir


def _score(folder: Path, base: str, candidate: str) -> dict[str, Any]:
    run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    key = json.loads((folder / "key.json").read_text(encoding="utf-8"))
    votes = json.loads((folder / "votes.json").read_text(encoding="utf-8"))
    stats = run.get("summary") or summary(run)
    return {"summary": stats, **verdict(votes, key, stats, base=base, candidate=candidate)}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set", type=Path)
    p.add_argument("--business", default="sole-ro")
    p.add_argument("--efforts", default="low,medium")
    p.add_argument("--only", default="", help="id-uri de conversație, separate prin virgulă")
    p.add_argument("--seed", type=int, default=407)
    p.add_argument("--keep", action="store_true", help="nu șterge vizitatorii de test")
    p.add_argument("--yes", action="store_true", help="rulează modelul (consumă credite)")
    p.add_argument("--final", action="store_true", help="permite un set nevăzut (heldout-*)")
    p.add_argument("--score", type=Path, help="dosarul unei rulări: aplică regula GO")
    p.add_argument("--base", default="low")
    p.add_argument("--candidate", default="medium")
    args = p.parse_args(argv)
    if args.score:
        print(
            json.dumps(_score(args.score, args.base, args.candidate), indent=1, ensure_ascii=False)
        )
        return 0
    if args.set is None:
        p.error("--set e obligatoriu (sau --score <dosar>)")
    if args.set.name.startswith("heldout-") and not args.final:
        p.error(f"{args.set.name} e setul nevăzut: se rulează doar la verdictul final (--final)")
    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    if not efforts or any(e not in EFFORTS for e in efforts) or len(set(efforts)) != len(efforts):
        p.error(f"--efforts: valori distincte din {EFFORTS}")
    doc = load_set(args.set)
    convs = doc["conversations"]
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        convs = [c for c in convs if c["id"] in wanted]
    n_turns = sum(len(c["turns"]) for c in convs)
    if not args.yes:
        print(
            f"dry-run: {doc.get('version')} · {len(convs)} conversații · {n_turns} ture × "
            f"{len(efforts)} eforturi ({', '.join(efforts)}) = "
            f"{n_turns * len(efforts)} ture de agent"
        )
        print("rularea reală: adaugă --yes (o pornește Adi, consumă credite, ține poolerul comun)")
        return 0
    try:
        from dotenv import load_dotenv  # noqa: PLC0415

        load_dotenv()
    except ImportError:
        pass
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    out_dir = asyncio.run(_run(args, doc, convs))
    print(f"raport: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
