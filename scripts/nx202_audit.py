"""NX-202 — auditul setului golden: clasificare pe semnale MĂSURABILE, nu pe impresie.

Cardul cere ca fiecare caz să fie clasificat `KEEP` / `LEGACY` / `REWRITE` / `HOLDOUT` / `UNIT`, cu
motivul notat. Premisa lui („multe legate de pipeline-ul vechi și de `ai_summary`") e însă
DEPĂȘITĂ, iar auditul o corectează în loc s-o repete:

  • **Cazurile nu sunt cuplate la catalog.** Fixturile poartă un catalog SINTETIC, self-contained
    (`p1`, `g1`, url-uri `example`), deci nu ating DB-ul și nu expiră când se schimbă tenantul —
    spre deosebire de qrels-ul NX-203, care chiar a murit așa.
  • **`ai_summary` în fixtură nu e datorie tehnică, e un câmp al produsului sintetic.** Codul
    tratează deja absența lui (`(p.get("ai_summary") or "")`). Ce rămâne e o divergență de FORMĂ,
    raportată aici ca atare: fixtura dă modelului un rezumat pe care catalogul real nu-l are pe
    niciun rând, deci testul e mai UȘOR decât producția.
  • **„Valid pe arhitectura țintă" se măsoară, nu se opinează.** `tests/test_golden.py` rulează
    FIECARE caz pe ambele contracte (v1 și creier unic, parametrizat). Un caz care trece pe
    profilul `brain` E valid pe arhitectura țintă; unul care nu trece e listat cu motivul lui în
    `_BRAIN_NO_SCRIPTED_TEXT`. Clasificarea se citește de acolo, nu se inventează.

Ce rămâne judecată structurală, derivată din formă:

  `UNIT`    — cazul afirmă un ȘIR EXACT scris în cod (`must_include` = o replică deterministă) și
              n-are catalog. Testează mecanica pipeline-ului, nu calitatea conversației: mutat la
              regresii, nu numărat în setul de calitate.
  `KEEP`    — caz conversațional care rulează pe ambele contracte.
  `LEGACY`  — nu rulează pe contractul țintă.
  `HOLDOUT` — rezervat; NU se optimizează pe el (marcat explicit, altfel nu e holdout).

Raportul publică și **compoziția** față de ținta cardului (10 simple · 15 recomandări · 15 grele ·
5 comparații · 5 acțiuni) și golul rămas, ca următorul pas să fie o listă, nu o intuiție.

    python scripts/nx202_audit.py
    python scripts/nx202_audit.py --write
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.safety.external_data import contains_pii  # noqa: E402

CASES = ROOT / "tests" / "golden" / "cases.json"
CONVERSATIONS = ROOT / "tests" / "golden" / "conversations.json"
TEST_FILE = ROOT / "tests" / "test_golden.py"
OUT = ROOT / "tests" / "golden" / "nx202_classification.json"
REPORT = ROOT / "reports" / "nx202-audit.md"

#: Compoziția ȚINTĂ din card. Nu e un total, e o formă: un set de 100 de cazuri toate de vânzare
#: n-ar măsura conversația, ci un singur reflex.
TARGET_MIX = {
    "simple": 10,
    "recommendation": 15,
    "hard_query": 15,
    "comparison": 5,
    "action_followup": 5,
}


def _brain_gaps() -> set[str]:
    """Cazurile pe care harnessul le declară incapabile pe contractul ȚINTĂ.

    Citite din test, nu re-derivate: dacă lista s-ar dubla aici, cele două copii ar diverge și
    auditul ar raporta pentru o suită pe care n-o rulează nimeni."""
    text = TEST_FILE.read_text(encoding="utf-8")
    match = re.search(r"_BRAIN_NO_SCRIPTED_TEXT\s*=\s*(?:frozenset\()?\{([^}]*)\}", text)
    if not match:
        return set()
    return set(re.findall(r"[\"']([^\"']+)[\"']", match.group(1)))


def _dimension(case: dict) -> str:
    """Ce DIMENSIUNE acoperă cazul, pentru compoziția din card."""
    expect = case.get("expect") or {}
    fixtures = case.get("fixtures") or {}
    route = expect.get("route")
    if expect.get("expected_product_ids") and len(expect["expected_product_ids"]) > 1:
        return "comparison"
    if route == "order" or "checkout" in json.dumps(fixtures, ensure_ascii=False):
        return "action_followup"
    if route == "sales":
        # „Grea" = cerere colocvială/compusă, adică una care poartă o constrângere în plus față de
        # numele raftului: nevoie, ingredient, gramaj, finish, contraindicație.
        if expect.get("forbidden_categories") or expect.get("require_reason"):
            return "hard_query"
        return "recommendation"
    return "simple"


def _classify(case: dict, gaps: set[str]) -> tuple[str, str]:
    cid = case["id"]
    if cid in gaps:
        return "LEGACY", "nu rulează pe contractul țintă (declarat în `_BRAIN_NO_SCRIPTED_TEXT`)"
    expect = case.get("expect") or {}
    fixtures = case.get("fixtures") or {}
    if not fixtures.get("catalog") and expect.get("must_include"):
        # Fără catalog, cazul NU poate testa grounding, recomandare sau retrieval — poate testa
        # doar rutarea și formularea. E un rol, nu o condamnare: rămâne pin de regresie și rămâne
        # în ACOPERIRE, fiindcă acoperă tipul de conversație „simplu/clarificare".
        #
        # Am încercat un test mai fin — „fraza cerută există literal în `src/`?" — și l-am aruncat
        # pe măsurătoare: „pentru cine" apare în `src/` (deși e o așteptare semantică),
        # iar „82.99"
        # la fel (e un preț, coincidență). Un semnal cu fals-pozitive pe exact cazurile de
        # departajat e mai rău decât o regulă simplă și declarată.
        return "UNIT", "fără catalog — pin de rutare/formulare, nu poate testa grounding"
    return "KEEP", "rulează pe ambele contracte (v1 + creier unic)"


def audit() -> dict:
    raw = json.loads(CASES.read_text(encoding="utf-8"))
    cases = raw if isinstance(raw, list) else raw["cases"]
    raw_conv = json.loads(CONVERSATIONS.read_text(encoding="utf-8"))
    convs = raw_conv if isinstance(raw_conv, list) else raw_conv["conversations"]
    gaps = _brain_gaps()

    rows = []
    for case in cases:
        klass, reason = _classify(case, gaps)
        fx = json.dumps(case.get("fixtures") or {}, ensure_ascii=False)
        rows.append(
            {
                "id": case["id"],
                "kind": "case",
                "classification": klass,
                "reason": reason,
                "dimension": _dimension(case),
                "language": case.get("language", "ro"),
                # Fixturile sunt scrise la birou. Marcat explicit: un set fără nicio cerere
                # reală măsoară ce ne-am imaginat că întreabă clienții.
                "provenance": "synthetic",
                "fixture_has_ai_summary": "ai_summary" in fx,
                "fixture_has_catalog": bool((case.get("fixtures") or {}).get("catalog")),
            }
        )
    for conv in convs:
        rows.append(
            {
                "id": conv["id"],
                "kind": "conversation",
                "classification": "KEEP",
                "reason": "conversație multi-tur; starea curge între tururi (gate CI separat)",
                "dimension": "action_followup",
                "language": conv.get("language", "ro"),
                "provenance": "synthetic",
                "fixture_has_ai_summary": "ai_summary" in json.dumps(conv, ensure_ascii=False),
                "fixture_has_catalog": True,
            }
        )

    pii = [
        r["id"]
        for r, src in zip(rows, cases + convs, strict=True)
        if contains_pii(json.dumps(src, ensure_ascii=False))
    ]
    return {"rows": rows, "pii": pii, "n_cases": len(cases), "n_conversations": len(convs)}


def render(data: dict) -> str:
    rows = data["rows"]
    by_class = Counter(r["classification"] for r in rows)
    quality = [r for r in rows if r["classification"] in ("KEEP", "HOLDOUT")]
    # Acoperirea se numără pe TOT ce nu e legacy. `UNIT` e un rol (pin de regresie), nu o
    # excludere:
    # un caz de clarificare acoperă tipul de conversație „simplu" chiar dacă serveşte şi ca pin.
    covering = [r for r in rows if r["classification"] != "LEGACY"]
    by_dim = Counter(r["dimension"] for r in covering)
    ai = sum(1 for r in rows if r["fixture_has_ai_summary"])

    lines = [
        "# NX-202 — auditul setului golden",
        "",
        f"Inventar: **{data['n_cases']} cazuri + {data['n_conversations']} conversații**.",
        "",
        "## Clasificare",
        "",
        "| clasă | n |",
        "| --- | ---: |",
    ]
    lines += [f"| {k} | {v} |" for k, v in sorted(by_class.items())]
    lines += [
        "",
        "`KEEP` nu e o opinie: `tests/test_golden.py` rulează FIECARE caz pe ambele contracte",
        "(v1 și creier unic, parametrizat). Un caz care trece pe profilul `brain` e valid pe",
        "arhitectura țintă. `LEGACY` sunt exact cele pe care harnessul le declară incapabile.",
        "",
        "## Compoziție față de ținta cardului",
        "",
        "Numărată pe tot ce nu e `LEGACY`. `UNIT` e un ROL (pin de regresie), nu o excludere din",
        "acoperire: un caz de clarificare acoperă tipul simplu chiar dacă servește și ca pin.",
        "",
        "| dimensiune | are | ținta | gol |",
        "| --- | ---: | ---: | ---: |",
    ]
    for dim, target in TARGET_MIX.items():
        have = by_dim.get(dim, 0)
        lines.append(f"| {dim} | {have} | {target} | {max(0, target - have)} |")
    lines += [
        "",
        f"Set de calitate (KEEP + HOLDOUT): **{len(quality)}** din 50-100 ceruți · "
        f"acoperire totală: {len(covering)}.",
        "",
        "## Ce a ieșit altfel decât presupunea cardul",
        "",
        "- **Cazurile NU sunt cuplate la catalog.** Fixturile poartă un catalog sintetic,",
        "  self-contained, deci nu au expirat la schimbarea tenantului — spre deosebire de",
        "  qrels-ul NX-203, care chiar a murit așa.",
        f"- **`ai_summary` apare în {ai} fixturi**, dar nu e datorie tehnică: codul tratează deja",
        "  absența lui. Ce rămâne e o divergență de FORMĂ: fixtura dă modelului un rezumat pe",
        "  care catalogul real nu-l are pe niciun rând, deci testul e mai UȘOR decât producția.",
        "",
        "## Ce lipsește, măsurat",
        "",
        f"- **Proveniență:** {sum(1 for r in rows if r['provenance'] == 'synthetic')}/{len(rows)}",
        "  sunt sintetice. Cardul cere ca query-urile grele să fie REALE (sanitizate), nu",
        "  inventate la birou. Sursa există: cele 12.665 de fraze din `recommendation_trigger`.",
        "- **Holdout:** 0 cazuri marcate. Un holdout nemarcat nu e holdout, e set de tuning.",
        f"- **PII:** {len(data['pii'])} cazuri semnalate"
        + (f" ({', '.join(data['pii'])})" if data["pii"] else " — curat"),
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    data = audit()
    text = render(data)
    print(text)
    if args.write:
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        REPORT.parent.mkdir(exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        print(f"Scris: {OUT.relative_to(ROOT)} · {REPORT.relative_to(ROOT)}")
    else:
        print("(dry-run — nimic scris; adaugă --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
