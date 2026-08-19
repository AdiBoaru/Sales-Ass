"""Fiecare `*.sh` din repo trebuie să fie executabil ÎN GIT (mod 100755).

Regresie măsurată 2026-08-19: prima promovare care a trecut de semnătură, manifest, preflight și
migrare a căzut la pasul de deploy cu

    ./scripts/release/deploy.sh: Permission denied
    Process completed with exit code 126

`deploy.sh` fusese comis cu mod `100644`. Nu e neglijență izolată: repo-ul se dezvoltă pe Windows,
unde `core.fileMode=false`, deci bitul de execuție nu se propagă singur — un `git add` normal îl
pierde, iar local nimeni nu observă, fiindcă nimeni nu rulează scriptul de pe Windows. Se vede abia
pe runnerul Linux, în jobul de release.

Reparat cu `git update-index --chmod=+x`. Testul ăsta există fiindcă următorul `*.sh` adăugat de pe
Windows va avea exact aceeași problemă, iar locul unde s-ar descoperi e din nou un deploy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_toate_scripturile_shell_sunt_executabile_in_git() -> None:
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", "*.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    neexecutabile = [
        line.split("\t", 1)[1]
        for line in out.splitlines()
        if line and not line.startswith("100755")
    ]
    assert not neexecutabile, (
        "shell scripts fără bit de execuție în git: "
        + ", ".join(neexecutabile)
        + ". Repară cu: git update-index --chmod=+x <fișier>"
    )
