#!/usr/bin/env python3
"""Exportă fiecare diagramă din docs/ARCHITECTURE-WORKFLOWS.md ca fișier separat.

DE CE: documentul e sursa de adevăr, dar o singură pagină de 1500 de linii nu se
descarcă, nu se editează bucată cu bucată și nu se pune într-o prezentare. Exportul
dă fiecărei figuri o viață proprie:

  docs/diagrams/NN-slug.mmd   — SURSA editabilă (text Mermaid). Se deschide în
                                VS Code (extensia Mermaid), pe https://mermaid.live
                                (editor vizual, export PNG/SVG de acolo) sau se
                                importă în draw.io (Insert → Advanced → Mermaid).
  docs/diagrams/svg/NN.svg    — imagine gata de descărcat/embed (generată separat,
                                vezi --svg; cere node + Edge/Chrome local).

DIRECȚIA DE ADEVĂR: documentul → exporturi. Fișierele .mmd sunt GENERATE; dacă
editezi un .mmd și vrei schimbarea permanentă, ea trebuie să ajungă înapoi în
docs/ARCHITECTURE-WORKFLOWS.md (manual sau printr-o cerere către Claude), altfel
următorul export o suprascrie. Poarta CI (`verify_architecture_doc.py`) verifică
că exporturile sunt sincronizate cu documentul — un .mmd divergent = CI roșu, ca
divergența să fie o decizie, nu un accident.

Utilizare:
    python scripts/export_diagrams.py           # (re)generează docs/diagrams/*.mmd
    python scripts/export_diagrams.py --check   # exit 1 dacă exporturile diferă de doc
    python scripts/export_diagrams.py --svg     # + randează SVG (node + mermaid-cli + Edge)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "ARCHITECTURE-WORKFLOWS.md"
OUT = REPO / "docs" / "diagrams"

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# Subtitlurile figurilor multiple dintr-o secțiune (azi doar 4b are trei).
_SUBTITLES = {
    ("Diagram 4b", 1): "privirea-de-sus",
    ("Diagram 4b", 2): "faza-e-precedenta",
    ("Diagram 4b", 3): "faza-f-render",
}

_HEADER = (
    "%% GENERAT din docs/ARCHITECTURE-WORKFLOWS.md — nu edita doar aici.\n"
    "%% Regenerare: python scripts/export_diagrams.py\n"
    "%% Editare vizuală: https://mermaid.live (paste tot fișierul)\n"
    "%% {title}\n"
)


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    # doar partea dinaintea primei paranteze — parantezele din titluri sunt context, nu nume
    text = text.split("(")[0]
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if len(text) > 34:  # taie la graniță de cuvânt, nu în mijloc
        text = text[:34].rsplit("-", 1)[0]
    return text


def _num_key(num: str) -> str:
    """'1' → '01', '4b' → '04b' — sortare naturală în listing."""
    m = re.match(r"(\d+)([a-c]?)", num)
    return m.group(1).zfill(2) + m.group(2)


def extract() -> list[tuple[str, str, str]]:
    """(nume_fișier, titlu, cod) pentru fiecare bloc mermaid, în ordinea din doc."""
    doc = DOC.read_text(encoding="utf-8")
    parts = re.split(r"^## (Diagram [^\n]+)$", doc, flags=re.M)
    out: list[tuple[str, str, str]] = []
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        key = re.match(r"Diagram (\d+[a-c]?)", title)
        num = key.group(1) if key else str(i)
        blocks = re.findall(r"```mermaid\n(.*?)```", parts[i + 1], re.S)
        for j, code in enumerate(blocks, 1):
            sub = _SUBTITLES.get((f"Diagram {num}", j))
            if len(blocks) > 1:
                name = f"{_num_key(num)}-{j}-{sub or 'figura-' + str(j)}"
                full_title = f"{title} · figura {j}" + (f" ({sub})" if sub else "")
            else:
                # slug scurt din titlu, fără prefixul „Diagram N — "
                rest = re.sub(r"^Diagram \S+\s*[—-]\s*", "", title)
                name = f"{_num_key(num)}-{_slug(rest)}"
                full_title = title
            out.append((name + ".mmd", full_title, code.rstrip() + "\n"))
    return out


def render_file(title: str, code: str) -> str:
    return _HEADER.format(title=title) + code


def write_all() -> int:
    OUT.mkdir(exist_ok=True)
    wanted = extract()
    names = {n for n, _, _ in wanted}
    n_changed = 0
    for name, title, code in wanted:
        path = OUT / name
        content = render_file(title, code)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            n_changed += 1
            print(f"scris   {path.relative_to(REPO)}")
    # exporturi orfane (diagrama a dispărut/redenumită din doc) → șterse, nu lăsate să mintă
    for stray in OUT.glob("*.mmd"):
        if stray.name not in names:
            stray.unlink()
            n_changed += 1
            print(f"șters   {stray.relative_to(REPO)} (nu mai există în doc)")
    print(f"{len(wanted)} figuri, {n_changed} fișiere modificate")
    return 0


def check() -> int:
    """Pentru poarta CI: exporturile de pe disc = exact ce ar genera documentul."""
    problems: list[str] = []
    wanted = extract()
    names = {n for n, _, _ in wanted}
    for name, title, code in wanted:
        path = OUT / name
        if not path.exists():
            problems.append(f"lipsește {path.relative_to(REPO)}")
        elif path.read_text(encoding="utf-8") != render_file(title, code):
            problems.append(f"divergent {path.relative_to(REPO)}")
    if OUT.exists():
        for stray in OUT.glob("*.mmd"):
            if stray.name not in names:
                problems.append(f"orfan {stray.relative_to(REPO)} (nu mai există în doc)")
    if problems:
        print("FAIL: exporturile diagramelor nu sunt sincronizate cu documentul:", file=sys.stderr)
        for p in problems:
            print(f"  • {p}", file=sys.stderr)
        print("Rulează: python scripts/export_diagrams.py", file=sys.stderr)
        return 1
    print(f"OK: {len(wanted)} exporturi sincronizate cu documentul")
    return 0


def svg() -> int:
    """Randare SVG prin mermaid-cli (mmdc), cu Edge-ul local drept browser.

    Best-effort și LOCAL-only (nu în CI): cere node + pachetul instalat + un browser.
    SVG-urile sunt produse derivate de vizualizare — sursa rămâne .mmd.
    """
    import json
    import os
    import shutil
    import tempfile

    # ordinea: MMDC_PATH din env → node_modules-ul repo-ului → PATH
    mmdc: str | Path | None = os.environ.get("MMDC_PATH") or None
    if mmdc is None:
        cand = REPO / "node_modules" / ".bin" / ("mmdc.cmd" if sys.platform == "win32" else "mmdc")
        mmdc = cand if cand.exists() else shutil.which("mmdc")
    if mmdc is None:
        print(
            "SVG sărit: mermaid-cli (mmdc) nu e găsit. Instalează-l "
            "(npm i -D @mermaid-js/mermaid-cli) sau setează MMDC_PATH.",
            file=sys.stderr,
        )
        return 1

    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    puppeteer_cfg = None
    if edge.exists():
        cfg = {"executablePath": str(edge), "headless": "shell"}
        tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(cfg, tf)
        tf.close()
        puppeteer_cfg = tf.name

    svg_dir = OUT / "svg"
    svg_dir.mkdir(parents=True, exist_ok=True)
    fails = 0
    for path in sorted(OUT.glob("*.mmd")):
        dest = svg_dir / (path.stem + ".svg")
        cmd = [str(mmdc), "-i", str(path), "-o", str(dest), "-b", "transparent"]
        if puppeteer_cfg:
            cmd += ["-p", puppeteer_cfg]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and dest.exists():
            print(f"svg     {dest.relative_to(REPO)}")
        else:
            fails += 1
            print(f"EȘEC    {path.name}: {(r.stderr or r.stdout)[-200:]}", file=sys.stderr)
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verifică sincronizarea (CI)")
    ap.add_argument("--svg", action="store_true", help="randează și SVG (local, cere node+browser)")
    args = ap.parse_args()
    if args.check:
        return check()
    rc = write_all()
    if args.svg and rc == 0:
        rc = svg()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
