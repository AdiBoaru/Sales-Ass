"""NX-211 production AnswerPlan contract and deterministic validator.

The model proposes a plan, but server-owned evidence decides whether it may be rendered. The
contract deliberately stores no conversation text, profile, or raw tool payload.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.safety.external_data import contains_pii

Scalar = str | int | float | bool
ClaimType = Literal["fact", "recommendation"]
EvidenceKind = Literal["identity", "variant", "price", "stock", "url", "claim"]
ConstraintVerdict = Literal["MATCH", "MISMATCH", "UNKNOWN"]
# NX-239: obligațiile unui tur (vocabular ÎNCHIS — intră în telemetrie ca labels).
ObligationKind = Literal[
    "answer",
    "recommend",
    "compare",
    "explain",
    "action",
    "clarify",
    "safety",
    # NX-275 felia 5: o SECVENTA de pasi, nu o lista de produse. Distincta de `recommend`
    # fiindca acoperirea se verifica altfel: o rutina cu un singur produs nu e rutina.
    "routine",
]
# NX-239: taxonomia ONESTĂ de no-results (D7: „n-am găsit" ≠ „nu știu" ≠ „nu pot verifica acum").
NoResultsClass = Literal["no_match", "insufficient_data", "dependency_unavailable"]
ValidationCode = Literal[
    "action_not_successful",
    "ambiguous_product",
    "cross_tenant_evidence",
    "cross_tenant_product",
    "duplicate_selected_product",
    "fact_evidence_kind_mismatch",
    "fact_value_mismatch",
    "hard_constraint_mismatch",
    "hard_relaxation",
    "missing_claim_evidence",
    "missing_direct_answer",
    "missing_product_evidence",
    "obligation_uncovered",
    "pii_detected",
    "revoked_need_used",
    "stale_evidence",
    "tenant_mismatch",
    "unknown_action_intent",
    "unknown_evidence",
    "unknown_need",
    "unknown_product",
    "unknown_variant",
]


class SelectedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    variant_id: str | None
    evidence_ids: tuple[str, ...]


class PlanClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    claim_type: ClaimType = Field(alias="type")
    text: str
    evidence_ids: tuple[str, ...]
    need_ids: tuple[str, ...]


class FactAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    variant_id: str | None
    value: Scalar
    evidence_id: str


class PlanFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prices: tuple[FactAssertion, ...]
    stocks: tuple[FactAssertion, ...]
    urls: tuple[FactAssertion, ...]


class ConfirmedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["cart_add", "checkout_link", "subscribe_back_in_stock"]
    action_id: str
    reference_id: str | None


class AnswerPlan(BaseModel):
    """Versioned production plan emitted before response rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    business_id: str
    locale: str
    selected_products: tuple[SelectedProduct, ...]
    claims: tuple[PlanClaim, ...]
    facts: PlanFacts
    uncertainties: tuple[str, ...]
    unmet_constraints: tuple[str, ...]
    confirmed_actions: tuple[ConfirmedAction, ...]

    @model_validator(mode="after")
    def _contains_no_pii(self) -> AnswerPlan:
        text_values = [
            self.business_id,
            self.locale,
            *(item.product_id for item in self.selected_products),
            *(item.variant_id or "" for item in self.selected_products),
            *(evidence_id for item in self.selected_products for evidence_id in item.evidence_ids),
            *(claim.text for claim in self.claims),
            *(evidence_id for claim in self.claims for evidence_id in claim.evidence_ids),
            *(need_id for claim in self.claims for need_id in claim.need_ids),
            *self.uncertainties,
            *self.unmet_constraints,
            *(action.action_id for action in self.confirmed_actions),
            *(action.reference_id or "" for action in self.confirmed_actions),
        ]
        for collection in (self.facts.prices, self.facts.stocks, self.facts.urls):
            for fact in collection:
                text_values.extend(
                    [
                        fact.product_id,
                        fact.variant_id or "",
                        fact.evidence_id,
                        fact.value if isinstance(fact.value, str) else "",
                    ]
                )
        if any(value and contains_pii(value) for value in text_values):
            raise ValueError("AnswerPlan contains PII")
        return self


