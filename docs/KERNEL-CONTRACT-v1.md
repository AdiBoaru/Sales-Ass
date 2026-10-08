# Conversation kernel contract v1.0

> Sursa: tabul „Contract v1.0” din documentul „Aria conversation kernel — technical design” (claude.ai/code/artifact/d32bcfd1-104d-4b00-9800-ecaba4e67fcd), exportat pe 2026-09-25. De aici încolo sursa de adevăr e ACEST fișier: orice schimbare trece prin PR, cu regula minor/major din „Status and scope”, iar documentul online nu mai e normativ.

Sep 25, 2026 · @Dan

## Status and scope

This tab is normative: where it and the design tab disagree, this tab wins, and code that contradicts it is a defect. It governs every component between the user's message and the existing tools: interpretation, reference resolution, delta, reducer, ambiguity gate, planner. Tools, composition, validators and the web contract are out of scope and unchanged.

Versioning: the contract is `kernel.v1.0`. **Minor** (`v1.1`): additive schema fields, new enum values, new optional metadata. **Major** (`v2.0`): a change in the meaning of a field, an invariant, an ownership row or state semantics. Every bump updates the tests that encode it in the same PR, and major bumps need the replay gate. The version is stamped on every `turn_interpretation` event, so replays can be split by contract version.

**Status: `kernel.v1.0` frozen after review round 4 (2026-09-25).** From here, changes follow the minor/major rule; architecture changes are out of scope. Future defects go through the replay corpus and are fixed in pack data, a reducer rule or the one component the trace blames.

**`kernel.v1.1` (minor, NX-333, 2026-09-27).** Additive changes only, none of them to the model-written schema (`TurnInterpretation` is byte-identical to v1.0; the schema snapshot differs only in its version stamp): `SearchArgs.prefer`, a second planner-only field next to `rank_terms` (see „Bounds on `unmapped`"); two planner rows that the v1.0 table was missing (see „Primary act → executor"); and two tightenings found in review, both stated where they apply: the delegated loop has no mutation either, and the planned search keeps the state's hard budget instead of re-judging it on the recent text. The delta now writes `unmapped` + `avoid` as an exclusion, which is what „Operations” already required (`avoid` creates an exclusion need), so it is a defect fix, not a rule change. The meaning of every existing field, invariant, ownership row and state rule is unchanged, so no replay gate is required.

**`kernel.v1.2` (minor, NX-336 PR A, 2026-09-28).** Additive fields and wording only; `TurnInterpretation` is again byte-identical (the schema snapshot differs only in its version stamp):

