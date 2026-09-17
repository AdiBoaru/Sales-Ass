"""NX-296 — proba de APĂSARE: ce sugestii poate emite azi tenantul, și duc ele undeva?

Un test spune „regula e implementată". Proba asta spune „pe catalogul ăsta, în turul ăsta, ies
cinci sugestii sau două, iar dacă le apeși ajungi la produse sau în gol". A doua întrebare e
singura care contează pentru un chip, fiindcă apăsarea lui retrimite textul ca MESAJ NOU al
clientului — un chip care nu se rezolvă e o promisiune pe care magazinul n-o poate onora.

Ce verifică, pentru fiecare tur simulat:

  1. **Acoperire.** Câte sloturi din `CHIP_SLOTS` se umplu efectiv și cu ce ROLURI. Un tur cu
     cinci îngustări și nicio cale laterală e o degradare pe care numai măsurătoarea o arată.
  2. **Apăsarea.** Fiecare chip emis se trece prin ACEEAȘI `resolve_any` pe care o folosește
     căutarea. Ancora trebuie să se rezolve pe o cheie cu produse (`evidence > 0`), altfel
     chip-ul e raportat ca `dead`. Mutările ancorate pe produse afișate se verifică pe
     identitate, nu pe vocabular: numele lor nu e vocabular de catalog.
  3. **Onestitatea plafonului.** Niciun chip peste `MAX_CHIP_LEN`, nicio ancoră trunchiată.

ZERO OpenAI (modelul nu e chemat deloc: probăm partea DETERMINISTĂ, adică exact ce rămâne când
modelul tace). ZERO scriere. Tenant-scoped, prin `tenant_conn`.

Rulare:

    python scripts/chip_press_probe.py --business sole-ro
    python scripts/chip_press_probe.py --business sole-ro --json reports/nx296/press.json

Ce NU măsoară, declarat: cât de natural sună reformularea modelului. Aia cere un apel pe trafic
real și se rulează O SINGURĂ dată, ca gate, nu la fiecare iterație.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.catalog.clarify_menu import build_menu, contains_run, words_of
from src.catalog.vocabulary import ResolutionStatus, facet_overlays, resolve_any, topic_root_of
from src.catalog.vocabulary_cache import get_vocabulary
from src.config import chip_slots, get_settings
from src.conversation import chip_moves
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn
from src.db.queries.businesses import load_business
from src.db.queries.catalog import facet_keys_in_scope
from src.models import MAX_CHIP_LEN

#: Turele simulate. Fiecare e o stare plauzibilă a conversației, nu un mesaj: mutările se
#: construiesc din CATALOG plus ce s-a arătat, nu din text. Perechea (raft, obligație) e ce
#: schimbă mixul de roluri, deci exact ce vrem să vedem variind.
SCENARIOS: tuple[tuple[str, str | None, str], ...] = (
    ("primul tur, fără raft", None, "clarify"),
    ("recomandare pe raftul discutat", "__topic__", "recommend"),
    ("întrebare factuală despre un produs", "__topic__", "answer"),
)


@dataclass
class _FakeDeps:
    """`deps.db(op)` peste o conexiune deja deschisă — proba ține un singur checkout, nu unul
    per apel, fiindcă e un script de raport, nu drumul fierbinte (NX-231 e despre ture)."""

    conn: Any

    def db(self, _op: str):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def _sample_cards(conn: Any, business_id: str, limit: int = 3) -> list[dict[str, Any]]:
    """Trei produse reale, ca „ce tocmai s-a arătat". Nu contează CARE: mutările derivate din
    carduri nu depind de conținut, ci de identitate, nume și preț."""
    rows = await conn.fetch(
        """
        select p.id::text as product_id, p.name as name,
               coalesce(p.sale_price, p.price)::float8 as price
        from products p
        where p.business_id = $1::uuid and p.status = 'active'
        order by p.review_count desc nulls last
        limit $2
        """,
        business_id,
        limit,
    )
    return [dict(r) for r in rows]


def _press(move: chip_moves.ChipMove, text: str, vocab: Any, overlays: Any) -> str:
    """Verdictul apăsării: `resolves` | `product_ref` | `numeric` | `dead` | `anchor_lost`.

    `product_ref` și `numeric` nu sunt scuze, sunt clase diferite de ancoră: numele unui produs
    afișat se rezolvă prin `reference_resolver` pe starea conversației, iar o bandă de preț prin
    extragerea de constrângere. Raportate separat tocmai ca să nu se ascundă printre cele
    verificate pe vocabular.
    """
    if not contains_run(words_of(text), words_of(move.anchor)):
        return "anchor_lost"
    if move.kind in ("refine_facet", "pivot_shelf"):
        r = resolve_any(vocab, move.anchor, overlays=overlays)
        return (
            "resolves" if (r.status is not ResolutionStatus.UNKNOWN and r.evidence > 0) else "dead"
        )
    if move.kind == "price_band":
        # Ancora e o cifră, nu vocabular: se rezolvă ca CONSTRÂNGERE (`budget_max`), pe calea
        # obișnuită a unui mesaj de client. Clasă separată tocmai ca să nu treacă drept verificată.
        return "numeric"
    return "product_ref"


async def probe(slug: str) -> dict[str, Any]:
    settings = get_settings()
    slots = chip_slots(settings)
    pool = await get_pool()
    async with admin_conn(pool) as admin:
        row = await admin.fetchrow(
            "select id::text as id from businesses where slug = $1 or id::text = $1", slug
        )
    if row is None:
        raise SystemExit(f"tenant necunoscut: {slug!r} (nici slug, nici uuid)")

    async with tenant_conn(row["id"]) as conn:
        business = await load_business(conn, row["id"])
        if business is None:
            raise SystemExit(f"tenant ilizibil: {slug!r}")
        deps = _FakeDeps(conn)
        vocab = await get_vocabulary(deps, business.id)
        pack = business.domain_pack
        overlays = facet_overlays(pack, vocab.facet_names)
        cards = await _sample_cards(conn, business.id)
        # Raftul cu cele mai multe produse: cazul realist, și cel mai sever pentru fațete.
        top_shelf = max(
            (e for e in vocab.categories if not e.depth), key=lambda e: e.count, default=None
        )
        topic = topic_root_of(vocab, top_shelf.key) if top_shelf else None
        from src.catalog.clarify_menu import CATEGORY_DIMENSION, menu_dimensions

        facet_dims = tuple(d for d in menu_dimensions(vocab, pack) if d != CATEGORY_DIMENSION)

        turns: list[dict[str, Any]] = []
        for label, shelf, obligation in SCENARIOS:
            scoped = None
            active_topic = topic if shelf == "__topic__" else None
            if active_topic:
                slugs = tuple(
                    sorted(
                        e.key
                        for e in vocab.categories
                        if e.path
                        and (e.path == active_topic or e.path.startswith(f"{active_topic}/"))
                    )
                )
                scoped = await facet_keys_in_scope(
                    conn, business.id, dimensions=facet_dims, category_slugs=slugs
                )
            menu = build_menu(
                vocab,
                pack,
                locale=business.default_locale or "ro",
                topic=active_topic,
                scoped_keys=scoped,
            )
            locale = business.default_locale or "ro"
            candidates = [
                *chip_moves.renderable(chip_moves.from_menu(menu), pack, locale),
                *chip_moves.renderable(
                    chip_moves.from_cards(cards) if obligation != "clarify" else [], pack, locale
                ),
            ]
            picked = chip_moves.select(
                candidates, slots=slots, role_order=chip_moves.roles_for([obligation])
            )
            texts, _stats = chip_moves.apply_labels(picked, {}, pack, locale)
            chips = []
            for move, text in zip(picked, texts, strict=False):
                chips.append(
                    {
                        "kind": move.kind,
                        "role": move.role,
                        "evidence": move.evidence,
                        "text": text,
                        "press": _press(move, text, vocab, overlays),
                        "too_long": len(text) > MAX_CHIP_LEN,
                    }
                )
            turns.append(
                {
                    "scenario": label,
                    "obligation": obligation,
                    "slots": slots,
                    "filled": len(chips),
                    "roles": sorted({c["role"] for c in chips}),
                    "chips": chips,
                }
            )

    press = Counter(c["press"] for t in turns for c in t["chips"])
    total = sum(press.values())
    dead = press.get("dead", 0) + press.get("anchor_lost", 0)
    return {
        "business": slug,
        "chip_slots": slots,
        "turns": turns,
        "totals": {
            "chips": total,
            "press": dict(press),
            "dead": dead,
            # Verdictul e binar și sever: o singură sugestie moartă e o promisiune pe care
            # magazinul n-o poate onora, iar clientul o apasă exact ca pe celelalte.
            "verdict": "PASS" if (total and not dead) else ("NOT-READY" if not total else "FAIL"),
        },
    }


def _print(report: dict[str, Any]) -> None:
    print(f"tenant: {report['business']}   sloturi: {report['chip_slots']}")
    for turn in report["turns"]:
        print(f"\n— {turn['scenario']} [{turn['obligation']}] → {turn['filled']}/{turn['slots']}")
        print(f"  roluri: {', '.join(turn['roles']) or '(niciunul)'}")
        for chip in turn["chips"]:
            flag = "" if chip["press"] not in ("dead", "anchor_lost") else "  <== MORT"
            print(f"  [{chip['kind']:<12} {chip['press']:<11}] {chip['text']}{flag}")
    totals = report["totals"]
    print(f"\ntotal: {totals['chips']} chips, {totals['dead']} moarte → {totals['verdict']}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", default="sole-ro", help="slug de tenant")
    ap.add_argument("--json", type=Path, default=None, help="scrie raportul și ca JSON")
    args = ap.parse_args()
    try:
        report = await probe(args.business)
    finally:
        await close_pool()
    _print(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
