"""NX-248 — rollback la digestul precedent. DRY-RUN implicit, ireversibil doar cu `--apply`.

Rollbackul e o operație de INCIDENT, deci se proiectează pentru cineva obosit, la 3 dimineața,
care nu are timp să citească. De aceea:

  • **dry-run e implicitul.** Fără `--apply`, scriptul spune exact ce ar face și iese cu 0.
  • **ținta nu se ghicește.** Vine din `previous_digest`-ul manifestului. Un rollback către un
    digest tastat manual e cel mai bun mod de a promova o imagine pe care n-a testat-o nimeni.
  • **fezabilitatea se verifică ÎNAINTE.** Dacă schema aplicată a depășit ce tolerează imaginea
    precedentă, rollbackul NU e o opțiune: ar rula cod orb peste coloane pe care nu le știe.
    Scriptul refuză și îndrumă către un release-fix (expand/contract), fiindcă alternativa —
    un down-migration sub incident — e cum se pierd date.
  • **nu șterge nimic.** Nici volume, nici rânduri de ledger, nici rezultate de tur. Rollbackul
    schimbă ce COD rulează, nu ce s-a întâmplat.

Uz:
    python scripts/release/rollback.py --manifest manifest.json                # dry-run
    python scripts/release/rollback.py --manifest manifest.json --apply        # execută
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from scripts.migrate import _connect_kwargs, applied_version  # noqa: E402
from src.ops.manifest import ManifestError, load  # noqa: E402

EXIT_OK = 0
EXIT_IMPOSSIBLE = 1
EXIT_ERROR = 2


async def _applied_schema() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("SUPABASE_DB_URL lipsește — nu pot verifica fezabilitatea rollbackului")
    conn = await asyncpg.connect(**_connect_kwargs(dsn), statement_cache_size=0)
    try:
        return await applied_version(conn)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rollback la digestul precedent (NX-248)")
    ap.add_argument("--manifest", default="manifest.json")
    ap.add_argument("--compose-file", default="docker-compose.prod.yml")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--apply", action="store_true", help="EXECUTĂ (implicit: dry-run)")
    args = ap.parse_args(argv)

    key = os.environ.get("RELEASE_MANIFEST_KEY")
    try:
        manifest = load(
            Path(args.manifest).read_text(encoding="utf-8"), key=key, require_signature=bool(key)
        )
    except (OSError, ManifestError) as e:
        print(f"::error::manifest invalid: {e}", file=sys.stderr)
        return EXIT_ERROR

    try:
        applied = asyncio.run(_applied_schema())
    except Exception as e:  # noqa: BLE001
        print(f"::error::nu pot citi schema aplicată ({type(e).__name__})", file=sys.stderr)
        return EXIT_ERROR

    possible, why = manifest.rollback_possible(applied)
    print(f"țintă: {manifest.previous_digest or '—'}")
    print(
        f"schemă aplicată: {applied:03d} · imaginea precedentă tolerează: "
        f"{manifest.previous_schema_tolerates:03d}"
    )
    if not possible:
        print(f"::error::ROLLBACK IMPOSIBIL: {why}", file=sys.stderr)
        print(
            "Nu face down-migration. Calea corectă e un release-fix (expand/contract) — vezi "
            "docs/RELEASE-RUNBOOK.md §Când rollbackul nu e o opțiune.",
            file=sys.stderr,
        )
        return EXIT_IMPOSSIBLE

    steps = [
        f"scrie IMAGE_DIGEST={manifest.previous_digest} în {args.env_file}",
        f"docker compose -f {args.compose_file} pull",
        f"docker compose -f {args.compose_file} up -d --remove-orphans",
        "așteaptă /health/ready",
        "rulează scripts/release/smoke_web_v2.py",
    ]
    if not args.apply:
        print("\nDRY-RUN — pașii care s-ar executa (nimic nu s-a schimbat):")
        for i, step in enumerate(steps, 1):
            print(f"  {i}. {step}")
        print("\nRulează din nou cu --apply pentru a executa.")
        return EXIT_OK

    env_path = Path(args.env_file)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    # Rescriem DOAR linia digestului: `.env` ține secretele hostului, iar un rollback nu are voie
    # să distrugă configurația în timp ce repară codul.
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("IMAGE_DIGEST="):
            lines[i] = f"IMAGE_DIGEST={manifest.previous_digest}"
            replaced = True
    if not replaced:
        lines.append(f"IMAGE_DIGEST={manifest.previous_digest}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ {args.env_file}: IMAGE_DIGEST ← {manifest.previous_digest}")

    for cmd in (
        ["docker", "compose", "-f", args.compose_file, "pull"],
        ["docker", "compose", "-f", args.compose_file, "up", "-d", "--remove-orphans"],
    ):
        print(f"→ {' '.join(cmd)}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            print(f"::error::comanda a eșuat ({result.returncode})", file=sys.stderr)
            return EXIT_ERROR
    print("✓ rollback aplicat. Verifică: scripts/release/verify_manifest.py + smoke_web_v2.py")
    return EXIT_OK


if __name__ == "__main__":
    from src.ops.cli import enable_utf8_stdout

    enable_utf8_stdout()

    raise SystemExit(main())