- `KernelTrace` gains additive fields, all with defaults, so every earlier constructor stays valid: `plans`, `gaps`, `disclosures`, `dropped_acts`, `delta_counters`, `gate_memory`, `truncated` (see „Canonical turn trace"). `plan` and `executor` keep their meaning (the primary act's plan). `LAYERS` does not grow.
- The trace is **redacted** with the NX-230 boundary and **capped** at 16 KB before it is stored, with a declared truncation order. This corrects the wording „bounded like the rest of `diagnostics`": `diagnostics` has no bound and is stored verbatim.
- A new module role, `orchestrator` (see „Enforcement").
- Design §D is corrected to the as-built stage (a branch in `agent_stage`, exact shortcuts before the interpretation, the planned search through `ToolRun.execute_planned`).

No invariant, ownership row or state rule changes in v1.2, so no replay gate is required. The open questions step 6 raised on I5 (the safety prune on `aside`), I20 (the cart on `cart_ref`) and I15a/I12 (`grounding_guard` does not run on the v1 composition) are decided in the PRs that need them (B and C), under the minor/major rule.

**Current version: `kernel.v8.1` (minor, NX-380, 2026-10-08).** Two additive fields in the
model-written schema. `Act.question` is the customer's question on a `detail` or `compare` act, in
their words («does it have SPF?»): on the wide set of 2026-10-07, 72 `detail` turns were answered
with the fixed product sheet because the question had nowhere to go. It has no consumer yet: the
detail executor keeps serving the sheet until its own card reads the field, so this bump changes no
behaviour downstream. `Reference.dimension` also accepts `rating` (`REFERENCE_ONLY_DIMENSIONS`): the
resolver, the planner's sort and the answer policy already order by it as a product column, but the
enum did not let the model write «the best rated». `StateChange.dimension` is unchanged (a rating is
not a requirement that persists). The interpretation prompt moves to `interpret.v5` in the same PR
(the rules come from the 398 non-ok turns of that set; the item-type menu is no longer capped at 20;
the pack may add `interpret_notes`, shown as `STORE NOTES`). No meaning, invariant, ownership row or
state rule changes, so no replay gate is required; the prompt itself is judged on an unseen set
(NX-380).

**`kernel.v8.0` (MAJOR, NX-387, 2026-10-08).** The omissions of the same audit:
(1) validator: a word that resolves on ANOTHER dimension no longer contradicts a value whose HEAD
word (first content word of its key or label, modulo the locale's inflection) the quote carries:
«un luciu» is also a finish but is the head of „luciu de buze"; a name's tail is not enough («ten»
of „fond de ten", NX-350), and «lemn» for the colour „negru" stays a `semantic_mismatch`; the NX-330
rule (a shelf never contradicts a facet value) and the same-dimension rule are unchanged; (2) reducer: on a LIST key whose tombstones are all the customer's VALUE retractions (with a
fingerprint), a retraction is of the value, not the key, so a new value on a key the customer
emptied is not a revival (I6 unchanged in text); code tombstones (`topic_reset`, `superseded`), a
retraction without fingerprint and scalar keys keep the key-level block; (3) `TurnPlan.steps` (additive) and `RoutineArgs.steps`: the turn's types mapped through
`routine_steps.by_product_type`, at least two of the routine's family, scope the planned routine;
(4) a read with no target (`no_target`, e.g. an ordinal with no list on screen) is answered with the
pack sentence instead of the v1 loop; (5) a fallback after the chain keeps the redacted chain trace
under `kernel_fallback.chain`. The replay gate is waived as before.

**`kernel.v7.1` (minor, NX-386, 2026-10-08).** Additive fields and executor rows
from the same set: (1) `TurnPlan.then` + `TurnPlan.names`: a `detail`/`link`/`compare` (or a `find`
naming a product) whose name the resolver did not find written whole is planned as today's name
search, carrying the asked act and every missing name; the executor searches each name and matches
the results as a PHRASE (`references.name_in_results`: the product's whole name in the request, or
the whole request in the name; no "all content words" and no "word owned by one item" step, which a
set fetched by those words cannot tell apart); every name on 1 to 3 products (one for a `find`) ⇒
the act is served on them next to the other targets, without `not_exact_match`, the found products
judged by the answer policy as partners (I12); promoted only when every target is usable or a name
not found; otherwise today's search with its disclosure (the resolution searches leave no session
and no events);
(2) a `compare` on a single target compares the rest of the set the target sits in (screen, then
earlier sets, at most two), else a graph partner only if it is a curated substitute or the same
type, else the target's detail (the graph partner was a towel); (3) `SearchArgs.price_min`
(planner-only) from a hard `budget_min`, a typed `gte` price constraint on the planned path (a soft
one stays the `price_min` gap); (4) `routine_steps.family_by_need` (pack data derived from the
catalog): a `bundle` without subject takes the family of its active needs when they all agree.

**`kernel.v7.0` (MAJOR, NX-384, 2026-10-08).** Three rules that discarded what the
interpretation understood (set `wide-2026-10-07`, turns labelled "interpretation right, code wrong"):
(1) a product KIND said in the turn that the vocabulary lacks (moved to `unmapped` by the
validator) heads the search text like a named type, before spoken facet filters; next to a subject
type already in state it is only added to that type's label; (2) on the planned path, a search text
made only of the shelf's own name (key, label, path) is no evidence against the shelf, so the
NX-313 guard keeps it (it judged «ochi» by eye-care matches and dropped Makeup > Eyes), unless the
shelf is an NX-319 homograph (a sub-shelf whose root the customer never named); (3) `avoid` on the
shelf or the type is an exclusion (`restriction`, a filter only when the catalog carries the exact
value and the customer said it), never the subject («nu vreau creme» became the cream umbrella).
The replay gate is waived as for v2.0 to v6.0. The model-written schema is unchanged.

**`kernel.v6.2` (minor, NX-375, 2026-10-01).** One planner row, additive: a
`find` act that names a product. The name reference is chosen by one pure function,
`references.find_name_reference`, shared by the planner and the gate: a `name` target of the `find`
act, or a `name` reference no other act targets and no change anchors (`relative_to`). It is not a
product name when the resolver reclassified it (a name that denotes a property, I24) or found it
`stale`; it is not the request on a turn whose ACCEPTED changes carry a price bound without a
number (the name is then the anchor of «cheaper than X», the customer wants something else), nor
when two different names are unused («X or Y?»). The plan follows the resolver: an `exact` name
is served as the product (`detail` on the re-read id, as the `detail` row on an exact target, I1);
an `ambiguous` name with at most three candidates answers about all of them (`detail` with ≥ 2
candidates, as `act_both` on a read); otherwise (`not_found`, or more candidates) the search runs
on that NAME (`SearchArgs.product_name` and the query text), exactly like `detail`/`compare` on a
name not found. The name search carries only what THIS turn says (needs written now from an
accepted change, a shelf or a type named now, the turn's ranking signals): the old subject's shelf,
budget, needs, exclusions and type would hide the product asked for by name, and are dropped with
the gap `name_unscoped`, never silently. A name alone is a subject for the gate: a `find` without
query words or a subject that names a product is not asked for a shelf. The rule this follows was
already the contract's: the resolver finds only a distinctive name written in full, and approximate
name search is the planner's. Declared: a description the model labels `name` («telefon rezistent
pentru santier») is searched as a name, because no structural signal available to the planner
separates it from a partially written product name («ANUA Heartleaf 77 toner»); see NX-375. Found by
the production run `kernel-live-2026-10-01` (k6 T1, «aveți ANUA Heartleaf 77 toner?»: the reference
was declared, `find.targets` was empty, the search ran on „toner” and the customer was told the
product does not exist; it is in stock). `TurnInterpretation` is unchanged; the schema snapshot
differs only in its version stamp. Additive only (a planner row, a gap code); no meaning, invariant,
ownership row or state rule changes, so no replay gate is required.

**Previous version: `kernel.v6.1` (minor, NX-374, 2026-10-01).** One additive value in a closed
vocabulary the planner writes: the disclosure `need_unverifiable`. A need the customer SPOKE (source
`user_explicit`, which the delta writes only from an accepted `explicit` change) that falls into the
gap `unsupported_need` is told to the customer once per conversation, with the pack sentence
`kernel_sentences.need_unverifiable`, before the answer, on the FIRST plan that searches with it: a
requirement spoken on a turn that did not search (an aside, a gate question, a search with no words)
is told on the next search that carries it. The gap `unsupported_need` is written only where
`SearchArgs` has no field that can carry the need (a non-text value such as a yes/no facet, since
no boolean filter exists in SQL; a facet that is not a product attribute; a key without a facet),
so "I can't check it" is true by construction, not a reading of the data. The memory is the
conversation's anti-loop memory: the orchestrator proposes `note_asked` with the key
`unverifiable:<need key>` (in the commit's fixed second pass, like the gate's question memory) only
when the sentence is actually in the reply, so a plan that did not serve leaves it to be told later;
the prefix keeps it apart from the gate's question keys. Gaps still never become text; this is a
disclosure, like `not_exact_match`. A described (`user_implicit`) or an `inferred` need stays a gap
only, and a search the planner abandons (`no_query`) drops the disclosure together with its gaps.
Found by the production run `kernel-live-2026-10-01` (k2 T3, «să fie și fără parfum»).
`TurnInterpretation` is unchanged; the schema snapshot differs only in its version stamp. Additive
only (a disclosure code, `PlannedTurn.disclosed_needs`, a new key namespace in `asked_questions`
that no existing reader interprets); no meaning, invariant, ownership row or state rule changes, so
no replay gate is required.

**Previous version: `kernel.v6.0` (MAJOR, NX-364, asked for by Adi on 2026-09-30).**

**Why.** Four real conversations on 2026-09-30 (`sole-ro`) hit four rules of v5.1 that the NX-363
defect detectors now count on all traffic. Each change keeps the kernel's split: the model proposes,
code decides on structure.

- **An ordinal after a detail counts the list the customer zoomed into.** When the screen holds one
  product and that product is a member of the most recent earlier set of at least two, an ordinal
  resolves on that set (`source: shown_earlier`, reason `ordinal_in_zoomed_list`). „The most
  recent earlier set of at least two”, not the immediately previous one: two details in a row leave
  a one-product set on top of the list. The rule is one pure function (`references.zoomed_list`);
  when the kernel serves, the exact v1 shortcuts step aside on an ordinal over such a screen
  (trace key `shortcut_deferred_to_kernel`), so link, detail and reviews follow it too. On v5.1, «compară
  prima cu a treia» after «spune-mi mai multe despre a doua» came out `ordinal_out_of_range`, and
  «prima» was the product of the detail. The criterion is membership, not text: a single product
  from a new search is not in the earlier set, so the ordinal stays on screen. Not on `resume`
  (the focus is the parked set). Only the kernel's sources zoom (`ReferenceSources.zoom_ordinals`,
  set by `sources_from_state`); the v1 shortcuts build their own sources and keep the ordinal on
  screen (I16). A mutation (cart) whose target was counted on the list and differs from the product
  on screen is `must_ask` with both candidates (I10): on a detail screen «adaugă-l pe primul» may
  mean the card or the first of the list.
