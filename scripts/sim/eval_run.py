"""NX-180 — evaluatorul conversațional + BASELINE, pe calea `/web/chat` REALĂ.

Rulează un set de conversații reprezentative (`qa-suite/conversations/*.json`), fiecare de N ori
(default 3), prin `web_chat()`, și produce un artefact `reports/eval-*.json`.
Fiecare tur e evaluat de: gate-uri DETERMINISTE (`eval_gates`, pure) + un judge LLM (`eval_judge`).

RIGLA, nu poarta: baseline-ul ÎNREGISTREAZĂ realitatea (inclusiv eșecuri) — nu întoarce „verde".
Judge-ul NU poate anula un eșec determinist (P2). Cache OPRIT (pe `settings.cache_enabled`, doar pe
durata rulării, restaurat după) + state RESETAT (vizitator PROASPĂT per rulare) → comparabile.

Reproductibilitate (pinuri în `meta`): model triaj/agent, hash prompt judge, semnătură catalog,
cache off, runs/case, flag (paired ON/OFF). Zero PII (fixture sintetice; transcript trunchiat).

Rulare (cere OpenAI + DB live — LENT, apeluri reale):
    PYTHONPATH=. python scripts/sim/eval_run.py --only discovery_oily_serum --runs 1   # smoke
    PYTHONPATH=. python scripts/sim/eval_run.py                          # baseline complet
    PYTHONPATH=. python scripts/sim/eval_run.py --flag prompt_vnext_enabled   # paired OFF vs ON
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

# Cache OFF NU se mai setează la import (fix review #234: fără efect global de mediu care ar
# contamina alte procese/teste). Se aplică pe `settings.cache_enabled` DOAR pe durata rulării
# pipeline-ului, în `main()`, și se RESTAUREAZĂ după (try/finally).
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SIM_DIR = Path(__file__).resolve().parent
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

import eval_gates  # noqa: E402
import eval_judge  # noqa: E402
import web_audit  # noqa: E402  — reutilizăm driverul /web/chat dovedit (fakeredis, sesiune, purjă)

CONV_DIR = ROOT / "qa-suite" / "conversations"
OUT_DIR = ROOT / "reports"


# --- instrumentare tokeni per-tur (usage e ContextVar, resetat când web_chat revine) --------------
_turn_tokens = {"in": 0, "out": 0}


def _install_token_meter() -> None:
    """Wrap `usage.record_chat` ca să tally-uim tokenii TUTUROR apelurilor LLM dintr-un tur într-un
    contor de harness (citit + resetat în jurul fiecărui `web_chat`, ÎNAINTE de judge → judge-ul nu
    contaminează tokenii turului). Patch pe modulul usage (llm.py cheamă usage.record_chat)."""
    import src.agent.usage as usage_mod  # noqa: PLC0415

    orig = usage_mod.record_chat

    def _patched(resp: Any, model: str) -> None:
        try:
            u = getattr(resp, "usage", None)
            if u is not None:
                _turn_tokens["in"] += int(getattr(u, "prompt_tokens", 0) or 0)
                _turn_tokens["out"] += int(getattr(u, "completion_tokens", 0) or 0)
        except Exception:  # noqa: BLE001 — instrumentarea nu blochează turul
            pass
        orig(resp, model)

    usage_mod.record_chat = _patched  # type: ignore[assignment]


def _p95(values: list[float]) -> float:
    """Percentila 95 nearest-rank (robustă pt n mic). Gol → 0."""
    if not values:
        return 0.0
    s = sorted(values)
    import math  # noqa: PLC0415

    k = max(1, math.ceil(0.95 * len(s)))
    return round(s[k - 1], 1)


async def _catalog_signature(conn, business_id: str) -> str:
    """Semnătură deterministă a catalogului (count + sha256 pe (id, price) sortat) → pin de
    reproductibilitate: dacă se re-seedează catalogul, baseline-urile nu se compară orb."""
    rows = await conn.fetch(
        "select id::text as id, coalesce(price, 0) as price, coalesce(name, '') as name, "
        "coalesce(availability, '') as availability from products "
        "where business_id = $1 order by id",
        business_id,
    )
    h = hashlib.sha256()
    for r in rows:
        # nume + disponibilitate în semnătură, nu doar id+preț (review Codex #234): o re-seedare
        # care schimbă nume/stoc dar nu prețul trebuie să invalideze comparația baseline.
        h.update(f"{r['id']}:{float(r['price']):.2f}:{r['name']}:{r['availability']}".encode())
    return f"n={len(rows)};sha256={h.hexdigest()[:16]}"


def _fixtures_signature() -> str:
    """Hash-ul fixture-urilor pe JSON CANONIC (`json.dumps` sort_keys), NU pe bytes bruți (fix
    review #234): independent de LF/CRLF (git convertește pe Windows) → reproductibil pe orice
    checkout/OS. Sortat determinist pe cale."""
    h = hashlib.sha256()
    for path in sorted(CONV_DIR.glob("*.json")):
        h.update(path.name.encode())
        data = json.loads(path.read_text(encoding="utf-8"))
        h.update(json.dumps(data, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:16]


# Redactare PII (fix #234): chiar dacă fixture-urile sunt sintetice, contractul de raport NU trebuie
# să persiste PII (rulări viitoare pe conversații reale). Scrub telefon (E.164/RO) + email. Aplicat
# la ce se SCRIE în raport (întrebare + eșantion răspuns), NU la ce vede judge-ul (transient).
_PII_RE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)|([\w.+-]+@[\w-]+\.[\w.-]+)", re.IGNORECASE)


def _redact(s: str) -> str:
    return _PII_RE.sub("[REDACTED]", s or "")


def _load_conversations(only: str | None) -> list[dict[str, Any]]:
    convos: list[dict[str, Any]] = []
    for path in sorted(CONV_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        convos.extend(data.get("conversations", []))
    if only:
        convos = [c for c in convos if c["id"] == only]
    return convos


def _turn_dict(t) -> dict[str, Any]:
    return {
        "content": t.content,
        "products": t.products,
        "suggestions": t.suggestions,
        "offer": t.offer,
    }


async def _run_conversation(convo: dict[str, Any], mk, llm, runs: int) -> dict[str, Any]:
    """Rulează conversația de `runs` ori (vizitator proaspăt = state resetat); agregă per tur."""
    turns_spec = convo["turns"]
    # per turn_index acumulăm peste rulări: judge, gate fails, latency, tokens, opening-rep.
    acc: list[dict[str, list]] = [
        {
            "judge": [],
            "gate_fails": [],
            "latency_ms": [],
            "tokens": [],
            "opening_rep": [],
            "sample": [],
        }
        for _ in turns_spec
    ]
    for _run in range(runs):
        client = await mk(convo["id"])
        transcript: list[dict[str, str]] = []
        prev_dict: dict[str, Any] | None = None
        for i, tspec in enumerate(turns_spec):
            user_msg = tspec["user"]
            _turn_tokens["in"] = _turn_tokens["out"] = 0
            t0 = time.perf_counter()
            turn = await client.say(user_msg)  # calea /web/chat REALĂ
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            tokens = {"in": _turn_tokens["in"], "out": _turn_tokens["out"]}
            cur = _turn_dict(turn)

            fails = eval_gates.check_turn(cur, prev_dict, tspec.get("gates", {}))
            op_rep = eval_gates.opening_repeated(cur, prev_dict)
            # transcript INCLUDE întrebarea curentă ÎNAINTE de judge (fix blocant #234): altfel
            # judge-ul nu vede LA CE răspunde botul → `answered`/`natural`/`overall` invalide.
            # Bot-reply se adaugă DUPĂ judge. Judge după tokeni (nu-i contaminează).
            transcript.append({"role": "user", "text": user_msg})
            # #234: judge-ul primește EXPERIENȚA completă (text + carduri + offer), nu doar textul.
            jscore = await eval_judge.judge_turn(
                llm, transcript, turn.content, turn.products, turn.offer
            )

            acc[i]["judge"].append(jscore)
            acc[i]["gate_fails"].append(fails)
            acc[i]["latency_ms"].append(latency_ms)
            acc[i]["tokens"].append(tokens)
            acc[i]["opening_rep"].append(op_rep)
            acc[i]["sample"].append(
                {
                    "content": _redact(turn.content[:280]),  # #234: fără PII în raport
                    "n_cards": len(turn.products),
                    "fails": fails,
                }
            )
            transcript.append({"role": "bot", "text": turn.content[:280]})
            prev_dict = cur

    return {
        "id": convo["id"],
        "turns": [_agg_turn(turns_spec[i], acc[i]) for i in range(len(turns_spec))],
    }


def _agg_turn(tspec: dict[str, Any], a: dict[str, list]) -> dict[str, Any]:
    """Agregă un tur peste rulări: mediană + spread judge, gate pass count, p95 latență, tokeni."""
    metrics = ("answered", "natural", "non_repetitive", "concise", "honest", "overall")
    jmed: dict[str, Any] = {}
    for m in metrics:
        vals = [j[m] for j in a["judge"] if j.get(m) is not None]
        jmed[m] = {
            "median": median(vals) if vals else None,
            "spread": (max(vals) - min(vals)) if vals else None,
        }
    gate_pass_runs = sum(1 for f in a["gate_fails"] if not f)
    n = len(a["gate_fails"])
    tokens_out = [t["out"] for t in a["tokens"]]
    tokens_in = [t["in"] for t in a["tokens"]]
    # instabilitate: gate trece în unele rulări dar nu în toate, SAU judge overall variază ≥2.
    unstable = (0 < gate_pass_runs < n) or (jmed["overall"]["spread"] or 0) >= 2
    return {
        "user": _redact(tspec["user"]),  # #234: fără PII în raport (contractul, nu doar fixturile)
        "judge_focus": tspec.get("judge_focus", ""),
        "runs": n,
        "gate_pass_runs": gate_pass_runs,
        "gate_fails_union": sorted({f for run in a["gate_fails"] for f in run}),
        "opening_repeat_runs": sum(1 for x in a["opening_rep"] if x),
        "judge": jmed,
        "latency_ms_raw": [
            float(x) for x in a["latency_ms"]
        ],  # #234: p95 GLOBAL pe raw, nu p95-de-p95
        "latency_ms_p95": _p95([float(x) for x in a["latency_ms"]]),
        "latency_ms_median": round(median(a["latency_ms"]), 1) if a["latency_ms"] else 0,
        "tokens_out_median": median(tokens_out) if tokens_out else 0,
        "tokens_in_median": median(tokens_in) if tokens_in else 0,
        "unstable": unstable,
        "samples": a["sample"],
    }


def _summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    all_turns = [t for c in cases for t in c["turns"]]
    followups = [t for c in cases for t in c["turns"][1:]]  # index>0 = follow-up

    def _turn_median(t: dict, m: str):
        return t["judge"][m]["median"]

    nat = [_turn_median(t, "natural") for t in all_turns if _turn_median(t, "natural") is not None]
    fu_answered = [
        _turn_median(t, "answered") for t in followups if _turn_median(t, "answered") is not None
    ]
    # p95 GLOBAL peste TOATE latențele brute (fiecare tur × fiecare rulare), NU p95-de-p95 (fix
    # #234): doar așa pragul „+≤10% vs baseline" e măsurabil corect.
    lat_raw = [x for t in all_turns for x in t.get("latency_ms_raw", [])]

    def _turns_with(prefix: str) -> int:
        # numără TURURILE DISTINCTE cu ≥1 eșec din categoria dată (fix #234: nu fail-strings,
        # care supra-numărau — un tur cu 1>0 ȘI 2>0 e UN tur, nu două).
        return sum(1 for t in all_turns if any(f.startswith(prefix) for f in t["gate_fails_union"]))

    def _pct_ge4(vals: list[float]) -> float:
        return round(100 * sum(1 for v in vals if v >= 4) / len(vals), 1) if vals else 0.0

    # Metrică JOINT (review #234): un răspuns e „bun" doar dacă e ȘI natural ȘI la obiect. Natural
    # fără answered (proză care nu răspunde) SAU answered fără natural (corect dar șablon) nu se
    # califică. Bara reală de calitate.
    def _both_ge4(t: dict) -> bool:
        nm, am = _turn_median(t, "natural"), _turn_median(t, "answered")
        return nm is not None and am is not None and nm >= 4 and am >= 4

    joint = (
        round(100 * sum(1 for t in all_turns if _both_ge4(t)) / len(all_turns), 1)
        if all_turns
        else 0.0
    )

    return {
        "n_conversations": len(cases),
        "n_turns": len(all_turns),
        "n_followup_turns": len(followups),
        "judge_natural_median": round(median(nat), 2) if nat else None,
        "pct_turns_natural_ge4": _pct_ge4(nat),
        "pct_turns_natural_AND_answered_ge4": joint,  # #234: bara reală (joint)
        "pct_followup_answered_ge4": _pct_ge4(fu_answered),
        "det_gate_pass_rate_pct": round(
            100
            * sum(t["gate_pass_runs"] for t in all_turns)
            / max(1, sum(t["runs"] for t in all_turns)),
            1,
        ),
        # contoare pe TURURI DISTINCTE (fix #234) + linkuri
        "turns_ungrounded_price": _turns_with("ungrounded_price"),
        "turns_ungrounded_link": _turns_with("ungrounded_link"),
        "turns_missing_offer_link": _turns_with("missing_offer_link"),
        "turns_too_many_cards": _turns_with("too_many_cards"),
        "turns_new_cards_on_followup": _turns_with("new_cards_on_followup"),
        "opening_repeat_turns": sum(1 for t in all_turns if t["opening_repeat_runs"] > 0),
        "unstable_turns": [
            {"conv": c["id"], "user": t["user"]} for c in cases for t in c["turns"] if t["unstable"]
        ],
        "latency_ms_p95": _p95(lat_raw),
        "latency_ms_p50": round(median(lat_raw), 1) if lat_raw else 0,
        "n_latency_samples": len(lat_raw),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="NX-180 evaluator conversațional + baseline")
    ap.add_argument("--only", default=None, help="un singur id de conversație (smoke)")
    ap.add_argument("--runs", type=int, default=3, help="rulări per conversație (default 3)")
    ap.add_argument("--flag", default=None, help="feature flag de togglat paired OFF vs ON")
    ap.add_argument("--token", default=None, help="public token webchat (default: din DB, demo)")
    args = ap.parse_args()

    convos = _load_conversations(args.only)
    if not convos:
        print(f"Nicio conversație{' cu id ' + args.only if args.only else ''} în {CONV_DIR}")
        return 2

    web_audit._install_fake_redis()  # ÎNAINTE de importurile care capturează get_redis
    _install_token_meter()

    from src.agent.llm import get_llm  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415
    from src.db.connection import admin_conn, close_pool, get_pool  # noqa: PLC0415
    from src.db.queries.channels import resolve_web_session  # noqa: PLC0415

    llm = get_llm()
    if llm is None:
        print("OPENAI_API_KEY lipsă → evaluatorul are nevoie de LLM real (agent + judge).")
        return 1
    settings = get_settings()

    # rezolvă tokenul webchat + business (ca web_audit).
    token, biz_id = args.token, web_audit.DEMO_BIZ
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        if token:
            r = await resolve_web_session(conn, token)
            if r:
                biz_id = r["business_id"]
        else:
            row = await conn.fetchrow(
                "select provider_account_id, business_id::text as business_id from channels "
                "where business_id=$1 and kind='webchat' limit 1",
                web_audit.DEMO_BIZ,
            )
            if row:
                token, biz_id = row["provider_account_id"], row["business_id"]
        if not token:
            print("Niciun canal webchat pe tenantul demo.")
            return 1
        catalog_sig = await _catalog_signature(conn, biz_id)

    async def mk(label: str):
        vid, sig = await web_audit._session(token, label)
        return web_audit.WebClient(token, vid, sig, label)

    if args.flag:
        pass_specs = [
            (args.flag, False, f"{args.flag}=False"),
            (args.flag, True, f"{args.flag}=True"),
        ]
    else:
        pass_specs = [(None, False, "baseline")]

    # INTERCALAT per conversație (OFF apoi ON pe ACELAȘI caz, înainte de următorul) — nu tot OFF
    # apoi tot ON (fix #234): altfel diferența măsoară drift temporal (rate limit / warmup / oră),
    # nu efectul flagului. Baseline (fără flag) = un singur pass, neschimbat.
    print(
        f"\n{'=' * 70}\nruns/case={args.runs} cache=OFF(scoped) passes={[s[2] for s in pass_specs]}"
    )
    # #234: CACHE OFF DOAR pe durata rulării pipeline-ului, restaurat garantat (try/finally). Fără
    # efect global de mediu. `settings` e singleton mutabil (get_settings lru_cached).
    _cache_prev = settings.cache_enabled
    settings.cache_enabled = False
    pass_cases: dict[str, list] = {label: [] for _, _, label in pass_specs}
    try:
        for convo in convos:
            print(f"  • {convo['id']} …", flush=True)
            for flag, value, label in pass_specs:
                if flag:
                    setattr(settings, flag, value)  # toggle paired per caz (settings mutabil)
                pass_cases[label].append(await _run_conversation(convo, mk, llm, args.runs))
    finally:
        settings.cache_enabled = _cache_prev  # restaurare, orice s-ar întâmpla
    report_passes = [
        {
            "pass": label,
            "flag": flag,
            "flag_value": value,
            "summary": _summarize(pass_cases[label]),
            "cases": pass_cases[label],
        }
        for flag, value, label in pass_specs
    ]

    now = datetime.now(timezone.utc)
    report = {
        "meta": {
            "generated_at": now.isoformat(),
            "kind": "baseline" if not args.flag else "paired",
            "business_id": biz_id,
            "runs_per_case": args.runs,
            "cache_enabled": False,  # #234: mereu OFF pe durata rulării (scoped, restaurat după)
            "model_triage": llm.model_triage,
            "model_agent": llm.model_agent,
            "judge_model": llm.model_agent,
            "judge_prompt_sha256": eval_judge.judge_prompt_sha256(),
            "judge_version": eval_judge.JUDGE_VERSION,
            "catalog_signature": catalog_sig,
            "fixtures_sha256": _fixtures_signature(),
            "paired_mode": "interleaved_per_conversation" if args.flag else "single",
            "denominator": "scor per TUR (mediană peste rulări); follow-up = index>0",
        },
        "passes": report_passes,
    }

    OUT_DIR.mkdir(exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    out_path = OUT_DIR / f"eval-{'baseline' if not args.flag else args.flag}-{stamp}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # curățare vizitatori de audit (reutilizăm purja dovedită din web_audit).
    try:
        async with admin_conn(pool) as conn:
            purged = await web_audit._purge_audit(conn, biz_id)
        if purged:
            print(f"Auto-curățat {purged} vizitator(i) de eval.")
    except Exception as e:  # noqa: BLE001 — curățarea nu maschează rezultatul
        print(f"⚠ auto-curățarea a eșuat ({type(e).__name__}).")
    await close_pool()

    for p in report_passes:
        s = p["summary"]
        print(
            f"\n[{p['pass']}] natural_med={s['judge_natural_median']} "
            f"nat≥4={s['pct_turns_natural_ge4']}% fu_answered≥4={s['pct_followup_answered_ge4']}% "
            f"gate_pass={s['det_gate_pass_rate_pct']}% p95={s['latency_ms_p95']}ms "
            f"unstable={len(s['unstable_turns'])} opening_repeats={s['opening_repeat_turns']}"
        )
    print(f"\n→ raport: {out_path}")
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    raise SystemExit(asyncio.run(main()))
