"""NX-329 PR B — sonda de APRINDERE a resolverului v2 pe scurtăturile de link și comparație.

Întrebarea: pe traficul REAL, ce ar fi decis calea NX-326 (cea din producție, flagul stins) și ce
decide calea v2 (`_v2_shortcut`, cu faptele recitite din catalog) pe EXACT același tur?

Pentru fiecare tur din `conversation_traces` (fereastra dată, un tenant):

  1. **Ecranul** e `reply.products` al ultimului tur ANTERIOR din conversație care a arătat produse,
     cu filtrul lui `processor._displayed_product_refs` (id + nume + preț). Aceeași regulă scrie
     `displayed_products` în stare, deci ecranul reconstruit e cel pe care l-a văzut turul. Cardul
     spunea `recommended`, dar pe o comparație acela ia coloanele, nu produsele.
  2. **Poarta** e cea din `try_pre_intents`: declanșator de link/comparație, fără «mai ieftin», fără
     constrângeri noi (`turn_has_new_constraints`, funcția producției), ecran nevid (≥ 2 la
     comparație). Se sar turele pe care le-ar fi luat o poartă de ÎNAINTE (recenzii, detaliu,
     «compară-l cu un produs similar»). O apăsare de chip se sare DOAR cu recunoașterea NX-316
     aprinsă (`CHIP_MOVES_V2_ENABLED`); stinsă, cum e în producție, textul chip-ului trece prin
     scurtătură ca orice mesaj tastat, deci se măsoară (și se marchează `chip_press`).
  3. **Decizia veche** vine din funcțiile producției: `_link_targets` la link, `named_targets` +
     ramura NX-326 din `try_pre_intents` la comparație. **Decizia nouă** e `_v2_shortcut`, cu
     vocabularul și faptele citite pe `tenant_conn`.

Verdictul pe tur: `same`, `narrowed` (v2 servește o submulțime), `to_model_not_found` (numele nu e
nicăieri sau produsul a dispărut: exact clasa pe care o închidem), `to_model_other`, `changed`
(v2 servește alte produse), `unavailable` (faptele n-au putut fi citite; producția cade pe NX-326).

**Poarta aprinderii** (cardul NX-329 §7): zero ture în care calea veche servea ținte NUMITE
(`source` named/ambiguous) iar v2 le trimite la model din alt motiv decât `not_found`/`stale`.
Turele `changed` se listează pentru citire, fiindcă nu se pot judeca automat.

ZERO scriere, ZERO model. Tenant-scoped, `business_id` explicit în fiecare query.

    PYTHONPATH=. python scripts/nx329_shortcut_probe.py --business sole-ro --days 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

from src.agent import deterministic as det
from src.agent.reference_resolver import named_targets
from src.catalog.clarify_menu import words_of
from src.config import get_settings
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn
from src.db.queries.businesses import load_business
from src.models import (
    Contact,
    InboundMessage,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps

#: Rezultatele resolverului care justifică plecarea la model: numele nu e nicăieri, sau ținta a
#: dispărut din catalog. Orice alt motiv pe o țintă pe care calea veche o servea e o regresie.
_JUSTIFIED = frozenset({"not_found", "stale"})


class _Provider:
    """`deps.db(op)` peste `tenant_conn`: checkout scurt, RLS pe tenant (NX-231)."""

    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, operation: str = "unlabeled"):  # noqa: ARG002
        return tenant_conn(self.business_id)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _shown_of(reply: dict[str, Any] | None) -> list[ProductRef]:
    """Ecranul lăsat de un tur: aceeași regulă ca `processor._displayed_product_refs`."""
    if not isinstance(reply, dict):
        return []
    out: list[ProductRef] = []
    for p in reply.get("products") or ():
        if not isinstance(p, dict):
            continue
        pid = p.get("product_id") or p.get("id")
        if pid is None or p.get("name") is None or p.get("price") is None:
            continue
        out.append(ProductRef(str(pid), str(p["name"]), float(p["price"])))
    return out


def _chips_of(reply: dict[str, Any] | None) -> list[str]:
    if not isinstance(reply, dict):
        return []
    rich = reply.get("rich") or {}
    comparison = reply.get("comparison") or {}
    out: list[str] = []
    for source in (rich.get("chips"), reply.get("suggestions"), comparison.get("chips")):
        for chip in source or ():
            text = chip.get("label") if isinstance(chip, dict) else chip
            if isinstance(text, str) and text.strip():
                out.append(text)
    return out


def _ctx(business: Any, locale: str, body: str, shown: list[ProductRef]) -> TurnContext:
    ctx = TurnContext(
        turn_id="probe",
        business=business,
        contact=Contact(id="probe", business_id=business.id),
        message=InboundMessage(provider_msg_id="probe", body=body),
        conversation_id="probe",
    )
    ctx.language = locale
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.state.displayed_products = list(shown)
    return ctx


def _gate(ctx: TurnContext, query: str) -> str | None:
    """Poarta din `try_pre_intents`, cu funcțiile ei, sau None când turul n-ar ajunge la
    scurtătură (altă poartă de dinainte, sau nicio scurtătură)."""
    norm = det._norm_followup(query)
    if det._REVIEW_RE.search(norm) or det._DETAIL_RE.search(norm):
        return None
    if det.is_compare_with_similar(query, ctx.language):
        return None
    if det._CHEAPER_RE.search(query) is not None:
        return None
    if det.turn_has_new_constraints(ctx, ctx.route):
        return None
    shown = ctx.state.displayed_products
    if shown and det._LINK_RE.search(query) is not None:
        return "link"
    if len(shown) >= 2 and det._COMPARE_RE.search(query) is not None:
        return "compare"
    return None


def _old_decision(ctx: TurnContext, query: str, gate: str) -> dict[str, Any]:
    """Calea NX-326, cea din producție cu flagul stins."""
    refs = list(ctx.state.displayed_products)
    if gate == "link":
        ids = det._link_targets(ctx, query, refs)
        [event] = [e.properties for e in ctx.events if e.type == "shortcut_targets"] or [{}]
        if ids is None:
            return {
                "action": "fallback",
                "ids": [r.product_id for r in refs],
                "source": event.get("source", "none"),
            }
        return {"action": "served", "ids": ids, "source": event.get("source")}
    # Ramura de comparație NX-326 din `try_pre_intents` (inline acolo, deci reprodusă aici).
    targets = named_targets(query, refs, locale=ctx.language)
    if targets.source == "named" and len(targets.indices) >= 2:
        ids = [refs[i].product_id for i in targets.indices][:4]
        return {"action": "served", "ids": ids, "source": "named"}
    if targets.source != "none":
        return {"action": "model", "ids": [], "source": targets.source}
    return {"action": "fallback", "ids": [r.product_id for r in refs[:2]], "source": "none"}


async def _new_decision(
    ctx: TurnContext, deps: PipelineDeps, query: str, gate: str
) -> dict[str, Any]:
    decision = await det._v2_shortcut(ctx, deps, query, gate)
    refs = [
        {k: e.properties.get(k) for k in ("kind", "source", "outcome", "reason")}
        for e in ctx.events
        if e.type == "reference_v2"
    ]
    if decision is None:
        return {"action": "unavailable", "ids": [], "refs": refs, "reason": None}
    ids = list(decision.ids)
    if decision.action == "fallback":
        shown = [r.product_id for r in ctx.state.displayed_products]
        ids = shown if gate == "link" else shown[:2]
    return {"action": decision.action, "ids": ids, "refs": refs, "reason": decision.reason}


def _verdict(old: dict[str, Any], new: dict[str, Any]) -> str:
    if new["action"] == "unavailable":
        return "unavailable"
    if old["action"] == new["action"] and old["ids"] == new["ids"]:
        return "same"
    if new["action"] == "model":
        if any(r.get("outcome") in _JUSTIFIED for r in new["refs"]):
            return "to_model_not_found"
        return "to_model_other"
    if old["action"] != "model" and set(new["ids"]) < set(old["ids"]):
        return "narrowed"
    return "changed"


def _violates(old: dict[str, Any], new: dict[str, Any], verdict: str, gate: str) -> bool:
    """Poarta cardului: calea veche servea CORECT ținte NUMITE, iar v2 le trimite la model fără
    ca vreo referință să fie negăsită sau ștearsă.

    „Corect" are o parte mecanică: o comparație care servea MAI MULTE produse decât referințe
    scrise nu servea ce a numit clientul (turul real `d8639f26`: două nume, patru coloane cu
    același nume, fiindcă NX-326 lua fiecare card al familiei drept numit). Cazurile astea rămân
    în lista strictă (`named_to_model`), de citit, dar nu sunt o regresie a lui v2."""
    over_served = gate == "compare" and len(old["ids"]) > max(len(new["refs"]), 2)
    return (
        verdict == "to_model_other"
        and old["action"] == "served"
        and old.get("source") in ("named", "ambiguous")
        and not over_served
    )


def _named_to_model(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Lista STRICTĂ, de citit de un om: calea veche servea ținte numite pe ecran, iar v2 pleacă la
    model din ORICE motiv, inclusiv `not_found`. Poarta cardului scutește `not_found`, dar sonda a
    arătat că un `not_found` poate ascunde o regresie (un calificativ lipit de nume: «BELIF pentru
    ten uscat»), deci cazurile se listează chiar dacă poarta trece."""
    return (
        old["action"] == "served"
        and old.get("source") in ("named", "ambiguous")
        and new["action"] == "model"
    )


