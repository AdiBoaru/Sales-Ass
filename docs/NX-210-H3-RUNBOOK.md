# NX-210 Quality H3 runbook

H3 is a conversational quality decision, not a retrieval benchmark. Its primary input is a
sealed `QualityH3Set` owned by NX-202. NX-203/NX-209 qrels remain a separate prerequisite that
proves the retrieval layer is ready; they never supply the prompts scored by evaluators.

## Preconditions

The runner fails closed until all of these are available:

- at least 50 human-verified, sealed quality cases;
- at least 30 `hard` cases and 10 `simple_fact` cases;
- sanitized prompt, history, and profile fields with no detected PII;
- a frozen `DecisionPolicy` including provisional latency and cost limits;
- a green NX-209 retrieval readiness gate over independently maintained qrels;
- baseline and candidate artifacts covering exactly the same quality case IDs and business.

The current legacy golden inventory is useful source material, but it is not implicitly treated as
sealed H3 data. Rewrites, validation, difficulty labels, and holdout sealing remain explicit NX-202
work.

## Commands

Generate a PII-safe readiness projection without opening the holdout:

```powershell
python -m scripts.nx210_h3 readiness `
  --quality-h3 path/to/quality-h3.json `
  --retrieval-qrels tests/golden/qrels_confirmed.json `
  --policy path/to/frozen-policy.json `
  --output reports/nx210-h3-readiness.json
```

Seal blind evaluator packets and keep the reveal key in a separate restricted location:

```powershell
python -m scripts.nx210_h3 pack `
  --quality-h3 path/to/quality-h3.json `
  --retrieval-qrels path/to/ready-qrels.json `
  --policy path/to/frozen-policy.json `
  --baseline path/to/baseline-run.json `
  --candidate path/to/candidate-run.json `
  --seed sealed-run-seed `
  --packets-output path/to/evaluator-packets.json `
  --reveal-output restricted/path/to/reveal.json
```

After every blind pair is scored, reveal and evaluate:

```powershell
python -m scripts.nx210_h3 evaluate `
  --policy path/to/frozen-policy.json `
  --ratings path/to/ratings.json `
  --reveal restricted/path/to/reveal.json `
  --output path/to/decision-report.json
```

The automated outcome is only `insufficient_data`, `no_go`, or `candidate_for_adi_review`.
Production rollout still requires Adi's explicit signature.
