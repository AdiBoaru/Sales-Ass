# Conversation kernel contract v1.0

> Sursa: tabul „Contract v1.0” din documentul „Aria conversation kernel — technical design” (claude.ai/code/artifact/d32bcfd1-104d-4b00-9800-ecaba4e67fcd), exportat pe 2026-09-25. De aici încolo sursa de adevăr e ACEST fișier: orice schimbare trece prin PR, cu regula minor/major din „Status and scope”, iar documentul online nu mai e normativ.

Sep 25, 2026 · @Dan

## Status and scope

This tab is normative: where it and the design tab disagree, this tab wins, and code that contradicts it is a defect. It governs every component between the user's message and the existing tools: interpretation, reference resolution, delta, reducer, ambiguity gate, planner. Tools, composition, validators and the web contract are out of scope and unchanged.

Versioning: the contract is `kernel.v1.0`. **Minor** (`v1.1`): additive schema fields, new enum values, new optional metadata. **Major** (`v2.0`): a change in the meaning of a field, an invariant, an ownership row or state semantics. Every bump updates the tests that encode it in the same PR, and major bumps need the replay gate. The version is stamped on every `turn_interpretation` event, so replays can be split by contract version.

**Status: `kernel.v1.0` frozen after review round 4 (2026-09-25).** From here, changes follow the minor/major rule; architecture changes are out of scope. Future defects go through the replay corpus and are fixed in pack data, a reducer rule or the one component the trace blames.

## Review decisions

All eight points are accepted. Three go further than the review asked (thread, goal, provenance), and one moves to a different layer (insufficient evidence). None is rejected.

| # | Review point | Decision | What changes in v1.0 |
| --- | --- | --- | --- |
| 1 | `thread=switch` mixes navigation with category change | Accepted, further | `switch` removed. `thread` is navigation only: `continue`, `aside`, `resume`. Parking is a reducer side effect of a **subject** change (category or product type), never of a constraint change. „Nu mai vreau roșu, vreau albastru” is a `replace` on color: nothing parked |
| 2 | Turn goal vs conversation goal | Accepted, further | `goal` removed from the interpretation. The turn goal *is* the primary act. A conversation goal has no consumer in v1.0, so it is not built; `Topic.goal` stays reserved until something reads it |
| 3 | `changes: 0-6` too rigid | Accepted | No count limit in the schema. Runtime caps: 10 changes, 6 references, 3 acts; overflow is dropped in order and counted (`interpretation_truncated`) |
| 4 | `free` must be a last resort | Accepted | Code re-resolves every `free` value against the tenant vocabulary. A hit is promoted to that dimension; a miss becomes a query and ranking signal. `free` is never dropped and never a filter |
| 5 | `act_both` needs an `insufficient_evidence` outcome | Accepted, moved | It can only be decided after facts are fetched, so it belongs to the answer policy (after tools, before compose), not to the ambiguity gate. A verdict between candidates is forbidden when the deciding attribute is unknown on any of them |
| 6 | Provenance needs three levels | Accepted, made deterministic | `explicit` / `implicit` / `inferred`, decided by code from the quote and the vocabulary, not by the model. Strength follows from the level (section 5). The confirmation question is decided by information gain, not per domain |
| 7 | Quote presence ≠ semantic correctness | Accepted | Relations `avoid`, `lte`, `gte` also need a polarity or comparator marker in the quote, from per-locale tables. A missing marker downgrades the change to `implicit` |
| 8 | `find` does not always mean search | Accepted | The planner resolves `find` to search, ask, recommend-from-state or delegate (section 7) |

Also kept as the review asked: `brain.py` stays frozen as a benchmark, and the phase order is 0 → 1 → 2 → 3 → 4 → v2 shadow in production → 5 dark → replay → canary → production.

**Round 2.** All four accepted; none needed a design change, only sharper rules.

| # | Review point | Decision | Where |
| --- | --- | --- | --- |
| 9 | `resume` could be read as a stack pop | Accepted: it is a swap with the single parked slot, stated as invariant I19 | Reducer § Thread |
| 10 | `corrects_previous_turn` is too powerful | Accepted: the flag is evidence; code applies correction semantics only on a real contradiction (I21) | Reducer § Corrections |
| 11 | I7 should state the positive rule | Accepted: I7 now reads „only explicit user evidence produces a hard filter”, with the full strength table | Invariants, Provenance |
| 12 | `free` re-resolution must not depend on search results | Accepted: re-resolution happens once, at validation, against the turn's vocabulary snapshot; needs and topic are a pure function of the turn's inputs (I20) | Provenance, Invariants |

**Round 3 (Codex).** 15 of 17 accepted as written, 2 adjusted (13 and 19), none rejected.

