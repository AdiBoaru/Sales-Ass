"""NX-328 felia 1d — harnessul diferențial al invariantului I16 (contractul `kernel.v1.0`).

I16: cu flagul kernelului stins, calea v1 e NESCHIMBATĂ pe suprafața observabilă (răspunsul,
apelurile de unealtă cu argumente, starea persistată, `selected_product`, `active_search`,
accesele la DB). Harnessul rulează ACELAȘI corpus (suita golden) pe două versiuni de cod și le
compară. Orice diferență iese nenul, cu testul și suprafața care diferă.

    # înregistrează o versiune de cod (implicit: repo-ul curent)
    python scripts/kernel_differential.py record --out head.json
    python scripts/kernel_differential.py record --root ../base --out base.json
    # compară
    python scripts/kernel_differential.py diff base.json head.json

`--root` e directorul cu codul MĂSURAT; pluginul vine mereu din directorul ACESTUI script. Așa se
poate înregistra și `main`, unde pluginul nu există încă (un `git worktree` al lui `origin/main`).

Fiecare pas al kernelului (2-6) trebuie să lase diff-ul gol cu flagurile lui stinse. Poarta se
aplică doar PR-urilor care ating kernelul (`applies`): o reparație pe calea de azi (ca NX-326)
schimbă legitim suprafața v1, iar asta e o decizie de produs, nu un efect secundar al kernelului.

    python scripts/kernel_differential.py applies --base origin/main"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE / "kernel_diff"
DEFAULT_ROOT = HERE.parent
#: Corpusul: suita golden, pe AMBELE căi pe care le parametrizează (v1 și creierul unic).
CORPUS: tuple[str, ...] = ("tests/test_golden.py",)
SURFACES: tuple[str, ...] = ("reply", "tool_calls", "state", "db")


def record(root: Path, out: Path, *, flip: str | None = None) -> int:
    env = dict(os.environ)
    env["KERNEL_DIFF_OUT"] = str(out.resolve())
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(PLUGIN_DIR), str(root.resolve()), env.get("PYTHONPATH", "")) if p
    )
    env.pop("KERNEL_DIFF_FLIP", None)
    if flip:
        env["KERNEL_DIFF_FLIP"] = flip
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *CORPUS,
        "-q",
        "-p",
        "kernel_diff_plugin",
        "-p",
        "no:cacheprovider",
        "--no-header",
    ]
    done = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, check=False)
    if not out.exists():
        sys.stderr.write(done.stdout[-4000:] + done.stderr[-4000:])
        raise SystemExit(f"înregistrarea n-a produs {out} (pytest a ieșit cu {done.returncode})")
    # Suita măsurată poate avea teste roșii pe versiunea ei: asta e treaba job-ului de teste, nu
    # a diff-ului. Se raportează, nu se ascunde.
    tail = done.stdout.strip().splitlines()[-1:] or [""]
    print(f"înregistrat {root} → {out}  ({tail[0]})")
    return done.returncode


MODULES_FILE = DEFAULT_ROOT / "tests" / "kernel_modules.json"
#: Infrastructura kernelului: un PR care o atinge e un PR de kernel chiar fără modul nou.
KERNEL_INFRA_PREFIXES: tuple[str, ...] = (
    "tests/kernel/",
    "tests/kernel_modules.json",
    "scripts/kernel_",
    "src/conversation/interpretation.py",
    "src/conversation/kernel_trace.py",
)


def kernel_paths(registry: dict) -> set[str]:
    """Modulele de kernel înscrise sau planificate, FĂRĂ executori: executorii sunt calea de azi,
    iar o reparație pe ei (ca NX-326) schimbă legitim suprafața v1 și nu e un PR de kernel."""
    paths = {
        rel
        for role, spec in registry["roles"].items()
        if role != "executor"
        for rel in spec["modules"]
    }
    return paths | set(registry.get("planned", {}))


def touches_kernel(changed: list[str], registry: dict) -> bool:
    """I16 se aplică PR-urilor de kernel. PUR."""
    owned = kernel_paths(registry)
    return any(path in owned or path.startswith(KERNEL_INFRA_PREFIXES) for path in changed)


def _changed_files(base_ref: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        cwd=DEFAULT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def diff(base: dict, head: dict) -> list[str]:
    """Diferențele, ca linii lizibile. Gol ⇒ suprafețe identice."""
    out: list[str] = []
    for test in sorted(set(base) | set(head)):
        if test not in head:
            out.append(f"{test}: lipsește din head")
            continue
        if test not in base:
            out.append(f"{test}: nou în head (fără pereche în base)")
            continue
        b_turns, h_turns = base[test], head[test]
        if len(b_turns) != len(h_turns):
            out.append(f"{test}: {len(b_turns)} ture în base, {len(h_turns)} în head")
            continue
        for index, (b, h) in enumerate(zip(b_turns, h_turns, strict=True)):
            for surface in SURFACES:
                if b.get(surface) != h.get(surface):
                    out.append(f"{test} #{index}: suprafața `{surface}` diferă")
    return out


def main(argv: list[str] | None = None) -> int:
    # Consola Windows (cp1252) nu poate scrie diacriticele raportului.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="înregistrează suprafața unei versiuni de cod")
    rec.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    rec.add_argument("--out", type=Path, required=True)
    rec.add_argument(
        "--flip", help="DOAR pentru auto-test: setare=valoare aplicată înaintea suitei"
    )
    dif = sub.add_parser("diff", help="compară două înregistrări")
    dif.add_argument("base", type=Path)
    dif.add_argument("head", type=Path)
    app = sub.add_parser("applies", help="PR-ul atinge kernelul? (scrie `run=` în GITHUB_OUTPUT)")
    app.add_argument("--base", required=True, help="ref-ul bazei, ex. origin/main")
    args = parser.parse_args(argv)

    if args.cmd == "applies":
        registry = json.loads(MODULES_FILE.read_text(encoding="utf-8"))
        changed = _changed_files(args.base)
        applies = touches_kernel(changed, registry)
        print(
            "PR de kernel: I16 se verifică"
            if applies
            else "PR fără module de kernel: I16 nu se aplică (calea v1 poate fi schimbată "
            "intenționat de o reparație, ca NX-326)"
        )
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as fh:
                fh.write(f"run={'true' if applies else 'false'}\n")
        return 0
    if args.cmd == "record":
        record(args.root, args.out, flip=args.flip)
        return 0
    base = json.loads(args.base.read_text(encoding="utf-8"))
    head = json.loads(args.head.read_text(encoding="utf-8"))
    problems = diff(base, head)
    if problems:
        print(f"I16 ÎNCĂLCAT: {len(problems)} diferențe de suprafață cu flagurile stinse")
        for line in problems[:200]:
            print("  " + line)
        return 1
    turns = sum(len(v) for v in head.values())
    print(f"OK: suprafețe identice pe {len(head)} teste, {turns} ture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
