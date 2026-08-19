"""NX-248 — compune manifestul de deploy din starea REALĂ, nu din argumente de bunăvoie.

Ce vine din afară (CI): imaginea, digestul, SHA-ul, timestampul, run-id-ul. Ce se DERIVĂ aici:
intervalul de schemă (din migrările prezente în repo/imagine), revizia de config și — dacă există
un manifest precedent — digestul anterior + intervalul lui de schemă.

Derivarea contează: dacă `schema_requires` ar fi un argument, cineva l-ar putea trece greșit exact
în releaseul în care contează, iar poarta de rollback ar aproba o revenire imposibilă.

Uz:
    python scripts/release/build_manifest.py --image ghcr.io/... --digest sha256:... \\
        --release-sha <sha> --built-at <iso> --out manifest.json [--previous manifest-prec.json]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# NU importăm `src.config`: `Settings()` ar cere `.env`-ul unei mașini, iar aici rulăm pe un
# runner de CI care nu are (și nu trebuie să aibă) configurația de producție. Vezi nota din
# `src/ops/manifest.py` despre de ce `config_revision` a ieșit din manifest în v2. Efect practic:
# scriptul ăsta merge cu stdlib + `src/ops/*`, fără nicio dependență instalată în CI.
from src.ops.build_info import SCHEMA_FORWARD_TOLERANCE, bundled_schema_version  # noqa: E402
from src.ops.manifest import MANIFEST_VERSION, DeployManifest, ManifestError, load  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compune manifestul de deploy (NX-248)")
    ap.add_argument("--image", required=True)
    ap.add_argument("--digest", required=True)
    ap.add_argument("--release-sha", required=True)
    ap.add_argument("--built-at", required=True)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--previous", default="", help="manifestul releaseului curent (champion)")
    ap.add_argument("--out", default="manifest.json")
    args = ap.parse_args(argv)

    if not args.digest.startswith("sha256:"):
        print(f"REFUZ: --digest={args.digest} nu e un digest", file=sys.stderr)
        return 2

    requires = bundled_schema_version()
    previous_digest, previous_tolerates = "", -1
    if args.previous:
        try:
            prev = load(Path(args.previous).read_text(encoding="utf-8"))
        except (OSError, ManifestError) as e:
            # Un manifest precedent ilizibil NU e o eroare fatală de build — dar devine „rollback
            # imposibil", ceea ce poarta de promovare va trata ca blocaj. Preferăm degradarea
            # explicită unei presupuneri optimiste.
            print(f"ATENȚIE: manifestul precedent nu se poate citi ({e}) → rollback necunoscut")
        else:
            previous_digest, previous_tolerates = prev.digest, prev.schema_tolerates

    manifest = DeployManifest(
        version=MANIFEST_VERSION,
        image=args.image,
        digest=args.digest,
        release_sha=args.release_sha,
        built_at=args.built_at,
        schema_requires=requires,
        schema_tolerates=requires + SCHEMA_FORWARD_TOLERANCE,
        previous_digest=previous_digest,
        previous_schema_tolerates=previous_tolerates,
        run_id=args.run_id,
    )
    Path(args.out).write_text(
        manifest.to_json(os.environ.get("RELEASE_MANIFEST_KEY")), encoding="utf-8"
    )
    print(
        f"manifest scris în {args.out}: digest={manifest.digest[:19]}… "
        f"schema={manifest.schema_requires:03d}..{manifest.schema_tolerates:03d} "
        f"fingerprint={manifest.fingerprint()[:12]}"
    )
    return 0


if __name__ == "__main__":
    from src.ops.cli import enable_utf8_stdout

    enable_utf8_stdout()

    raise SystemExit(main())
