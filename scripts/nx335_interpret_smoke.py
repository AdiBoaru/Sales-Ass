"""NX-335 felia 5c — proba furnizorului: acceptă schema de interpretare a tenantului?

Construiește schema și promptul REALE ale tenantului (pachetul din `businesses.settings`,
vocabularul și meniul de rafturi din DB, read-only), pe o stare goală, și le trece prin adaptorul
de interpretare (`src/conversation/turn_interpreter.py`).

- implicit (`--dry-run`): ZERO apeluri. Scrie schema, promptul, tokenii estimați și
  `vocabulary_snapshot`;
- `--yes`: face UN apel și tipărește `outcome` + interpretarea. Done-ul contractului („Provider
  accepts the schema") = `outcome=ok` pe acest apel. **Consumă credite OpenAI; îl rulează Adi.**

`--message` e obligatoriu: un mesaj scris în script ar fi un literal de domeniu (poarta NX-264
scanează `scripts/`) și o frază în limba clientului în cod (P11).

    PYTHONPATH=. python scripts/nx335_interpret_smoke.py --business sole-ro --message "<text>"
    PYTHONPATH=. python scripts/nx335_interpret_smoke.py --business sole-ro --message "<text>" --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.conversation import turn_interpreter as ti  # noqa: E402
from src.conversation.interpretation_check import snapshot_id  # noqa: E402
from src.conversation.state_v2 import ConversationStateV2  # noqa: E402

#: Estimare grosieră (≈ 4 caractere pe token), ca în raportul de replay.
CHARS_PER_TOKEN = 4


async def load_input(business_ref: str, message: str) -> tuple[str, ti.InterpretInput]:
    """(business_id, intrarea adaptorului) pentru tenant, pe o stare goală. DB read-only."""
    from src.catalog.vocabulary import load_vocabulary  # noqa: PLC0415
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415
    from src.db.queries.businesses import load_business  # noqa: PLC0415
    from src.db.queries.catalog import list_category_menu  # noqa: PLC0415

    try:
        pool = await get_pool()
        async with admin_conn(pool) as admin:
            bid = await admin.fetchval(
                "select id::text from businesses where slug = $1 or id::text = $1", business_ref
            )
        if bid is None:
            raise SystemExit(f"tenant necunoscut: {business_ref!r}")
        async with tenant_conn(bid) as conn:
            business = await load_business(conn, bid)
            vocab = await load_vocabulary(conn, bid)
            menu = await list_category_menu(conn, bid)
    finally:
        await close_pool()
    if business is None:
        raise SystemExit("businessul nu s-a putut încărca")
    return bid, ti.InterpretInput(
        locale=business.default_locale,
        pack=business.domain_pack,
        vocab=vocab,
        category_menu=tuple(menu),
        state=ConversationStateV2(),
        history=(),
        message=message,
    )


def describe(inp: ti.InterpretInput) -> dict[str, Any]:
    """Ce ar pleca pe sârmă, fără să plece: schema, promptul, mărimile, amprenta vocabularului."""
    schema = ti.interpretation_schema(inp.pack)
    system, user = ti.system_prompt(inp), ti.user_prompt(inp)
    schema_chars = len(json.dumps(schema, ensure_ascii=False))
    return {
        "schema": schema,
        "system": system,
        "user": user,
        "tokens_estimated": {
            "system": len(system) // CHARS_PER_TOKEN,
            "user": len(user) // CHARS_PER_TOKEN,
            "schema": schema_chars // CHARS_PER_TOKEN,
        },
        "vocabulary_snapshot": snapshot_id(inp.pack, inp.vocab),
    }


async def main(argv: list[str] | None = None, *, loader=load_input, llm_factory=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--message", required=True, help="mesajul clientului (obligatoriu)")
    ap.add_argument("--dry-run", action="store_true", help="implicit; zero apeluri de model")
    ap.add_argument("--yes", action="store_true", help="UN apel real (consumă credite)")
    args = ap.parse_args(argv)

    bid, inp = await loader(args.business, args.message)
    info = describe(inp)
    print("=== SCHEMA ===")
    print(json.dumps(info["schema"], ensure_ascii=False, indent=2))
    print("\n=== SYSTEM ===")
    print(info["system"])
    print("\n=== USER ===")
    print(info["user"])
    print("\n=== ESTIMARE ===")
    print(json.dumps(info["tokens_estimated"], indent=2))
    print(f"vocabulary_snapshot: {info['vocabulary_snapshot']}")
    if args.dry_run or not args.yes:
        print("\nZero apeluri. Apelul real (UN apel, consumă credite) cere --yes; îl rulează Adi.")
        return 0

    if llm_factory is None:
        from src.agent.llm import get_llm  # noqa: PLC0415

        llm_factory = get_llm
    llm = llm_factory()
    if llm is None:
        raise SystemExit("lipsește OPENAI_API_KEY")
    out = await ti.interpret_turn(llm, inp, business_id=bid)
    print(f"\noutcome: {out.outcome}")
    if out.interpretation is not None:
        print(json.dumps(out.interpretation.model_dump(), ensure_ascii=False, indent=2))
    print(json.dumps(out.event, ensure_ascii=False, indent=2))
    return 0 if out.outcome == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