# ---------------------------------------------------------------------------
# NX-239 — AnswerPlanV2: extinderea versionată a contractului NX-211.
#
# V2 NU e un al doilea planner: e ACELAȘI contract, cu obligațiile turului, răspunsul direct,
# recomandările motivate pe evidence, clarificarea UNICĂ și taxonomia onestă de no-results.
# Validarea REFOLOSEȘTE `validate_answer_plan` printr-o proiecție `to_v1()` — un singur validator
# de evidence/tenant/fapte, nu două. Planul nu conține CoT, scoruri psihologice, payload brut de
# tool sau instrucțiuni către frontend; toate câmpurile sunt REQUIRED (structured output strict).
# ---------------------------------------------------------------------------


class PlanObligation(BaseModel):
    """O obligație pe care planul o recunoaște și o acoperă. `key` = slug scurt, nu text liber."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ObligationKind
    key: str = Field(max_length=48)


class PlanRecommendation(BaseModel):
    """O recomandare cu motiv CONCRET: legat de evidence și de nevoia clientului, nu generic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    variant_id: str | None
    reason: str = Field(max_length=280)
    evidence_ids: tuple[str, ...] = Field(max_length=8)
    need_ids: tuple[str, ...] = Field(max_length=6)


class ComparisonCell(BaseModel):
    """O celulă de comparație: fapt sourced (produs × axă → valoare + evidence), nu opinie."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    axis: str = Field(max_length=48)
    value: Scalar
    evidence_id: str


class PlanComparison(BaseModel):
    """Comparație pe refs + axe + celule sourced. FĂRĂ winner inventat — concluzia o trage
    validatorul/randarea din celule, nu un câmp liber al modelului."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product_ids: tuple[str, ...] = Field(min_length=2, max_length=4)
    axes: tuple[str, ...] = Field(max_length=6)
    cells: tuple[ComparisonCell, ...] = Field(max_length=24)


class PlanClarification(BaseModel):
    """Clarificarea UNICĂ a turului (structural: un singur câmp, nu o listă — max una per tur)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(max_length=280)
    target_need: str = Field(max_length=48)
    reason: str = Field(max_length=120)  # information-gain reason, cod scurt/frază scurtă
    options: tuple[str, ...] = Field(max_length=4)  # etichete de action intents, nu texte lungi


class PlanNoResults(BaseModel):
    """No-results ONEST: clasa închisă + criteriile care n-au fost satisfăcute + alternative safe.
    `no_match` ≠ `insufficient_data` ≠ `dependency_unavailable` — a le confunda e exact minciuna
    pe care D7 o interzice (UNKNOWN nu e MISMATCH)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_class: NoResultsClass
    criteria: tuple[str, ...] = Field(max_length=6)
    alternatives: tuple[str, ...] = Field(max_length=4)


class NeedProposal(BaseModel):
    """Propunere de state emisă de brain — reducerul NX-235 decide, planul doar propune."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["set_need", "revoke", "set_topic"]
    key: str = Field(max_length=48)
    value: Scalar | None


class StyleSignals(BaseModel):
    """Semnale de stil LIMITATE (ton/verbozitate). Fără CSS/layout/UI remote — randarea e a
    backendului (NX-240), frontendul e pasiv."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tone: Literal["neutral", "warm", "concise"]
    verbosity: Literal["short", "medium"]


