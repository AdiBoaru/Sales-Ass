"""NX-248 — pachetul de EVIDENȚĂ: ce s-a dovedit, ce nu, și de ce nu putem promova încă.

Un checklist bifat manual nu e o dovadă — e o amintire despre o dovadă. Aici fiecare element are
o SURSĂ (un artefact pe disc) și un verdict derivat din ea. Ce lipsește nu devine „probabil ok":
devine `NOT_READY`, exact ca verdictul din NX-238 și gate-ul de calitate NX-246 felia 3.

Trei verdicte, nu două — distincția e chiar ce face pachetul util:

  • `READY`     — toate elementele CRITICE există și trec;
  • `BLOCKED`   — un element critic există și a PICAT (am măsurat, nu e bine);
  • `NOT_READY` — un element critic LIPSEȘTE (n-am măsurat încă).

`BLOCKED` și `NOT_READY` cer acțiuni diferite: primul e un bug de reparat, al doilea e o măsurătoare
de făcut. Confundate sub un singur „fail", ambele se transformă în „mai încercăm o dată".

Elementele critice sunt cele fără de care o promovare în producție ar fi un pariu: identitatea
artefactului (manifest + semnătură + scan), verificarea pe staging (smoke v2 + migrare) și
recuperabilitatea (drill de restore cu RPO/RTO măsurat).

Uz: python scripts/release/evidence.py --out reports/nx248/evidence.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

VERDICT_READY = "READY"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_NOT_READY = "NOT_READY"


@dataclass(frozen=True, slots=True)
class Element:
    """Un element de evidență: de unde vine și ce înseamnă absența lui."""

    key: str
    critical: bool
    source: str
    description: str


#: Ce trebuie dovedit înainte ca NX-249 (canary + cutover) să poată începe. `critical=False` =
#: util de avut, dar absența lui nu blochează.
ELEMENTS: tuple[Element, ...] = (
    Element("manifest", True, "manifest.json", "digest, release, config, interval de schemă"),
    Element("signature", True, "signature.json", "cosign verify pe repo + workflow identity"),
    Element("sbom", True, "sbom.json", "SBOM SPDX/CycloneDX atașat imaginii"),
    Element("scan", True, "trivy.json", "vulnerabilități OS+Python, fail-closed pe critical/high"),
    Element(
        "image_contract", True, "image_contract.json", "non-root, conținut complet, zero secrete"
    ),
    Element("migrations", True, "migrations.json", "fresh→latest, concurență, expand/contract"),
    Element("staging_smoke", True, "smoke.json", "bootstrap→accept→terminal→replay pe staging"),
    Element("readiness", True, "readiness.json", "live/startup/ready cu dependențe reale"),
    Element("dr_restore", True, "dr.json", "restore izolat + RPO/RTO MĂSURAT"),
    Element(
        "rollback_drill", True, "rollback.json", "revenire la digestul precedent, cronometrată"
    ),
    Element("secret_scan", False, "secret_scan.json", "canary absent din imagine/loguri/artefacte"),
    Element("approvals", False, "approvals.json", "cine a aprobat staging și producție"),
)


def collect(evidence_dir: Path) -> dict:
    items: dict[str, dict] = {}
    missing: list[str] = []
    failed: list[str] = []

    for element in ELEMENTS:
        path = evidence_dir / element.source
        if not path.exists():
            items[element.key] = {
                "present": False,
                "critical": element.critical,
                "source": element.source,
                "description": element.description,
            }
            if element.critical:
                missing.append(element.key)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            payload = {}
        # Convenție unică pentru toate artefactele: `ok`/`verdict`. Un artefact fără verdict
        # lizibil e tratat ca ABSENT, nu ca trecut — altfel un fișier gol ar promova un release.
        verdict = payload.get("verdict")
        ok = payload.get("ok")
        if ok is None and verdict is not None:
            ok = verdict in ("PASS", "READY", "OK")
        items[element.key] = {
            "present": True,
            "ok": ok,
            "verdict": verdict,
            "critical": element.critical,
            "source": element.source,
            "description": element.description,
        }
        if element.critical:
            if ok is None:
                missing.append(element.key)
            elif not ok:
                failed.append(element.key)

    if failed:
        verdict = VERDICT_BLOCKED
    elif missing:
        verdict = VERDICT_NOT_READY
    else:
        verdict = VERDICT_READY

    return {
        "schema_version": "release-evidence.v1",
        "verdict": verdict,
        "missing_critical": sorted(missing),
        "failed_critical": sorted(failed),
        "elements": items,
        "gate": (
            "NX-249 poate începe DOAR cu verdict READY. `NOT_READY` = lipsesc măsurători; "
            "`BLOCKED` = măsurătorile există și au picat."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pachetul de evidență al releaseului (NX-248)")
    ap.add_argument("--dir", default="reports/nx248/evidence", help="unde stau artefactele")
    ap.add_argument("--out", default="reports/nx248/evidence.json")
    args = ap.parse_args(argv)

    report = collect(Path(args.dir))
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"EVIDENȚĂ: {report['verdict']}", file=sys.stderr)
    if report["missing_critical"]:
        print(f"lipsesc: {', '.join(report['missing_critical'])}", file=sys.stderr)
    return 0 if report["verdict"] == VERDICT_READY else 1


if __name__ == "__main__":
    from src.ops.cli import enable_utf8_stdout

    enable_utf8_stdout()

    raise SystemExit(main())
