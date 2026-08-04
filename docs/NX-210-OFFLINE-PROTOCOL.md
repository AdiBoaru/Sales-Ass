# NX-210 Offline Prototype and Blind Evaluation Protocol

**Status:** pre-registered implementation protocol; H3 evaluation not run; no GO/NO-GO.

## Boundary

The prototype lives only under `src/evals`. It has no worker registration, production tool
registration, database write path, or migration. Search is an injected read-only function.
`RuntimeQuerySpec` remains in turn memory and only `SafeQuerySpec` may enter a report.

`AnswerPlanV0` is experimental and throwaway by default. It is not the NX-211 production
contract. Promotion may be considered only after a stable schema, a named owner, contract tests,
and Adi's signed GO decision. Until all four exist, downstream production code must not import it.

## Prototype Flow

1. The single-agent boundary receives raw message, relevant history, and profile.
2. It produces an internal `RuntimeQuerySpec`.
3. It calls the injected `search_entities` boundary at most three times.
4. Re-search stops deterministically on grounded candidates, complete coverage, exact identifier,
   no new query, no new candidates, clarification, hard-constraint relaxation, time, cost, or the
   search cap.
5. The minimal validator rejects ungrounded or rejected selections. Every exception returns a
   visible fallback or clarification plan with a fixed degradation code.

## Blind Design

- Paired: baseline and candidate answer the same controlled case.
- Target sample: 50 hard cases. The policy cannot accept fewer than 30 paired observations.
- Assignment: deterministic A/B randomization from a sealed seed and case ID.
- Evaluator packet: prompt, response A, response B, and product IDs only.
- Sealed reveal: case ID, candidate side, deterministic failures, latency, and cost. The reveal is
  opened only after human ratings are complete.
- PII: prompt/answer text fails closed to `[REDACTED]`; product IDs matching the shared PII guard
  are omitted. Reports contain hashes, controlled IDs, numbers, and fixed codes only.

## Human Rubric

Each response receives a 1-5 score for task success, factual grounding, constraint adherence, and
clarity. The primary paired score is the arithmetic mean of these four dimensions. Every report
also records the mean paired delta for each dimension.

## Frozen Decision Rule

The concrete `DecisionPolicy` is serialized and SHA-256 fingerprinted before ratings are opened.
The default design uses 95% paired bootstrap confidence with 5,000 resamples and requires:

- at least 50 paired observations for the planned H3 run;
- mean overall delta at least 0.25;
- lower confidence bound strictly above zero;
- zero simple-fact regressions;
- zero candidate hard-constraint failures;
- candidate P90 latency at or below the NX-201 provisional SLO;
- mean candidate cost at or below the approved cost cap, with no missing cost values.

Any failed condition is `no_go`. Passing conditions produce only
`candidate_for_adi_review`; software cannot emit GO. The final ADR and GO/NO-GO require Adi's
signature.

## Readiness Record

The code and deterministic tests can establish offline behavior, isolation from production,
fallback behavior, PII-safe report projections, and evaluation math. They cannot establish the
quality gate until the following external inputs are sealed and the paired run is performed:

- NX-201 provisional SLO and approved cost cap;
- reviewed NX-202 hard holdout expanded to the pre-registered sample size;
- baseline and candidate response artifacts generated on that holdout;
- blind human ratings;
- signed ADR decision by Adi.

No scores, confidence interval, or migration recommendation are claimed by this implementation
commit.