class AnswerPlanV2(BaseModel):
    """Planul structurat FINAL al MainBrain (NX-239) — emis în aceeași buclă de tool-calling.

    Superset compatibil al lui `AnswerPlan` v1: `selected_products`/`claims`/`facts`/
    `confirmed_actions` au aceeași semantică, deci `to_v1()` e o proiecție fără pierdere pentru
    validator. Toate câmpurile sunt required (structured output strict); absența se exprimă prin
    null/tuple gol, nu prin câmp lipsă."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
    business_id: str
    locale: str
    intent_summary: str = Field(max_length=240)
    obligations: tuple[PlanObligation, ...] = Field(max_length=8)
    direct_answer: str = Field(max_length=900)
    selected_products: tuple[SelectedProduct, ...] = Field(max_length=6)
    claims: tuple[PlanClaim, ...] = Field(max_length=12)
    facts: PlanFacts
    recommendations: tuple[PlanRecommendation, ...] = Field(max_length=6)
    comparison: PlanComparison | None
    constraints_applied: tuple[str, ...] = Field(max_length=8)
    unknowns: tuple[str, ...] = Field(max_length=8)
    relaxations: tuple[str, ...] = Field(max_length=6)
    clarification: PlanClarification | None
    no_results: PlanNoResults | None
    state_update_proposals: tuple[NeedProposal, ...] = Field(max_length=6)
    action_intents: tuple[str, ...] = Field(max_length=4)
    disclosures: tuple[str, ...] = Field(max_length=4)
    confirmed_actions: tuple[ConfirmedAction, ...] = Field(max_length=4)
    style_signals: StyleSignals

    @model_validator(mode="after")
    def _contains_no_pii(self) -> AnswerPlanV2:
        text_values: list[str] = [
            self.business_id,
            self.locale,
            self.intent_summary,
            self.direct_answer,
            *(o.key for o in self.obligations),
            *(claim.text for claim in self.claims),
            *(rec.reason for rec in self.recommendations),
            *self.constraints_applied,
            *self.unknowns,
            *self.relaxations,
            *self.action_intents,
            *self.disclosures,
        ]
        if self.clarification is not None:
            clarification = self.clarification
            text_values.extend(
                [clarification.question, clarification.reason, *clarification.options]
            )
        if self.no_results is not None:
            text_values.extend([*self.no_results.criteria, *self.no_results.alternatives])
        if any(value and contains_pii(value) for value in text_values):
            raise ValueError("AnswerPlanV2 contains PII")
        return self

    def to_v1(self) -> AnswerPlan:
        """Proiecția pe contractul v1, pentru REFOLOSIREA validatorului (nu un al doilea validator).

        Recomandările devin claims `recommendation` (motivul = textul claimului) → evidence/nevoi
        se verifică pe exact aceleași reguli ca orice claim. `unknowns` → `uncertainties`."""
        rec_claims = tuple(
            PlanClaim(
                claim_type="recommendation",
                text=rec.reason,
                evidence_ids=rec.evidence_ids,
                need_ids=rec.need_ids,
            )
            for rec in self.recommendations
        )
        return AnswerPlan(
            schema_version=1,
            business_id=self.business_id,
            locale=self.locale,
            selected_products=self.selected_products,
            claims=(*self.claims, *rec_claims),
            facts=self.facts,
            uncertainties=self.unknowns,
            unmet_constraints=(),
            confirmed_actions=self.confirmed_actions,
        )


class GroundedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    business_id: str
    resolution: Literal["exact", "ambiguous"]
    variant_ids: tuple[str, ...]


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    business_id: str
    product_id: str
    variant_id: str | None
    kind: EvidenceKind
    value: Scalar
    source_version: str
    current: bool


class HardConstraintOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    facet: str
    verdict: ConstraintVerdict
    unknown_is_violation: bool


class AnswerPlanContext(BaseModel):
    """Trusted server projection used to validate model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    business_id: str
    locale: str
    products: tuple[GroundedProduct, ...]
    evidence: tuple[EvidenceRecord, ...]
    hard_constraints: tuple[HardConstraintOutcome, ...]
    successful_action_ids: tuple[str, ...]
    known_need_ids: tuple[str, ...]


class AnswerPlanValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    failures: tuple[ValidationCode, ...]


def _same_value(left: Scalar, right: Scalar) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 0.005
    return str(left).strip() == str(right).strip()