| # | Point | Decision | Where |
| --- | --- | --- | --- |
| 13 | `Reference.kind` ownership was ambiguous | Accepted: the model proposes reference semantics; code validates and may reclassify or reject | Ownership |
| 14 | Act targets and `relative_to` can name a phantom reference | Accepted: I22 | Invariants |
| 15 | `within` is system knowledge | Accepted, and removed outright: the user's temporal deixis is already `kind=earlier`; where an object lives is only `ResolvedRef.source` | Models |
| 16 | `dimension`/`value` in references are proposals | Accepted: code resolves membership and the canonical value | Ownership |
| 17 | `free` looks like a real dimension | Accepted: renamed `unmapped`, a query signal, never a filter | Models, Provenance |
| 18 | I13 reads as „one call per request” | Accepted: one call for kernel interpretation; executor and compose calls are outside it | I13 |
| 19 | Adapter vs pure kernel boundary | Accepted: `turn_interpreter` (edge, may call the LLM) vs pure kernel (may not) | I13, Models |
| 20 | Is `naturalize` a model call? | Clarified: it never was. `voice.naturalize` is a pure function. Adjusted further: clarification questions are now template-only, so no model text reaches a question | Ownership |
| 21 | `bundle → routine_plan` leaks skincare into the kernel | Accepted: the pack declares the bundle executor; SOLE maps it to `routine_plan` | Planner |
| 22 | Parked ids are not catalog truth | Accepted: every candidate id is revalidated against the catalog before use | I1 |
| 23 | I15 too broad for store URLs | Accepted: product facts and product URLs only; store and policy links keep their existing grounding | I15 |
| 24 | „No text ifs” would ban structured branching | Accepted: no branching on raw user text, except the central locale marker tables | Enforcement |
| 25 | „Byte-identical” undefined | Accepted: the observable surface is listed; telemetry and traces are excluded | I16 |
| 26 | Need a `semantic_mismatch` reject | Accepted, with its limit: code detects it only when the quote resolves to a *different* value. A quote that resolves to nothing stays `implicit` | Provenance |
| 27 | Inferred changes should not persist | Accepted: `inferred` is turn-local (I23) | Provenance, Invariants |
| 28 | Budget scope | Decided: conversation-scoped; a new explicit budget replaces it | Reducer |
| 29 | Multi-act failure semantics | Accepted: dependent acts are skipped on failure, independent ones run, the reply reports the failure first | Planner |

Versioning was also unified to the minor/major rule (Status).

**Round 4.** All 8 accepted; two refined (30 and 31) so they don't introduce a regression.

| # | Point | Decision | Where |
| --- | --- | --- | --- |
| 30 | An act's targets must be semantically compatible with it | Accepted, refined: a target must *denote products*. „Samsung-ul”, „varianta neagră” and „cel mai ieftin” do, and are valid even for `cart` when they resolve `exact`. A reference that denotes a property („roșeața”) is rejected (`invalid_reference_target`); that is a state change, not a target | I24 |
| 31 | `unmapped` must stay weak | Accepted, refined. Never hard, never promoted by search results, and bounded rank-only weight: it cannot enter the lexical `WHERE`. Rejected: „never persisted”. A user-said signal („bun pentru gaming”) has to survive „și sub 3000”; only an `inferred` one is turn-local (I23) | I25, Provenance |
| 32 | One-level parking needs metrics | Accepted: `resume_not_available`, `parked_evicted`, `resume_after_multiple_switches` | Reducer |
| 33 | `find` must not become a god enum | Accepted: an act names what the user asked, never how to execute it. A new `ActKind` needs a user request that no existing act plus a planner rule can express | Planner |
| 34 | Clarification templates strictly pack-owned | Accepted: `DomainPack.clarify_templates` per locale and ambiguity kind; the kernel only fills canonical labels | Planner |
| 35 | Split I15 | Accepted: I15a product facts, I15b product identity and URL | Invariants |
| 36 | I16 needs a strict differential test | Accepted: a differential harness, `main` vs branch with the flag OFF, over the full journey corpus, as a CI gate from step 1 | I16, Implementation order |
| 37 | Inventory every state writer before building the reducer | Accepted as step 0.5 | Implementation order |

## Ownership contract

The model proposes meaning; code decides everything that is a fact, an identifier, a filter or a state change. „Proposes” means the value is input to a code rule that can reject it; „owns” means no other component may produce it.

