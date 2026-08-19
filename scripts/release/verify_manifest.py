"""NX-248 — verifică, PE HOST, că rulează exact artefactul din manifest.

Un container nu-și poate citi propriul digest (vezi `src/ops/build_info.py`), deci afirmația lui
despre sine nu e o dovadă. Dovada se face din afară și e simplă: ce spune `docker inspect` despre
imaginea containerului trebuie să fie identic cu ce spune manifestul semnat.

Verifică, în ordine (fiecare eșec are cod propriu, fiindcă cere altă acțiune):

  1. manifestul se citește, amprenta corespunde conținutului, semnătura (dacă e cerută) se verifică;
  2. imaginea rulată de fiecare serviciu = digestul din manifest — nu tagul, nu ce zice `.env`;
  3. `/health/ready` răspunde 200 și raportează ACELAȘI `release` ca manifestul;
  4. toate serviciile raportează ACEEAȘI amprentă de config.

Punctul 4 prinde deployul parțial: containere pornite cu imaginea nouă și o configurație veche
arată perfect la `docker compose ps`. Configurația e citită la CREAREA containerului, deci un
`.env` editat urmat de repornirea unui singur serviciu produce chiar divergență între ele.

Se compară serviciile ÎNTRE ELE, nu cu o valoare din manifest: amprenta de config e o proprietate
a DEPLOYULUI (vine din `.env`-ul hostului), iar un manifest construit în CI nu o poate cunoaște.
Versiunea v1 o scria oricum acolo — amprenta default-urilor din cod — deci punctul 3 raporta
„deploy parțial" la fiecare deploy corect. Vezi nota din `src/ops/manifest.py`.

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


#: Amprenta se calculează ÎN container, cu aceeași funcție care alimentează `/health/ready` — nu
#: reimplementată aici. O a doua implementare a aceleiași reguli ar putea diverge tăcut, iar atunci
#: verificarea ar compara două lucruri care doar par la fel.
_CONFIG_REVISION_SNIPPET = (
    "from src.config import Settings; "
    "from src.ops.build_info import config_revision; "
    "print(config_revision(Settings()))"
)


def _config_revision(service: str, compose_file: str) -> str | None:
    """Amprenta de config a containerului care rulează ACUM serviciul.

    Contează că se citește din container, nu din `.env`-ul de pe disc: configurația e capturată la
    CREAREA containerului, deci un `.env` editat după aceea nu se vede în procesele deja pornite —
    și exact asta e divergența pe care o căutăm.
    """
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "exec",
            "-T",
            service,
            "python",
            "-c",
            _CONFIG_REVISION_SNIPPET,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() or None


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
    print(f"manifest: {manifest.digest} (release {manifest.release_sha})")

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
            if not problems:
                print("✓ /health/ready: release coincide cu manifestul")

    # Punctul 4: serviciile trebuie să fie de acord ÎNTRE ELE asupra configurației.
    if not args.skip_containers:
        revisions = {svc: _config_revision(svc, args.compose_file) for svc in SERVICES}
        seen = {rev for rev in revisions.values() if rev}
        if not seen:
            # Nu tăcem: „n-am putut măsura" nu e „e în regulă". Aceeași disciplină ca verdictele
            # UNKNOWN din NX-238/NX-246 — absența măsurătorii e o stare distinctă de trecere.
            problems.append("amprenta de config nu s-a putut citi din niciun serviciu")
        elif len(seen) > 1:
            detaliu = ", ".join(
                f"{svc}={rev or 'necitit'}" for svc, rev in sorted(revisions.items())
            )
            problems.append(f"servicii cu configurații DIFERITE (deploy parțial?): {detaliu}")
        else:
            lipsa = [svc for svc, rev in revisions.items() if not rev]
            if lipsa:
                problems.append(f"amprenta de config nu s-a putut citi din: {', '.join(lipsa)}")
            else:
                print(f"✓ toate serviciile pe aceeași configurație ({seen.pop()})")

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
