"""NX-238 — `RetrievalPort` + `RetrievalBundle`: contractul pe care NX-239 îl consumă.

De ce un port și nu un import condiționat în codul de prompt/tool: alternativa ar fi fost un
`if settings.candidate_enabled: from src.tools.search_entities_shadow import ...` undeva în agent.
Asta leagă promovarea unui retrieval de forma promptului și face imposibil de demonstrat că
„OFF" înseamnă chiar comportamentul de dinainte. Cu un port, candidatul nu are cum să ajungă în
producție decât trecând prin `selector.select_provider`, unde stă poarta de GO.

**Contractul e de REFERINȚE, nu de obiecte** (principiul 8): un candidat poartă `product_id`,
verdictele lui pe constrângeri, coduri de motiv și id-uri de evidence. Textul produsului rămâne
în `products` — câmpul pe care îl consumă validatorul și randorii — și NU intră niciodată în
proiecția de telemetrie.

**UNKNOWN ≠ MISMATCH (D7)** e păstrat structural: `match_class` are trei valori disjuncte, iar
`missing_information` spune pe ce fațete nu putem confirma. Un consumator care vrea „doar ce se
potrivește sigur" cere `bundle.exact_ids`; unul care vrea alternative onest etichetate cere
`bundle.alternative_ids`. Niciunul nu primește `rejected` — acelea sunt contraziceri de fapt.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.agent.match_gate import ConstraintResult, FacetCoverage
from src.agent.query_spec import RuntimeQuerySpec
from src.domain.search_entities import EvidenceReference

#: Versiunea contractului. Un bundle produs de un provider vechi nu se amestecă cu unul nou:
#: `pipeline_version` (capturat la accept) + `schema_version` sunt legătura anti-drift.
BUNDLE_SCHEMA_VERSION = 1

#: Coduri FIXE de degradare — vocabular închis, sigur de pus în labels de metrică (fără query/PII).
DEGRADATION_DEADLINE_EXCEEDED = "retrieval_deadline_exceeded"
DEGRADATION_PROVIDER_FAILED = "retrieval_provider_failed"
DEGRADATION_NO_FACET_REGISTRY = "retrieval_no_facet_registry"
DEGRADATION_CONSTRAINTS_NOT_ENFORCED = "retrieval_constraints_not_enforced"


class RetrievalError(RuntimeError):
    """Eșec de retrieval pe care apelantul TREBUIE să-l vadă (nu o degradare tăcută)."""


@dataclass(frozen=True, slots=True)
class RetrievalDeadline:
    """Bugetul de timp al retrievalului, în ceas MONOTON.

    Monoton, nu wall-clock: un NTP step în mijlocul unui tur ar face un deadline să expire
    instant sau niciodată. `budget_ms <= 0` = fără buget (kill-switch numeric, ca
    `embed_timeout_ms`).
    """

    budget_ms: int
    started_at: float = field(default_factory=time.monotonic)

    @property
    def unbounded(self) -> bool:
        return self.budget_ms <= 0

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def remaining_ms(self) -> int | None:
        """Milisecunde rămase, sau None dacă nu există buget. Niciodată negativ."""
        if self.unbounded:
            return None
        return max(0, self.budget_ms - self.elapsed_ms())

    def expired(self) -> bool:
        return not self.unbounded and self.elapsed_ms() >= self.budget_ms


def deadline_from_turn(cap_ms: int = 0) -> RetrievalDeadline:
    """NX-241 — bugetul retrievalului = `min(capul lui, ce a mai rămas din TURUL curent)`.

    Fără un `TurnDeadline` activ (flag stins, job, script) rămâne exact capul lui de dinainte —
    inclusiv `0` = fără buget. Un retrieval nu are voie să-și „lărgească" bugetul peste ce a mai
    rămas din tur: ăsta e exact felul în care timeouturile ajungeau să se înmulțească.
    """
    from src.runtime import deadline as turn_deadline  # noqa: PLC0415 — evită ciclul la import

    d = turn_deadline.current()
    if d is None or d.unbounded:
        return RetrievalDeadline(budget_ms=max(0, cap_ms))
    remaining = d.remaining_ms()
    return RetrievalDeadline(budget_ms=min(cap_ms, remaining) if cap_ms > 0 else remaining)


def query_count_bucket(count: int) -> str:
    """Numărul de query-uri DB → bandă low-cardinality (o metrică nu are voie să explodeze)."""
    if count <= 0:
        return "0"
    if count <= 2:
        return "1-2"
    if count <= 5:
        return "3-5"
    if count <= 10:
        return "6-10"
    return "10+"


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    """Un candidat: REFERINȚĂ + verdicte, niciodată conținutul produsului.

    `rank` e poziția în ordinea producătorului (0 = primul). O păstrăm explicit fiindcă
    `candidates` poate fi filtrată de consumator, iar „al câtelea a fost în retrieval" e exact
    semnalul de care are nevoie un diff baseline↔candidate.
    """

    product_id: str
    rank: int
    match_class: str  # "exact" | "alternative" | "rejected" (mulțimi DISJUNCTE, NX-187)
    constraint_results: tuple[ConstraintResult, ...] = ()
    soft_penalty: int = 0
    reason_codes: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    warning: str | None = None
    #: Scorul intern al providerului, când există. Poate lipsi (calea lexicală n-are cosine).
    score: float | None = None

    @property
    def rejected(self) -> bool:
        return self.match_class == "rejected"


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Referințele de evidence, grupate pe produs. Conținutul rămâne în storage."""

    references: tuple[EvidenceReference, ...] = ()

    def for_product(self, product_id: str) -> tuple[EvidenceReference, ...]:
        return tuple(ref for ref in self.references if ref.product_id == product_id)

    @property
    def product_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(ref.product_id for ref in self.references))