| Information | Model | Code | Rule |
| --- | --- | --- | --- |
| What the user wants this turn (acts) | proposes | validates shape and handles | Acts come only from the interpretation or a fast-path shortcut |
| Conversation navigation (`thread`) | proposes | applies | `aside` can never change state |
| The user's wording (`quote`, `query`) | supplies | verifies against the user's text | A quote not found in the user's messages is not user evidence |
| Dimension and value of a change | proposes | owns membership and canonical value | Must exist in the tenant vocabulary; otherwise `unmapped`, then re-resolved once by code |
| Category key | proposes from a closed menu | owns | Menu membership + the NX-313 contradiction check |
| Relation and polarity (`avoid`, `lte`, `gte`) | proposes | verifies marker | No marker → downgraded to `implicit` |
| Numbers and units | proposes | owns | Unit must belong to the dimension (`UnitRegistry`) |
| Provenance level | — | owns | From quote + vocabulary; the model has no field for it |
| Hard vs soft | supplies evidence only | owns | From provenance + hard capability |
| Product mention as text | supplies | — | — |
| Reference semantics (kind, ordinal, name, dimension, value, direction) | proposes | validates, may reclassify or reject | e.g. a `name` matching no product is `not_found`; an ordinal beyond the set is `ambiguous` |
| Where a referenced object lives | — | owns (`ResolvedRef.source`) | The model has no field for it |
| Product and variant ids | never | owns | Every candidate id, including parked and recent, is revalidated against the catalog |
| Price, stock, product URL, product attributes | never | owns (catalog) | Re-read at resolution time |
| Value of a relative change („mai ieftin decât”) | direction only | computes the number | From the resolved product's current price |
| State mutation | proposes (delta) | applies (reducer) | Single writer: `state_reducer` |
| State persistence and budget | — | owns | `serialize`, 6 KB; `inferred` never persisted |
| Ambiguity candidates | proposes | owns final verdict | Ambiguity gate |
| Clarification question text | — | owns | Pack template + vocabulary labels, deterministic interpolation; no model text |
| `SearchArgs` | never | owns (planner) | The only producer of `SearchArgs` on the interpreted path |
| Which executor runs, bundle semantics | — | owns (planner + pack) | The pack declares which executor serves `bundle` |
| Tool execution, safety policy, tenant (`business_id`) | never | owns | Unchanged from today |
| Reply prose | writes (compose) | validates | Existing validator + `grounding_guard`; `naturalize` is a pure function |
| Whether a verdict between products is allowed | — | owns | Answer policy: forbidden on unknown deciding attributes |

## Invariants

Twenty-six invariants, each with the test or gate that fails when it is broken. An invariant without an enforcing test is not part of the contract.

| # | Invariant | Enforced by |
| --- | --- | --- |
| I1 | No product or variant id on the interpreted path originates from model output. Candidate ids from shown, recent, parked or page sets are references only: each is revalidated against the catalog before any executor uses it | Schema test: no id-like property besides turn-local `r\d+`; property test: every id in a `TurnPlan` passed catalog revalidation this turn |
| I2 | On the interpreted path, `SearchArgs` is constructed only by the planner | AST gate: `SearchArgs(` call sites in kernel modules allowlisted to `turn_planner.py`; toolset test: the delegated loop has no catalog-search tool |
| I3 | State changes only through reducer proposals | AST gate: kernel modules never assign to `ctx.state*` fields; the step 0.5 inventory lists every legacy writer and its fate |
| I4 | No blind reset: conversation-scoped needs are cleared only by `remove` or `clear all`; topic-scoped needs leave only through a subject change, and are parked, not lost | Property test over random op sequences × 5 packs |
| I5 | `thread=aside` is the identity on state, including `active_search` | Property test |
| I6 | A revoked need is revived only by `explicit` evidence | Reducer unit test (exists as `revoked_key`) + property test |
| I7 | Only `explicit` user evidence can produce a hard filter: explicit + hard-capable → hard; explicit + not hard-capable → soft; implicit → soft; inferred → ranking only, this turn | Property test on the delta mapper: no `implicit`/`inferred` change ever reaches a `WHERE` |
| I8 | Hard-capable means an `enforce_ready` facet or a hard universal spec (budget, size, restriction) | Unit test on the vocabulary |
| I9 | A number becomes a value of dimension D only if its unit belongs to D, or it has no unit and D is the price dimension | Unit tests across 5 packs („100 ml”, „256 GB”, „SPF 50”, „3 locuri”) |
| I10 | A mutation (cart, checkout) executes only on `exact` targets | Gate unit test |
| I11 | At most one clarification question per turn; the same key is not asked more than `max_attempts_per_key` | Existing `clarification_policy` tests |
| I12 | No verdict between products when the deciding attribute is unknown on any of them | Answer-policy unit test + compose snapshot |
| I13 | On the interpreted path, interpretation uses at most one model call, made by the interpretation adapter (`turn_interpreter`). Validation, resolution, reduction, gating and planning are pure kernel components and make no model call. Executor and compose calls are outside this budget and unchanged | AST gate: pure kernel modules import no LLM client; replay check on `llm_usage.per_call` (exactly one `interpret` call per interpreted turn) |
| I14 | Kernel modules contain no vertical-specific literal | The NX-264 domain-leak gate, extended to the kernel modules |
| I15a | Every product fact in the reply (price, stock state, brand, attributes, dimensions) comes from a catalog read of this turn | Existing validator + `grounding_guard` |
| I15b | Every product identity in the reply (product id, variant id, product URL) is an exact catalog object resolved this turn. Store and policy links keep their existing grounding (FAQ, generated links) | Validator link check + I1 property test |
| I16 | Flag OFF ⇒ the v1 path is unchanged on its observable surface: response JSON and text, tool calls and their arguments, persisted conversation state, `selected_product`, `active_search`, DB writes. Timestamps, latency, telemetry and diagnostics are excluded; no `KernelTrace` is written | Differential harness, a CI gate from step 1: the full journey corpus runs on `main` and on the branch with the flag OFF, with the same input, state and environment; every listed surface must be equal, DB writes captured by a recording connection |
| I17 | State fits 6 KB; the drop order is declared and `recent_sets` goes first | `serialize` test extended |
| I18 | Every interpretation event carries the contract version | Event schema test |
| I19 | `resume` swaps the current subject with the single parked one; it is not a stack pop. A subject change while a topic is parked evicts the old parked topic, counted as `parked_evicted` | Reducer unit test: tablets → resume phones → laptops leaves phones parked and tablets gone |
| I20 | Needs and topic after the reducer are a pure function of (state before, interpretation, vocabulary snapshot of the turn, resolved references). Executor output may write only `references` and `active_search`, never needs or topic | Property test: same inputs ⇒ byte-identical needs/topic; AST gate: executors emit no need or topic proposals |
| I21 | Correction semantics apply only when a change contradicts a need written by the previous turn; `corrects_previous_turn` alone never revokes anything | Unit test: „Nu, vreau și protecție solară” with the flag set is an ordinary `add` |
| I22 | Every `Act.targets` entry and every `StateChange.relative_to` names a `Reference.id` declared in the same interpretation; unknown handles are rejected before planning (`unknown_reference`) | Validation unit test |
| I23 | An `inferred` change never creates persistent state: it is a ranking signal for the current turn's plan only | Property test: after any turn, no persisted need has source `model_inferred` from the interpreted path |
| I24 | Every act target must denote products: its reference resolves to a set of catalog products, never to a dimension value. Otherwise the act is rejected with `invalid_reference_target`. Mutations additionally need `exact` (I10) | Validation + resolver unit tests: „link → roșeața” rejected; „cart → cel mai ieftin” accepted when it resolves exact |
| I25 | `unmapped` signals are never hard, never promoted by search results, and rank-only: they never enter the lexical `WHERE`, and their ranking weight is capped | Planner golden test: `SearchArgs` carries them only as rank terms; SQL snapshot: no rank term in the predicate |

