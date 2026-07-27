"""NX-206 — poarta de completitudine a catalogului, peste auditul v3.

Nu înlocuiește auditul NX-168d: îi păstrează toate violation-urile și adaugă parserul tipizat
NX-205, care vede intrările malformate și contradicțiile structurale. Este pură (fără DB), astfel
încât seed-ul o rulează înainte de orice scriere, iar raportul poate fi folosit offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_catalog_v2 import DEFAULT_CATALOG, evaluate  # noqa: E402
from src.domain.contracts import parse_product  # noqa: E402

_AUDIT_BUSINESS_ID = "catalog-audit"
_AUDIT_LOCALE = "ro"


def completeness_report(data: dict[str, Any]) -> dict[str, Any]:
    """Întoarce blocajele de publicare și progresul per categorie.

    `violations` este lista unică folosită de pre-flight. Fiecare finding are `code`, `message` și
    `product_slugs`, ca o extensie aditivă a shape-ului auditului. Un finding global (fără slug) e
    fail-closed: apare în `global_violations` și niciun produs nu este declarat publicabil.
    """
    by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    global_violations: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    def add(code: str, message: str, slugs: list[str]) -> None:
        finding = {"code": code, "message": message, "product_slugs": sorted(set(slugs))}
        violations.append(finding)
        if finding["product_slugs"]:
            for slug in finding["product_slugs"]:
                by_slug[slug].append(finding)
        else:
            global_violations.append(finding)

    # Strat 1: schema + R1-R13, sursa existentă de adevăr a auditului v3.
    audit_result = evaluate(data, contract="v3")
    for code, findings in audit_result["violations"].items():
        for finding in findings:
            add(code, finding["message"], list(finding["product_slugs"]))

    # Strat 2: contractul NX-205. Acoperă contradicțiile facts↔facts și forma pe care JSON Schema
    # nu o exprimă; parse_product raportează toate problemele, fără să oprească inventarul.
    raw_products = data.get("products") if isinstance(data, dict) else None
    for product in raw_products if isinstance(raw_products, list) else ():
        if not isinstance(product, dict):
            continue  # auditul structural de mai sus este deja finding-ul canonic
        slug = product.get("slug")
        _, problems = parse_product(
            product,
            business_id=_AUDIT_BUSINESS_ID,
            locale=_AUDIT_LOCALE,
        )
        for problem in problems:
            add("facts_contract", problem, [slug] if isinstance(slug, str) and slug else [])

    categories: dict[str, dict[str, Any]] = {}
    products = raw_products if isinstance(raw_products, list) else []
    for product in products:
        if not isinstance(product, dict):
            continue
        slug = product.get("slug")
        category = product.get("primaryCategorySlug")
        key = category if isinstance(category, str) and category else "(fără categorie)"
        entry = categories.setdefault(
            key,
            {"total": 0, "passed": 0, "blocked": 0, "issues": Counter(), "blocked_products": []},
        )
        entry["total"] += 1
        product_issues = by_slug.get(slug, []) if isinstance(slug, str) else []
        if global_violations or product_issues:
            entry["blocked"] += 1
            if isinstance(slug, str) and slug:
                entry["blocked_products"].append(slug)
            entry["issues"].update(finding["code"] for finding in product_issues)
        else:
            entry["passed"] += 1

    for entry in categories.values():
        entry["issues"] = dict(sorted(entry["issues"].items()))
        entry["blocked_products"].sort()
    return {
        "violations": violations,
        "global_violations": global_violations,
        "categories": dict(sorted(categories.items())),
    }


def gate_violations(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Lista de blocaje pentru seed/publicare; păstrează warnings-urile în afara porții."""
    return completeness_report(data)["violations"]


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", nargs="?", default=str(DEFAULT_CATALOG))
    args = parser.parse_args()
    data = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    report = completeness_report(data)
    for category, row in report["categories"].items():
        issues = ", ".join(f"{k}={v}" for k, v in row["issues"].items()) or "—"
        print(f"{category}: {row['passed']}/{row['total']} trec; blocat={row['blocked']}; {issues}")
    if report["violations"]:
        print(f"✗ poarta NX-206: {len(report['violations'])} blocaje")
        return 1
    print("✓ poarta NX-206: catalog complet pentru publicare")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
