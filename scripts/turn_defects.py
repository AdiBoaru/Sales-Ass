"""NX-363 — detectorii de defecte pe tot traficul: ce mecanism a lovit câte ture.

    PYTHONPATH=. python scripts/turn_defects.py --business sole-ro [--since 2026-09-24] [--until …]

READ-ONLY, zero apeluri de model. Până acum defectele se găseau citind conversații de mână: cele
din 2026-09-30 (`495ad405`, `0e88752a`, `0c071497`, `625ab925`) au scos 21 de probleme din 16 ture,
dar fiecare a apărut dintr-un semnal pe care îl scriem DEJA pe fiecare tur (evenimente,
`conversation_traces`, catalog). Scriptul rulează acele semnale pe toată fereastra și întoarce o
listă ordonată: mecanism, câte ture a lovit, din câte ture pe care se aplică, exemple de tur.

Fiecare detector e o funcție PURĂ pe un `Turn` (traceul, evenimentele lui, faptele de catalog ale
produselor afișate, contextul conversației) și întoarce `None` (nu se aplică pe turul ăsta),
`False` sau `True`. Numitorul e deci „turele pe care se putea întâmpla”, nu toate turele: un
ordinal ieșit din interval se numără doar pe turele cu ordinale.

Nu e o poartă de CI și nu dă verdict (vezi `quality_watch.py`, NX-272): e lista din care se alege
cardul următor. În consolă doar agregate și id-uri de tur; raportul complet e LOCAL
(`reports/nx363/`, ignorat de git), fără text de client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "reports" / "nx363"
EXAMPLES = 3

#: Pragurile detectorilor, declarate aici și nu în funcții, ca să se vadă ce e judecată.
SUBJECT_SHARE_MIN = 0.5  # sub jumătate din carduri de tipul cerut = subiect pierdut
MANY_MODEL_CALLS = 4  # tur normal: interpretare + unealtă + compunere = 3
SLOW_TURN_MS = 15_000
FEW_CARDS = 2  # ≤ 2 carduri arătate…
FULL_SEARCH = 6  # …dintr-o căutare care a adus o pagină întreagă
#: Sub atâtea ture aplicabile, rata nu se raportează: „100%” dintr-un tur nu e o cifră.
MIN_APPLICABLE = 20


# --- intrarea (formă neutră, construită din DB sau din teste) ------------------------------------


@dataclass(frozen=True)
class Turn:
    turn_id: str
    conversation_id: str
    created_at: str
    #: `conversation_traces.reply` (forma persistată a răspunsului).
    reply: Mapping[str, Any]
    #: `conversation_traces.diagnostics`.
    diagnostics: Mapping[str, Any]
    #: Evenimentele turului, grupate pe tip (o listă de proprietăți per tip).
    events: Mapping[str, Sequence[Mapping[str, Any]]]
    #: `product_id` → disponibilitate, pentru produsele arătate.
    availability: Mapping[str, str | None] = field(default_factory=dict)
    #: Numele afișat → id-urile de produs arătate SUB el mai devreme în aceeași conversație.
    earlier_names: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: `product_id` → `attributes.product_type` (None = tip nederivat, un sfert din catalog).
    product_types: Mapping[str, str | None] = field(default_factory=dict)

    def first(self, event_type: str) -> Mapping[str, Any] | None:
        rows = self.events.get(event_type) or ()
        return rows[0] if rows else None

    def all(self, event_type: str) -> Sequence[Mapping[str, Any]]:
        return self.events.get(event_type) or ()


def shown_cards(reply: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Cardurile pe care le-a VĂZUT clientul: itemii bogați, altfel produsele simple. PUR."""
    rich = reply.get("rich") or {}
    items = rich.get("items") if isinstance(rich, Mapping) else None
    if items:
        return [i for i in items if isinstance(i, Mapping)]
    return [p for p in (reply.get("products") or []) if isinstance(p, Mapping)]


def card_id(card: Mapping[str, Any]) -> str | None:
    pid = card.get("product_id") or card.get("id")
    return str(pid) if pid else None


# --- detectorii (puri) ---------------------------------------------------------------------------

Verdict = bool | None


