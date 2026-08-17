"""NX-247 — generatorul/verificatorul manifestului canonic de contract Stage 1 (`web-view.v2`).

Un singur artefact, consumat de AMBELE repo-uri: backendul îl generează din propriul cod, iar
CI-ul frontendului (PR B) îl recalculează dintr-un checkout al backendului la SHA-ul declarat și
pică dacă diferă. Copy/paste manual e interzis pentru că nu lasă provenance: un fixture „ajustat
ca să treacă testul" e un contract care nu mai descrie ce livrează serverul.

De ce un script și nu un `dict` într-un test: manifestul trebuie REGENERABIL cu o comandă din
runbook (`--write`) și VERIFICABIL fără efecte (`--check`, cod ≠0 la drift). Testul
`tests/e2e/test_stage1_contract_manifest.py` îl cheamă pe același cod — o singură definiție.

**Determinism, explicit:**

  • ZERO timestamp. `generated_at` nu există în artefact, deliberat: un câmp de ceas ar face ca
    două generări ale ACELUIAȘI tree să difere, iar atunci „driftul rupe CI" n-ar mai putea
    distinge o schimbare de contract de trecerea timpului. Provenance-ul e `backend_commit`, și
    el trăiește în CERTIFICATUL de rulare (`--certificate`), nu aici — un fișier nu poate conține
    hash-ul commitului care îl conține.
  • hash-urile de fișier se calculează pe bytes NORMALIZAȚI (CRLF→LF), ca în `scripts/migrate.py`:
    altfel același fișier dă alt sha256 pe Windows (autocrlf) și pe Linux (CI), iar
    cross-repo-ul ar raporta drift fals. Rezultatul e egal cu sha256 al blobului din git, care e
    exact ce hash-uiește frontendul (`git show`), deci cele două repo-uri compară același număr.
  • `schema_sha256` NU e hash-ul unui fișier, ci `contracts_v2.schema_hash()` — moneda negocierii
    de capability. Dacă cele două ar divergea, negocierea ar promite un contract și livra altul.

Rulare:
    python scripts/stage1_contract_manifest.py --check          # cod ≠0 la drift
    python scripts/stage1_contract_manifest.py --write          # regenerează artefactul
    python scripts/stage1_contract_manifest.py --certificate reports/stage1/certificate.json \
        --frontend-sha <sha>                                    # perechea certificată (NX-249)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.contracts_v2 import (  # noqa: E402 — după ajustarea sys.path
    TURN_SCHEMA_VERSION,
    VIEW_SCHEMA_VERSION,
    _canonical,
    schema_hash,
    turn_json_schema,
    view_json_schema,
)

QA_DIR = ROOT / "qa-suite" / "stage1" / "web-v2"
MANIFEST_PATH = QA_DIR / "manifest.json"
SCENARIOS_PATH = QA_DIR / "scenarios.json"
THRESHOLDS_PATH = QA_DIR / "gate-thresholds.json"

#: Fixturile care fac parte din PACHETUL de contract, în ordine STABILĂ (căi repo-relative, cu `/`
#: pe orice platformă). Setul e închis și verificat: un fixture nou care nu ajunge aici ar fi un
#: fixture pe care frontendul nu-l validează niciodată, adică exact gaura pe care manifestul
#: există ca să o închidă (vezi testul `test_fixture_set_is_complete`).
CONTRACT_FIXTURES: tuple[str, ...] = (
    "tests/fixtures/web_v2/valid_views.json",
    "tests/fixtures/web_v2/invalid_views.json",
    "tests/fixtures/web_v2/requests.json",
)

#: Proiecțiile REALE ale projectorului (NX-240), separate de fixturile de contract: ele nu descriu
#: forma, ci ce produce serverul pe scenariile canonice. Frontendul le randează în browser.
PROJECTION_FIXTURES_DIR = "tests/fixtures/web_v2_golden"

_NOTE = (
    "GENERAT de scripts/stage1_contract_manifest.py — nu edita manual. "
    "Fără timestamp: determinismul e condiția ca driftul să însemne ceva."
)


def normalized_sha256(path: Path) -> str:
    """sha256 peste bytes cu CRLF→LF. Egal cu sha256 al blobului git (LF) pe orice platformă."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _digest_of(mapping: dict[str, str]) -> str:
    """Un singur număr pentru un SET de fișiere: hash peste maparea canonică nume→hash. Prinde și
    conținutul schimbat, și setul TRUNCHIAT (un fixture șters schimbă digestul)."""
    return hashlib.sha256(
        json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _projection_fixtures() -> dict[str, str]:
    base = ROOT / PROJECTION_FIXTURES_DIR
    return {
        f"{PROJECTION_FIXTURES_DIR}/{p.name}": normalized_sha256(p)
        for p in sorted(base.glob("*.json"))
    }


def contract_pack() -> dict:
    """Pachetul de contract, PUR: aceleași bytes pentru același tree, pe orice platformă."""
    fixtures = {name: normalized_sha256(ROOT / name) for name in CONTRACT_FIXTURES}
    projections = _projection_fixtures()
    pack: dict = {
        "_note": _NOTE,
        "schema_version": VIEW_SCHEMA_VERSION,
        "turn_schema_version": TURN_SCHEMA_VERSION,
        "schema_sha256": schema_hash(view_json_schema()),
        "turn_schema_sha256": schema_hash(turn_json_schema()),
        "fixtures_sha256": _digest_of(fixtures),
        "fixtures": fixtures,
        "projections_sha256": _digest_of(projections),
        "projections": projections,
        # `backend_commit` rămâne null AICI: vezi docstring. Se completează în certificat.
        "backend_commit": None,
    }
    if SCENARIOS_PATH.exists():
        pack["scenarios_sha256"] = normalized_sha256(SCENARIOS_PATH)
    if THRESHOLDS_PATH.exists():
        pack["thresholds_sha256"] = normalized_sha256(THRESHOLDS_PATH)
    return pack


def render(pack: dict) -> str:
    """Serializarea canonică a artefactului (indent 2 + newline final = ce scrie `--write`)."""
    return f"{json.dumps(pack, indent=2, ensure_ascii=False)}\n"


def canonical_schema_bytes() -> bytes:
    """Exact bytes-ii peste care se calculează `schema_hash()` — ce copiază frontendul."""
    return _canonical(view_json_schema()).encode("utf-8")


def read_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def drift() -> list[str]:
    """Diferențele dintre artefactul versionat și codul curent. Listă goală = la zi."""
    if not MANIFEST_PATH.exists():
        return [f"{MANIFEST_PATH.relative_to(ROOT).as_posix()} lipsește (rulează --write)"]
    stored = read_manifest()
    fresh = contract_pack()
    out: list[str] = []
    for key in sorted(set(stored) | set(fresh)):
        if key == "_note":
            continue
        if stored.get(key) != fresh.get(key):
            out.append(f"{key}: manifest={stored.get(key)!r} cod={fresh.get(key)!r}")
    return out


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 — argumente fixe, fără shell
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def certificate(frontend_sha: str | None) -> dict:
    """Certificatul unei RULĂRI: pachetul de contract + perechea exactă de SHA-uri care a trecut.

    Nu se comite: e artefact de CI. NX-249 poate face canary DOAR pe perechea de aici — nu pe
    „latest" independent al fiecărui repo (două „latest" verzi separat nu sunt o pereche testată).
    Tree murdar ⇒ refuz: un certificat emis dintr-un checkout necomis e provenance mincinos.
    """
    dirty = _git("status", "--porcelain")
    if dirty:
        raise SystemExit(
            "tree murdar — certificatul ar declara un SHA care nu conține codul rulat.\n" + dirty
        )
    return {
        "_note": "Perechea certificată de NX-247. Consumat de NX-249; nu se comite.",
        "contract": contract_pack(),
        "backend_repo": "Sales Ass",
        "backend_commit": _git("rev-parse", "HEAD"),
        "frontend_repo": "Sales MVP Frontend Final",
        "frontend_commit": frontend_sha,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Manifestul canonic de contract Stage 1 (NX-247)")
    ap.add_argument("--check", action="store_true", help="cod ≠0 dacă manifestul a driftat")
    ap.add_argument("--write", action="store_true", help="regenerează manifestul versionat")
    ap.add_argument("--print-schema", action="store_true", help="bytes-ii canonici ai schemei")
    ap.add_argument("--certificate", metavar="CALE", help="emite certificatul de rulare")
    ap.add_argument("--frontend-sha", default=None, help="SHA-ul frontendului certificat")
    args = ap.parse_args()

    if args.print_schema:
        sys.stdout.reconfigure(encoding="utf-8", newline="")
        sys.stdout.write(canonical_schema_bytes().decode("utf-8"))
        return
    if args.certificate:
        out = Path(args.certificate)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(certificate(args.frontend_sha)), encoding="utf-8")
        print(f"certificat scris: {out}")
        return
    if args.write:
        QA_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(render(contract_pack()), encoding="utf-8")
        print(f"manifest scris: {MANIFEST_PATH.relative_to(ROOT).as_posix()}")
        return

    problems = drift()
    if problems:
        print("DRIFT de contract (manifest ≠ cod):", file=sys.stderr)
        for p in problems:
            print(f"  • {p}", file=sys.stderr)
        print("\nRulează: python scripts/stage1_contract_manifest.py --write", file=sys.stderr)
        raise SystemExit(2)
    pack = read_manifest()
    print(f"contract la zi: {pack['schema_version']} schema={pack['schema_sha256'][:16]}…")


if __name__ == "__main__":
    main()
