"""NX-316 — sonda de APĂSARE pe trafic REAL: ce se întâmplă când clientul apasă un chip?

`scripts/chip_press_probe.py` (NX-296) răspunde la „ce chips POATE emite tenantul". Sonda asta
răspunde la „ce s-a întâmplat EFECTIV după apăsare", pe `conversation_traces`:

  1. **Cât de des se apasă.** Un mesaj de client identic, cuvânt cu cuvânt (fără diacritice și
     punctuație, ca `words_of`), cu un chip din turul ANTERIOR = o apăsare. Pe contractul v1 nu
     există alt semn: apăsarea retrimite textul ca mesaj nou.
  2. **Ce fel de mutare era.** Clasificat pe ȘABLOANELE pachetului (`chip_templates`), nu pe
     cuvinte scrise aici (P11). Un chip care nu se potrivește niciunui șablon e `other` (chips de
     comparație / recenzii din copy-ul determinist, sugestiile modelului de dinainte de NX-296).
  3. **Cine l-a servit.** Din evenimentele turului de apăsare (`analytics_events.turn_id`):
     `detail_intent` / `review_intent` / `link_intent` / `agent_compared` = handler determinist,
     altfel `agent` (bucla de model). Pentru `link` se numără și câte linkuri au plecat: handlerul
     de azi ignoră numele din chip și servește TOT setul afișat.
  4. **Perechea comparată.** Pe o apăsare de `compare`, produsele comparate sunt cele NUMITE în
     chip, sau primele două afișate?

  5. **NX-338, `--recognize`:** apăsarea ar fi RECUNOSCUTĂ de `chip_press.recognize`, cu V2 stins
     (producția) și aprins? Setul oferit se reconstruiește din `recommended` al turului anterior
     (traceul nu ține `offered_chips`), felul se ia din șabloane, independent, deci un fel cu
     handler oferit și nerecunoscut e `pressable_miss` (poarta: zero).

ZERO scriere, ZERO model. Tenant-scoped (`tenant_conn`), `business_id` explicit în fiecare query.

    PYTHONPATH=. python scripts/nx316_chip_press_probe.py --business sole-ro --days 30 [--recognize]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from src.catalog.clarify_menu import words_of
from src.catalog.render_text import display_name
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn
from src.db.queries.businesses import load_business

_HANDLER_EVENTS = ("detail_intent", "review_intent", "link_intent", "agent_compared")


def _chips_of(reply: dict[str, Any] | None) -> list[str]:
    """Textele apăsabile ale unui răspuns: chips bogate, sugestii, chips de comparație."""
    if not isinstance(reply, dict):
        return []
    out: list[str] = []
    rich = reply.get("rich") or {}
    comparison = reply.get("comparison") or {}
    for source in (rich.get("chips"), reply.get("suggestions"), comparison.get("chips")):
        for chip in source or ():
            text = chip.get("label") if isinstance(chip, dict) else chip
            if isinstance(text, str) and text.strip():
                out.append(text)
    return out


def _cards_of(reply: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(reply, dict):
        return []
    return [p for p in reply.get("products") or () if isinstance(p, dict)]


def _template_patterns(pack: Any, locale: str) -> list[tuple[str, re.Pattern[str]]]:
    """Șablonul fiecărei mutări → regex pe textul PLIAT, cu sloturile ca grupuri numite."""
    table = getattr(pack, "chip_templates", None) or {}
    out: list[tuple[str, re.Pattern[str]]] = []
    for kind, per_locale in table.items():
        template = per_locale.get(locale) if isinstance(per_locale, dict) else None
        if not isinstance(template, str):
            continue
        body = re.escape(
            " ".join(words_of(template.replace("{slot_b}", " SLOTB ").replace("{slot}", " SLOTA ")))
        )
        body = body.replace("slota", r"(?P<a>.+?)").replace("slotb", r"(?P<b>.+?)")
        out.append((kind, re.compile(rf"^{body}$")))
    return out


def _classify(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> tuple[str, dict]:
    norm = " ".join(words_of(text))
    for kind, pattern in patterns:
        m = pattern.match(norm)
        if m:
            return kind, {k: v for k, v in m.groupdict().items() if v}
    return "other", {}


def _named(slot: str, cards: list[dict[str, Any]]) -> str | None:
    """Produsul afișat pe care îl numește un slot de chip (prefix de cuvinte al numelui scurt)."""
    want = slot.split()
    hits = [
        str(c.get("product_id") or c.get("id"))
        for c in cards
        if words_of(display_name(str(c.get("name") or "")))[: len(want)] == want
    ]
    return hits[0] if len(hits) == 1 else None


def _move_shaped(chip: str, kind: str, pack: Any, locale: str, cards: list[dict[str, Any]]) -> bool:
    """NX-338: textul are FORMA unei mutări din carduri? Există o împărțire a lui pe șablonul
    felului în care fiecare slot e prefixul de cuvinte al numelui scurt al CEL PUȚIN unui produs
    afișat. Nu unul singur: două carduri cu același nume dau două mutări cu același text, pe care
    recunoașterea le refuză (ambiguu), deci exact o ratare de văzut. Și nu o singură tăietură: un
    nume care conține cuvântul de legătură al șablonului («... cu ...») ar fi tăiat greșit de un
    regex, iar ratarea s-ar ascunde tot în `not_a_move`."""
    per_locale = (getattr(pack, "chip_templates", None) or {}).get(kind)
    template = per_locale.get(locale) if isinstance(per_locale, dict) else None
    if not isinstance(template, str):
        return False
    tokens = words_of(template.replace("{slot_b}", " SLOTB ").replace("{slot}", " SLOTA "))
    said = words_of(chip)
    names = [words_of(display_name(str(c.get("name") or ""))) for c in cards]

    def names_a_card(part: list[str]) -> bool:
        return any(n[: len(part)] == part for n in names)

    def match(t: int, s: int) -> bool:
        if t == len(tokens):
            return s == len(said)
        if tokens[t] not in ("slota", "slotb"):
            return s < len(said) and said[s] == tokens[t] and match(t + 1, s + 1)
        return any(
            names_a_card(said[s:end]) and match(t + 1, end) for end in range(s + 1, len(said) + 1)
        )

    return bool(tokens) and match(0, 0)


#: NX-338: felurile din MENIU sunt fraze de căutare (fără handler de mutare): o apăsare a lor pleacă
#: la interpretare prin construcție, corect.
_MENU_KINDS = frozenset({"refine_facet", "pivot_shelf", "price_band"})


def _shown_refs(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """Setul afișat, cum îl vede recunoașterea (`state.displayed_products` = ref-uri `{id, nume,
    preț}`): `recommended` al turului, altfel cardurile din răspuns."""
    refs = turn.get("recommended") or _cards_of(turn.get("reply"))
    out = []
    for r in refs or ():
        if not isinstance(r, dict):
            continue
        pid = r.get("product_id") or r.get("id")
        if pid and r.get("name"):
            out.append({"product_id": str(pid), "name": str(r["name"]), "price": r.get("price")})
    return out


def recognize_press(
    chip: str,
    client_text: str,
    cards: list[dict[str, Any]],
    pack: Any,
    locale: str,
    *,
    unique_anchor: bool,
) -> Any:
    """NX-338: `chip_press.recognize` pe o apăsare istorică, prin funcțiile PRODUCȚIEI.

    `conversation_traces` nu ține `offered_chips`, deci setul oferit se reconstruiește: mutările
    din cardurile turului anterior (`from_cards` → `renderable` → `render_move`) al căror text e
    chiar chip-ul arătat. Șabloanele le alege `chip_moves` pe flagul V2 din setări, ca în tur."""
    from src.conversation import chip_moves, chip_press  # noqa: PLC0415

    moves = chip_moves.renderable(
        chip_moves.from_cards(cards, unique_anchor=unique_anchor, locale=locale), pack, locale
    )
    want = words_of(chip)
    offered = []
    for move in moves:
        text = chip_moves.render_move(move, pack, locale)
        if text is not None and words_of(text) == want:
            offered.append(move.move_id)
    return chip_press.recognize(
        client_text, cards, offered, pack, locale, unique_anchor=unique_anchor
    )


#: Felurile din carduri: sloturile lor NUMESC produse afișate.
_CARD_KINDS = frozenset({"detail", "reviews", "link", "compare"})


def _miss_class(
    kind: str,
    chip: str,
    reply: dict[str, Any] | None,
    cards: list[dict[str, Any]],
    pack: Any,
    locale: str,
) -> str:
    """Clasa unei apăsări NErecunoscute. Șablonul singur nu face dintr-un text o mutare: «compară
    {a} cu {b}» prinde și chip-ul scris de model («Compară IUNIK cu BELIF pentru ten uscat»), și pe
    cel determinist de sub detaliu («Compară-l cu un produs similar»), și unul vechi trunchiat cu
    „…". O mutare din carduri are forma din `_move_shaped` (fiecare slot numește cel puțin un
    produs afișat, independent de randarea mutărilor, deci o ratare reală rămâne vizibilă). Altfel
    `not_a_move`: text care doar seamănă cu șablonul."""
    from src.conversation.chip_press import PRESSABLE  # noqa: PLC0415

    comparison = (reply or {}).get("comparison") or {}
    compare_chips = {
        " ".join(words_of(c.get("label") if isinstance(c, dict) else c or ""))
        for c in comparison.get("chips") or ()
    }
    if " ".join(words_of(chip)) in compare_chips:
        return "comparison_chip"
    if kind in _CARD_KINDS and not _move_shaped(chip, kind, pack, locale, cards):
        return "not_a_move"
    if kind in PRESSABLE:
        return "pressable_miss"
    if kind in _MENU_KINDS:
        return "menu"
    return "other"


def recognition_report(
    rows: list[dict[str, Any]],
    pack: Any,
    locale: str,
    *,
    unique_anchor: bool,
    arms: tuple[bool, ...] = (False, True),
) -> dict[str, Any]:
    """Pe fiecare apăsare: recunoscută sau nu, pe brațul V2 stins (producția) și aprins.

    Felul se ia din ȘABLOANE (`_classify`), independent de recunoaștere, deci o ratare pe un fel
    `PRESSABLE` e un defect vizibil (`pressable_miss`), nu o absență. Brațul aprins e informativ:
    textul istoric a fost randat cu `chip_templates`, deci pe felurile cu `chip_templates_v2`
    diferit numără mai puțin, prin construcție."""
    from src.config import get_settings  # noqa: PLC0415

    patterns = _template_patterns(pack, locale)
    settings = get_settings()
    saved = settings.chip_moves_v2_enabled
    presses: list[dict[str, Any]] = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        if prev["conv"] != cur["conv"]:
            continue
        said = words_of(cur.get("client_text") or "")
        chip = next((c for c in _chips_of(prev.get("reply")) if words_of(c) == said), None)
        if chip is None:
            continue
        kind, _ = _classify(chip, patterns)
        cards = _shown_refs(prev)
        entry: dict[str, Any] = {"kind": kind}
        try:
            for v2 in arms:
                settings.chip_moves_v2_enabled = v2
                move = recognize_press(
                    chip,
                    cur.get("client_text") or "",
                    cards,
                    pack,
                    locale,
                    unique_anchor=unique_anchor,
                )
                entry["on" if v2 else "off"] = move is not None
        finally:
            settings.chip_moves_v2_enabled = saved
        entry["miss"] = (
            None
            if entry.get("off")
            else _miss_class(kind, chip, prev.get("reply"), cards, pack, locale)
        )
        presses.append(entry)

    by_kind: dict[str, dict[str, int]] = {}
    for p in presses:
        row = by_kind.setdefault(p["kind"], {"presses": 0, "off": 0, "on": 0})
        row["presses"] += 1
        row["off"] += int(bool(p.get("off")))
        row["on"] += int(bool(p.get("on")))
    return {
        "presses": len(presses),
        "recognized_off": sum(1 for p in presses if p.get("off")),
        "recognized_on": sum(1 for p in presses if p.get("on")),
        "by_kind": by_kind,
        "misses": dict(Counter(p["miss"] for p in presses if p["miss"])),
        "pressable_miss": sum(1 for p in presses if p["miss"] == "pressable_miss"),
    }


async def probe(slug: str, days: int, *, recognize: bool = False) -> dict[str, Any]:
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
        locale = (business.default_locale if business else None) or "ro"
        patterns = _template_patterns(getattr(business, "domain_pack", None), locale)
        traces = await conn.fetch(
            """
            select conversation_id::text as conv, turn_id::text as turn_id, client_text, reply,
                   recommended
            from conversation_traces
            where business_id = $1::uuid and created_at > now() - make_interval(days => $2)
            order by conversation_id, created_at
            """,
            business_id,
            days,
        )
        rows = [
            {
                **dict(t),
                "reply": json.loads(t["reply"]) if isinstance(t["reply"], str) else t["reply"],
                "recommended": (
                    json.loads(t["recommended"])
                    if isinstance(t["recommended"], str)
                    else t["recommended"]
                ),
            }
            for t in traces
        ]
        turn_ids = [r["turn_id"] for r in rows]
    # `analytics_events` e append-only pentru `bot_runtime` (doar INSERT), deci citirea e pe
    # conexiunea de operator, cu `business_id` explicit (P7), ca la celelalte sonde.
    async with admin_conn(pool) as admin:
        events = await admin.fetch(
            """
            select turn_id::text as turn_id, event_type, properties
            from analytics_events
            where business_id = $1::uuid and turn_id::text = any($2::text[])
              and event_type = any($3::text[])
            """,
            business_id,
            turn_ids,
            list(_HANDLER_EVENTS),
        )
    by_turn: dict[str, list[tuple[str, dict]]] = {}
    for e in events:
        props = e["properties"]
        props = json.loads(props) if isinstance(props, str) else (props or {})
        by_turn.setdefault(e["turn_id"], []).append((e["event_type"], props))

    after_chips = 0
    presses: list[dict[str, Any]] = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        if prev["conv"] != cur["conv"]:
            continue
        chips = _chips_of(prev["reply"])
        if not chips:
            continue
        after_chips += 1
        said = words_of(cur["client_text"] or "")
        match = next((c for c in chips if words_of(c) == said), None)
        if match is None:
            continue
        kind, slots = _classify(match, patterns)
        handled = by_turn.get(cur["turn_id"], [])
        handler = handled[0][0] if handled else "agent"
        entry: dict[str, Any] = {"kind": kind, "handler": handler}
        shown = _cards_of(prev["reply"])
        if handler == "link_intent":
            entry["links_served"] = int(handled[0][1].get("served") or 0)
        if kind == "compare" and slots:
            want = [_named(slots.get("a", ""), shown), _named(slots.get("b", ""), shown)]
            cols = ((cur["reply"] or {}).get("comparison") or {}).get("columns") or []
            got = [str(c.get("product_id")) for c in cols]
            # Judecat DOAR când ambele sloturi numesc câte un produs afișat. „Compară-l cu un
            # produs similar" se potrivește pe șablon (`compara {slot} cu {slot_b}`) fără să
            # numească nimic, iar numărat „pereche greșită" ar fi fost un defect al sondei.
            entry["pair_named"] = (set(want) <= set(got)) if all(want) else None
        presses.append(entry)

    kinds = Counter(p["kind"] for p in presses)
    handlers = Counter(f"{p['kind']} → {p['handler']}" for p in presses)
    extra: dict[str, Any] = {}
    if recognize:
        from src.config import get_settings  # noqa: PLC0415

        extra["recognition"] = recognition_report(
            rows,
            getattr(business, "domain_pack", None),
            locale,
            unique_anchor=bool(getattr(get_settings(), "unique_name_prefix_enabled", False)),
        )
    return {
        **extra,
        "business": slug,
        "days": days,
        "traces": len(rows),
        "turns_after_chips": after_chips,
        "presses": len(presses),
        "press_rate": round(len(presses) / after_chips, 3) if after_chips else None,
        "by_kind": dict(kinds),
        "by_handler": dict(handlers),
        "link_presses_serving_more_than_one": sum(
            1 for p in presses if p["kind"] == "link" and p.get("links_served", 0) > 1
        ),
        "compare_presses_wrong_pair": sum(
            1 for p in presses if p["kind"] == "compare" and p.get("pair_named") is False
        ),
    }


def _print(r: dict[str, Any]) -> None:
    print(f"tenant: {r['business']}   fereastră: {r['days']} zile   ture capturate: {r['traces']}")
    print(f"ture după un răspuns cu chips: {r['turns_after_chips']}")
    print(f"apăsări (mesaj identic cu un chip): {r['presses']}   rată: {r['press_rate']}")
    for kind, n in sorted(r["by_kind"].items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<14} {n}")
    print("cine a servit apăsarea:")
    for key, n in sorted(r["by_handler"].items(), key=lambda kv: -kv[1]):
        print(f"  {key:<34} {n}")
    print(f"link apăsat → mai mult de un link servit: {r['link_presses_serving_more_than_one']}")
    print(f"compare apăsat → altă pereche decât cea numită: {r['compare_presses_wrong_pair']}")
    rec = r.get("recognition")
    if rec is None:
        return
    print("NX-338 — recunoașterea (V2 stins = producția; aprins = informativ):")
    print(
        f"  apăsări: {rec['presses']}   recunoscute stins: {rec['recognized_off']}"
        f"   aprins: {rec['recognized_on']}"
    )
    for kind, row in sorted(rec["by_kind"].items(), key=lambda kv: -kv[1]["presses"]):
        print(f"  {kind:<14} {row['presses']:>3}   stins {row['off']:>3}   aprins {row['on']:>3}")
    print(f"  nerecunoscute pe clase (stins): {rec['misses']}")
    verdict = "PASS" if rec["pressable_miss"] == 0 else "FAIL"
    print(f"  poarta pressable_miss == 0: {verdict} ({rec['pressable_miss']})")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument(
        "--recognize", action="store_true", help="NX-338: rulează recunoașterea pe apăsări"
    )
    args = ap.parse_args()
    try:
        report = await probe(args.business, args.days, recognize=args.recognize)
    finally:
        await close_pool()
    _print(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