- **Provenance confirms an inflected name of the proposed value.** Step 2 also accepts the quote
  when it spells, word by word and in order, one of the proposed value's known names (its key and
  the overlay phrases that map to it), with the locale's inflection table (the rule the NX-350
  umbrella already uses, stem of at least three letters), provided at least one of the name's words
  is spelled exactly, and a three-letter stem only takes a suffix of two letters or more
  («tenul» is „ten” plus the article, «pare» in «mi se pare» is not „par”): «am tenul uscat» is
  `explicit` for `skin_type=dry`. Inflection only confirms,
  it never contradicts: a short stem also matches verbs («mi se pare uscată» spells „par uscat”),
  so an inflected contradiction could drop a need the customer stated. Nothing else moves: a
  description still names no value and stays `implicit`. Measured on the labelled set A before the change: `implicit` facet changes are
  right 46% of the time against 82% for `explicit`, so the vocabulary check stays; the offline
  rerun of the validator on the 127 stored interpretations moved exactly two changes. A value the
  pack has no phrase for («par gras» → `oily`) is a pack-data gap, not a code one.
- **A spoken exclusion on a catalog value is a filter.** `SearchArgs.exclude` (planner-only, like
  `rank_terms`/`prefer`): an exclusion need from `user_explicit`, on an attribute facet, whose value
  the catalog carries exactly, removes the products carrying it after fusion (the NX-322b anti-fit
  net). On the universal `restriction` key the facet is found back through the vocabulary and must
  be unique. On a list facet with an open vocabulary (no declared values: the ingredients) a
  product goes when one of its values CONTAINS the excluded phrase word by word (the article form of a word counts): «acid hialuronic» appears in 51 forms on
  the SOLE catalog («complex de 8 tipuri de acid hialuronic»), and an exact match caught 590 of 726;
  for an exclusion, removing too much is the safe error. A chemical synonym («hialuronat de sodiu»)
  is not caught (pack data). On an enum or a text facet the match stays exact: `am_pm` is not
  `am`, «gel crema» is not «gel». On a `partitioning` facet (who the product is for) only the products
  marked for the
  excluded values alone go: «nu pentru ten gras» keeps a cream declared for every skin type. A
  product without the attribute stays (UNKNOWN ≠ MISMATCH, D7). The NX-303 pool tail goes through
  the same exclusion. A routine (`bundle`) cannot exclude yet and discloses the `exclusion` gap.
  Anything else stays the `exclusion` gap, as before. When the turn has no words to search and no
  subject («nu vreau cu acid hialuronic» after «ceva de hidratare»), the search text is the locale
  label of the first need filter carried from the state (the pack's `value_labels`), not `no_query`:
  on v5.1 such a turn never searched, so the exclusion never ran. On the real turn «nu vreau cu acid hialuronic», all four products
  v1 showed carried the ingredient.
- **A price limit without a number is a band.** `lte`/`gte` on price with no number and no
  `relative_to` no longer falls to `unmapped` (which made the words a rank term, «ft scump»). It is
  checked as `price` with the value `band:low` / `band:high`, strength `ranking` (turn-local, I23:
  a vague wish is not a budget). The planner turns `band:low` into `SearchArgs.price_band = "low"`
  (planner-only): the tool keeps the products priced at most the median of the pool that matches the
  request, after fusion and exclusions, in relevance order; the pool tail is capped at the same
  median. Not on a search for a named product. A value with digits is a sum written without
  `number`, not a band. `band:high`, and a band on a routine, stay the `soft_budget` gap. The replay
  scorer counts a band like an `inferred` change (neutral), since it is never persisted.

All three new `SearchArgs` fields are empty by default and enter the session fingerprint only when
set, so the v1 path is byte-identical (I16). The replay gate is waived, as for v2.0 to v5.0: no
interpreted turn has been served in production. The schema the model writes is unchanged.

**`kernel.v5.1` (MINOR, NX-349).**

**What v5.1 fixes.** On `kernel.v5.0` a boolean facet of the pack (`value_type: bool`, for example `fragrance_free`) never survived validation. The catalog vocabulary does not index booleans, so `set fragrance_free true` became `unmapped` with the value "true", and the planner sent that value to `rank_terms`.

**How a boolean change is validated now:**
- **Accepted values.** The validator accepts `true`/`false` on a declared boolean facet without a vocabulary entry.
- **How the quote is judged.** It is judged on the pack phrases that name the facet: the locale's label names the true state, and an alias names the state in its value.
  - The quote contains one of those phrases with the same state ⇒ `explicit`.
  - The phrase names the opposite state ⇒ `semantic_mismatch`.
  - A negation right before the phrase, or a relation other than `eq`/`contains` ⇒ `polarity_conflict`.
  - No phrase in the quote ⇒ `implicit`.
- **Strength.** `hard` needs `enforce_ready`, as for any facet (I8).
- **State and plan.** The need reaches the state as a boolean. The search has no boolean filter, and the SOLE catalog holds no boolean attribute, so the planner discloses `unsupported_need`.
- **Search text.** The words of a boolean facet's quote never become search text: neither in the planner's composed text (`matched` stays empty) nor in the whole-request fallback. Those words are often a negation, and «fără parfum» would otherwise search for «parfum».

The schema the model writes is unchanged.

**`kernel.v5.0` (MAJOR, NX-352, decided by Adi on 2026-09-29).** Three planner and
delta rules change after the real-catalog probe (NX-351), which served fewer products of the right kind
and fewer carrying the stated needs than v1:
- **A facet need the customer stated is a relaxable filter.** An `explicit` facet need goes into
  `concerns`/`features` even when the facet is not `enforce_ready`; the search's relaxation ladder drops
  it when nothing comes back, as on v1. A need that is only described (`implicit`) stays a ranking
  preference. Price and brand still filter only from a hard need (the brand filter is never relaxed).
  I7 is restated accordingly. A routine (`bundle`) does not relax its step filters, so there a stated
  but not `enforce_ready` need stays a preference. On the planned search these facet filters count as
  uttered for the relaxation ladder and the guessed-filter guard (NX-313): the kernel already judged
  their provenance. The shelf does not: the state may hold an `implicit` shelf or one written by a v1
  turn from the model's guess, so it stays judged (relaxed before a stated need, NX-313 and the
  homograph guard apply).