## Normative models

These replace section B of the design tab. Changes from it: `thread` loses `switch`; `goal` and `Reference.within` are gone; `free` is renamed `unmapped`; lists have no count limit in the schema; and the code-side models gain provenance, a closed reject vocabulary and the answer policy.

**Module boundary.** `src/conversation/turn_interpreter.py` is the only adapter: it builds the prompt and schema, makes the one `complete_schema` call and returns a `TurnInterpretation`. Everything after it (validation, resolver, delta, reducer, gate, planner, answer policy) is the pure kernel and must not import an LLM client (I13).

### Written by the model

```python
KERNEL_CONTRACT_VERSION = "kernel.v1.0"

ActKind = Literal["find", "show_more", "compare", "detail", "link", "cart",
                  "bundle", "order_status", "store_info", "chitchat", "other"]

class Act(BaseModel):
    kind: ActKind
    targets: list[str]            # Reference ids declared in this interpretation (I22)
    query: str | None             # the user's words, for find / store_info

class StateChange(BaseModel):
    op: Literal["set", "add", "remove", "replace", "clear"]
    target: str | None            # constraint handle "cN" (remove/replace), or "topic" / "all" (clear)
    dimension: str | None         # per-tenant enum incl. "category", "price"; "unmapped" = no dimension fits
    relation: Literal["eq", "lte", "gte", "contains", "avoid"] | None
    value: str | None
    number: float | None
    unit: str | None
    relative_to: str | None       # Reference id declared in this interpretation (I22)
    quote: str                    # the user's exact words: evidence, not proof

class Reference(BaseModel):       # a semantic proposal; code validates, may reclassify or reject
    id: str                       # "r1"
    text: str
    kind: Literal["ordinal", "deictic", "name", "attribute", "extreme", "the_other", "earlier"]
    ordinal: int | None
    name: str | None
    dimension: str | None         # proposal; code resolves membership
    value: str | None             # proposal; code resolves the canonical value
    direction: Literal["min", "max"] | None

class Ambiguity(BaseModel):
    about: Literal["reference", "scope", "value"]
    target: str | None
    readings: list[str]           # used by the gate to map readings to subject values, never shown

class TurnInterpretation(BaseModel):
    thread: Literal["continue", "aside", "resume"]
    acts: list[Act]                   # ordered; the last one is the primary act
    changes: list[StateChange]        # empty = nothing changed; no KEEP
    references: list[Reference]
    ambiguities: list[Ambiguity]
    corrects_previous_turn: bool      # evidence only (I21)
```

### Written only by code

