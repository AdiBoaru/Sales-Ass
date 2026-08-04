# NX-211 AnswerPlan v1

`AnswerPlan` is the production, versioned contract between the sales agent and response rendering.
It is distinct from the disposable `AnswerPlanV0` in the NX-210 offline experiment.

## Runtime order

When `ANSWER_PLAN_ENABLED=true`, every sales turn follows this order:

1. project server-owned product rows, live facts, constraints, and successful mutation IDs into
   `AnswerPlanContext`;
2. request `AnswerPlan` v1 with structured output;
3. validate tenant, exact product/variant identity, current evidence, fact values, hard constraints,
   known needs, PII, and action success;
4. allow exactly one plan revision when validation fails;
5. render the existing response path only after the plan is valid;
6. run the deterministic prose validator;
7. optionally run the semantic critic, revise the draft at most once, and validate again;
8. produce a localized, non-empty safe fallback on every terminal failure.

The full plan remains in `TurnContext` memory only. Analytics persist schema version, counts,
revision count, and closed failure codes; no claim text, product IDs, evidence IDs, prompt, history,
or profile is emitted.

## Deterministic guarantees

- `business_id` and locale must equal the server context.
- Products require exact identity evidence; variants require exact variant membership and evidence.
- Price, stock, and URL values must equal current evidence from the same tenant.
- Factual and recommendation claims require current evidence.
- `MISMATCH` always blocks. `UNKNOWN` blocks only when the constraint explicitly marks unknown as a
  safety violation.
- Action confirmations require a successful server-owned action ID with the same action type.
- Draft claims such as "added to cart" are rejected unless the plan contains that confirmed action.
- PII in plan or final draft fails closed.

## Semantic critic triggers

The critic is selected by code, not by the model. It runs for recommendations, comparisons,
medical or legal language, evidence coverage below the configured threshold, an initial validator
failure, or every AnswerPlan sales response on the max-quality tier.

The critic returns only fixed failure codes. If it is unavailable, the already deterministic-
validated draft remains usable. If it rejects, one revision is allowed; an invalid revision becomes
the safe fallback.

## Kill switches

- `ANSWER_PLAN_ENABLED=false`: calls the pre-NX-211 renderer directly.
- `ANSWER_PLAN_CRITIC_ENABLED=false`: keeps AnswerPlan validation but makes no critic call.
- `ANSWER_PLAN_MAX_QUALITY=true`: adds the critic trigger to every AnswerPlan sales response.
- `ANSWER_PLAN_CRITIC_COVERAGE_THRESHOLD=0.99`: evidence-coverage trigger threshold.

Both feature switches default to OFF. Activation remains blocked until the NX-210 H3 decision is
signed; implementation and tests do not constitute that GO.