def refused_set_shown(t: Turn) -> Verdict:
    """NX-362: setul relaxat refuzat de model a ajuns totuși pe ecran, aprobat de o proză care nu
    era a modelului. Pe traceurile de dinaintea câmpului `model_prose`, proza sărită ține loc."""
    withheld = t.first("refused_set_withheld")
    if withheld is None:
        return None
    if not withheld.get("named"):
        return False
    model_prose = withheld.get("model_prose")
    if model_prose is None:
        skipped = (t.first("prose_round") or {}).get("skipped")
        return bool(skipped)
    return model_prose is False


def oos_on_card(t: Turn) -> Verdict:
    """Un produs marcat epuizat în catalog, arătat pe un card (fără nicio marcă pe card)."""
    ids = [i for i in (card_id(c) for c in shown_cards(t.reply)) if i]
    if not ids:
        return None
    return any(t.availability.get(i) == "out_of_stock" for i in ids)


def oos_first(t: Turn) -> Verdict:
    """Primul card e epuizat: cel pe care clientul îl vede întâi."""
    ids = [i for i in (card_id(c) for c in shown_cards(t.reply)) if i]
    if not ids:
        return None
    return t.availability.get(ids[0]) == "out_of_stock"


def uttered_need_unknown(t: Turn) -> Verdict:
    """O valoare cerută căutării a ieșit `not_in_vocabulary` (ex. «păr gras»): filtrul n-a rulat."""
    rows = t.all("vocabulary_resolved")
    if not rows:
        return None
    return any(
        r.get("status") == "unknown" and r.get("reason") == "not_in_vocabulary" for r in rows
    )


def subject_lost(t: Turn) -> Verdict:
    """Sub jumătate din cardurile CU TIP CUNOSCUT sunt de tipul cerut (măști la o cerere de cremă).

    `subject_match.share` împarte la toate cardurile, deci un card fără `product_type` derivat
    (un sfert din catalog) arată ca un tip greșit: șase creme de față, patru fără tip, ieșeau 0,33.
    Numitorul e aici doar cardurile al căror tip îl știm."""
    m = t.first("subject_match")
    if not m or not m.get("subject_type_known"):
        return None
    ids = [i for i in (card_id(c) for c in shown_cards(t.reply)) if i]
    typed = sum(1 for i in ids if t.product_types.get(i))
    if typed < 2:
        return None
    return int(m.get("type_matched") or 0) / typed < SUBJECT_SHARE_MIN


def _resolved_refs(t: Turn) -> list[Mapping[str, Any]]:
    kernel = t.diagnostics.get("kernel") or {}
    return [r for r in (kernel.get("resolved_refs") or []) if isinstance(r, Mapping)]


def ordinal_out_of_range(t: Turn) -> Verdict:
    """Un ordinal («a treia») n-a mai găsit lista la care se referea (ex. după un detaliu)."""
    refs = [r for r in _resolved_refs(t) if r.get("kind") == "ordinal"]
    refs += [r for r in t.all("reference_v2") if r.get("kind") == "ordinal"]
    if not refs:
        return None
    return any(r.get("reason") == "ordinal_out_of_range" for r in refs)


def _kernel_plans(t: Turn) -> list[Mapping[str, Any]]:
    kernel = t.diagnostics.get("kernel") or {}
    return [p for p in (kernel.get("plans") or []) if isinstance(p, Mapping)]


def vague_to_rank_terms(t: Turn) -> Verdict:
    """Kernel: cuvinte fără cheie de catalog («nu ft scump», «mai calitativ») ajung termeni de
    ordonare. Pe calea v1 aceleași cuvinte nu au niciun efect."""
    searches = [p for p in _kernel_plans(t) if p.get("search_args")]
    if not searches:
        return None
    return any((p["search_args"] or {}).get("rank_terms") for p in searches)


def exclusion_unserved(t: Turn) -> Verdict:
    """Kernel: clientul a exclus ceva («nu vreau cu acid hialuronic»), iar căutarea n-o poate
    executa (golul `exclusion`)."""
    kernel = t.diagnostics.get("kernel")
    if not kernel:
        return None
    return "exclusion" in (kernel.get("gaps") or [])