```python
Provenance = Literal["explicit", "implicit", "inferred"]

ChangeReject = Literal[
    "unknown_dimension", "unknown_handle", "unknown_reference", "unit_mismatch",
    "polarity_conflict", "semantic_mismatch", "hard_conflict", "truncated",
    "invalid_reference_target",        # I24: an act target that does not denote products
]

class CheckedChange(BaseModel):          # a StateChange after validation
    change: StateChange
    dimension: str                     # after re-resolution of "unmapped"
    canonical_value: str | float | None
    provenance: Provenance
    strength: Literal["hard", "soft", "ranking"]   # ranking = turn-local, never persisted (I23)
    rejected: ChangeReject | None

class ResolvedRef(BaseModel):
    ref_id: str
    kind: str                          # final kind, after any reclassification by code
    outcome: Literal["exact", "ambiguous", "not_found", "stale"]
    product_ids: list[str]             # revalidated against the catalog this turn (I1)
    source: Literal["action", "shown_now", "shown_earlier", "parked", "page", "catalog"]
    reason: str | None

class AmbiguityDecision(BaseModel):
    verdict: Literal["act", "resolve_from_context", "act_both", "must_ask"]
    reason: str
    question: str | None               # pack template, deterministic interpolation

class TurnPlan(BaseModel):
    executor: Literal["search", "page", "compare", "detail", "link", "cart", "bundle",
                      "faq", "order", "ask", "delegate", "reply_only"]
    product_ids: list[str]
    search_args: SearchArgs | None
    depends_on: int | None             # index of an earlier plan in the same turn (multi-act)

class AnswerPolicy(BaseModel):         # computed after tools, before compose
    verdict_allowed: bool              # False ⇒ insufficient_evidence
    missing: list[str]
```

`TurnInterpretation` is emitted by one `complete_schema` call with `strict: true`. Dimension and category enums are built per tenant from the pack in a stable order, so the prompt prefix stays cacheable.

## Provenance and strength

Provenance is computed by code in four steps, in order. The model supplies only the quote; it cannot declare a level.

1. **Locate the quote.** Search it, normalized (lower case, no diacritics, collapsed spaces, whole words), in the user's messages: the current one plus the user's turns in the history window. Bot messages never count. Not found → `inferred`.
2. **Resolve the quote** through the tenant vocabulary (value, facet aliases, value labels, need map, unit alias + number):
   - resolves to the proposed `(dimension, value)` → `explicit` candidate;
   - resolves to a **different** value (any dimension), and not to the proposed one → rejected, `semantic_mismatch`;
   - resolves to nothing → `implicit`.

   The limit is declared: when the quote resolves to nothing, code cannot tell a reasonable inference („se usucă” → dry) from a structural hallucination („se usucă” → redness). Both stay `implicit`, which means soft: a wrong one can reorder results but never exclude any. The replay metric on implicit changes is how that rate is watched.
3. **Check polarity.** The marker tables are per locale, next to the existing stop-word and comparator tables in `query_terms`. They are function words, not domain words.
   - `avoid` needs a negation marker in the quote.
   - `lte` / `gte` need a comparator marker, or a bare price number for `lte`.
   - `eq` / `contains` with a negation marker next to the value → rejected (`polarity_conflict`).
   - A missing marker downgrades `explicit` → `implicit`.
4. **Derive strength.** `inferred` is turn-local: it can order this turn's results and is never persisted (I23).

| Provenance | Maps to `NeedSource` | Strength | Can revive a revoked need | Can override an explicit need |
| --- | --- | --- | --- | --- |
| explicit | `user_explicit` | hard if the dimension is hard-capable, else soft | yes | yes (supersede) |
| implicit | `user_implicit` (new) | soft (`prefer`); a confirmation candidate for the gate | no | no |
| inferred | `model_inferred` | ranking only, this turn; never persisted | no | no |

`user_implicit` is added to `NeedSource` and left out of `HARD_CAPABLE_SOURCES` and `REVIVE_CAPABLE_SOURCES`. The existing reducer rules then apply unchanged.

Whether an `implicit` need triggers a confirmation question is decided by information gain over the current pool, the same threshold as today (0.30), not by vertical.