def validate_answer_plan(plan: AnswerPlan, context: AnswerPlanContext) -> AnswerPlanValidation:
    """Validate tenant, identity, evidence, constraints, needs, and confirmed mutations."""

    failures: list[ValidationCode] = []
    if plan.business_id != context.business_id or plan.locale != context.locale:
        failures.append("tenant_mismatch")

    products = {product.product_id: product for product in context.products}
    evidence = {item.evidence_id: item for item in context.evidence}
    selected_ids = [item.product_id for item in plan.selected_products]
    if len(selected_ids) != len(set(selected_ids)):
        failures.append("duplicate_selected_product")

    for selected in plan.selected_products:
        product = products.get(selected.product_id)
        if product is None:
            failures.append("unknown_product")
            continue
        if product.business_id != context.business_id:
            failures.append("cross_tenant_product")
        if product.resolution != "exact":
            failures.append("ambiguous_product")
        if selected.variant_id is not None and selected.variant_id not in product.variant_ids:
            failures.append("unknown_variant")
        has_identity = False
        has_variant = selected.variant_id is None
        if not selected.evidence_ids:
            failures.append("missing_product_evidence")
        for evidence_id in selected.evidence_ids:
            item = evidence.get(evidence_id)
            if item is None:
                failures.append("unknown_evidence")
                continue
            if item.business_id != context.business_id:
                failures.append("cross_tenant_evidence")
            if not item.current:
                failures.append("stale_evidence")
            if item.product_id != selected.product_id:
                failures.append("unknown_evidence")
            if item.kind == "identity" and item.variant_id is None:
                has_identity = True
            if item.kind == "variant" and item.variant_id == selected.variant_id:
                has_variant = True
        if not has_identity or not has_variant:
            failures.append("missing_product_evidence")

    known_needs = set(context.known_need_ids)
    for claim in plan.claims:
        if not claim.evidence_ids:
            failures.append("missing_claim_evidence")
        if any(need_id not in known_needs for need_id in claim.need_ids):
            failures.append("unknown_need")
        for evidence_id in claim.evidence_ids:
            item = evidence.get(evidence_id)
            if item is None:
                failures.append("unknown_evidence")
            elif item.business_id != context.business_id:
                failures.append("cross_tenant_evidence")
            elif not item.current:
                failures.append("stale_evidence")
            elif item.product_id not in selected_ids:
                failures.append("unknown_evidence")

    fact_groups = (
        ("price", plan.facts.prices),
        ("stock", plan.facts.stocks),
        ("url", plan.facts.urls),
    )
    for expected_kind, facts in fact_groups:
        for fact in facts:
            item = evidence.get(fact.evidence_id)
            if fact.product_id not in selected_ids:
                failures.append("unknown_product")
            if item is None:
                failures.append("unknown_evidence")
                continue
            if item.business_id != context.business_id:
                failures.append("cross_tenant_evidence")
            if not item.current:
                failures.append("stale_evidence")
            if (
                item.kind != expected_kind
                or item.product_id != fact.product_id
                or item.variant_id != fact.variant_id
            ):
                failures.append("fact_evidence_kind_mismatch")
            if not _same_value(item.value, fact.value):
                failures.append("fact_value_mismatch")

    selected = set(selected_ids)
    for outcome in context.hard_constraints:
        if outcome.product_id not in selected:
            continue
        if outcome.verdict == "MISMATCH" or (
            outcome.verdict == "UNKNOWN" and outcome.unknown_is_violation
        ):
            failures.append("hard_constraint_mismatch")

    successful = set(context.successful_action_ids)
    if any(
        action.action_id not in successful or not action.action_id.startswith(f"{action.action}:")
        for action in plan.confirmed_actions
    ):
        failures.append("action_not_successful")

    return AnswerPlanValidation(
        ok=not failures,
        failures=tuple(dict.fromkeys(failures)),
    )


# NX-239: ce secțiune a planului „acoperă" fiecare fel de obligație. Coverage-ul e verificat
# DETERMINIST: modelul poate propune obligations, dar validatorul le confruntă cu semnalele
# extrase din cod (control_plane) — o obligație cerută fără secțiune corespunzătoare = uncovered.
def _obligation_covered(plan: AnswerPlanV2, kind: str) -> bool:
    if kind == "answer" or kind == "explain":
        return bool(plan.direct_answer.strip()) or plan.no_results is not None
    if kind == "recommend":
        return (
            bool(plan.recommendations)
            or plan.no_results is not None
            or plan.clarification is not None
        )
    if kind == "routine":
        # O rutină cu UN produs nu e rutină, e o recomandare cu alt nume — de aceea pragul e 2,
        # nu 1, și de aceea `routine` nu se validează ca `recommend`. `no_results` rămâne o
        # acoperire validă (ca la toate celelalte tipuri): o ancoră fără muchii declarate trebuie
        # să poată primi un răspuns onest, nu să cadă în repair și apoi în fallback.
        return (len(plan.selected_products) >= 2 and bool(plan.recommendations)) or (
            plan.no_results is not None
        )
    if kind == "compare":
        return plan.comparison is not None or plan.no_results is not None
    if kind == "action":
        return (
            bool(plan.confirmed_actions) or bool(plan.action_intents) or plan.no_results is not None
        )
    if kind == "clarify":
        return plan.clarification is not None or bool(plan.direct_answer.strip())
    if kind == "safety":
        return bool(plan.disclosures) or bool(plan.direct_answer.strip())
    return False