async def probe(slug: str, days: int) -> dict[str, Any]:
    pool = await get_pool()
    async with admin_conn(pool) as admin:
        row = await admin.fetchrow(
            "select id::text as id from businesses where slug = $1 or id::text = $1", slug
        )
    if row is None:
        raise SystemExit(f"tenant necunoscut: {slug!r}")
    business_id = row["id"]
    async with tenant_conn(business_id) as conn:
        business = await load_business(conn, business_id)
        if business is None:
            raise SystemExit(f"tenant fără configurație: {slug!r}")
        traces = await conn.fetch(
            """
            select conversation_id::text as conv, turn_id::text as turn_id, language,
                   client_text, reply, created_at
            from conversation_traces
            where business_id = $1::uuid and created_at > now() - make_interval(days => $2)
            order by conversation_id, created_at
            """,
            business_id,
            days,
        )
    default_locale = business.default_locale or "ro"
    deps = PipelineDeps(llm=None, db=_Provider(business_id))
    # Fără recunoașterea NX-316 (flagul stins în producție), o apăsare de chip ajunge la scurtătură
    # exact ca un mesaj tastat, deci intră în măsurătoare. Aprinsă, o servește `move_id`-ul.
    recognizes_chips = bool(getattr(get_settings(), "chip_moves_v2_enabled", False))

    cases: list[dict[str, Any]] = []
    skipped = Counter()

    async def evaluate(
        text: str, shown: list[ProductRef], locale: str, t: Any, origin: str
    ) -> None:
        gate = _gate(_ctx(business, locale, text, shown), text)
        if gate is None:
            return
        old = _old_decision(_ctx(business, locale, text, shown), text, gate)
        new = await _new_decision(_ctx(business, locale, text, shown), deps, text, gate)
        verdict = _verdict(old, new)
        cases.append(
            {
                "conv": t["conv"][:8],
                "turn": t["turn_id"][:8],
                "date": str(t["created_at"])[:10],
                "origin": origin,
                "gate": gate,
                "text": text,
                "shown": len(shown),
                "screen": [[r.product_id[:8], r.name] for r in shown],
                "old": old,
                "new": new,
                "verdict": verdict,
                "violates": _violates(old, new, verdict, gate),
                "named_to_model": _named_to_model(old, new),
            }
        )

    conv = None
    shown: list[ProductRef] = []
    prev_chips: list[str] = []
    for t in traces:
        if t["conv"] != conv:
            conv, shown, prev_chips = t["conv"], [], []
        reply = _json(t["reply"])
        query = (t["client_text"] or "").strip()
        locale = t["language"] or default_locale
        if query and shown:
            said = words_of(query)
            pressed = any(words_of(c) == said for c in prev_chips)
            if pressed and recognizes_chips:
                skipped["chip_press"] += 1
            else:
                await evaluate(query, shown, locale, t, "chip_press" if pressed else "typed")
        next_shown = _shown_of(reply)
        if next_shown:
            shown = next_shown
        prev_chips = _chips_of(reply)
        # Chip-urile OFERITE de turul ăsta, pe ecranul pe care îl lasă: texte reale, emise de noi,
        # pe care clientul le poate apăsa la turul următor. Fără recunoașterea NX-316 trec prin
        # scurtătură ca orice mesaj; cu ea le servește `move_id`-ul, deci nu se măsoară.
        if shown and not recognizes_chips:
            for chip in dict.fromkeys(prev_chips):
                await evaluate(chip, shown, locale, t, "offered_chip")

    def _counts(origin: str) -> dict[str, int]:
        return dict(Counter(f"{c['gate']}:{c['verdict']}" for c in cases if c["origin"] == origin))

    return {
        "business": slug,
        "days": days,
        "traces": len(traces),
        "window": [
            str(min((t["created_at"] for t in traces), default="")),
            str(max((t["created_at"] for t in traces), default="")),
        ],
        "chip_moves_v2_enabled": recognizes_chips,
        "skipped": dict(skipped),
        "by_origin": dict(Counter(c["origin"] for c in cases)),
        "by_verdict": {o: _counts(o) for o in ("typed", "chip_press", "offered_chip")},
        "violations": sum(1 for c in cases if c["violates"]),
        "named_to_model": sum(1 for c in cases if c["named_to_model"]),
        "gate": "PASS" if not any(c["violates"] for c in cases) else "FAIL",
        "cases": cases,
    }