def side_search_overwrote_session(t: Turn) -> Verdict:
    """Modelul a căutat în același tur cu o comparație/un detaliu și a creat o sesiune nouă de
    căutare peste cea în care era clientul."""
    names = {c.get("name") for c in t.all("tool_call")}
    if not names & {"compare_products", "get_product_details"}:
        return None
    new_session = any(s.get("action") == "new" for s in t.all("search_session"))
    return "search_products" in names and new_session


def _rich_handles(t: Turn) -> Mapping[str, str]:
    handles = t.diagnostics.get("rich_handles") or {}
    return handles if isinstance(handles, Mapping) else {}


def card_reason_dropped(t: Turn) -> Verdict:
    """Modelul a scris motivul cardului, iar un scrub l-a aruncat (ex. «orez» citit ca «ore»)."""
    raw = t.diagnostics.get("rich_raw") or {}
    items = raw.get("items") if isinstance(raw, Mapping) else None
    cards = {card_id(c): c for c in shown_cards(t.reply)}
    if not items or not cards:
        return None
    handles = _rich_handles(t)
    for it in items:
        if not isinstance(it, Mapping) or not it.get("fit_clause"):
            continue
        pid = handles.get(str(it.get("product_id")), it.get("product_id"))
        card = cards.get(str(pid))
        if card is not None and not card.get("reason"):
            return True
    return False


def card_without_reason(t: Turn) -> Verdict:
    """Card bogat fără niciun motiv, din orice cauză."""
    rich = t.reply.get("rich") or {}
    items = rich.get("items") if isinstance(rich, Mapping) else None
    if not items:
        return None
    return any(not i.get("reason") for i in items if isinstance(i, Mapping))


def same_name_other_product(t: Turn) -> Verdict:
    """Același nume afișat a numit alt produs mai devreme în conversație (BEAUTY OF JOSEON Dynasty
    la 135,92 și la 93,41 lei)."""
    cards = shown_cards(t.reply)
    if not cards:
        return None
    for c in cards:
        name, pid = c.get("name"), card_id(c)
        if name and pid and (t.earlier_names.get(str(name), frozenset()) - {pid}):
            return True
    return False


def closing_missing(t: Turn) -> Verdict:
    """Forma cerea o închidere („cum alegi”) și n-a ieșit."""
    shape = t.first("answer_shape")
    if not shape or "closing" not in (shape.get("required") or []):
        return None
    return "closing" in (shape.get("missing") or [])


def _turn_usage(t: Turn) -> Mapping[str, Any] | None:
    return next((u for u in t.all("llm_usage") if u.get("phase") in (None, "turn")), None)


def many_model_calls(t: Turn) -> Verdict:
    """Patru sau mai multe apeluri de model pe drumul răspunsului (FAQ-ul de livrare a avut 5).
    Interpretarea kernelului în dark (NX-353) nu se numără: e un cost al măsurătorii, nu al căii."""
    usage = _turn_usage(t)
    if usage is None:
        return None
    per_call = usage.get("per_call") or []
    total = int(usage.get("llm_calls") or len(per_call))
    interpret = sum(1 for c in per_call if c.get("purpose") == "interpret")
    return total - interpret >= MANY_MODEL_CALLS


def slow_turn(t: Turn) -> Verdict:
    lat = next((x for x in t.all("turn_latency") if x.get("phase") in (None, "turn")), None)
    if lat is None or lat.get("e2e_ms") is None:
        return None
    return float(lat["e2e_ms"]) > SLOW_TURN_MS


def mostly_rejected_set(t: Turn) -> Verdict:
    """Căutarea a adus o pagină întreagă, iar compunerea a păstrat ≤ 2: setul era în mare parte
    nepotrivit (semn că un filtru n-a rulat, ex. «păr gras»)."""
    search = t.first("product_search")
    if search is None or (search.get("count") or 0) < FULL_SEARCH:
        return None
    return 0 < len(shown_cards(t.reply)) <= FEW_CARDS


@dataclass(frozen=True)
class Detector:
    key: str
    severity: str
    fn: Callable[[Turn], Verdict]
    what: str