@dataclass(frozen=True, slots=True)
class RetrievalTiming:
    """Metadate de timp/cost SIGURE: numere și benzi, zero text de query."""

    elapsed_ms: int = 0
    deadline_ms: int | None = None
    deadline_exceeded: bool = False
    db_query_count: int = 0
    external_calls: int = 0

    @property
    def query_bucket(self) -> str:
        return query_count_bucket(self.db_query_count)


@dataclass(frozen=True, slots=True)
class RetrievalBundle:
    """Rezultatul unui retrieval, indiferent de provider.

    `products` e singurul câmp „gras" (dict-urile complete de catalog) și există dintr-un motiv
    strict: validatorul (stagiul 8) verifică prețuri/linkuri contra a ceea ce s-a retrievat, iar
    randorii au nevoie de fapte. Ordinea lui e ordinea providerului. `candidates` e proiecția de
    decizie peste aceleași produse, în aceeași ordine.
    """

    provider_version: str
    candidates: tuple[RetrievalCandidate, ...] = ()
    products: tuple[dict[str, Any], ...] = ()
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)
    coverage: tuple[FacetCoverage, ...] = ()
    missing_information: tuple[str, ...] = ()
    degradations: tuple[str, ...] = ()
    timing: RetrievalTiming = field(default_factory=RetrievalTiming)
    needs_refinement: bool = False
    identifier_status: str | None = None
    #: True dacă hard constraints au fost EXECUTATE (candidații rejected excluși), nu doar evaluate.
    #: Traseul live curent le ADNOTEAZĂ fără să excludă — NX-188/189 rămân înghețate până la GO.
    constraints_enforced: bool = False
    schema_version: int = BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.constraints_enforced and any(c.rejected for c in self.candidates):
            # Invariant, nu preferință: „am aplicat hard constraints" și „un hard-rejected e în
            # rezultat" nu pot fi ambele adevărate. Dacă ajung, un rerank a reînviat un produs
            # contrazis de date și rezultatul minte despre propria lui garanție.
            raise RetrievalError(
                "constraints_enforced=True dar bundle-ul conține candidați rejected "
                "(un hard-rejected a supraviețuit filtrării/rerankului)"
            )

    def _ids(self, match_class: str) -> tuple[str, ...]:
        return tuple(c.product_id for c in self.candidates if c.match_class == match_class)

    @property
    def exact_ids(self) -> tuple[str, ...]:
        """Toate constrângerile hard sunt MATCH."""
        return self._ids("exact")

    @property
    def alternative_ids(self) -> tuple[str, ...]:
        """Zero hard MISMATCH, dar ≥1 hard UNKNOWN — nu putem confirma, deci nu pretindem match."""
        return self._ids("alternative")

    @property
    def rejected_ids(self) -> tuple[str, ...]:
        """Datele CONTRAZIC o constrângere dură. Nu se prezintă ca alternativă."""
        return self._ids("rejected")

    @property
    def is_empty(self) -> bool:
        return not self.candidates

    def to_safe_dict(self) -> dict[str, Any]:
        """Proiecție PII-safe pentru telemetrie/rapoarte: coduri, numere și benzi.

        Nu conține query, nume de produs, brand sau text de evidence. `product_id`-urile sunt
        ID-uri controlate de noi (uuid de catalog), nu date de client — ele rămân, fiindcă fără
        ele un diff baseline↔candidate n-ar putea spune CE s-a schimbat.
        """
        return {
            "schema_version": self.schema_version,
            "provider_version": self.provider_version,
            "constraints_enforced": self.constraints_enforced,
            "needs_refinement": self.needs_refinement,
            "identifier_status": self.identifier_status,
            "counts": {
                "candidates": len(self.candidates),
                "exact": len(self.exact_ids),
                "alternative": len(self.alternative_ids),
                "rejected": len(self.rejected_ids),
                "evidence": len(self.evidence.references),
            },
            "candidate_ids": list(c.product_id for c in self.candidates),
            "coverage": [
                {
                    "facet": cov.facet,
                    "match": cov.match,
                    "mismatch": cov.mismatch,
                    "unknown": cov.unknown,
                }
                for cov in self.coverage
            ],
            "missing_information": list(self.missing_information),
            "degradations": list(self.degradations),
            "timing": {
                "elapsed_ms": self.timing.elapsed_ms,
                "deadline_ms": self.timing.deadline_ms,
                "deadline_exceeded": self.timing.deadline_exceeded,
                "query_count_bucket": self.timing.query_bucket,
                "external_calls": self.timing.external_calls,
            },
        }