| User wrote | Proposed change | Level | Why |
| --- | --- | --- | --- |
| „Ai ceva care să reducă roșeața?” | `add concerns=redness` | explicit | „roșeața” is an alias of `redness` |
| „Pielea mea se usucă după duș” | `add skin_type=dry` | implicit (unless the pack aliases „se usucă”) | quote present, does not resolve to `dry` |
| „Să nu fie foarte gras” | `add texture avoid greasy` | explicit | alias hit + negation marker „nu” |
| quote „gras”, relation `avoid` | same | implicit | no negation marker in the quote |
| „Minim 256 GB” | `add storage gte 256 GB` | explicit | unit belongs to `storage`, comparator „minim” |
| „Bun pentru gaming”, no such facet | `add unmapped="gaming"` | implicit | re-resolution misses → query/ranking signal |
| (bot said „roșeață”, user didn't) | `add concerns=redness` | inferred | quote only in bot text |

### When `unmapped` is re-resolved

Once per change, at validation, before the reducer, against the vocabulary snapshot loaded at the start of the turn:

```
interpretation → unmapped value → vocabulary resolution → known? promote to that dimension
                                                        → unknown? stays unmapped: a soft query signal
```

Never afterwards:

- Search results never feed back into needs or topic (I20), and never promote an unmapped signal to a facet.
- A stored unmapped signal is not rewritten when the pack later gains a matching facet. The promotion happens the next time the user's words resolve through the new vocabulary.
- The vocabulary snapshot id is recorded in the turn trace, so a replay resolves exactly as production did.

### Bounds on `unmapped` (I25)

| Property | Rule |
| --- | --- |
| Strength | never hard |
| Persistence | `explicit`/`implicit` (the user said it) persist as a soft signal, at most 3 per topic, topic-scoped. `inferred` is turn-local (I23) |
| Promotion | only through vocabulary resolution of the user's words at validation, never through search results |
| Effect on search | rank-only: passed as `SearchArgs.rank_terms`, used in `ORDER BY` next to `ts_rank_cd`, never in the lexical predicate; weight capped below any facet match |

`rank_terms` is the one additive change the contract requires in the search tool. Today the only textual input is `query`, and on the `strict` rung the query is a gate: its terms are joined with AND. Passing unmapped words through `query` would turn „piele obosită după avion” into a hidden filter, which is the NX-298 lesson.

## Reducer, thread and parking

The subject of a conversation is `(category_key, product_type)`. Only a change of subject parks anything; a change of any other dimension is an ordinary constraint change.

### Order of application within a turn

1. Validation (`CheckedChange`): rejected changes are dropped and counted.
2. Subject changes.
3. The other changes, in the order the model wrote them.
4. Reference side effects: `selected_product` = the resolved target of the primary act, when it is `exact`.

### Operations

| Op | Applies to | Effect | Rejected when |
| --- | --- | --- | --- |
| `set` | scalar dimension | New active value; the previous one is superseded with a tombstone | the source can't override the existing value (`hard_downgrade`) |
| `add` | list dimension | Appends; `avoid` creates an exclusion need | revoked value and the source can't revive it (`revoked_key`) |
| `remove cN` | a handle | Revokes, with a tombstone | unknown handle; the source can't revoke it (`unsupported_revoke`) |
| `replace cN` | a handle | Atomic supersede to the new value | unknown handle; dimension mismatch |
| `clear topic` | topic-scoped needs | Superseded (`topic_reset`); nothing parked | — |
| `clear all` | every need except safety/policy-protected ones | Superseded | — |

### Subject change and parking

- The current subject, its topic-scoped active needs and its last shown set are parked, one level deep. A later subject change replaces the parked slot.
- Topic-scoped needs are superseded with `topic_reset`. Conversation-scoped needs stay. A pending clarification is cleared.
- Scope is data: `NeedSpec.scoped` for universal keys, and a new `TypedFacet.scope` for facets (default `topic`).
- **Budget (decided in round 3): conversation-scoped.** „Cremă de față sub 100 lei” → „Arată-mi ce ai la corp” keeps price ≤ 100; a new explicit budget („Pentru corp pot da până la 200”) supersedes it. This changes today's `NeedSpec` for `budget_max`/`budget_min` (`scoped=True` → `False`), and it is part of step 3.

### Thread

| `thread` | Effect | Validation |
| --- | --- | --- |
| `continue` | Changes applied as above | — |
| `aside` | No change at all: needs, topic, references, `active_search` | An `aside` carrying changes is treated as `continue` and counted (`aside_with_changes`) |
| `resume` | Swap with the single parked slot: the parked subject becomes current, the current one becomes parked. Not a stack | No parked topic → no-op, counted |

Worked sequence (I19): current `tablete`, parked `telefoane`. „Hai înapoi la telefoane” → current `telefoane`, parked `tablete`. „Acum arată-mi laptopuri” → a subject change: current `laptopuri`, parked `telefoane`, and `tablete` is evicted (`parked_evicted`). Going deeper than one level is a contract change, not an implementation choice.

One level is a v1 bet, measured rather than assumed. Three counters decide whether it ever grows: `resume_not_available` (the user asks to go back and nothing is parked), `parked_evicted` (a parked topic is lost to a third subject), and `resume_after_multiple_switches` (a resume that targets a subject older than the parked one). A stack is considered only when these show real demand.

### Corrections and conflicts

- `corrects_previous_turn` is **evidence**. Code applies correction semantics only when a change actually contradicts the previous turn (I21), meaning one of:
  - `replace` or `remove` on a handle the previous turn created;
  - `set` on a scalar dimension whose active value the previous turn wrote, with a different value;
  - a subject change when the previous turn set the subject.
- **On a contradiction:** the contradicted need is superseded, and the previous turn's `implicit`/`inferred` needs on the same dimension are revoked (reason `correction`).
- **Without one:** the changes apply as an ordinary delta, and `correction_unconfirmed` is counted. „Nu, vreau și protecție solară” is an `add`; hydration stays.
- The asymmetry is deliberate. If the model writes a real correction as an `add`, the old need survives as an extra soft preference. Wrongly revoking a valid need would be worse, and the counter makes both visible.
- Crossing bounds in the **same** turn („sub 100, minim 150”) → a `hard_conflict` candidate, and the gate asks.
- Crossing a bound from an **earlier** turn → the newer `explicit` one wins and the old one is superseded (the user changed their mind). Either way it is counted.

## Planner and answer policy

The planner reads the reduced state, the resolved references and the gate verdict, never the raw interpretation text. A `find` becomes a search only when the state has something to search for.

### Primary act → executor

| Act | Condition (after reduction and gating) | Executor |
| --- | --- | --- |
| `find` | no subject, no query words, no facet needs | `ask`: a subject question from the pack's top shelves, gain-gated |
| `find` | subject known, no new words („Ce recomanzi?”) | `search` from state. The plan carries no query; because today's `SearchArgs.query` requires ≥ 1 character, the `SearchArgs` builder fills it with the subject's label and the `filters_only` rung serves it. This is a declared v1 workaround inside the builder, not planner logic |
| `find` | subject or query words present | `search`, `SearchArgs` derived as in design section D |
| `find` | an `implicit` need with gain ≥ 0.30, not yet asked | `search` + one confirmation question as the closing line (the NX-315 `question` slot) |
| `show_more` | `active_search` present, no changes this turn | `page` |
| `show_more` | changes this turn | `search` (it is a refinement) |
| `compare` | ≥ 2 exact targets | `compare` |
| `compare` | 1 exact target | `compare` with a similar product (the existing `COMPARE_WITH_SIMILAR` path) |
| `detail`, `link` | exact target | `detail` / `link` |
| `link` | name `not_found` | `search` with the name, then disclose the result is not an exact match |
| `cart` | exact target | `cart` |
| `bundle` | subject known, and the pack declares a bundle executor for it | `bundle`: the executor named by the pack (SOLE: `routine_plan`), with the family from the subject and the budget only from an `explicit` need. No executor declared → `search` |
| `store_info` / `order_status` | — | `faq` / `order` (login wall unchanged) |
| `chitchat` | — | `reply_only` |
| `other` | — | `delegate`: the tool loop without catalog search |

**Acts say what, the planner says how.** An act never carries an execution hint, and `find` is a request („the user wants products”), not a strategy. A new `ActKind` is admitted only for a user request that no existing act plus a planner rule can express; a new strategy is always a planner row.

**Clarification text is pack data.** `DomainPack.clarify_templates[locale][about]` holds the sentence, with `{options}` filled by canonical labels from the vocabulary (e.g. „Te interesează {options}?”). The kernel never holds a sentence. A pack without a template for that kind falls back to the pack's generic locale template, still pack data.

### Multi-act turns

- At most two executors run, a mutation before a read. A third act is reported in the reply as not done.
- A plan **depends on** an earlier one (`TurnPlan.depends_on`) when it targets the same product or uses a `relative_to` resolved by it.
- If a plan fails, the plans that depend on it are skipped; independent plans still run.
- The reply states the failure first, then the results that did run. „Adaugă X în coș și spune-mi dacă Y e mai bun”: if the cart fails, the Y answer is still given, after the failure is named.

### Answer policy (after tools, before compose)

`AnswerPolicy` is computed from the facts the executors returned:

- On `compare` or `detail` turns that ask for a judgement, the deciding dimension is the one named in the act's `query` or in an `extreme` reference.
- If that dimension is unknown on any candidate, `verdict_allowed=false`. Compose must state the known facts per product and name what is missing, with no winner. The existing `grounding_guard` superlative rule is the enforcement: a winner without a source is rejected.
- `act_both` follows the same rule per candidate. If neither candidate has the facts, the reply says so instead of answering „ambele sunt bune”.

## Enforcement and change control

The contract survives only if breaking it fails CI. Five gates make the likely regressions mechanical failures instead of review comments.

| Gate | Catches | Form |
| --- | --- | --- |
| Kernel AST gate | `SearchArgs` built outside the planner (I2), state written outside the reducer (I3), an LLM client imported by a pure kernel module (I13), executors emitting need/topic proposals (I20) | `tests/test_kernel_contract.py`; exceptions in a JSON allowlist with a written reason, the pattern `scripts/conn_allowlist.json` already uses |
| Schema snapshot | A silent change to what the model writes | The generated `TurnInterpretation` schema is checked in; a diff fails unless `KERNEL_CONTRACT_VERSION` changes in the same PR |
| No raw-user-text branching | Case-by-case fixes keyed on message wording | AST gate: pure kernel components never pattern-match (regex, `in`, `startswith`, literal comparison) against the raw user text. The only exception is the central per-locale marker tables that the provenance validator consumes. Branching on structured fields (`change.relation == "lte"`, `reference.kind == "ordinal"`) is normal code and allowed |
| Domain-leak gate | Vertical literals in kernel modules (I14) | The NX-264 gate, extended to the kernel paths |
| Replay gate | Behavior drift on any family | The phase 0 corpus runs on every major bump; the metrics in design section H may not regress on any family |

The rule for fixing a bad conversation, from now on:

1. Add the conversation to the replay corpus as a journey.
2. Classify the fix: pack data (alias, facet, scope), a rule in this contract, or a bug in a component.
3. A rule change bumps the contract version, with its test.

There is no fourth option: a fix that branches on the raw user text inside a kernel module is rejected by the gate above.

Version bumps follow the minor/major rule in Status.

## Canonical turn trace

Every interpreted turn writes one `KernelTrace`: the output of each layer, in order. It is built before any executor code (implementation step 1) and is the unit that replay compares.

```python
class KernelTrace(BaseModel):
    contract_version: str            # "kernel.v1.0"
    vocabulary_snapshot: str         # id of the pack/vocabulary used this turn
    interpretation: TurnInterpretation
    checked_changes: list[CheckedChange]
    resolved_refs: list[ResolvedRef]
    state_before: dict               # needs + topic + parked + references, as handles
    proposals: list[dict]            # reducer input
    rejected: list[dict]             # reducer rejects with reason
    state_after: dict
    ambiguity: AmbiguityDecision
    plan: TurnPlan
    executor: str
    answer_policy: AnswerPolicy | None
```

**Where it lives.** In `conversation_traces.diagnostics["kernel"]`. That capture already runs in production (NX-256), so there is no new table and no migration. The size is bounded like the rest of `diagnostics`. It follows the existing redaction: user text appears only as the quotes the interpretation already carries.

**How it reads.** `scripts/kernel_trace.py <turn_id>` prints it in this form:

```
USER             „Vreau ceva sub 100 lei”
INTERPRETATION   thread=continue  acts=[find]  changes=[set price lte 100 · quote „sub 100 lei”]
CHECKED          price lte 100  provenance=explicit (unit lei ∈ price, comparator „sub”)  strength=hard
REFERENCES       —
STATE BEFORE     topic=ingrijire-ten  c1 concerns ∋ hidratare (explicit, soft)
PROPOSALS        set_need budget_max=100 source=user_explicit
STATE AFTER      topic=ingrijire-ten  c1 concerns ∋ hidratare  c2 budget_max ≤ 100 (hard)
AMBIGUITY        act
PLAN             search  SearchArgs(category=ingrijire-ten, price_max=100, concerns=[hidratare] prefer, query=„cremă de față”)
EXECUTOR         search_products → 6 products, step=strict
```

**First-divergence debugging.** A replay journey labels the expected output of each layer it cares about. The comparator walks the layers in order and reports the first mismatch:

```
interpretation ✓ → checked ✓ → resolver ✗ (r1 ordinal=2 → expected #2, got ambiguous: set changed)
```

Only that layer is blamed; the ones after it are expected to be wrong. „De ce a recomandat produse pentru corp aici?” then becomes: open the turn, read the first ✗.

## Implementation order

One component per PR, and no step starts before the previous one is merged with its tests green. That way a failure can be blamed on one layer. Step 0 runs in parallel because it fixes today's production path.

| Step | Builds | Done when | Invariants proven |
| --- | --- | --- | --- |
| 0 (parallel) | Link/compare shortcuts resolve named targets; `aside` keeps `active_search`; field-level tool errors | B1/B2 regressions fail on `main`, pass after | — (current path) |
| 0.5 | State writer inventory: every place that writes needs, topic/category, `selected_product`, `active_search`, references or clarification state, on v1 and v2. A matrix of writer · field · why · fate (stays as executor output / becomes a proposal / retired) | Matrix reviewed, derived mechanically (AST over assignments and proposal constructors), not by grep | Prerequisite for I3, I20 |
| 1 | Contract scaffolding: models, schema snapshot, `KernelTrace`, AST gates, 4 fixture packs, replay harness, **I16 differential harness** | Gates and the differential harness run in CI; OFF = `main` on every surface | I1 (schema), I13, I14 (gates armed), I16 |
| 2 | Reference resolver v2 (pure); also plugged into the step 0 shortcuts | Resolver suite green on 5 packs | I1 (property), I10, I24 |
| 3 | Provenance checker, delta mapper, reducer extensions (park/resume/aside, `user_implicit`, facet scope, budget conversation-scoped, correction rule), legacy writers moved per the 0.5 matrix | Property tests green | I3–I9, I17, I19–I23 |
| 4 | Ambiguity gate, planner, answer policy, pack clarification templates; `SearchArgs.rank_terms` (additive, `ORDER BY` only) | `SearchArgs` golden snapshots; gate tables; SQL snapshot | I2, I11, I12, I25 |
| 5 | Interpretation adapter (`turn_interpreter`): prompt, per-tenant schema, `complete_schema` call, validation into `CheckedChange` | Provider accepts the schema (one smoke call, run by Adi); offline agreement report on real SOLE turns | I18, I22 |
| — | Production: `CONVERSATION_STATE_V2_ENABLED` shadow, then v2 write | Shadow diff read and explained | — |
| 6 | Full stage behind `INTERPRETED_TURN_ENABLED` (OFF), wired to existing executors and compose, trace written | `ScriptedLLM` journeys × 5 packs green; differential harness still equal with OFF | I13 (replay), I15a, I15b, I20 |
| 7 | Dark replay on the full corpus (one run, started by Adi) → canary via NX-249 → production | Section H metrics hold on every family | all |

What each step must not do: steps 1–4 call no model and touch no live path, step 5 writes no state, and step 6 changes nothing while the flag is off.

From here on, a failing conversation goes into the replay corpus and gets fixed in the one layer its trace blames. Only a change to a rule bumps the contract version.
