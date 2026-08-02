# NX-210 H3 Runbook

This runbook handles only offline artifacts. It does not call production workers, register tools,
or write to a database.

## 1. Readiness

Without an approved NX-201 policy, readiness reports `decision_policy_unavailable` and exits 2:

```powershell
$env:PYTHONPATH='.'
python scripts/nx210_h3.py readiness `
  --qrels tests/golden/qrels_confirmed.json `
  --output reports/nx210-h3-readiness.json
```

After NX-201 is approved, store the exact `DecisionPolicy` JSON outside evaluator packets and pass
it with `--policy`. The policy fingerprint is copied into every subsequent artifact.

## 2. Run Artifacts

Generate one JSON file per system on the same H3 cases. The runner requires exact case-ID coverage,
the qrels `business_id`, and the correct variant label:

```json
{
  "schema_version": 1,
  "business_id": "server-owned-business-id",
  "variant": "baseline",
  "cases": [
    {
      "case_id": "controlled-case-id",
      "response": {"text": "response text", "product_ids": ["controlled-product-id"]},
      "metrics": {
        "deterministic_failures": [],
        "latency_ms": 1000,
        "cost_usd": 0.01
      }
    }
  ]
}
```

Allowed deterministic failures are fixed by `RunMetrics`; arbitrary strings are rejected.

## 3. Seal Blind Packets

Run this only when readiness is green. Keep the reveal file away from evaluators:

```powershell
python scripts/nx210_h3.py pack `
  --qrels tests/golden/qrels_confirmed.json `
  --policy path/to/nx210-policy.json `
  --baseline path/to/baseline-run.json `
  --candidate path/to/candidate-run.json `
  --seed sealed-random-seed `
  --packets-output path/to/evaluator-packets.json `
  --reveal-output path/to/sealed-reveal.json
```

Evaluator packets contain no side label, latency, cost, deterministic failure, or case ID. The
separate reveal contains controlled IDs and metrics, but no prompts or response text.

## 4. Ratings and Decision

Ratings use the same four 1-5 rubric dimensions for responses A and B. They must cover every pair
exactly once and carry the policy fingerprint.

```powershell
python scripts/nx210_h3.py evaluate `
  --policy path/to/nx210-policy.json `
  --ratings path/to/blind-ratings.json `
  --reveal path/to/sealed-reveal.json `
  --output reports/nx210-h3-decision.json
```

Exit 0 means only `candidate_for_adi_review`. Exit 3 means `insufficient_data` or `no_go`. No
software path can emit a signed GO; Adi records the final decision in the ADR.