class RetrievalPort(Protocol):
    """Ce trebuie să ofere orice provider de retrieval.

    Semnătura e cea din card: snapshotul turului (NX-234, tenant/actor/suprafață rehidratate
    server-side), specul de query al runtime-ului (NX-208, cu `raw_query` care NU iese din tur),
    nevoile active ale conversației (NX-235) și bugetul de timp.
    """

    #: Identitate stabilă a implementării, pusă în telemetrie și în `pipeline_version`.
    provider_version: str

    async def retrieve(
        self,
        snapshot: Any,
        runtime_query_spec: RuntimeQuerySpec,
        active_needs: Sequence[Any] = (),
        deadline: RetrievalDeadline | None = None,
    ) -> RetrievalBundle:
        """Candidați + evidence + acoperire, în ordinea providerului."""
        ...


def facets_by_key(business: Any) -> Mapping[str, Any]:
    """Registrul tipizat de fațete al tenantului (NX-186), sau gol.

    Gol NU e o eroare: un business fără `facets` configurate produce verdicte UNKNOWN peste tot,
    ceea ce e exact răspunsul corect („nu putem confirma"), nu MISMATCH. Fail-closed spre UNKNOWN.
    """
    pack = getattr(business, "domain_pack", None)
    return {facet.key: facet for facet in (getattr(pack, "facets", ()) or ())}