def _print(r: dict[str, Any]) -> None:
    print(f"tenant: {r['business']}   fereastră: {r['days']} zile   ture capturate: {r['traces']}")
    chips = "aprinsă" if r["chip_moves_v2_enabled"] else "stinsă"
    print(f"recunoașterea chip-urilor (NX-316): {chips}")
    print(f"capturi între {r['window'][0][:10]} și {r['window'][1][:10]}")
    print(f"texte care ajung la o scurtătură: {r['by_origin']}")
    if r["skipped"]:
        print(f"sărite (servite de altă poartă): {r['skipped']}")
    for origin, counts in r["by_verdict"].items():
        if not counts:
            continue
        print(f"decizia veche → nouă ({origin}):")
        for key, n in sorted(counts.items()):
            print(f"  {key:<32} {n}")
    for c in r["cases"]:
        if c["verdict"] == "same":
            continue
        mark = "!!" if c["violates"] else "? " if c["named_to_model"] else "  "
        refs = ", ".join(f"{x['kind']}:{x['outcome']}/{x['reason']}" for x in c["new"]["refs"])
        print(
            f"{mark} {c['conv']}/{c['turn']} {c['gate']:<7} {c['verdict']:<20} "
            f"vechi={c['old']['action']}({c['old'].get('source')},{len(c['old']['ids'])}) "
            f"nou={c['new']['action']}({len(c['new']['ids'])}) [{refs}]"
        )
        print(f"     «{c['text']}»  ({c['origin']})")
    print(f"ținte numite trimise la model (listă strictă, de citit): {r['named_to_model']}")
    print(f"încălcări ale porții de aprindere: {r['violations']}   → {r['gate']}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    try:
        report = await probe(args.business, args.days)
    finally:
        await close_pool()
    _print(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if report["gate"] == "PASS" else 1)


if __name__ == "__main__":
    asyncio.run(main())