- **The search text is COMPOSED from what the kernel validated.** `SearchArgs.query` is, in order:
  the type (the words that named it this turn, `CheckedChange.matched` of an `explicit` type, written
  by the validator and redacted in the trace; else the type's label from the state, the umbrella's head
  for a vague type); else the customer's word for the shelf (the words that named it, or, for an
  `implicit` shelf, the quote's words that look like the shelf's name, «telefon» for „Telefoane", or a
  short quote of at most three content words, «cremă de față»; a long quote is a description and never
  text); else the words that named a facet filter; else this turn's unmapped values; else the shelf's
  label (a shelf name does not occur in product names, NX-293); each with the words of a stated value
  that reaches no filter; the whole request without the negated words is the last resort. Words come
  in the request's order, in the customer's spelling. Only changes accepted by the validator and the
  delta count (`plan_turn(checked=…)`); an `avoid` change never yields text. A first design SUBTRACTED
  from the sentence (the locale's formula, the need words) and failed two adversarial reviews: every
  unforeseen word stayed a gate. On the `strict` rung every word is a gate, so the whole sentence („si
  ceva mai ieftin ?") returned nothing (64 of 81 probed plans searched the customer's sentence).
- **A relative price on an ambiguous target is the median.** „Ceva mai ieftin” with several products on
  screen is cheaper than most of them: the bound is the median of the available candidates' re-read
  prices (counted `relative_price_median`), only for a reference pointing at the screen (deictic,
  attribute) that is not also the target of an act the gate may ask about (reads, mutations; not
  `find`, `show_more`, `bundle`); fewer than two known prices ⇒ rejected, as before.
- **A shelf follows a new kind only on a verified pair.** When a stated type changes, or a vague type
  of another kind replaces the subject, the old shelf stays only if the turn names it or the pair
  (shelf, type) exists in the catalog (`mark_pairs`, which now also checks an umbrella by its first
  code present on the shelf): the NX-348 fill rule, extended.

The replay gate is waived, as for v2.0 to v4.0: no interpreted turn has been served in production. The
model-written schema is unchanged.

**Previous version: `kernel.v4.0` (MAJOR, NX-350, decided by Adi on 2026-09-29).** The product type
of the SUBJECT changes only from a type the customer stated (`explicit`). An `implicit` type (the
customer's words do not name the whole code: „cremă" → `crema de fata`, which could as well be a body
or hand cream) is remembered as the customer's UMBRELLA instead: the validator takes the quote word that
is the HEAD of the assumed code (its first content word, with the locale's inflection suffixes; a word
from a code's tail, like „ten" in „fond de ten", never opens or narrows an umbrella) and returns the
assumed code first plus every other code with that head (`CheckedChange.umbrella`, at most 8, largest
first; only the assumed code when the quote has no head word or spells the whole code in a row).
The delta puts them on the subject proposal (`set_topic.type_umbrella`, counted
`subject_type_umbrella`; several vague types merge rank by rank), and the subject keeps them
(`Topic.type_umbrella`). The reducer is the only writer, with one rule in both directions: what the
customer says now is ANOTHER KIND of item when it does not overlap what they said (a stated type outside
the umbrella, an umbrella without the stated type, or disjoint umbrellas), and that is a subject change
(the old subject is parked; the shelf stays, as when a stated type changes in v3.0). Otherwise an
overlapping umbrella replaces the old one without parking and a stated type from the umbrella refines
it. An umbrella-only subject parks, resumes, clears and is contradicted (I21) like any other subject.
The planner prefers every code of the umbrella (a code-inferred type does not beat it) and labels the
subject with the head of its first code; the model's view shows it. An
umbrella never filters. The
replay gate is waived, as for v2.0 and v3.0: no interpreted turn has been served in production. The
model-written schema is unchanged.

**Previous version: `kernel.v3.0` (MAJOR, NX-348, decided by Adi on 2026-09-29).** The meaning of a
subject change: the subject `(category_key, product_type)` changes only when a half that is already
set gets a DIFFERENT value. Filling an empty half (the first product type on a shelf without one, the
first shelf on a subject that has only a type) is a REFINEMENT: the subject's needs stay and nothing
is parked, but only when the resulting pair exists in the catalog; otherwise (or without data) it is
a subject change and the other half is dropped. A type the code inferred from the shown set counts as
an empty half. Topic-scoped needs of a shelfless subject leave (parked) with it. A subject change TO
the parked subject is a swap (the `resume` semantics), not a park plus eviction, so its parked needs
come back. A product type the customer names now reaches `Topic.product_type` through one
`set_topic` proposal for the whole pair per turn (before, it was proposed as a need and always
rejected as `topic_key`). The replay gate is waived, as for v2.0: no interpreted turn has been served
in production. The model-written schema is unchanged.

**Previous version: `kernel.v2.1` (minor, NX-336 D3).** One additive, planner-written field:
`TurnPlan.family`, the routine family of a `bundle` plan, read from the state's subject and the
pack's `routine_steps.family_by_shelf` (shelf root or shelf key → family). No invariant, ownership
row or state rule changes; the model-written schema is unchanged (its snapshot differs only in the
version stamp). Without a declared family a `bundle` plan keeps `family = None` and stays on
today's path.

**`kernel.v2.0` (MAJOR, NX-336 PR B, decided by Adi on 2026-09-28).** What changes is the **meaning of I5**, and with it the `thread` ownership row and the `aside` row of „Thread": `thread=aside` is the identity on the CONVERSATION state (needs, topic, the references written by executors, `active_search`), while the safety prune (NX-173) and the gate's question memory still apply on `aside`. The prune is expressed as a **removal** of the blocked products from the displayed set, the earlier sets (`recent_sets`) and the parked set, applied last in the turn's commit, and the turn's revision grows once only when something was removed (an ordinal emitted on the old list then resolves `stale`). Why: (1) **safety parity with v1**: on today's path the prune runs on every turn where a safety context becomes active, so a customer who declares a pregnancy inside an aside would otherwise keep a contraindicated product on the screen and in the parked set, reachable by an ordinal; (2) **no repeated question**: a question the gate asked (or a confirmation it noted) on an aside must be remembered, or the anti-loop rule (I11) would ask it again. The model-written schema (`TurnInterpretation`) is unchanged; the snapshot differs only in its version stamp. **The replay gate is waived explicitly for this bump**, by Adi's decision: no interpreted turn has ever been served in production (`INTERPRETED_TURN_ENABLED` has never been on), so there is no v1.x behaviour to replay against; step 7's replay is the first one and runs on v2.0. Tests that encode the new I5: `tests/test_interpreted_turn_b.py` and `tests/test_interpreted_turn_b_review.py` (the commit of an `aside` keeps needs, topic, `active_search` and executor references, applies the removal and the memory); the reducer's own `aside` identity (`reduce_turn`) is unchanged and keeps its property test.

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
| Conversation navigation (`thread`) | proposes | applies | `aside` can never change the conversation state (v2.0: the safety prune and the gate's question memory still apply, see I5) |
| The user's wording (`quote`, `query`) | supplies | verifies against the user's text | A quote not found in the user's messages is not user evidence |
| Dimension and value of a change | proposes | owns membership and canonical value | Must exist in the tenant vocabulary; otherwise `unmapped`, then re-resolved once by code |
| Category key | proposes from a closed menu | owns | Menu membership + the NX-313 contradiction check |
| Relation and polarity (`avoid`, `lte`, `gte`) | proposes | verifies marker | No marker → downgraded to `implicit` |
| Numbers and units | proposes | owns | Unit must belong to the dimension (`UnitRegistry`) |
| Provenance level | — | owns | From quote + vocabulary; the model has no field for it |
| Hard vs soft | supplies evidence only | owns | From provenance + hard capability |
| Product mention as text | supplies | — | — |
| Reference semantics (kind, ordinal, name, dimension, value, direction) | proposes | validates, may reclassify or reject | e.g. a `name` matching no product is `not_found`; an ordinal beyond the set is `ambiguous`; (v6.0) on a one-product screen whose product belongs to the latest earlier set of ≥ 2, an ordinal counts that set |
| Where a referenced object lives | — | owns (`ResolvedRef.source`) | The model has no field for it |
| Product and variant ids | never | owns | Every candidate id, including parked and recent, is revalidated against the catalog |
| Price, stock, product URL, product attributes | never | owns (catalog) | Re-read at resolution time |
| Value of a relative change („mai ieftin decât”) | direction only | computes the number | From the resolved product's current price; (v5.0) on an ambiguous target, the median of the candidates' re-read prices |
| State mutation | proposes (delta) | applies (reducer) | Single writer: `state_reducer` |
| State persistence and budget | — | owns | `serialize`, 6 KB; `inferred` never persisted |
| Ambiguity candidates | proposes | owns final verdict | Ambiguity gate |
| Clarification question text | — | owns | Pack template + vocabulary labels, deterministic interpolation; no model text |
| `SearchArgs` | never | owns (planner) | The only producer of `SearchArgs` on the interpreted path. The planner-only fields (`rank_terms`, `prefer`) are stripped from the model's tool arguments before validation and counted (`planner_field_from_model`) |
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
| I5 | `thread=aside` is the identity on the CONVERSATION state (needs, topic, executor-written references, `active_search`); the safety prune (NX-173, as a removal of blocked products from displayed/parked/recent sets) and the gate's question memory still apply on `aside` (v2.0) | Property test (`reduce_turn`) + commit tests (`tests/test_interpreted_turn_b*.py`) |
| I6 | A revoked need is revived only by `explicit` evidence | Reducer unit test (exists as `revoked_key`) + property test |
| I7 | Only `explicit` user evidence can produce a hard filter: explicit + hard-capable → hard; explicit + not hard-capable → soft; implicit → soft; inferred → ranking only, this turn. (v5.0) An explicit facet need, hard or soft, is a relaxable facet filter (`concerns`/`features`); price and brand filter only from a hard need | Property test on the delta mapper and the planner: no `implicit`/`inferred` change ever reaches a `WHERE`; `price_max`/`brand` only from hard needs |
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
KERNEL_CONTRACT_VERSION = "kernel.v1.2"   # v1.1 (NX-333): planner-side additions; v1.2 (NX-336): trace + clarifications

ActKind = Literal["find", "show_more", "compare", "detail", "link", "cart",
                  "bundle", "order_status", "store_info", "chitchat", "other"]

class Act(BaseModel):
    kind: ActKind
    targets: list[str]            # Reference ids declared in this interpretation (I22)
    query: str | None             # the user's words, for find / store_info
    question: str | None          # (v6.3) the customer's question, for detail / compare

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
    dimension: str | None         # proposal; code resolves membership (v6.3: also "rating")
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
    family: str | None = None          # kernel.v2.1: routine family of a `bundle` plan (planner-written)

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
   - (v6.0) the quote spells, word by word and with the locale's inflection table (at least one
     word exact), one of the proposed value's known names → `explicit`. Inflection only confirms.

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
| implicit | `user_implicit` (new) | soft (`prefer`); a confirmation candidate for the gate. (v4.0) On `product_type`, the umbrella of the customer's words on the subject, never the subject's type | no | no |
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
| Effect on search | rank-only: passed as `SearchArgs.rank_terms`, used in `ORDER BY` as a secondary key after the text rank (`ts_rank_cd`), never in the lexical predicate; weight capped below any facet match |

The contract requires two additive changes in the search tool, both written only by the planner and both empty by default (empty ⇒ the SQL, the fusion and the session fingerprint are byte-identical to v1.0):

- `SearchArgs.rank_terms` (v1.0). Today the only textual input is `query`, and on the `strict` rung the query is a gate: its terms are joined with AND. Passing unmapped words through `query` would turn „piele obosită după avion” into a hidden filter, which is the NX-298 lesson.
- `SearchArgs.prefer` (v1.1, NX-333): the `soft` facet needs (v5.0: only the described, `implicit` ones; a stated need is a relaxable filter), the subject's product type (v4.0: or every code of its umbrella when no type was stated) and this turn's `inferred` facet signals, as attribute key → catalog values, merged in the tool with the NX-322 need-menu preference and passed to the existing fusion (`need_preference`). No new SQL. Without it, a `soft` need had no channel into the search, and since no facet is `enforce_ready` on today's data, every facet need the user stated would have vanished from the interpreted search, which is worse than today's path. Only dimensions that are **attribute** facets enter `prefer`: fusion reads the preference from `attributes`, so a column-backed dimension (the brand, on the real catalog) would order nothing and dilute the others; it goes into `gaps` instead.

„Weight capped below any facet match” is measured, not assumed (`tests/test_kernel_planner.py`): with today's fusion weights and the real fusion pool (50), a facet preference always beats the one-position lift a rank term gives between two adjacent products. The declared limit: a rank term is a secondary key, so inside a large tie group of the text rank (the `filters_only` rung, where hundreds of products rank zero) it can lift a product by several positions at once, and one preferred dimension (0.25) undoes a lift of at most 7 positions from the top of the pool; with several preferred dimensions the preference is their mean, so the bound is lower. Moving `rank_terms` into a fusion signal would lift that limit and is a contract change on measurement, not on principle.

What the planner cannot express in `SearchArgs` (`budget_min`, an exclusion that is not spoken or has no unique catalog value, numeric facet bounds, a `soft` budget, a variant, a preference on a non-attribute dimension) goes into the plan's `gaps` (closed vocabulary), never into a hidden filter. (v6.0) A spoken exclusion on a catalog value is the declared filter `SearchArgs.exclude`, and a price limit without a number the declared band `SearchArgs.price_band`. An `avoid` on an `unmapped` word is an exclusion (the delta writes it on the universal `restriction` key, `soft`), never a rank term: a rank term can only lift the products that carry the word.

On the planned path the tool does not re-judge `price_max` on the recent text (the NX-319 guard judges the **model's** bound on today's path): the planner's `price_max` comes only from a hard `budget_max` of the reduced state, whose provenance the kernel already checked on the user's quote, and the budget is conversation-scoped, so its number can be far outside the text window. It is counted as `price_bound_provenance{source: state}`.

## Reducer, thread and parking

The subject of a conversation is `(category_key, product_type)`. Only a change of subject parks anything; a change of any other dimension is an ordinary constraint change. (v3.0) A change of subject means a half that is already set gets a different value; filling an empty half is a refinement and parks nothing. Several values for one half in the same turn keep the last one, counted as `subject_multiple`. (v4.0) The type half is set only by a stated (`explicit`) type; a vague one sets the subject's umbrella (`Topic.type_umbrella`), which counts as a subject for the gate and the planner. What the customer says now is another kind of item when it does not overlap what they said (a stated type outside the umbrella, an umbrella without the stated type, disjoint umbrellas): that is a subject change, and the shelf stays, as when a stated type changes. An overlapping umbrella replaces the old one without parking; a stated type from the umbrella refines it.

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
| `aside` | No change to the conversation state: needs, topic, executor-written references, `active_search`. The safety prune (removal of blocked products) and the gate's question memory still apply (I5, v2.0) | An `aside` carrying changes is treated as `continue` and counted (`aside_with_changes`) |
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
| `find` | (v6.2) names a product: a `name` target, or a `name` reference nothing else uses (`references.find_name_reference`) | `exact` ⇒ `detail` on the re-read id; `ambiguous` with ≤ 3 candidates ⇒ `detail` on all (as `act_both`); otherwise `search` on the name (`product_name` + query) with only this turn's filters, the dropped old ones as the gap `name_unscoped`. Takes precedence over the other `find` rows, and the gate does not ask for a subject |
| `find` | no subject, no query words, no facet needs | `ask`: a subject question from the pack's top shelves, gain-gated |
| `find` | subject known, no new words („Ce recomanzi?”) | `search` from state. The plan carries no query; because today's `SearchArgs.query` requires ≥ 1 character, the `SearchArgs` builder fills it with the subject's label and the `filters_only` rung serves it. This is a declared v1 workaround inside the builder, not planner logic |
| `find` | subject or query words present | `search`, `SearchArgs` derived as in design section D; (v5.0) `query` is composed from what the kernel validated (the subject's words or name, a filtered facet's words, the unmapped values), the whole request is the last resort |
| `find` | an `implicit` need with gain ≥ 0.30, not yet asked | `search` + one confirmation question as the closing line (the NX-315 `question` slot) |
| `show_more` | `active_search` present, no changes this turn | `page` |
| `show_more` | changes this turn | `search` (it is a refinement) |
| `show_more` | no `active_search`, no changes this turn (v1.1) | `search` from state, as a `find` with a known subject; no subject → `reply_only` |
| `compare` | ≥ 2 exact targets | `compare` |
| `compare` | 1 exact target | `compare` with a similar product (the existing `COMPARE_WITH_SIMILAR` path) |
| `detail`, `link` | exact target | `detail` / `link` |
| `link` | name `not_found` | `search` with the name, then disclose the result is not an exact match |
| `detail`, `compare` | name `not_found`, or a target `stale` with `not_in_catalog` (v1.1) | as `link`: `search` with the name (`product_name`), disclosed as `not_exact_match`; a stale target without a name searches the subject |
| `cart` | exact target | `cart` |
| `bundle` | subject known, and the pack declares a bundle executor for it | `bundle`: the executor named by the pack (SOLE: `routine_plan`), with the family from the subject and the budget only from an `explicit` need. No executor declared → `search` |
| `store_info` / `order_status` | — | `faq` / `order` (login wall unchanged) |
| `chitchat` | — | `reply_only` |
| `other` | — | `delegate`: the tool loop without catalog search and without mutations (v1.1: the set is derived from the schema registry minus `CATALOG_READ_TOOLS` minus the tools `tool_budget` classifies as mutations, so no model-chosen product id reaches a mutation, I1 + I10) |

The two v1.1 rows are additive planner rules: they fill cases the v1.0 table left without an executor (design §D already sends every read act on a missing name to the search), and change the meaning of no existing row. „Changes this turn” is `TurnDelta.proposals` non-empty, passed to the planner as a required boolean, never recomputed from text; a `resume` (it swaps the subject without any proposal, and the session still belongs to the subject just parked) and an `inferred` ranking signal also count as changes, so `show_more` searches instead of paging a stale pool. The reducer does not touch `active_search` on park or resume; whether it should is a step 6 decision. A disclosure travels in the plan as a closed code (`not_exact_match`, `invalid_target`, `dropped_act`, `no_target`), and composition writes the sentence (step 6).

**Acts say what, the planner says how.** An act never carries an execution hint, and `find` is a request („the user wants products”), not a strategy. A new `ActKind` is admitted only for a user request that no existing act plus a planner rule can express; a new strategy is always a planner row.

**Clarification text is pack data.** `DomainPack.clarify_templates[locale][about]` holds the sentence, with `{options}` filled by canonical labels from the vocabulary (e.g. „Te interesează {options}?”). The kernel never holds a sentence. A pack without a template for that kind falls back to the pack's generic locale template, still pack data. The keys are `reference`, `scope` and `value` (the values of `Ambiguity.about`), plus the data keys `subject` (a `find` with no subject), `confirm` (an `implicit` need), `conflict` (two crossed bounds in one turn) and `generic`, each with exactly one `{options}`; `bound_lte` and `bound_gte` label the bounds inside the `conflict` question, each with exactly one `{value}`. A template with any other marker is rejected when the pack loads.

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

Every kernel module has a **role** in `tests/kernel_modules.json`, and the role decides which gates apply: `pure`, `planner`, `adapter`, `reducer`, `executor`, and (v1.2, step 6) `orchestrator`. The orchestrator (`src/agent/interpreted_turn.py`, the branch of `agent_stage` that runs the interpreted turn) calls the adapter and the executors, so I13 does not apply to it; it gets I2 (`SearchArgs` only in the planner), I3 (state only through proposals, with one declared exception: one helper restores `state_patch` and `state_proposals` to their pre-branch value on fallback), the raw-text gates (raw text read only in declared readers) and I14. Registering it also makes the I16 differential apply to every PR that touches it or the kernel commit (`src/worker/kernel_commit.py`, step 6 PR B), while repairs of the v1 path in `agent.py` stay outside it. The I13 gate's list of model methods covers every `LLMClient` generation method (`complete_schema_raw`, `run_tool_loop_structured`, `moderate` and `describe_image` added in v1.2).

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
    plan: TurnPlan                   # the primary act's plan
    executor: str
    answer_policy: AnswerPolicy | None
    # v1.2, additive, all with defaults
    plans: list[TurnPlan] = []       # every plan of the turn (multi-act)
    gaps: list[str] = []             # planner gaps (closed vocabulary); never reply text
    disclosures: list[tuple[int, str]] = []   # (plan index, disclosure code)
    dropped_acts: int = 0
    delta_counters: dict[str, int] = {}       # delta counters + non-applied reducer outcomes
    gate_memory: dict | None = None  # the question memory the gate would write (op, key)
    truncated: bool = False          # the cap cut something
```

**Where it lives.** In `conversation_traces.diagnostics["kernel"]`. That capture already runs in production (NX-256), so there is no new table and no migration. A turn that falls back to v1 writes no `KernelTrace`, only `diagnostics["kernel_fallback"] = {reason, vocabulary_snapshot}`.

**Redaction and cap (v1.2).** `diagnostics` is stored verbatim and has no bound, so the trace carries its own. Before `model_dump`, every text field the model writes (`StateChange.quote`, `value`, `unit`, `dimension`, `target`, `relative_to`; `Reference.id`, `text`, `name`, `value`, `dimension`; `Act.query` and `targets`; `Ambiguity.readings` and `target`), `CheckedChange.canonical_value` (for `unmapped` it is the user's word) and `CheckedChange.dimension`, `ResolvedRef.ref_id` (the model's reference id, copied by the resolver), the need labels in `state_before`/`state_after` (a need's value can be the user's word), and every text field of the plans' `SearchArgs` pass through the NX-230 redactor, with the same categories as `apply_boundary`. Product ids and codes do not (a digit detector could damage an id). A test walks every string leaf of a stored trace, produced through the real stage with PII seeded into every model-written field. The stored trace is then capped at 16 KB (measured on fixtures: 0.7-2.8 KB, p50 1.2 KB), cut in this order: `product_ids` beyond 6 in `resolved_refs`; `state_before`/`state_after` down to topic + needs; the quotes in `checked_changes`; the interpretation down to acts + `thread`. If it still does not fit, only a summary remains (version, snapshot, acts without their query, plans, executor; the search arguments last), with `truncated: true`. A cut trace stays valid for `render`.

**How it reads.** `scripts/kernel_trace.py --business <slug> <turn_id>` prints it in this form:

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
| 4 | Ambiguity gate, planner, answer policy, pack clarification templates; `SearchArgs.rank_terms` (additive, `ORDER BY` only) and `SearchArgs.prefer` (additive, fusion only, v1.1) | `SearchArgs` golden snapshots; gate tables; SQL snapshot | I2, I11, I12, I25 |
| 5 | Interpretation adapter (`turn_interpreter`): prompt, per-tenant schema, `complete_schema` call, validation into `CheckedChange` | Provider accepts the schema (one smoke call, run by Adi); offline agreement report on real SOLE turns | I18, I22 |
| — | Production: `CONVERSATION_STATE_V2_ENABLED` shadow, then v2 write | Shadow diff read and explained | — |
| 6 | Full stage behind `INTERPRETED_TURN_ENABLED` (OFF), wired to existing executors and compose, trace written | `ScriptedLLM` journeys × 5 packs green; differential harness still equal with OFF | I13 (replay), I15a, I15b, I20 |
| 7 | Dark on live traffic (`INTERPRETED_TURN_DARK_ENABLED`) → canary per conversation (`INTERPRETED_TURN_CANARY_PERCENT`) → production (NX-353) | The pre-registered GO rule in `tasks/stage1/NX-353.md` §4 holds on the dark report, then the canary rule registered before the canary starts | all |

What each step must not do: steps 1–4 call no model and touch no live path, step 5 writes no state, and step 6 changes nothing while the flag is off.

**Step 7 (NX-353).** The replay and the NX-351/352 probe measured turns that were already analysed, so they are a necessary check, not the proof. Step 7 gets the proof from new traffic in three stages:

- **Dark.** `INTERPRETED_TURN_DARK_ENABLED` runs the chain on every eligible turn (the same conditions as serving). It needs the v2 state *read*, not written, so it can run next to the state shadow. It refuses the single brain.
- **What dark does not do.** It runs no exact shortcut and no executor (compose would be a second model call). On a primary `search` plan it runs only the planned search, which is read-only, and records what the kernel would have served in `ctx.trace["kernel_dark"]`: product ids, lexical step, ms, executor, plan count, plus the kernel's own state after the reducer (type or umbrella codes, shelf key, active facet needs). These are catalog keys, never customer text.
- **Time cap.** The customer waits for v1 after the branch, so in dark mode the interpretation call has its own cap (`INTERPRETED_TURN_DARK_TIMEOUT_S`, 5 s including retries). When it expires the turn falls back with `dark_timeout`. Serving is unchanged. In dark mode, `kernel_turn` also carries `mode: "dark"`, so the report never mixes dark and served populations.
- **v1 still answers.** The context is restored (`ContextSnapshot`), so the turn equals the flag-off turn on the I16 surface. The exceptions are the reads made inside the branch and the kernel's events (`kernel_turn{fallback_reason: "dark"}`, plus the new `kernel_dark{executor, searched, kernel_n, lexical_step}`).
- **Canary.** With `INTERPRETED_TURN_ENABLED` on, only conversations whose sticky bucket `sha256("nx353:{business_id}:{conversation_id}") mod 100` is below the percent are served; the rest go dark (if on) or to v1.
- **Tenants.** `INTERPRETED_TURN_TENANTS` limits both modes to a list of slugs.
- **Why not NX-249.** Its controller only assigns on `/web/v2/turns`, and production runs on `/web/chat`.
- **The report.** `scripts/kernel_canary_report.py` is read-only and applies the GO rule. The match metrics are a *proxy*: their truth is the kernel's own state, so they favour the kernel. Manual review of 20 seeded turns is the real label.

From here on, a failing conversation goes into the replay corpus and gets fixed in the one layer its trace blames. Only a change to a rule bumps the contract version.
