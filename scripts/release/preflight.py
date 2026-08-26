"""NX-248 — preflight: ce trebuie adevărat ÎNAINTE ca un deploy să atingă un host.

Un deploy eșuat la jumătate costă mult mai mult decât un deploy refuzat la început. Preflightul
răspunde, read-only, la întrebările care decid dacă mai are rost să continuăm:

  1. **Migrări** — există pending? drift de checksum? (`scripts/migrate.py --check`, DSN de citire)
     Cu `--before-migration` (ordinea din `release.yml`), migrările ADUSE de imaginea promovată
     sunt așteptate: pasul următor le aplică. Doar cele pe care imaginea NU le conține blochează.
  2. **Compatibilitate de schemă** — imaginea care urmează tolerează schema EFECTIVĂ (cea care va
     fi live la deploy)? Prea nouă ⇒ promovezi imaginea greșită.
  3. **Fezabilitatea ROLLBACKULUI** — imaginea precedentă mai tolerează schema EFECTIVĂ? Dacă nu,
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
    ap.add_argument(
        "--before-migration",
        action="store_true",
        help=(
            "Preflightul rulează ÎNAINTEA pasului de migrare (ordinea din release.yml). Migrările "
            "pending care sunt ADUSE de imaginea promovată sunt așteptate, nu o problemă — pasul "
            "următor le aplică. Verificările de schemă și de rollback se fac atunci pe versiunea "
            "de DUPĂ migrare."
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
    # Ce schemă va fi LIVE în momentul deployului. Fără `--before-migration` e cea aplicată acum;
    # cu el, migrările aduse de imagine sunt pe cale să ruleze, deci starea relevantă e `requires`.
    #
    # De ce contează (măsurat 2026-08-26, prima promovare care a purtat o migrare prin pipeline):
    # `release.yml` rulează preflight ÎNAINTE de migrare, dar preflightul cerea zero pending și
    # spunea „rulează jobul întâi" — deci orice release cu o migrare nouă se bloca singur, iar
    # migrarea 045 nu avea cum să ajungă vreodată în producție. În plus, `rollback_possible` era
    # evaluat pe schema VECHE: întrebarea „mai pot reveni la imaginea precedentă?" are sens doar
    # pe schema care va fi live DUPĂ migrare, altfel poarta măsoară o stare care nu va mai exista.
    effective = applied
    if args.before_migration:
        # Pending care NU vine cu imaginea = cineva a adăugat o migrare pe care artefactul promovat
        # nu o conține. Pasul de migrare ar aplica-o oricum, dar codul care urmează n-o cunoaște.
        foreign = [v for v in state["pending"] if int(v) > requires]
        if foreign:
            problems.append(
                f"migrări pending care NU sunt în imaginea promovată: {', '.join(foreign)} "
                f"(imaginea aduce până la {requires:03d})"
            )
        else:
            effective = max(applied, requires)
    elif state["pending"]:
        problems.append(f"migrări neaplicate: {', '.join(state['pending'])} (rulează jobul întâi)")
    report["checks"]["schema"]["effective"] = effective
    if state["drift"]:
        problems.append(f"drift de checksum: {', '.join(state['drift'])}")
    if effective > tolerates:
        problems.append(
            f"schema ({effective:03d}) depășește ce tolerează imaginea ({tolerates:03d})"
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
        # 3. Fezabilitatea rollbackului — pe schema care va fi LIVE (vezi `effective` mai sus).
        # Cu `--before-migration` asta e versiunea de DUPĂ migrare, deci poarta prinde acum cazul
        # pe care îl rata: o migrare care iese din intervalul tolerat de imaginea precedentă
        # blochează releaseul ÎNAINTE de a fi aplicată, nu după.
        possible, why = manifest.rollback_possible(effective)
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
            f"preflight: schema aplicată={applied:03d} efectivă={effective:03d} "
            f"imagine={requires:03d}..{tolerates:03d} config={revision} "
            f"pending={len(state['pending'])}"
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