DETECTORS: tuple[Detector, ...] = (
    Detector("refused_set_shown", "P0", refused_set_shown, "set refuzat de model, afișat"),
    Detector("oos_on_card", "P0", oos_on_card, "produs epuizat pe card"),
    Detector("oos_first", "P0", oos_first, "primul card e epuizat"),
    Detector("uttered_need_unknown", "P1", uttered_need_unknown, "valoare cerută necunoscută"),
    Detector("subject_lost", "P1", subject_lost, "sub 1/2 din carduri de tipul cerut"),
    Detector("ordinal_out_of_range", "P1", ordinal_out_of_range, "ordinal fără listă"),
    Detector("exclusion_unserved", "P1", exclusion_unserved, "excludere neexecutată (kernel)"),
    Detector("vague_to_rank_terms", "P1", vague_to_rank_terms, "calificativ vag → rank_terms"),
    Detector(
        "side_search_overwrote_session", "P1", side_search_overwrote_session, "sesiune suprascrisă"
    ),
    Detector("mostly_rejected_set", "P1", mostly_rejected_set, "≤2 carduri din pagină plină"),
    Detector("same_name_other_product", "P2", same_name_other_product, "același nume, alt produs"),
    Detector("card_reason_dropped", "P2", card_reason_dropped, "motiv de card tăiat de scrub"),
    Detector("card_without_reason", "P2", card_without_reason, "card bogat fără motiv"),
    Detector("closing_missing", "P2", closing_missing, "închiderea cerută lipsește"),
    Detector("many_model_calls", "P2", many_model_calls, "≥4 apeluri de model"),
    Detector("slow_turn", "P2", slow_turn, f"tur peste {SLOW_TURN_MS // 1000} s"),
)
_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


# --- agregarea (pură) ----------------------------------------------------------------------------


def with_conversation_context(turns: Iterable[Turn]) -> list[Turn]:
    """Completează `earlier_names`: ce id-uri au stat sub fiecare nume afișat, pe turele de dinainte
    ale aceleiași conversații. PUR; turele se ordonează pe conversație și timp."""
    seen: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    out: list[Turn] = []
    for t in sorted(turns, key=lambda x: (x.conversation_id, x.created_at)):
        names = seen[t.conversation_id]
        snapshot = {n: frozenset(ids) for n, ids in names.items()}
        out.append(
            Turn(
                t.turn_id,
                t.conversation_id,
                t.created_at,
                t.reply,
                t.diagnostics,
                t.events,
                t.availability,
                snapshot,
                t.product_types,
            )
        )
        for c in shown_cards(t.reply):
            name, pid = c.get("name"), card_id(c)
            if name and pid:
                names[str(name)].add(pid)
    return out


def summarize(turns: Sequence[Turn]) -> dict[str, Any]:
    """Pe fiecare detector: ture lovite, ture pe care se aplică, rata, conversații, exemple.
    Ordinea: severitate, apoi rata. PUR."""
    rows = []
    for d in DETECTORS:
        hits: list[Turn] = []
        applicable = 0
        for t in turns:
            verdict = d.fn(t)
            if verdict is None:
                continue
            applicable += 1
            if verdict:
                hits.append(t)
        rows.append(
            {
                "detector": d.key,
                "severity": d.severity,
                "what": d.what,
                "hits": len(hits),
                "applicable": applicable,
                "rate": round(len(hits) / applicable, 3) if applicable >= MIN_APPLICABLE else None,
                "conversations": len({t.conversation_id for t in hits}),
                "examples": [t.turn_id for t in hits[:EXAMPLES]],
            }
        )
    rows.sort(key=lambda r: (_SEVERITY_ORDER[r["severity"]], -(r["rate"] or 0.0), -r["hits"]))
    return {
        "turns": len(turns),
        "conversations": len({t.conversation_id for t in turns}),
        "detectors": rows,
    }


def render_console(report: Mapping[str, Any]) -> str:
    """Tabelul pentru terminal: agregate și id-uri de tur, fără text de client. PUR."""
    lines = [
        f"ture: {report['turns']}  conversații: {report['conversations']}",
        f"{'sev':3}  {'detector':30} {'lovite':>6} {'din':>5} {'rata':>6} {'conv':>4}  exemple",
    ]
    for r in report["detectors"]:
        rate = f"{r['rate']:.1%}" if r["rate"] is not None else f"n<{MIN_APPLICABLE}"
        lines.append(
            f"{r['severity']:3}  {r['detector']:30} {r['hits']:>6} {r['applicable']:>5} "
            f"{rate:>6} {r['conversations']:>4}  {' '.join(t[:8] for t in r['examples'])}"
        )
    return "\n".join(lines)


