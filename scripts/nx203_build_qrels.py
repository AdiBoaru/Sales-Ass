"""NX-203 populare — construiește qrels REALE din adevărul validat NX-202 (compound_truth).

Sursa: `tests/golden/compound_truth_proposed.json` (produse reale din catalogul demo, validate prin
exemplarele lui Adi). Ieșire: `tests/golden/retrieval_qrels_compound.json` (QrelsSet).

Doar cazurile cu `expected_products` (12/19) au sens pentru benchmark-ul de RETRIEVAL — cazurile
clarify/safety/imposibilitate-pură nu au produse de găsit (sunt calitate conversațională, nu
retrieval). Determinist, fără DB. Rulat o dată; ieșirea se comite.
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "tests" / "golden" / "compound_truth_proposed.json"
OUT = ROOT / "tests" / "golden" / "retrieval_qrels_compound.json"
BUSINESS_ID = "6098812a-50fc-44bd-a1ba-bc77e6399158"
CATALOG_VERSION = "demo-2026-07-24"  # snapshot Codex NX-202b

# match_class → relevanță graduală (0-3). Orice produs pe care botul TREBUIE să-l scoată = relevant.
RELEVANCE = {
    "exact": 3,
    "comparison_member": 3,  # produsele numite într-o comparație TREBUIE găsite
    "bundle_member": 3,
    "routine_member": 3,
    "hard_match_soft_partial": 2,
    "hard_match_soft_unknown": 2,
    "alternative": 2,
    "soft_match": 2,
}


def _category(truth: dict) -> str | None:
    for hc in truth.get("hard_constraints", []):
        if hc.get("facet") == "category":
            return str(hc.get("value"))
    # fallback: din primul produs, dacă are categorie în nume (best-effort, doar stratificare)
    return None


def build() -> dict:
    d = json.loads(SRC.read_text(encoding="utf-8"))
    queries = []
    skipped = []
    for section in ("compound", "compare"):
        for case in d[section]:
            t = case["truth"]
            exp = t.get("expected_products", [])
            if not exp:
                skipped.append((case["id"], t.get("form", "?")))
                continue
            judgments = []
            for p in exp:
                pid = p.get("product_id")
                if not pid:
                    continue
                rel = RELEVANCE.get(p.get("match_class", ""), 2)
                judgments.append({"product_id": pid, "relevance": rel})
            forbidden = [
                p["product_id"]
                for p in t.get("forbidden_products", [])
                if isinstance(p, dict) and p.get("product_id")
            ]
            # dedup forbidden care ar fi și judged (integritate: nu pot fi ambele)
            judged_ids = {j["product_id"] for j in judgments}
            forbidden = [f for f in dict.fromkeys(forbidden) if f not in judged_ids]
            queries.append(
                {
                    "id": case["id"],
                    "query": case["input"],
                    "locale": "ro",
                    "provenance": "synthetic",
                    "category": _category(t),
                    "catalog_version": CATALOG_VERSION,
                    "judgments": judgments,
                    "forbidden_products": forbidden,
                    "hard_constraints": t.get("hard_constraints", []),
                }
            )
    return {
        "_meta": {
            "source": "tests/golden/compound_truth_proposed.json (adevăr NX-202 validat)",
            "note": "qrels REALE din catalogul demo; doar cazuri cu expected_products. "
            "clarify/safety/imposibilitate-pură excluse (nu au produse de găsit).",
            "excluse": skipped,
        },
        "schema_version": 1,
        "business_id": BUSINESS_ID,
        "queries": queries,
    }


if __name__ == "__main__":
    out = build()
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    q = out["queries"]
    print(f"qrels scrise: {len(q)} interogări cu produse ({OUT.name})")
    print(f"excluse (fără produse): {[s[0] for s in out['_meta']['excluse']]}")
    tot_j = sum(len(x["judgments"]) for x in q)
    tot_f = sum(len(x["forbidden_products"]) for x in q)
    print(f"judgments: {tot_j}, forbidden: {tot_f}")
