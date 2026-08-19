"""NX-248 — preflight: ce trebuie adevărat ÎNAINTE ca un deploy să atingă un host.

Un deploy eșuat la jumătate costă mult mai mult decât un deploy refuzat la început. Preflightul
răspunde, read-only, la întrebările care decid dacă mai are rost să continuăm:

  1. **Migrări** — există pending? drift de checksum? (`scripts/migrate.py --check`, DSN de citire)
  2. **Compatibilitate de schemă** — imaginea care urmează tolerează schema APLICATĂ? Prea veche
     ⇒ rulează întâi migrarea; prea nouă ⇒ promovezi imaginea greșită.
  3. **Fezabilitatea ROLLBACKULUI** — imaginea precedentă mai tolerează schema curentă? Dacă nu,
     `--require-rollback-possible` oprește releaseul ACUM. Cardul cere exact asta: „rollbackul este
     declarat imposibil înainte de deploy și releaseul se blochează".
  4. **Config** — `Settings` se construiește (poarta de boot a tuturor flagurilor imposibile) și
     revizia de config coincide cu cea din manifest, dacă manifestul e dat.

Nu scrie nimic, nu pornește nimic, nu apelează niciun model. Cod ≠ 0 = nu deploya.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from scripts.migrate import _connect_kwargs, migration_state  # noqa: E402
from src.config import Settings  # noqa: E402
from src.ops.build_info import (  # noqa: E402
    SCHEMA_FORWARD_TOLERANCE,
    bundled_schema_version,
    config_revision,
)
from src.ops.manifest import ManifestError, load  # noqa: E402

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2


async def _schema_state() -> dict:
    dsn = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("SUPABASE_DB_URL lipsește — preflightul nu poate citi starea schemei")
    conn = await asyncpg.connect(**_connect_kwargs(dsn), statement_cache_size=0)
    try:
        return await migration_state(conn)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Preflight de release (NX-248)")
    ap.add_argument("--manifest", default="", help="manifestul care urmează să fie promovat")
    ap.add_argument("--manifest-digest", default="", help="digestul așteptat (verificare simplă)")
    ap.add_argument(
        "--require-rollback-possible",
        action="store_true",
        help="blochează releaseul dacă imaginea precedentă nu mai tolerează schema curentă",
    )
    ap.add_argument(
        "--allow-no-previous-digest",
        action="store_true",
        help=(
            "acceptă absența unei ținte de rollback (PRIMUL release). Nu relaxează celelalte "
            "motive: o schemă care depășește ce tolerează imaginea precedentă blochează în "
            "continuare."
        ),
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    problems: list[str] = []
    report: dict = {"checks": {}}

    # 1-2. Starea schemei + compatibilitate.
    try:
        state = asyncio.run(_schema_state())
    except Exception as e:  # noqa: BLE001 — preflightul raportează, nu propagă stack trace în CI
        print(
            f"::error::preflight: nu pot citi starea schemei ({type(e).__name__})", file=sys.stderr
        )
        return EXIT_ERROR

    requires = bundled_schema_version()
    tolerates = requires + SCHEMA_FORWARD_TOLERANCE
    applied = int(state["applied"])
    report["checks"]["migrations"] = state
    report["checks"]["schema"] = {
        "applied": applied,
        "requires": requires,
        "tolerates": tolerates,
    }
    if state["pending"]:
        problems.append(f"migrări neaplicate: {', '.join(state['pending'])} (rulează jobul întâi)")
    if state["drift"]:
        problems.append(f"drift de checksum: {', '.join(state['drift'])}")
    if applied > tolerates:
        problems.append(
            f"schema aplicată ({applied:03d}) depășește ce tolerează imaginea ({tolerates:03d})"
        )

    # 4. Config: construibilă + coerentă cu manifestul.
    try:
        settings = Settings()
    except Exception as e:  # noqa: BLE001
        print(f"::error::preflight: config invalidă ({type(e).__name__})", file=sys.stderr)
        return EXIT_ERROR
    revision = config_revision(settings)
    report["checks"]["config_revision"] = revision

    manifest = None
    if args.manifest:
        try:
            manifest = load(
                Path(args.manifest).read_text(encoding="utf-8"),
                key=os.environ.get("RELEASE_MANIFEST_KEY"),
                require_signature=bool(os.environ.get("RELEASE_MANIFEST_KEY")),
            )
        except (OSError, ManifestError) as e:
            print(f"::error::preflight: manifest invalid ({e})", file=sys.stderr)
            return EXIT_BLOCKED
        report["checks"]["manifest"] = {
            "digest": manifest.digest,
            "fingerprint": manifest.fingerprint(),
        }
        if args.manifest_digest and manifest.digest != args.manifest_digest:
            problems.append("digestul din manifest diferă de cel cerut pentru promovare")
        # 3. Fezabilitatea rollbackului.
        possible, why = manifest.rollback_possible(applied)
        report["checks"]["rollback"] = {"possible": possible, "reason": why}
        if args.require_rollback_possible and not possible:
            # Primul release e o STARE, nu un eșec: nu există versiune precedentă fiindcă nu a
            # existat niciodată una, iar o poartă care cere un predecesor nu poate fi trecută la
            # primul release — bootstrap imposibil prin construcție. A ieșit la iveală abia acum
            # (2026-08-19), la prima promovare care a ajuns până aici.
            #
            # Se acceptă doar DECLARAT, nu dedus. Motivul: din manifest, „primul release" și
            # „manifestul precedent n-a putut fi citit" arată identic — `build_manifest.py` lasă
            # `previous_digest` gol în ambele cazuri. A doua situație e exact aceea în care vrei să
            # te oprești. Un om spune care dintre ele e; codul nu are cum să știe.
            if not manifest.previous_digest and args.allow_no_previous_digest:
                report["checks"]["rollback"]["accepted_as_first_release"] = True
                print(
                    "ATENȚIE: promovare FĂRĂ țintă de rollback, acceptată explicit "
                    "(--allow-no-previous-digest). Notează digestul care rulează acum: "
                    "e singura cale de întoarcere până la releaseul următor."
                )
            else:
                problems.append(f"rollback imposibil: {why}")
    elif args.require_rollback_possible:
        problems.append("--require-rollback-possible cere --manifest (altfel nu există țintă)")

    report["ok"] = not problems
    report["problems"] = problems
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(
            f"preflight: schema aplicată={applied:03d} imagine={requires:03d}..{tolerates:03d} "
            f"config={revision} pending={len(state['pending'])}"
        )
        for problem in problems:
            print(f"::error::{problem}", file=sys.stderr)
    if problems:
        print(f"PREFLIGHT: BLOCAT ({len(problems)} probleme)", file=sys.stderr)
        return EXIT_BLOCKED
    print("PREFLIGHT: OK")
    return EXIT_OK


if __name__ == "__main__":
    from src.ops.cli import enable_utf8_stdout

    enable_utf8_stdout()

    raise SystemExit(main())