# --- citirea din DB ------------------------------------------------------------------------------

Opener = Callable[..., AbstractAsyncContextManager[Any]]

BUSINESS_SQL = "select id::text from businesses where slug = $1 or id::text = $1"
TRACES_SQL = """
select turn_id::text as turn_id, conversation_id::text as conversation_id, created_at,
       reply, diagnostics
from conversation_traces
where business_id = $1::uuid and created_at >= $2 and created_at < $3
"""
EVENTS_SQL = """
select turn_id::text as turn_id, event_type, properties
from analytics_events
where business_id = $1::uuid and created_at >= $2 and created_at < $3
  and turn_id = any($4::uuid[])
"""
AVAILABILITY_SQL = """
select p.id::text as id, p.availability, p.attributes ->> 'product_type' as product_type
from products p
where p.business_id = $1::uuid and p.id = any($2::uuid[])
"""


def _json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _uuid_like(value: str) -> bool:
    return len(value) == 36 and value.count("-") == 4


async def load(
    business: str, since: datetime, until: datetime, *, admin: Opener, tenant: Opener
) -> list[Turn]:
    """Turele ferestrei, cu evenimentele și disponibilitatea produselor arătate. `admin()` /
    `tenant(business_id)` deschid conexiunile (injectate, ca testul să ruleze pe una falsă)."""
    async with admin() as conn:
        business_id = await conn.fetchval(BUSINESS_SQL, business)
    if business_id is None:
        raise SystemExit(f"tenant necunoscut: {business!r}")
    async with tenant(business_id) as conn:
        trace_rows = await conn.fetch(TRACES_SQL, business_id, since, until)
    turn_ids = [r["turn_id"] for r in trace_rows]
    events: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    if turn_ids:
        # `analytics_events` e append-only pentru `bot_runtime` (doar INSERT): citirea e pe
        # conexiunea de operator, cu `business_id` explicit (P7), ca la NX-316/NX-353.
        async with admin() as conn:
            for r in await conn.fetch(
                EVENTS_SQL, business_id, since, until + timedelta(minutes=5), turn_ids
            ):
                events[r["turn_id"]][r["event_type"]].append(_json(r["properties"]))
    replies = {r["turn_id"]: _json(r["reply"]) for r in trace_rows}
    wanted = sorted(
        {
            i
            for rep in replies.values()
            for i in (card_id(c) for c in shown_cards(rep))
            if i and _uuid_like(i)
        }
    )
    availability: dict[str, str | None] = {}
    product_types: dict[str, str | None] = {}
    if wanted:
        async with tenant(business_id) as conn:
            for r in await conn.fetch(AVAILABILITY_SQL, business_id, wanted):
                availability[r["id"]] = r["availability"]
                product_types[r["id"]] = r["product_type"]
    turns = [
        Turn(
            turn_id=r["turn_id"],
            conversation_id=r["conversation_id"],
            created_at=r["created_at"].isoformat()
            if hasattr(r["created_at"], "isoformat")
            else str(r["created_at"]),
            reply=replies[r["turn_id"]],
            diagnostics=_json(r["diagnostics"]),
            events={k: tuple(v) for k, v in events.get(r["turn_id"], {}).items()},
            availability={
                i: availability.get(i)
                for i in (card_id(c) for c in shown_cards(replies[r["turn_id"]]))
                if i
            },
            product_types={
                i: product_types.get(i)
                for i in (card_id(c) for c in shown_cards(replies[r["turn_id"]]))
                if i
            },
        )
        for r in trace_rows
    ]
    return with_conversation_context(turns)


async def _main(business: str, since: datetime, until: datetime) -> dict[str, Any]:
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415

    try:
        pool = await get_pool()
        turns = await load(
            business, since, until, admin=lambda: admin_conn(pool), tenant=tenant_conn
        )
    finally:
        await close_pool()
    report = summarize(turns)
    report["window"] = {
        "business": business,
        "since": since.isoformat(),
        "until": until.isoformat(),
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
    args = parser.parse_args(argv)
    until = args.until or datetime.now(UTC)
    since = args.since or until - timedelta(days=7)
    report = asyncio.run(_main(args.business, since, until))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"report-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(render_console(report))
    print(f"raport local: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
