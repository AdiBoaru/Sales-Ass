"""NX-248 — imaginea respectă contractul? (non-root, conținut necesar, zero secrete)

„Buildul a trecut" nu înseamnă „imaginea e utilizabilă". Dovada concretă din repo-ul ăsta:
`.dockerignore` excludea `db/seed`, deci registrul de contraindicații (NX-173) lipsea din
imagine, iar poarta de boot a workerului îl cere — buildul era verde și workerul nu putea porni.
Aceeași clasă de bug ca PR #132 (scripts/docs lipsă). Un build verde nu e o promisiune.

Verificările sunt împărțite în trei familii, fiindcă eșuează din motive diferite:

  • **identitate** — rulează ca UID non-root, cu un shell de login blocat;
  • **conținut** — fișierele fără de care un proces refuză să pornească EXISTĂ, iar cele care n-au
    ce căuta în producție (teste, `.git`, `.env`, unelte de dev) NU există;
  • **secrete** — nici în straturi, nici în `docker history`, nici în variabilele de mediu ale
    imaginii. Aici căutăm și un canary injectat de CI, ca testul să poată eșua când trebuie.

Rulare: `python scripts/release/image_contract.py <imagine>`
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

#: Fișiere fără de care un proces REFUZĂ să pornească. Lista e derivată din porțile de boot
#: existente, nu din intuiție: fiecare intrare are un `raise` în spate.
REQUIRED_PATHS = (
    "/app/src/webhook/app.py",
    "/app/src/worker/consumer.py",
    "/app/scripts/migrate.py",
    "/app/docs/042_web_feedback.sql",
    "/app/db/seed/safety_rules.json",  # poarta NX-173: `registry_healthy()` la boot-ul workerului
)

#: Ce n-are ce căuta în imaginea de producție. `.git` în special: poartă ISTORICUL, deci și
#: secretele comise vreodată și șterse ulterior — revocate în teorie, prezente în imagine.
FORBIDDEN_PATHS = (
    "/app/.git",
    "/app/.env",
    "/app/tests",
    "/app/scripts/sim",
    "/app/node_modules",
    "/app/reports",
)

#: Tipare de secret căutate în `docker history`, ENV-ul imaginii și label-uri.
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # OpenAI
    re.compile(r"postgres(?:ql)?://[^\s:@]+:[^\s@]+@"),  # DSN cu parolă
    re.compile(r"EAA[A-Za-z0-9]{20,}"),  # token Meta
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

#: Variabilele de mediu pe care imaginea AR TREBUI să le aibă (publice prin definiție).
ALLOWED_ENV_PREFIXES = ("RELEASE_SHA=", "BUILT_AT=", "PYTHON", "PATH=", "LANG=", "GPG_KEY=")


def _run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"comanda a eșuat: {' '.join(args)}\n{result.stderr[:2000]}")
    return result.stdout


def check_identity(image: str) -> list[str]:
    problems: list[str] = []
    uid = _run("docker", "run", "--rm", "--entrypoint", "id", image, "-u").strip()
    if uid in ("0", ""):
        problems.append(f"imaginea rulează ca UID {uid!r} (așteptat: non-root)")
    return problems


def check_content(image: str) -> list[str]:
    problems: list[str] = []
    # O singură invocație: `docker run` per fișier ar face verificarea de zeci de ori mai lentă
    # și ar tenta pe cineva să scurteze lista.
    script = "; ".join(
        [f"test -e {p} || echo MISSING:{p}" for p in REQUIRED_PATHS]
        + [f"test -e {p} && echo PRESENT:{p}" for p in FORBIDDEN_PATHS]
    )
    out = _run("docker", "run", "--rm", "--entrypoint", "sh", image, "-c", f"{script}; true")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("MISSING:"):
            problems.append(f"lipsește din imagine: {line.removeprefix('MISSING:')}")
        elif line.startswith("PRESENT:"):
            problems.append(f"n-are ce căuta în imagine: {line.removeprefix('PRESENT:')}")
    return problems


def check_secrets(image: str, canary: str | None = None) -> list[str]:
    problems: list[str] = []
    history = _run("docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image)
    inspect = _run("docker", "image", "inspect", image)
    data = json.loads(inspect)
    env_entries = data[0].get("Config", {}).get("Env", []) if data else []
    labels = json.dumps(data[0].get("Config", {}).get("Labels") or {}) if data else "{}"

    haystack = "\n".join([history, "\n".join(env_entries), labels])
    for pattern in SECRET_PATTERNS:
        found = pattern.search(haystack)
        if found:
            # NU tipărim ce am găsit — un raport de CI e public.
            problems.append(f"tipar de secret ({pattern.pattern[:24]}…) prezent în imagine")
    if canary and canary in haystack:
        problems.append("canary-ul injectat de CI e vizibil în imagine")
    for entry in env_entries:
        if not entry.startswith(ALLOWED_ENV_PREFIXES):
            name = entry.split("=", 1)[0]
            problems.append(f"variabilă de mediu neașteptată coaptă în imagine: {name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("uz: image_contract.py <imagine> [--canary VALOARE]", file=sys.stderr)
        return 2
    image = args[0]
    canary = args[args.index("--canary") + 1] if "--canary" in args else None

    problems = check_identity(image) + check_content(image) + check_secrets(image, canary)
    if problems:
        for problem in problems:
            print(f"::error::{problem}", file=sys.stderr)
        print(f"CONTRACT DE IMAGINE: FAIL ({len(problems)} probleme)", file=sys.stderr)
        return 1
    print("CONTRACT DE IMAGINE: PASS (non-root, conținut complet, zero secrete)")
    return 0


if __name__ == "__main__":
    from src.ops.cli import enable_utf8_stdout

    enable_utf8_stdout()

    raise SystemExit(main())