def validate_answer_plan_v2(
    plan: AnswerPlanV2,
    context: AnswerPlanContext,
    *,
    required_obligations: tuple[tuple[str, str], ...] = (),
    revoked_need_keys: tuple[str, ...] = (),
    hard_constraint_keys: tuple[str, ...] = (),
    allowed_action_intents: tuple[str, ...] = (),
) -> AnswerPlanValidation:
    """Validatorul V2 = validatorul V1 (prin `to_v1()`) + regulile NOI ale contractului V2.

    `required_obligations` = (kind, key) extrase DETERMINIST din tur (control_plane) — nu ce a
    declarat modelul. `revoked_need_keys`/`hard_constraint_keys` vin din starea NX-235: un plan
    n-are voie să reînvie o nevoie revocată sau să relaxeze o constrângere hard. Clarificarea e
    UNICĂ structural (un singur câmp); aici verificăm restul."""

    base = validate_answer_plan(plan.to_v1(), context)
    failures: list[ValidationCode] = list(base.failures)

    # Obligații: fiecare cerință deterministă trebuie DECLARATĂ și ACOPERITĂ de o secțiune reală.
    declared = {(o.kind, o.key) for o in plan.obligations}
    declared_kinds = {o.kind for o in plan.obligations}
    for kind, key in required_obligations:
        if ((kind, key) not in declared and kind not in declared_kinds) or not _obligation_covered(
            plan, kind
        ):
            failures.append("obligation_uncovered")

    # Un răspuns fără NIMIC pentru client (nici direct answer, nici clarificare, nici no-results
    # onest) nu e un plan — e tăcere structurată.
    if not plan.direct_answer.strip() and plan.clarification is None and plan.no_results is None:
        failures.append("missing_direct_answer")

    # Hard constraints nu se relaxează de la sine (D7); doar soft-urile pot apărea în relaxations.
    hard = set(hard_constraint_keys)
    if any(key in hard for key in plan.relaxations):
        failures.append("hard_relaxation")

    # O nevoie revocată nu se reînvie: nici în motive/claims, nici printr-o propunere set_need.
    revoked = set(revoked_need_keys)
    if revoked:
        used = {
            *(nid for claim in plan.claims for nid in claim.need_ids),
            *(nid for rec in plan.recommendations for nid in rec.need_ids),
            *(p.key for p in plan.state_update_proposals if p.op == "set_need"),
        }
        if used & revoked:
            failures.append("revoked_need_used")

    # Acțiunile propuse trebuie să existe în registrul FINIT (NX-236): un intent inventat = reject.
    allowed = set(allowed_action_intents)
    if any(intent not in allowed for intent in plan.action_intents):
        failures.append("unknown_action_intent")

    # Comparația: refs grounded + celule cu evidence REAL (aceleași reguli de evidence ca V1 —
    # buclă mică pe conținut V2-only, nu un al doilea validator).
    if plan.comparison is not None:
        products = {p.product_id for p in context.products}
        evidence = {item.evidence_id: item for item in context.evidence}
        if any(pid not in products for pid in plan.comparison.product_ids):
            failures.append("unknown_product")
        for cell in plan.comparison.cells:
            item = evidence.get(cell.evidence_id)
            if item is None or item.product_id != cell.product_id:
                failures.append("unknown_evidence")
            elif item.business_id != context.business_id:
                failures.append("cross_tenant_evidence")
            elif not item.current:
                failures.append("stale_evidence")

    return AnswerPlanValidation(ok=not failures, failures=tuple(dict.fromkeys(failures)))


def evidence_coverage(plan: AnswerPlan) -> float:
    if not plan.claims:
        return 1.0
    covered = sum(bool(claim.evidence_ids) for claim in plan.claims)
    return covered / len(plan.claims)


def failure_counts(validation: AnswerPlanValidation) -> dict[str, int]:
    """PII-safe telemetry projection with closed codes only."""

    return dict(Counter(validation.failures))
