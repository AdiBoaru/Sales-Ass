"""NX-248 — verifică, PE HOST, că rulează exact artefactul din manifest.

Un container nu-și poate citi propriul digest (vezi `src/ops/build_info.py`), deci afirmația lui
despre sine nu e o dovadă. Dovada se face din afară și e simplă: ce spune `docker inspect` despre
imaginea containerului trebuie să fie identic cu ce spune manifestul semnat.

Verifică, în ordine (fiecare eșec are cod propriu, fiindcă cere altă acțiune):

  1. manifestul se citește, amprenta corespunde conținutului, semnătura (dacă e cerută) se verifică;
  2. imaginea rulată de fiecare serviciu = digestul din manifest — nu tagul, nu ce zice `.env`;
  3. `/health/ready` răspunde 200 și raportează ACELAȘI release/config ca manifestul.

Punctul 3 e cel care prinde deployul parțial: containere pornite cu imaginea nouă și o configurație
veche arată perfect la `docker ps` și răspund cu alt `config_revision`.

Uz: python scripts/release/verify_manifest.py --manifest manifest.json [--base-url https://…]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.ops.manifest import ManifestError, load  # noqa: E402

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_ERROR = 2

#: Serviciile care TREBUIE să ruleze digestul promovat. `migrate` lipsește deliberat: e un job
#: one-shot, deja terminat când verificăm.
SERVICES = ("webhook", "worker", "dispatcher", "scheduler")


def _running_digest(service: str, compose_file: str) -> str | None:
    """Digestul imaginii pe care rulează containerul serviciului (nu tagul lui)."""
    cid = subprocess.run(
        ["docker", "compose", "-f", compose_file, "ps", "-q", service],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not cid:
        return None
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{index .Config.Image}}", cid],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return out or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verifică deployul contra manifestului (NX-248)")
    ap.add_argument("--manifest", default="manifest.json")
    ap.add_argument("--compose-file", default="docker-compose.prod.yml")
    ap.add_argument("--base-url", default="", help="ex. http://localhost:8000 (health check)")
    ap.add_argument("--skip-containers", action="store_true", help="doar manifest + health")
    args = ap.parse_args(argv)

    key = os.environ.get("RELEASE_MANIFEST_KEY")
    try:
        manifest = load(
            Path(args.manifest).read_text(encoding="utf-8"),
            key=key,
            require_signature=bool(key),
        )
    except (OSError, ManifestError) as e:
        print(f"::error::manifest invalid: {e}", file=sys.stderr)
        return EXIT_ERROR

    problems: list[str] = []
    print(
        f"manifest: {manifest.digest} (release {manifest.release_sha}, "
        f"config {manifest.config_revision})"
    )

    if not args.skip_containers:
        for service in SERVICES:
            running = _running_digest(service, args.compose_file)
            if running is None:
                problems.append(f"{service}: container absent")
                continue
            if manifest.digest not in running:
                # NU tipărim digestul rulat integral lângă cel așteptat într-un mesaj de eroare
                # lung: scurtăm, ca diferența să fie citibilă sub incident.
                problems.append(
                    f"{service}: rulează {running[:24]}…, manifestul cere {manifest.digest[:24]}…"
                )
            else:
                print(f"✓ {service}: digest corect")

    if args.base_url:
        url = args.base_url.rstrip("/") + "/health/ready"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 — URL de operator
                status = response.status
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            problems.append(f"/health/ready inaccesibil ({type(e).__name__})")
        else:
            if status != 200:
                problems.append(f"/health/ready a răspuns {status}")
            if payload.get("release") != manifest.release_sha:
                problems.append(
                    f"release raportat {payload.get('release')!r} ≠ manifest "
                    f"{manifest.release_sha!r}"
                )
            if payload.get("config") != manifest.config_revision:
                # Deployul parțial: imagine nouă, configurație veche. Arată perfect la `docker ps`.
                problems.append(
                    f"config raportat {payload.get('config')!r} ≠ manifest "
                    f"{manifest.config_revision!r} (deploy parțial?)"
                )
            if not problems:
                print("✓ /health/ready: release + config coincid cu manifestul")

    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    if problems:
        print(f"VERIFICARE: FAIL ({len(problems)} nepotriviri)", file=sys.stderr)
        return EXIT_MISMATCH
    print("VERIFICARE: PASS — rulează exact artefactul din manifest")
    return EXIT_OK


if __name__ == "__main__":
    from src.ops.cli import enable_utf8_stdout

    enable_utf8_stdout()

    raise SystemExit(main())
